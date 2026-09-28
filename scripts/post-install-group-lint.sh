#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# post-install-group-lint.sh — BSB-06 deploy-time per-app group enforcement
# ==============================================================================
# Read-only assertion that every (app_slug, group_name) pair declared in
# core/Authentik/apply-policy-bindings.py is materialised as a real
# PolicyBinding in the live Authentik DB, with the bound group's name
# matching the binding script.
#
# WHY: apply-policy-bindings.py wraps every binding in a try/except that
# silently logs "Group not found." on a typo. The blueprint apply path
# returns success regardless, so a 4-way binding mismatch (cf. 97f7e2c0)
# can ship to a customer who only notices when SSO denies them at
# login. This lint catches the mismatch at deploy time.
#
# Hooked into:
#   - razzfazz-init.sh   (after wait_for_authentik_init)
#   - razzfazz-upgrade.sh (after verify_upgrade)
#
# Test-injection knobs (used only by tests/scripts/razzfazz-init/
# test_post_install_group_lint.py):
#   BSB06_BINDINGS_FILE  override the path to apply-policy-bindings.py
#   BSB06_ENV_FILE       override the path to .env
#   BSB06_API_BASE       override the Authentik base URL (default:
#                        https://auth.<MAIN_DOMAIN>). #1715: the headline
#                        guard for this script needs to drive it against a
#                        stub, or it can only run on a box with a live
#                        Authentik — and it was FAILING everywhere else
#                        instead of saying so.
#
# Exit codes:
#   0   every binding verified
#   1   one or more bindings missing OR API unreachable / token bad
#   2   bad invocation (missing token / .env / apply-policy-bindings.py)
# ==============================================================================
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib.sh disable=SC1091
source "${SCRIPT_DIR}/lib.sh"

usage() {
    cat <<'EOF'
post-install-group-lint.sh — BSB-06 deploy-time group-binding lint

Reads the canonical (app_slug, group_name) map from
core/Authentik/apply-policy-bindings.py and asserts that every entry
has at least one matching PolicyBinding in the live Authentik DB
pointing at a group whose name matches the binding script.

Catches blueprint typos that pass blueprint apply but break SSO at
customer login time (the 97f7e2c0 regression class).

Usage:
  post-install-group-lint.sh         # run the lint
  post-install-group-lint.sh --help

Required environment (read from .env):
  MAIN_DOMAIN                 — used to build https://auth.<domain>
  AUTHENTIK_BOOTSTRAP_TOKEN   — service-account API token

Exit codes:
  0   every (app_slug, group_name) binding present
  1   one or more bindings missing / API unreachable
  2   bad invocation
EOF
}

# --- arg parsing -------------------------------------------------------------
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "")        ;;
    *)         echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
esac

# --- inputs ------------------------------------------------------------------
ENV_FILE="${BSB06_ENV_FILE:-${REPO_ROOT}/.env}"
BINDINGS_FILE="${BSB06_BINDINGS_FILE:-${REPO_ROOT}/core/Authentik/apply-policy-bindings.py}"

if [ ! -f "$ENV_FILE" ]; then
    print_error "post-install-group-lint: .env not found at $ENV_FILE"
    exit 2
fi
if [ ! -f "$BINDINGS_FILE" ]; then
    print_error "post-install-group-lint: apply-policy-bindings.py not found at $BINDINGS_FILE"
    exit 2
fi

MAIN_DOMAIN="$(read_env_value "$ENV_FILE" "MAIN_DOMAIN")"
AUTHENTIK_BOOTSTRAP_TOKEN="$(read_env_value "$ENV_FILE" "AUTHENTIK_BOOTSTRAP_TOKEN")"

if [ -z "$MAIN_DOMAIN" ]; then
    print_error "post-install-group-lint: MAIN_DOMAIN is empty in $ENV_FILE"
    exit 2
fi
if [ -z "$AUTHENTIK_BOOTSTRAP_TOKEN" ]; then
    print_error "post-install-group-lint: AUTHENTIK_BOOTSTRAP_TOKEN is empty in $ENV_FILE"
    print_info  "  Cannot reach the Authentik API without it. Re-generate via razzfazz-setup.sh."
    exit 2
fi

print_step "BSB-06 — verifying per-app PolicyBindings against live Authentik DB"
print_substep "bindings source: $BINDINGS_FILE"
print_substep "Authentik API:   ${BSB06_API_BASE:-https://auth.${MAIN_DOMAIN}}"

# --- Python worker -----------------------------------------------------------
# We hand the heavy lifting to python — re-implementing the
# parse + http+json roundtrip in pure bash would be ~3x the LOC and
# fragile (see the 4-way 97f7e2c0 regression — text munging is exactly
# what we DON'T want here).
#
# Important: the python script runs LOCALLY on the host (not inside the
# authentik-worker), so it goes through Caddy on https://auth.<domain>.
# We allow self-signed certs (TLS_MODE=internal stacks would otherwise
# fail). The bootstrap token is a long-lived service-account token; the
# read-only endpoints we hit (/api/v3/core/applications/, /policies/bindings/,
# /core/groups/) require user-level permissions.
# Disable errexit around the python invocation: a non-zero exit here
# is the SIGNAL we want to surface, not a script bug. We capture both
# stdout/stderr + RC explicitly.
# #2090: the lint skips exactly what the binding script skips. The script
# reads COMPOSE_PROFILES from the caller's env (#1465); the lint reads the
# same value from .env. "Key absent" and "key empty" differ — absent means the
# script skipped nothing — so both facts travel.
_bsb06_profiles_present=0
_bsb06_profiles=""
if grep -qE '^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROFILES=' "$ENV_FILE"; then
    _bsb06_profiles_present=1
    _bsb06_profiles="$(read_env_value "$ENV_FILE" "COMPOSE_PROFILES")"
fi
set +e
LINT_OUTPUT=$(
    AUTHENTIK_TOKEN="$AUTHENTIK_BOOTSTRAP_TOKEN" \
    AUTHENTIK_BASE="${BSB06_API_BASE:-https://auth.${MAIN_DOMAIN}}" \
    BINDINGS_FILE="$BINDINGS_FILE" \
    BSB06_PROFILES_PRESENT="$_bsb06_profiles_present" \
    BSB06_COMPOSE_PROFILES="$_bsb06_profiles" \
    python3 - 2>&1 <<'PYEOF'
import ast
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

TOKEN = os.environ["AUTHENTIK_TOKEN"]
BASE = os.environ["AUTHENTIK_BASE"].rstrip("/")
BINDINGS_FILE = os.environ["BINDINGS_FILE"]

# `start` is the M028 exception — explicitly OPEN to every authenticated
# user (zero PolicyBindings is intentional). `apply-policy-bindings.py`
# wipes any leftover binding on every run. We must NOT flag start as a
# missing-binding offender.
# #1652: `searxng` is deliberately open too — decided 2026-09-08, reason in
# tests/unit/consistency/test_1652_*.py::DELIBERATELY_UNBOUND. It never reaches
# this check today (it has no ensure_binding call), but the two records must
# agree: if someone adds a call, the lint should not then report a slug we
# decided to leave open.
ALLOW_NO_BINDINGS = {"start", "searxng"}

# Apply-policy-bindings.py also calls ensure_binding for slugs that may
# not be registered as Applications when the matching profile is OFF
# (e.g. `lightrag` when the lightrag profile is disabled). For those,
# we skip cleanly with a note rather than erroring — the missing-app
# case is benign (no provider exists either).

CTX = ssl.create_default_context()
# Allow self-signed certs (TLS_MODE=internal stacks).
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api_get(path):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except urllib.error.URLError as e:
        print(f"FATAL: Authentik API unreachable at {BASE}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"FATAL: Authentik API call failed: {e}", file=sys.stderr)
        sys.exit(1)


def parse_ensure_binding_calls(path):
    """Return list of (app_slug, group_name) pairs from the python."""
    text = open(path, encoding="utf-8").read()
    # Same regex as tests/api/authentik/test_group_enforcement.py — keep
    # the parser identical so static + runtime checks stay consistent.
    pattern = re.compile(r'ensure_binding\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
    return pattern.findall(text)


def parse_app_profiles(path):
    """The binding script's slug → profile table (#1465), read with `ast` so
    the lint and the script cannot disagree about which apps are in scope
    (#2090: they did — the script skipped by profile, the lint by
    registration, and the blueprints register every app regardless)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "APP_PROFILES":
            return ast.literal_eval(node.value)
    return {}


def active_profiles():
    """None when .env has no COMPOSE_PROFILES key — then the script skipped
    nothing and neither does the lint."""
    if os.environ.get("BSB06_PROFILES_PRESENT") != "1":
        return None
    return {p.strip() for p in os.environ.get("BSB06_COMPOSE_PROFILES", "").split(",") if p.strip()}


def profile_active(slug, table, active):
    """Mirror of apply-policy-bindings.py::_profile_active, verbatim in effect:
    no list → active; core app (None) → active; unknown slug → active."""
    if active is None:
        return True
    want = table.get(slug, None)
    if want is None:
        return True
    if isinstance(want, str):
        want = (want,)
    return any(p in active for p in want)


_APPS_BY_SLUG = None


def _load_all_apps():
    """Fetch every Application visible to the bootstrap-token user.

    Critical: by default Authentik filters /api/v3/core/applications/
    down to apps the *bound user* has policy-binding access to (the
    ?for_user= behaviour, but applied to the auth principal). The
    bootstrap-token user inherits its access from Super Admins on
    fresh boxes but we want the full system-wide list regardless.
    Pass ?superuser_full_list=true to get every Application row
    (token MUST belong to a Super Admin / akadmin for this to work).
    """
    global _APPS_BY_SLUG
    if _APPS_BY_SLUG is not None:
        return _APPS_BY_SLUG
    apps = {}
    page = 1
    while True:
        data, status = api_get(
            f"/api/v3/core/applications/?page_size=200&page={page}"
            f"&superuser_full_list=true"
        )
        if data is None:
            print(
                f"FATAL: Authentik /core/applications/ returned status {status}",
                file=sys.stderr,
            )
            sys.exit(1)
        for r in data.get("results", []):
            apps[r["slug"]] = r["pk"]
        pagination = data.get("pagination") or {}
        if page >= int(pagination.get("total_pages", 1) or 1):
            break
        page += 1
    _APPS_BY_SLUG = apps
    return apps


def fetch_app_pk(slug):
    return _load_all_apps().get(slug)


_ALL_GROUP_NAMES = "unread"


def _load_all_group_names():
    """Every Group name, or None when the endpoint does not list (an older
    stub / a token without list permission) — then no skip is granted on the
    strength of a group's absence and a missing binding is reported."""
    global _ALL_GROUP_NAMES
    if _ALL_GROUP_NAMES != "unread":
        return _ALL_GROUP_NAMES
    names = set()
    page = 1
    while True:
        data, status = api_get(f"/api/v3/core/groups/?page_size=200&page={page}")
        if not isinstance(data, dict) or "results" not in data:
            _ALL_GROUP_NAMES = None
            return None
        for r in data.get("results", []):
            if r.get("name"):
                names.add(r["name"])
        pagination = data.get("pagination") or {}
        if page >= int(pagination.get("total_pages", 1) or 1):
            break
        page += 1
    _ALL_GROUP_NAMES = names
    return names


_GROUP_NAME_CACHE = {}


def fetch_group_name(group_pk):
    if group_pk in _GROUP_NAME_CACHE:
        return _GROUP_NAME_CACHE[group_pk]
    data, status = api_get(f"/api/v3/core/groups/{group_pk}/")
    name = data["name"] if data and "name" in data else None
    _GROUP_NAME_CACHE[group_pk] = name
    return name


def fetch_bound_group_names(app_pk):
    """All group names bound to this app via PolicyBinding."""
    data, status = api_get(f"/api/v3/policies/bindings/?target={app_pk}&page_size=100")
    if data is None:
        return set()
    names = set()
    for b in data.get("results", []):
        gid = b.get("group")
        if gid:
            n = fetch_group_name(gid)
            if n:
                names.add(n)
    return names


def main():
    pairs = parse_ensure_binding_calls(BINDINGS_FILE)
    if not pairs:
        print("FATAL: no ensure_binding() calls parsed from "
              f"{BINDINGS_FILE}", file=sys.stderr)
        sys.exit(1)

    # Group expected groups per slug.
    expected = {}
    for slug, group in pairs:
        expected.setdefault(slug, set()).add(group)

    # Bucket results.
    ok = []
    missing = []        # (slug, expected_group, bound_groups)
    skipped_app = []    # (slug, reason)
    skipped_pair = []   # (slug, expected_group, reason) — #2090

    table = parse_app_profiles(BINDINGS_FILE)
    active = active_profiles()

    for slug, expected_groups in sorted(expected.items()):
        app_pk = fetch_app_pk(slug)
        if app_pk is None:
            if slug in ALLOW_NO_BINDINGS:
                ok.append(f"{slug} (allow-listed: open to every authenticated user)")
                continue
            # App not registered — likely the matching profile is off.
            # We skip with a note; this is NOT a lint failure (no provider
            # exists either, so no auth path to break).
            skipped_app.append((slug, "Application not registered (profile off?)"))
            continue
        # #2090: a REGISTERED app is in scope whatever its profile — the
        # binding script binds it (an app with zero bindings is open to every
        # authenticated user). The one case the script skips without waiting
        # is an inactive profile whose group does not exist; skip that here
        # too, and nothing else.
        inactive = not profile_active(slug, table, active)
        bound = fetch_bound_group_names(app_pk)
        for grp in sorted(expected_groups):
            if grp in bound:
                ok.append(f"{slug} → {grp}")
                continue
            if inactive:
                names = _load_all_group_names()
                if names is not None and grp not in names:
                    skipped_pair.append((slug, grp, "profile off and the group does not exist — the binding script skips it too"))
                    continue
            missing.append((slug, grp, sorted(bound)))

    # Pretty report.
    print(f"checked: {sum(len(g) for g in expected.values())} (slug, group) pairs across {len(expected)} apps")
    if skipped_app:
        print(f"skipped: {len(skipped_app)} apps not registered (profile off):")
        for slug, reason in skipped_app:
            print(f"  - {slug}: {reason}")
    if skipped_pair:
        print(f"skipped: {len(skipped_pair)} (app, group) pairs of inactive profiles whose group does not exist:")
        for slug, grp, reason in skipped_pair:
            print(f"  - {slug} → {grp}: {reason}")
    if missing:
        print(f"\nMISSING bindings ({len(missing)}):")
        for slug, grp, bound in missing:
            print(f"  - app `{slug}`: expected PolicyBinding to group `{grp}`")
            if bound:
                print(f"    actually bound: {bound}")
            else:
                print(f"    actually bound: (none)")
        print(
            "\nWhat a missing binding MEANS (#2090, measured): an app with ZERO\n"
            "  bindings is OPEN to every authenticated user — not denied; an app\n"
            "  bound to the wrong set denies everyone outside it.\n"
            "Why it happens — apply-policy-bindings.py logged 'not found' for the\n"
            "  pair(s) above and moved on. One of:\n"
            "    a) the binding pass ran before the worker had applied the blueprint\n"
            "       that creates the group or the app (the day-1 race, #2090) —\n"
            "       re-run the idempotent pass: rzfz post-install --refresh\n"
            "       (or: docker exec -e COMPOSE_PROFILES=\"$(grep -oE '^COMPOSE_PROFILES=.*' .env | cut -d= -f2-)\"\n"
            "        authentik-worker python /tmp/apply_policy_bindings.py), then re-run this lint;\n"
            "    b) the group name in apply-policy-bindings.py is a typo (97f7e2c0 /\n"
            "       BS-AUTH-BUG-01), or\n"
            "    c) the matching group is missing from\n"
            "       core/Authentik/blueprints/base/02-groups.yaml (or its per-module blueprint)."
        )
        sys.exit(1)

    print(f"\nOK: all {len(ok)} (app, group) bindings verified.")
    sys.exit(0)


main()
PYEOF
)
LINT_RC=$?
set -e

# Forward python's output to the user (preserves indentation).
printf '%s\n' "$LINT_OUTPUT"

if [ $LINT_RC -eq 0 ]; then
    print_success "BSB-06 group-binding lint passed."
else
    print_error "BSB-06 group-binding lint FAILED (exit $LINT_RC)."
    print_info  "Re-run after fixing: scripts/post-install-group-lint.sh"
fi

exit $LINT_RC
