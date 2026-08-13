#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# cli/provision-sso-users.sh  (#31 Dify, #32 Cognee — True-SSO epic #33)
# ==============================================================================
# Provision Authentik-group members into apps that have NO native OIDC, so a
# user who has been added to the right Authentik group can log in to the app
# without an admin hand-creating the account.
#
#   Dify (CE)  — no OIDC; local auth, pbkdf2-sha256 hashes in dify_db.accounts.
#   Cognee     — no OIDC; fastapi-users local auth in cognee_db.
#
# ── Auth-model reality (READ THIS) ────────────────────────────────────────────
# Authentik stores passwords HASHED and never exposes the plaintext, so the
# user's account in Dify/Cognee CANNOT literally be given "the same password as
# Authentik". This is NOT single-sign-on (there is a second, app-local password);
# it is automated account provisioning. Apps that DO support OIDC (Gitea, Open
# WebUI) use that instead and are not handled here.
#
# ── Password model (operator-decided — epic #33, password broker #54) ─────────
# Each newly-CREATED account gets a **per-user RANDOM temporary password**
# (`openssl rand`, distinct every time). That temp password is set ONLY at
# create-time and is PRINTED in this command's output (this is an admin-run
# command) so the operator can hand it to the user in the interim — it is NEVER
# written to a file/log.
#
# An account that ALREADY exists is left ENTIRELY untouched on the password
# front: we never overwrite its stored hash. That keeps the run idempotent and
# broker-safe — a password the central password broker (#54) or a user has
# already set is not clobbered by a re-run. The generated temp password is a
# PLACEHOLDER only; ongoing password sync is owned by the broker (#54).
#
# (The legacy `--password` override is retained for the rare operator who wants
# to pin the create-time credential instead of a random one; it still only
# applies on CREATE — it never re-passwords an existing account.)
#
# ── What it does, per member of the target Authentik group ────────────────────
#   1. enumerate the group's members from the Authentik REST API
#   2. for each member: if the app account is MISSING, create it (with a real
#      tenant / workspace where the app needs one), set a per-user random temp
#      password, mark active + verified, and print the temp password. If the
#      account EXISTS, leave its password unchanged (just report it).
#      Idempotent — re-runnable, never duplicates, never re-passwords.
#
# ── Usage ─────────────────────────────────────────────────────────────────────
#   provision-sso-users.sh --app dify                 # default group
#   provision-sso-users.sh --app cognee --group "razzfazz.ai Cognee Users"
#   provision-sso-users.sh --app all                  # dify + cognee
#   provision-sso-users.sh --app dify --dry-run       # show, change nothing
#   provision-sso-users.sh --app dify --list          # list group members + state
#   provision-sso-users.sh --password 'Managed1!'     # pin create-time pw (else random)
#
# Exit: 0 = all targeted members reconciled (or dry-run); 1 = a hard failure.
# Only apps whose container is running are touched; others reported skipped.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
ENV_FILE="${SCRIPT_DIR}/.env"

[ -f "$ENV_FILE" ] || { print_error ".env not found at $ENV_FILE"; exit 1; }

# Targeted reads only — never source operator-edited .env (memory rule).
PG_USER="$(read_env_value "$ENV_FILE" POSTGRES_USER)";   PG_USER="${PG_USER:-docker}"
MAIN_DOMAIN="$(read_env_value "$ENV_FILE" MAIN_DOMAIN)"
AUTHENTIK_TOKEN="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_TOKEN)"
DIFY_DB="$(read_env_value "$ENV_FILE" DIFY_DB)";         DIFY_DB="${DIFY_DB:-dify_db}"
COGNEE_DB="$(read_env_value "$ENV_FILE" COGNEE_DB)";     COGNEE_DB="${COGNEE_DB:-cognee_db}"

# Per-user random temporary password (set ONLY on create; see header). 24 random
# bytes → base64 (~32 chars), distinct each call. Never persisted to a file.
gen_temp_password() {
    openssl rand -base64 24
}

# Per-app default Authentik group (the per-app "<App> Users" group from the
# blueprints in core/Authentik/blueprints/). Overridable with --group.
default_group_for() {
    case "$1" in
        dify)   echo "razzfazz.ai Workflow Automation Users" ;;
        cognee) echo "razzfazz.ai Cognee Users" ;;
        *)      return 1 ;;
    esac
}

# ── Authentik group enumeration ───────────────────────────────────────────────
# List the active members (username TAB email TAB name) of an Authentik group,
# resolved by group NAME. Uses the bootstrap token through https://auth.<domain>.
# -k tolerates self-signed certs on TLS_MODE=internal stacks. Paginated.
authentik_group_members() {
    local group=$1
    local base="https://auth.${MAIN_DOMAIN}"
    [ -n "$AUTHENTIK_TOKEN" ] || { print_error "AUTHENTIK_BOOTSTRAP_TOKEN is empty in .env."; return 1; }
    [ -n "$MAIN_DOMAIN" ]     || { print_error "MAIN_DOMAIN is empty in .env."; return 1; }

    # URL-encode the group name (spaces, dots) for the query string.
    local enc
    enc="$(printf '%s' "$group" | python3 -c 'import sys,urllib.parse; sys.stdout.write(urllib.parse.quote(sys.stdin.read()))')"

    # The users list endpoint supports filtering by group name; page through
    # all results. Emit only ACTIVE users with a non-empty email (Dify and
    # Cognee both key on email).
    local url="${base}/api/v3/core/users/?groups_by_name=${enc}&page_size=100"
    local guard=0
    while [ -n "$url" ] && [ "$guard" -lt 50 ]; do
        guard=$((guard + 1))
        local resp
        resp="$(curl -sk --max-time 30 \
            -H "Authorization: Bearer ${AUTHENTIK_TOKEN}" \
            -H "Accept: application/json" "$url" 2>/dev/null || true)"
        [ -n "$resp" ] || { print_error "Authentik API unreachable at ${base}"; return 1; }
        # Parse this page; capture the next-page URL via a trailing marker line.
        url="$(printf '%s' "$resp" | AK_BASE="$base" python3 -c '
import sys, json, os
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR", file=sys.stderr); sys.exit(2)
for u in d.get("results", []):
    if not u.get("is_active", False):
        continue
    email = (u.get("email") or "").strip()
    if not email:
        continue
    username = (u.get("username") or "").strip()
    name = (u.get("name") or username).strip()
    # TAB-separated; sanitise embedded tabs/newlines defensively.
    clean = lambda s: s.replace("\t", " ").replace("\n", " ")
    sys.stderr.write("%s\t%s\t%s\n" % (clean(username), clean(email), clean(name)))
nxt = (d.get("pagination") or {}).get("next")
# Authentik returns next as a full URL or empty string.
print(nxt or "")
' 2>>"${_MEMBERS_TMP}")" || { print_error "Failed to parse Authentik response."; return 1; }
    done
    return 0
}

# Wrapper: collect members into the temp file, return rows on stdout.
list_group_members() {
    local group=$1
    _MEMBERS_TMP="$(mktemp)"
    # shellcheck disable=SC2064
    trap "rm -f '${_MEMBERS_TMP}'" RETURN
    if ! authentik_group_members "$group"; then
        return 1
    fi
    sort -u "${_MEMBERS_TMP}"
}

# ── Dify provisioning ─────────────────────────────────────────────────────────
# Create-IF-MISSING an account using Dify's OWN service layer inside dify-api, so
# the Account + Tenant + TenantAccountJoin + status are all consistent with how
# the app itself creates users (far safer than hand-INSERTing rows). On CREATE
# the per-user random temp password is set via the same pbkdf2-sha256/10k scheme
# cli/set-admin-password uses, so `rzfz set-admin-password --app dify` (or the
# broker #54) can later rotate it identically. On EXISTS the password is left
# UNTOUCHED — we only ensure active + workspace membership (no re-passwording).
dify_provision_one() {
    local email=$1 name=$2 pw=$3
    # Pass the program via `python3 -c "$pyscript"` (NOT `python3 - <<'PY'`):
    # `docker exec` without -i has no stdin, so `python3 -` would read an empty
    # program and silently no-op — and adding -i is loop-fragile (it competes
    # with the caller's `while read` over stdin). cf. cli/set-admin-password.sh
    # apply_cognee, which uses the same -c pattern for the same reason. All user
    # data goes through `-e` env (os.environ), never the program text.
    local pyscript; pyscript="$(cat <<'PY'
import os, sys
sys.path.insert(0, "/app/api")
from app import create_app
from extensions.ext_database import db
from models.account import Account, AccountStatus, TenantAccountJoin, Tenant

email = os.environ["PE"]; name = os.environ["PN"]; pw = os.environ["PW"]

# create_app(): Dify 1.14 returns (socketio, app); pre-1.14 a single app.
res = create_app()
app = res[1] if isinstance(res, tuple) else res
with app.app_context():
    # Dify must be initialised (admin + first workspace exist) before we can
    # provision members — otherwise there is no workspace to attach them to.
    if db.session.query(Tenant).count() == 0:
        print("RZFZSTATUS:NOTSETUP"); sys.exit(0)

    acct = db.session.query(Account).filter(Account.email == email).first()
    created = False
    if acct is None:
        # Create via Dify's own service. create_workspace_required lets Dify make
        # a personal workspace IF the deployment allows workspace creation; if it
        # doesn't (Dify CE default ALLOW_CREATE_WORKSPACE=false), register skips
        # the workspace and we attach the user to the existing one below.
        # The per-user RANDOM temp password is applied HERE, on create only.
        from services.account_service import RegisterService
        acct = RegisterService.register(
            email=email, name=(name or email), password=pw,
            status=AccountStatus.ACTIVE, is_setup=True,
            create_workspace_required=True)
        created = True
    # else: account already exists — DO NOT touch its password (broker-safe,
    # #54). We only reconcile active-status + workspace membership below.

    # Mark active so login works immediately (Dify has no separate is_verified).
    try:
        if acct.status != AccountStatus.ACTIVE.value:
            acct.status = AccountStatus.ACTIVE.value
    except Exception:
        acct.status = "active"
    if hasattr(acct, "initialized_at") and acct.initialized_at is None:
        from libs.datetime_utils import naive_utc_now
        acct.initialized_at = naive_utc_now()
    db.session.add(acct)
    db.session.commit()

    # Ensure the account belongs to a workspace. RegisterService makes one only
    # when workspace creation is allowed; otherwise join the existing (admin's)
    # workspace as a normal member — matches Dify's "invite to a workspace" model.
    join = db.session.query(TenantAccountJoin).filter(
        TenantAccountJoin.account_id == acct.id).first()
    if join is None:
        from services.account_service import TenantService
        tenant = db.session.query(Tenant).order_by(Tenant.created_at).first()
        TenantService.create_tenant_member(tenant, acct, role="normal")
        acct.current_tenant = tenant
        db.session.commit()
        join = db.session.query(TenantAccountJoin).filter(
            TenantAccountJoin.account_id == acct.id).first()
    has_tenant = join is not None
    # Sentinel-prefixed so the caller can extract the status from Dify's noisy
    # app-init stdout ("Warning! You didn't set docs_url..." etc.).
    print("RZFZSTATUS:" + ("CREATED" if created else "EXISTS") + ("+tenant" if has_tenant else "+NOTENANT"))
PY
)"
    docker exec -e PE="$email" -e PN="$name" -e PW="$pw" dify-api python3 -c "$pyscript" 2>/dev/null \
        | sed -n 's/^RZFZSTATUS://p' | tail -n1
}

provision_app_dify() {
    local group=$1 pw=$2 dry=$3 listonly=$4
    check_container dify-api || { print_warning "dify-api not running — skipping Dify."; return 2; }
    print_step "Dify — provisioning members of group: ${group}"
    local rows; rows="$(list_group_members "$group")" || return 1
    if [ -z "$rows" ]; then print_warning "No active members with email in '${group}'."; return 0; fi
    local fail=0 email name
    while IFS=$'\t' read -r _user email name; do
        [ -n "$email" ] || continue
        if [ "$listonly" = true ]; then
            # SQL-safe: pass the (Authentik-sourced) email via psql -v and quote
            # it with :'em' so psql does the escaping — never interpolate user
            # data into the SQL string (apostrophes / metachars would break or
            # inject). NB :'var' substitution only works in psql's file/stdin
            # mode, NOT with -c, so the SQL is fed via stdin (here-string) and
            # docker exec needs -i. CREATE/UPDATE paths use -e env (os.environ).
            local hit; hit="$(docker exec -i postgres psql -U "$PG_USER" -d "$DIFY_DB" \
                -v em="$email" -tA <<< "SELECT 1 FROM accounts WHERE email = :'em' LIMIT 1;" 2>/dev/null | xargs)"
            printf '  %-40s %s\n' "$email" "${hit:+exists}${hit:-MISSING}"
            continue
        fi
        if [ "$dry" = true ]; then
            print_substep "[dry-run] would provision Dify account: ${email}"
            continue
        fi
        # Per-user RANDOM temp password generated fresh for THIS member (distinct
        # each run). Only ever applied if the account is newly created; printed
        # below on CREATE for hand-off. --password ($pw) pins it if the operator
        # gave one. Never written to a file.
        local upw="${pw:-$(gen_temp_password)}"
        local out; out="$(dify_provision_one "$email" "$name" "$upw" || true)"
        case "$out" in
            CREATED*tenant) print_success "Dify: created ${email} (active, workspace joined)."
                            print_substep "temporary password (hand to user): ${upw}" ;;
            CREATED*NOTENANT) print_warning "Dify: created ${email} but no workspace join — check manually."
                            print_substep "temporary password (hand to user): ${upw}" ;;
            EXISTS*)        print_success "Dify: ${email} already exists — password unchanged (active)." ;;
            NOTSETUP)       print_error   "Dify is not initialised (no workspace yet) — run post-install first."; return 1 ;;
            *)              print_error   "Dify: failed to provision ${email} (out='${out}')."; fail=1 ;;
        esac
    done <<< "$rows"
    return "$fail"
}

# ── Cognee provisioning ───────────────────────────────────────────────────────
# Create-IF-MISSING through cognee's OWN fastapi-users user-manager (never hand-
# hash — cf. cli/set-admin-password.sh apply_cognee). On CREATE the per-user
# random temp password is set and the user marked verified so the login API
# accepts it. On EXISTS the password is left UNTOUCHED (broker-safe, #54) — we
# only reconcile is_active/is_verified, never re-password. The python is passed
# via `-c`/stdin-less heredoc to avoid the loop-fragile `docker exec -i` path.
cognee_provision_one() {
    local email=$1 pw=$2
    # `-c "$pyscript"` (not `python3 -` heredoc): docker exec has no stdin here,
    # and -i is loop-fragile — same rationale as dify_provision_one /
    # set-admin-password.sh apply_cognee. User data via -e env only.
    local pyscript; pyscript="$(cat <<'PY'
import asyncio, os
from cognee.infrastructure.databases.relational import get_relational_engine
from cognee.modules.users.get_user_manager import get_user_manager_context
from cognee.modules.users.get_user_db import get_user_db_context
from cognee.modules.users.models.User import UserCreate, UserUpdate

email = os.environ["PE"]; pw = os.environ["PW"]

async def main():
    engine = get_relational_engine()
    async with engine.get_async_session() as session:
        async with get_user_db_context(session) as user_db:
            async with get_user_manager_context(user_db) as um:
                user = await user_db.get_by_email(email)
                if user is None:
                    # fastapi-users create: hashes the password correctly and
                    # persists the row. Mark verified so login is accepted. The
                    # per-user RANDOM temp password is applied HERE, on create only.
                    await um.create(UserCreate(
                        email=email, password=pw,
                        is_active=True, is_verified=True, is_superuser=False))
                    print("RZFZSTATUS:CREATED")
                else:
                    # Already exists — DO NOT touch the password (broker-safe, #54).
                    # Reconcile only the login-ability flags; UserUpdate carries
                    # NO password= so the stored hash is left unchanged.
                    await um.update(
                        UserUpdate(is_active=True, is_verified=True),
                        user)
                    print("RZFZSTATUS:EXISTS")

asyncio.run(main())
PY
)"
    docker exec -e PE="$email" -e PW="$pw" cognee python3 -c "$pyscript" 2>/dev/null \
        | sed -n 's/^RZFZSTATUS://p' | tail -n1
}

provision_app_cognee() {
    local group=$1 pw=$2 dry=$3 listonly=$4
    check_container cognee || { print_warning "cognee not running — skipping Cognee."; return 2; }
    print_step "Cognee — provisioning members of group: ${group}"
    local rows; rows="$(list_group_members "$group")" || return 1
    if [ -z "$rows" ]; then print_warning "No active members with email in '${group}'."; return 0; fi
    local fail=0 email name
    while IFS=$'\t' read -r _user email name; do
        [ -n "$email" ] || continue
        if [ "$listonly" = true ]; then
            # SQL-safe: see the Dify path — :'em' (via stdin, not -c) makes psql
            # quote the value; -i gives docker exec the here-string as stdin.
            local hit; hit="$(docker exec -i postgres psql -U "$PG_USER" -d "$COGNEE_DB" \
                -v em="$email" -tA <<< "SELECT 1 FROM users WHERE email = :'em' LIMIT 1;" 2>/dev/null | xargs)"
            printf '  %-40s %s\n' "$email" "${hit:+exists}${hit:-MISSING}"
            continue
        fi
        if [ "$dry" = true ]; then
            print_substep "[dry-run] would provision Cognee user: ${email}"
            continue
        fi
        # Per-user RANDOM temp password for THIS member (distinct each run); only
        # applied on CREATE, printed below for hand-off. --password ($pw) pins it.
        local upw="${pw:-$(gen_temp_password)}"
        local out; out="$(cognee_provision_one "$email" "$upw" || true)"
        case "$out" in
            CREATED) print_success "Cognee: created ${email} (verified)."
                     print_substep "temporary password (hand to user): ${upw}" ;;
            EXISTS)  print_success "Cognee: ${email} already exists — password unchanged (verified)." ;;
            *)       print_error   "Cognee: failed to provision ${email} (out='${out}')."; fail=1 ;;
        esac
    done <<< "$rows"
    return "$fail"
}

usage() { sed -n '/^# ── Usage/,/^# Only apps whose container/p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# ── arg parse ─────────────────────────────────────────────────────────────────
APP="" GROUP="" PASSWORD="" DRY=false LISTONLY=false
while [ $# -gt 0 ]; do
    case "$1" in
        --app)      APP="${2:?--app needs a name}"; shift 2 ;;
        --group)    GROUP="${2:?--group needs a value}"; shift 2 ;;
        --password) PASSWORD="${2:?--password needs a value}"; shift 2 ;;
        --dry-run)  DRY=true; shift ;;
        --list)     LISTONLY=true; shift ;;
        -h|--help)  usage 0 ;;
        *) print_error "Unknown option: $1"; usage 1 ;;
    esac
done

[ -n "$APP" ] || { print_error "--app is required (dify | cognee | all)."; usage 1; }
case "$APP" in dify|cognee|all) ;; *) print_error "Unknown app: $APP (dify|cognee|all)."; exit 1 ;; esac

# Create-time credential. Default EMPTY → a per-user RANDOM temp password is
# generated for each newly-created account (see gen_temp_password / the loops).
# --password pins a fixed create-time credential instead. In neither case is an
# already-existing account re-passworded (broker-safe, #54). The provisioned
# password is NOT the user's Authentik password (which is unobtainable).
PW="${PASSWORD:-}"

rc=0
run_app() {
    local app=$1
    local grp="${GROUP:-$(default_group_for "$app")}"
    case "$app" in
        dify)   provision_app_dify   "$grp" "$PW" "$DRY" "$LISTONLY" || { [ $? -eq 2 ] || rc=1; } ;;
        cognee) provision_app_cognee "$grp" "$PW" "$DRY" "$LISTONLY" || { [ $? -eq 2 ] || rc=1; } ;;
    esac
}

if [ "$APP" = "all" ]; then
    # --group is per-app when --app all (each gets its own default); reject an
    # explicit --group with --all to avoid pointing both apps at one group.
    [ -z "$GROUP" ] || { print_error "--group cannot be combined with --app all (groups differ per app)."; exit 1; }
    run_app dify
    run_app cognee
else
    run_app "$APP"
fi

if [ "$LISTONLY" = true ] || [ "$DRY" = true ]; then
    print_info "Done (no changes made)."
elif [ "$rc" -eq 0 ]; then
    print_success "Provisioning complete. Verify login at the app's UI."
else
    print_error "Provisioning finished with errors (see above)."
fi
exit "$rc"
