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
print_substep "Authentik API:   https://auth.${MAIN_DOMAIN}"

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
set +e
LINT_OUTPUT=$(
    AUTHENTIK_TOKEN="$AUTHENTIK_BOOTSTRAP_TOKEN" \
    AUTHENTIK_BASE="https://auth.${MAIN_DOMAIN}" \
    BINDINGS_FILE="$BINDINGS_FILE" \
    python3 - 2>&1 <<'PYEOF'
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
ALLOW_NO_BINDINGS = {"start"}

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
        bound = fetch_bound_group_names(app_pk)
        for grp in sorted(expected_groups):
            if grp in bound:
                ok.append(f"{slug} → {grp}")
            else:
                missing.append((slug, grp, sorted(bound)))

    # Pretty report.
    print(f"checked: {sum(len(g) for g in expected.values())} (slug, group) pairs across {len(expected)} apps")
    if skipped_app:
        print(f"skipped: {len(skipped_app)} apps not registered (profile off):")
        for slug, reason in skipped_app:
            print(f"  - {slug}: {reason}")
    if missing:
        print(f"\nMISSING bindings ({len(missing)}):")
        for slug, grp, bound in missing:
            print(f"  - app `{slug}`: expected PolicyBinding to group `{grp}`")
            if bound:
                print(f"    actually bound: {bound}")
            else:
                print(f"    actually bound: (none)")
        print(
            "\nThis is the 97f7e2c0 / BS-AUTH-BUG-01 regression class:\n"
            "  apply-policy-bindings.py logged 'Group not found' for the\n"
            "  above pair(s) and silently moved on. Either:\n"
            "    a) the group name in apply-policy-bindings.py is a typo, or\n"
            "    b) the matching group is missing from\n"
            "       core/Authentik/blueprints/base/02-groups.yaml\n"
            "       (or its per-module blueprint)."
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
