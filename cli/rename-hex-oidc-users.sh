#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# cli/rename-hex-oidc-users.sh  (#218 point 2 — one-time hex-username retrofit)
# ==============================================================================
# Point 1 (PR #780, modules/gitea/init-gitea.sh) made Gitea name FUTURE
# Authentik-OIDC auto-registrations after the readable `preferred_username`
# claim instead of the OIDC `sub`. It cannot touch accounts that already
# EXIST: Gitea only reads `--username preferred_username` at first
# registration, so anyone who logged in before #780 landed is still named
# after their `sub` — an Authentik hex identifier (a 64-char lowercase SHA256
# hex digest under Authentik's default `sub_mode=hashed_user_id`, or a dashed
# UUID4 under `sub_mode=user_uuid`). That account is unreachable by its
# readable name, which is exactly what the coding-agent's
# `provisioner._mint_gitea_token` needs
# (`gitea admin user generate-access-token --username <authentik name>` ->
# "user does not exist" -> empty token -> no `.git-credentials` -> "Clone
# from Gitea" fails). This script is the one-time admin fix for accounts that
# already exist: it finds them and renames them to their readable name.
#
# ── Why email, not the sub itself ────────────────────────────────────────────
# The obvious-looking approach — treat the hex username as the Authentik
# `sub` and look the user up BY that sub — does not work in general. This
# stack's Authentik blueprints (core/Authentik/blueprints/gitea-oidc/) never
# set `sub_mode`, so Authentik's OAuth2Provider default applies:
# `hashed_user_id`, which is `sha256(f"{user.id}-{install_secret}")` — a
# ONE-WAY hash of an install-local secret that never leaves Authentik's own
# database. There is no API that inverts it.
#
# What Gitea DOES already have, for every one of these accounts, is the
# user's real email address — captured from the OIDC `email` claim at
# auto-registration time (`gothUser.Email`, stored on the Gitea user row
# regardless of what username strategy was in effect). Authentik's user API
# supports an EXACT `email=` filter (`authentik/core/api/users.py`,
# `UsersFilter.Meta.fields` includes `email`), so `login -> email -> Authentik
# user -> username` is a real, resolvable chain — unlike `login(=sub) ->
# Authentik user` directly. If that lookup does not resolve to exactly one
# Authentik account (missing email, zero or multiple matches), the account is
# SKIPPED with a warning rather than guessed at — see the safety notes below.
#
# ── Safety (this touches user accounts — fail-safe over fail-fast) ──────────
#   * Only accounts whose Gitea `source_id` matches the "Authentik" OAuth2
#     source are considered (server-side filtered via the admin API's
#     `source_id` query param) — a locally-created admin, or one linked to
#     some OTHER auth source (e.g. Google), is never touched.
#   * Only usernames matching the hex/UUID shape below are considered.
#   * The email->Authentik lookup must resolve to EXACTLY ONE account.
#   * The rename target must not already be in use by a DIFFERENT Gitea
#     account — a collision is a SKIP + warning, never a clobber.
#   * Idempotent: an account whose current name already equals the resolved
#     target is left alone (a second run over the same box is a no-op).
#   * --dry-run prints the plan (including SKIP reasons) and issues no
#     mutating call — no Gitea rename POST is ever made.
#
# ── How it talks to Gitea/Authentik ───────────────────────────────────────────
# Same `docker exec gitea su-exec git gitea admin ...` CLI-in-container
# pattern init-gitea.sh / post-install.sh use for the auth-source lookup and
# for minting a token (post-install.sh's OpenHands/GitHub-connector wiring
# mints one the same way). Gitea's admin CLI has no `rename` subcommand in
# the pinned 1.27.1 (verified against upstream source) — the rename itself
# goes through the admin REST API's `POST /admin/users/{username}/rename`
# (added alongside `api.RenameUserOption`), reached over the host-bound
# `127.0.0.1:${GITEA_HTTP_PORT}` the same way post-install.sh's
# `step_gitea_provisioning` polls `/api/healthz`. The token minted for this
# run is never persisted — a fresh one is minted per invocation.
#
# ── Usage ─────────────────────────────────────────────────────────────────────
#   rename-hex-oidc-users.sh                # rename every resolvable account
#   rename-hex-oidc-users.sh --dry-run      # show the plan, change nothing
#
# Exit: 0 = every candidate resolved to a rename, a safe skip, or there was
# nothing to do; 1 = a hard failure (missing config, Gitea/Authentik
# unreachable, a rename call itself failed).
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

# `--help`/`-h` must render WITHOUT a .env (docs generator / fresh checkout).
for _arg in "$@"; do case "$_arg" in
    -h|--help) sed -n '/^# ── Usage/,/^# unreachable, a rename call itself failed/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac; done

ENV_FILE="${SCRIPT_DIR}/.env"
[ -f "$ENV_FILE" ] || { print_error ".env not found at $ENV_FILE"; exit 1; }

# ── arg parse ─────────────────────────────────────────────────────────────────
DRY=false
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=true; shift ;;
        *) print_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Targeted reads only — never source operator-edited .env (memory rule).
MAIN_DOMAIN="$(read_env_value "$ENV_FILE" MAIN_DOMAIN)"
AUTHENTIK_TOKEN="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_TOKEN)"
GITEA_ADMIN_USER="$(read_env_value "$ENV_FILE" GITEA_ADMIN_USER)"; GITEA_ADMIN_USER="${GITEA_ADMIN_USER:-admin}"
GITEA_HTTP_PORT="$(read_env_value "$ENV_FILE" GITEA_HTTP_PORT)"; GITEA_HTTP_PORT="${GITEA_HTTP_PORT:-3000}"
COMPOSE_PROFILES_VAL="$(read_env_value "$ENV_FILE" COMPOSE_PROFILES)"

if ! printf '%s' ",${COMPOSE_PROFILES_VAL}," | grep -q ',gitea,'; then
    print_info "gitea profile not active — nothing to do."
    exit 0
fi

check_container gitea || { print_error "gitea container is not running."; exit 1; }
[ -n "$MAIN_DOMAIN" ]     || { print_error "MAIN_DOMAIN is empty in .env."; exit 1; }
[ -n "$AUTHENTIK_TOKEN" ] || { print_error "AUTHENTIK_BOOTSTRAP_TOKEN is empty in .env."; exit 1; }

GITEA_URL="http://127.0.0.1:${GITEA_HTTP_PORT}"
AUTHENTIK_URL="https://auth.${MAIN_DOMAIN}"

# TLS for the Authentik call: verify against the box's own Caddy CA bundle
# (certs/caddy-ca.pem — the same OIDC CA superset init.sh/lib.sh maintain and
# status.sh checks) when it is present and non-empty; otherwise fall back to
# the system trust store. Never `-k`/`--insecure` — this call carries the
# admin bootstrap token.
AUTHENTIK_CACERT_ARGS=()
if [ -s "${SCRIPT_DIR}/certs/caddy-ca.pem" ]; then
    AUTHENTIK_CACERT_ARGS=(--cacert "${SCRIPT_DIR}/certs/caddy-ca.pem")
fi

# ── #218: hex-OIDC-sub username pattern ──────────────────────────────────────
# Authentik emits either a 64-char lowercase SHA256 hex digest
# (sub_mode=hashed_user_id, the Authentik DEFAULT and what this stack's
# blueprints leave in place) or a dashed UUID4 (sub_mode=user_uuid, in case a
# box overrides it). A hand-picked username never matches either shape: it is
# either short or contains a letter outside a-f. The 32-char lower bound
# admits a raw (undashed) UUID without over-matching short readable names.
# Exported so the single definition here is the one `list_candidates`'
# embedded python3 filter actually matches against — no second copy to drift.
export HEX_RE='^([0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$'

# Resolve the "Authentik" OAuth2 source id inside Gitea — the exact lookup
# modules/gitea/init-gitea.sh uses for its own retrofit block.
AUTH_SOURCE_ID="$(docker exec gitea su-exec git gitea admin auth list 2>/dev/null \
    | awk '$2=="Authentik"{print $1; exit}')"
if [ -z "$AUTH_SOURCE_ID" ]; then
    print_info "No 'Authentik' OAuth2 source configured in Gitea — nothing to rename."
    exit 0
fi

# Ephemeral admin API token, minted fresh for this run only (never persisted)
# — the same CLI call post-install.sh's OpenHands/GitHub-connector wiring
# uses to mint a token for REST-API use from the host.
GITEA_TOKEN="$(docker exec gitea su-exec git gitea admin user generate-access-token \
    --username "$GITEA_ADMIN_USER" --token-name "rename-hex-oidc-users-$$" --raw \
    --scopes read:admin,write:admin 2>/dev/null | tr -d '[:space:]')"
[ -n "$GITEA_TOKEN" ] || { print_error "Failed to mint a Gitea admin API token."; exit 1; }

# ── enumerate hex-username accounts linked to the Authentik OIDC source ─────
# Server-side filtered by source_id (point-1(b) safety gate: only OIDC-linked
# accounts are ever candidates), then hex-matched client-side. Emits
# "login<TAB>email" rows, one per candidate, deduplicated across pages.
list_candidates() {
    local page=1 limit=50
    local cand_tmp; cand_tmp="$(mktemp)"
    trap 'rm -f "${cand_tmp}"' RETURN
    while [ "$page" -le 50 ]; do
        local resp
        resp="$(curl -sS --max-time 20 \
            -H "Authorization: token ${GITEA_TOKEN}" -H "Accept: application/json" \
            "${GITEA_URL}/api/v1/admin/users?source_id=${AUTH_SOURCE_ID}&limit=${limit}&page=${page}" 2>/dev/null || true)"
        [ -n "$resp" ] || { print_error "Gitea admin API unreachable at ${GITEA_URL}"; return 1; }
        local page_count
        page_count="$(printf '%s' "$resp" | python3 -c '
import os, sys, json, re
HEX_RE = re.compile(os.environ["HEX_RE"])
try:
    users = json.load(sys.stdin)
except Exception:
    users = []
if not isinstance(users, list):
    users = []
clean = lambda s: s.replace("\t", " ").replace("\n", " ")
for u in users:
    login = (u.get("login") or "").strip()
    if HEX_RE.match(login):
        email = (u.get("email") or "").strip()
        sys.stderr.write("%s\t%s\n" % (clean(login), clean(email)))
print(len(users))
' 2>>"${cand_tmp}")"
        page_count="${page_count:-0}"
        [ "$page_count" -eq "$limit" ] || break
        page=$((page + 1))
    done
    sort -u "${cand_tmp}"
}

# Resolve one email to its Authentik `username` (= preferred_username), via
# an EXACT email filter. Prints the username on stdout and returns 0 only
# when the lookup resolves to exactly one Authentik account.
resolve_preferred_username() {
    local email="$1"
    [ -n "$email" ] || return 1
    local enc
    enc="$(printf '%s' "$email" | python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.stdin.read().strip()))')"
    local resp
    resp="$(curl -sf --max-time 20 "${AUTHENTIK_CACERT_ARGS[@]}" \
        -H "Authorization: Bearer ${AUTHENTIK_TOKEN}" -H "Accept: application/json" \
        "${AUTHENTIK_URL}/api/v3/core/users/?email=${enc}&page_size=2" 2>/dev/null || true)"
    [ -n "$resp" ] || return 1
    printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
results = d.get("results") or []
if len(results) != 1:
    sys.exit(1)
uname = (results[0].get("username") or "").strip()
if not uname:
    sys.exit(1)
sys.stdout.write(uname)
'
}

# True (exit 0) if a Gitea account named $1 already exists.
gitea_user_exists() {
    # URL-ENCODE the name — it is the Authentik-supplied `new_name`, not a
    # validated token, and it is interpolated into a URL PATH. A `/` or `..`
    # would make this probe hit a different endpoint entirely; the failure
    # direction is a silent SKIP of the account. Same helper the email lookup
    # at resolve_preferred_username uses (quote with an empty safe set, so `/`
    # is encoded too).
    local enc
    enc="$(printf '%s' "$1" | python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.stdin.read(), safe=""))')" || return 1
    curl -sf --max-time 20 -H "Authorization: token ${GITEA_TOKEN}" \
        "${GITEA_URL}/api/v1/users/${enc}" > /dev/null 2>&1
}

# Rename $1 -> $2 via the Gitea admin REST API.
gitea_rename_user() {
    # Defense-in-depth: $1 is already HEX_RE-filtered by list_candidates
    # before it ever reaches here, but re-assert immediately before the
    # mutating call/URL-path interpolation so a future refactor that moves
    # this call earlier (before the filter runs) can never fire a rename on
    # an unvalidated username.
    [[ "$1" =~ $HEX_RE ]] || { print_error "Internal error: refusing to rename unvalidated username '$1'"; return 1; }
    # Build the JSON body with a real encoder — never string-interpolate the
    # new username into a hand-written JSON literal (it may contain `"` or
    # `\`, which would break or inject into the request body).
    local body
    body="$(python3 -c 'import json, sys; print(json.dumps({"new_username": sys.argv[1]}, separators=(",", ":")))' "$2")"
    curl -sf --max-time 20 -X POST \
        -H "Authorization: token ${GITEA_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$body" \
        "${GITEA_URL}/api/v1/admin/users/$1/rename" > /dev/null
}

CANDIDATES="$(list_candidates)" || exit 1
if [ -z "$CANDIDATES" ]; then
    print_info "No hex-username Gitea accounts linked to the Authentik OIDC source found."
    exit 0
fi

total=0
renamed=0
skipped=0
errors=0
while IFS=$'\t' read -r old_name email; do
    [ -n "$old_name" ] || continue
    total=$((total + 1))

    if [ -z "$email" ]; then
        print_warning "SKIP ${old_name}: no email on the Gitea account — cannot resolve preferred_username"
        skipped=$((skipped + 1))
        continue
    fi

    new_name=""
    if ! new_name="$(resolve_preferred_username "$email")"; then
        print_warning "SKIP ${old_name}: could not resolve exactly one Authentik account for email ${email}"
        skipped=$((skipped + 1))
        continue
    fi

    if [ "$new_name" = "$old_name" ]; then
        print_info "SKIP ${old_name}: already matches the resolved preferred_username — no-op"
        skipped=$((skipped + 1))
        continue
    fi

    if gitea_user_exists "$new_name"; then
        print_warning "SKIP ${old_name} -> ${new_name}: target username already exists on a different account — refusing to clobber"
        skipped=$((skipped + 1))
        continue
    fi

    if [ "$DRY" = true ]; then
        print_info "[dry-run] would rename: ${old_name} -> ${new_name}  (resolved via email ${email})"
        continue
    fi

    if gitea_rename_user "$old_name" "$new_name"; then
        print_success "RENAMED ${old_name} -> ${new_name}"
        renamed=$((renamed + 1))
    else
        print_error "FAILED to rename ${old_name} -> ${new_name}"
        errors=$((errors + 1))
    fi
done <<< "$CANDIDATES"

print_step "Summary: ${total} hex-username OIDC account(s) found, ${renamed} renamed, ${skipped} skipped, ${errors} error(s)."
if [ "$DRY" = true ]; then
    print_info "Dry run — no changes were made."
fi
[ "$errors" -eq 0 ] || exit 1
exit 0
