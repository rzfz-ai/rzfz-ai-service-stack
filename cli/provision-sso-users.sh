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
#   provision-sso-users.sh --reconcile-roles --app all        # roles from core/rbac/roles.yaml
#   provision-sso-users.sh --reconcile-roles --app synapse --dry-run --summary
#       (--reconcile-roles: Dify, cognee and Synapse app roles follow the role
#        catalogue; an account whose owner left the module group is disabled,
#        never deleted. Only roles and disables the reconcile itself made are
#        undone: hand-granted roles and hand bans are left alone and reported,
#        and the last admin is never removed. Runs on every
#        `rzfz post-install --refresh` (so on every upgrade).
#        --summary prints RZFZ-DRIFT<TAB>app<TAB>pending-changes per app.
#        Without --summary it also restricts the Dify and cognee datasets of
#        each knowledge scope (#2473) to the scope's members and their owner.)
#
# Exit: 0 = all targeted members reconciled (or dry-run); 1 = a hard failure.
# Only apps whose container is running are touched; others reported skipped.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

# `--help`/`-h` must render WITHOUT a .env — the docs generator and a fresh
# checkout have none. Handle it before the .env precondition below so the help
# is environment-independent. Previously the missing-.env error truncated the
# whole help block, so the docs-freshness gate could never pass on a box (or CI
# checkout) that has no .env. Mirrors usage() (defined later) verbatim.
for _arg in "$@"; do case "$_arg" in
    -h|--help) sed -n '/^# ── Usage/,/^# Only apps whose container/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac; done

# #266 (E7): a stack-bound verb refuses on a thin inference node instead of
# reading a .env that is not there. Placed AFTER the early --help handler so
# help still renders anywhere, and BEFORE the .env precondition so the
# operator gets "this is a worker" rather than ".env not found".
refuse_on_worker_node provision-sso-users "Authentik and the app databases it reconciles live on the master."

ENV_FILE="${SCRIPT_DIR}/.env"

[ -f "$ENV_FILE" ] || { print_error ".env not found at $ENV_FILE"; exit 1; }

# Targeted reads only — never source operator-edited .env (memory rule).
PG_USER="$(read_env_value "$ENV_FILE" POSTGRES_USER)";   PG_USER="${PG_USER:-docker}"
MAIN_DOMAIN="$(read_env_value "$ENV_FILE" MAIN_DOMAIN)"
AUTHENTIK_TOKEN="$(read_env_value "$ENV_FILE" AUTHENTIK_BOOTSTRAP_TOKEN)"
DIFY_DB="$(read_env_value "$ENV_FILE" DIFY_DB)";         DIFY_DB="${DIFY_DB:-dify_db}"
COGNEE_DB="$(read_env_value "$ENV_FILE" COGNEE_DB)";     COGNEE_DB="${COGNEE_DB:-cognee_db}"
SYNAPSE_DB="$(read_env_value "$ENV_FILE" SYNAPSE_DB)";   SYNAPSE_DB="${SYNAPSE_DB:-synapse_db}"

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
# The Authorization header line, for `curl -H @-` (stdin). printf is a shell
# builtin, so the token is never on any process's argv (#2473 review I6).
authentik_auth_header() {
    printf 'Authorization: Bearer %s\n' "$AUTHENTIK_TOKEN"
}

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
    local url="${base}/api/v3/core/users/?groups_by_name=${enc}&page_size=100" first
    first="$url"
    local guard=0
    while [ -n "$url" ] && [ "$guard" -lt 50 ]; do
        guard=$((guard + 1))
        local resp
        # Never `-k`/`--insecure`: this call carries the admin bootstrap token
        # (#857). authentik_cacert_args pins the box's own CA bundle when it
        # exists, else falls back to the system trust store.
        authentik_cacert_args
        # #2473 review I6: the bootstrap token reaches curl on STDIN (`-H @-`),
        # never on its argv — argv is world-readable in /proc/<pid>/cmdline.
        # The role reconcile (and so `rzfz status`) bounds every call
        # (AK_CONNECT_TIME / AK_MAX_TIME, set by reconcile_roles); a paused
        # Authentik accepts the connection and never answers.
        local crc=0
        resp="$(authentik_auth_header | curl -s --connect-timeout "${AK_CONNECT_TIME:-10}" --max-time "${AK_MAX_TIME:-30}" \
            "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" -H @- -H "Accept: application/json" "$url" 2>/dev/null)" || crc=$?
        [ -n "$resp" ] || { print_error "Authentik API not answering at ${base} (curl rc=${crc}, limit ${AK_MAX_TIME:-30}s)"; return 1; }
        # Parse this page; capture the next-page URL via a trailing marker line.
        url="$(printf '%s' "$resp" | AK_BASE="$base" AK_FIRST="$first" python3 -c '
import sys, json, os
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR", file=sys.stderr); sys.exit(2)
for u in d.get("results", []):
    if not u.get("is_active", False):
        continue
    email = (u.get("email") or "").strip()
    uid = (u.get("uid") or "").strip()
    # The create path keys on the e-mail and skips a row without one; the role
    # reconcile keys Synapse on `uid` (the OIDC sub), so such a row still counts.
    if not email and not uid:
        continue
    username = (u.get("username") or "").strip()
    name = (u.get("name") or username).strip()
    # TAB-separated; sanitise embedded tabs/newlines defensively.
    clean = lambda s: s.replace("\t", " ").replace("\n", " ")
    sys.stderr.write("%s\t%s\t%s\t%s\n" % (clean(username), clean(email), clean(name), clean(uid)))
nxt = (d.get("pagination") or {}).get("next")
# Authentik 2026.x returns `next` as a page NUMBER (0 = none); older builds a URL.
if isinstance(nxt, int) and not isinstance(nxt, bool):
    print(os.environ["AK_FIRST"] + "&page=%d" % nxt if nxt > 0 else "")
else:
    print(nxt or "")
' 2>>"${_MEMBERS_TMP}")" || { print_error "Failed to parse Authentik response."; return 1; }
    done
    # #2473: a listing cut off at the page guard is INCOMPLETE — the role
    # reconcile would read the missing members as "left the group".
    if [ -n "$url" ]; then
        print_error "Authentik group '${group}' has more than ${guard} pages of members — listing incomplete."
        return 1
    fi
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
        try:
            from services.account_service import RegisterService
        except ImportError:
            RegisterService = None   # Dify 1.18 removed services/account_service.py
        if RegisterService is not None:
            # Dify >= 1.16 requires a keyword-only `session` (1.17.1: `*, session:
            # Session`); omitting it raises TypeError (#2662).
            acct = RegisterService.register(
                email=email, name=(name or email), password=pw,
                status=AccountStatus.ACTIVE, is_setup=True,
                create_workspace_required=True, session=db.session)
        else:
            # Dify 1.18: the lifecycle service is an instance on the app's
            # application services (it owns its session and returns a snapshot).
            from extensions.ext_application_services import application_services
            application_services().accounts.lifecycle.register(
                email=email, name=(name or email), password=pw,
                status=AccountStatus.ACTIVE.value, is_setup=True,
                create_workspace_required=True)
            db.session.expire_all()
            acct = db.session.query(Account).filter(Account.email == email).first()
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
        tenant = db.session.query(Tenant).order_by(Tenant.created_at).first()
        try:
            from services.account_service import TenantService
        except ImportError:
            TenantService = None   # Dify 1.18
        if TenantService is not None:
            TenantService.create_tenant_member(tenant, acct, session=db.session, role="normal")
            acct.current_tenant = tenant
        else:
            from extensions.ext_application_services import application_services
            from models.account import TenantAccountRole
            application_services().workspaces.members.join_member(
                workspace_id=tenant.id, account_id=acct.id, email=acct.email,
                role=TenantAccountRole.NORMAL, operator_account_id=None)
            db.session.expire_all()
        db.session.commit()
        join = db.session.query(TenantAccountJoin).filter(
            TenantAccountJoin.account_id == acct.id).first()
    has_tenant = join is not None
    # Sentinel-prefixed so the caller can extract the status from Dify's noisy
    # app-init stdout ("Warning! You didn't set docs_url..." etc.).
    print("RZFZSTATUS:" + ("CREATED" if created else "EXISTS") + ("+tenant" if has_tenant else "+NOTENANT"))
PY
)"
    # stderr is KEPT (#2662): a bare `2>/dev/null` reduced a TypeError from Dify
    # to an empty out=''. Without a status line we emit RZFZERR:<last stderr
    # line> so the caller can show the real reason.
    local errf; errf="$(mktemp)"
    local status
    status="$(PE="$email" PN="$name" PW="$pw" docker exec -e PE -e PN -e PW dify-api python3 -c "$pyscript" 2>"$errf" \
        | sed -n 's/^RZFZSTATUS://p' | tail -n1)" || true
    if [ -n "$status" ]; then
        printf '%s\n' "$status"
    else
        printf 'RZFZERR:%s\n' "$(grep -v '^[[:space:]]*$' "$errf" | tail -n1 | tr '\t\r' '  ' | cut -c1-300)"
    fi
    rm -f "$errf"
}

provision_app_dify() {
    local group=$1 pw=$2 dry=$3 listonly=$4
    check_container dify-api || { print_warning "dify-api not running — skipping Dify."; return 2; }
    print_step "Dify — provisioning members of group: ${group}"
    local rows; rows="$(list_group_members "$group")" || return 1
    if [ -z "$rows" ]; then print_warning "No active members with email in '${group}'."; return 0; fi
    local fail=0 email name nfail=0 ntotal=0
    while IFS=$'\t' read -r _user email name _uid; do
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
        ntotal=$((ntotal+1))
        local out; out="$(dify_provision_one "$email" "$name" "$upw" || true)"
        case "$out" in
            CREATED*tenant) print_success "Dify: created ${email} (active, workspace joined)."
                            print_substep "temporary password (hand to user): ${upw}" ;;
            CREATED*NOTENANT) print_warning "Dify: created ${email} but no workspace join — check manually."
                            print_substep "temporary password (hand to user): ${upw}" ;;
            EXISTS*)        print_success "Dify: ${email} already exists — password unchanged (active)." ;;
            NOTSETUP)       print_error   "Dify is not initialised (no workspace yet) — run post-install first."; return 1 ;;
            RZFZERR:*)      print_error   "Dify: failed to provision ${email}: ${out#RZFZERR:}"; fail=1; nfail=$((nfail+1)) ;;
            *)              print_error   "Dify: failed to provision ${email} (out='${out}')."; fail=1; nfail=$((nfail+1)) ;;
        esac
    done <<< "$rows"
    if [ "$ntotal" -gt 0 ]; then
        if [ "$nfail" -gt 0 ]; then print_error "Dify: ${nfail} of ${ntotal} member(s) failed to provision.";
        else print_substep "Dify: 0 of ${ntotal} member(s) failed."; fi
    fi
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
    PE="$email" PW="$pw" docker exec -e PE -e PW cognee python3 -c "$pyscript" 2>/dev/null \
        | sed -n 's/^RZFZSTATUS://p' | tail -n1
}

provision_app_cognee() {
    local group=$1 pw=$2 dry=$3 listonly=$4
    check_container cognee || { print_warning "cognee not running — skipping Cognee."; return 2; }
    print_step "Cognee — provisioning members of group: ${group}"
    local rows; rows="$(list_group_members "$group")" || return 1
    if [ -z "$rows" ]; then print_warning "No active members with email in '${group}'."; return 0; fi
    local fail=0 email name nfail=0 ntotal=0
    while IFS=$'\t' read -r _user email name _uid; do
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

# ── Role reconcile: Dify, cognee, Synapse (#2473 D5) ──────────────────────────
# `--reconcile-roles` sets each existing account's app role from the role
# catalogue (core/rbac/roles.yaml `downstream:`/`module_access:`) and disables,
# never deletes, an account whose owner left the module-access group (Dify
# status=banned, cognee is_active=false; Synapse only loses admin, D5). The
# planner is app/services/downstream_roles.py in razzfazz-config (it holds the
# catalogue and the ledger); this script gathers its inputs and applies its
# actions.
#
# Privilege rules (enforced by the planner, A12):
#   * a role is only RAISED, and only to the role the catalogue maps the user to;
#   * leaving the module group disables the account, whatever the ledger says;
#   * a role is lowered/replaced only when the LEDGER records that this
#     reconcile set exactly that role (hand-granted roles are HELD, a WARN);
#   * an account is re-enabled only when the ledger records that this reconcile
#     disabled it (a hand ban is never lifted);
#   * the app's last active admin is never removed (REFUSED, loudly);
#   * an unusable catalogue, an unreadable ledger, an unreachable Authentik or an
#     unreadable app DB changes NOTHING (the whole run when the catalogue is
#     the problem).
# The ledger lives in razzfazz-config's data volume (/data/rbac/, archived by
# the backup), written under flock by the planner. The first A12 build kept it
# at ${RR_OLD_LEDGER}; it is imported once and renamed.
# Synapse accounts are matched by the OIDC `sub` (user_external_ids, provider
# oidc-authentik = homeserver.yaml idp_id `authentik`); accounts without one are
# counted as unmanaged.
RR_OLD_LEDGER="${SCRIPT_DIR}/.rzfz-state/downstream-roles.json"
RR_SYNAPSE_PROVIDER="oidc-authentik"
# The Authentik admin username (post-install promotes it besides razzfazz-ai-admin).
RR_ADMIN_USERNAME="$(read_env_value "$ENV_FILE" RAZZFAZZ_ADMIN_USERNAME 2>/dev/null || true)"; RR_ADMIN_USERNAME="${RR_ADMIN_USERNAME:-akadmin}"
# Who runs the reconcile, for the portal audit log's `user` (the hourly run,
# scripts/scope-reconcile-run.sh, names itself; empty = this command).
RR_ACTOR="${RZFZ_RECONCILE_ACTOR:-}"

# #2473 D5 / #2536 item 11: values reach psql as `\set` meta-commands on stdin
# (argv is world-readable in the process table; a value is never spliced into
# SQL). Escaping as scripts/owui_config_reconcile.py::_psql_set_lines.
psql_set_line() {
    local name=$1 v=$2
    [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { print_error "psql variable name refused: $name"; return 2; }
    v=${v//\\/\\\\}; v=${v//\'/\'\'}
    v=${v//$'\n'/\\n}; v=${v//$'\r'/\\r}; v=${v//$'\t'/\\t}
    printf "\\set %s '%s'\n" "$name" "$v"
}

rr_psql() {
    local db=$1 sql=$2 kv
    shift 2
    {
        for kv in "$@"; do psql_set_line "${kv%%=*}" "${kv#*=}" || return 2; done
        printf '%s\n' "$sql"
    } | docker exec -i postgres psql -X -v ON_ERROR_STOP=1 -tA -U "$PG_USER" -d "$db"
}

# One statement per planner action; `app` picks the table shape (D5). Every
# statement re-checks what it may touch (owner joins, the account's state) and
# ends in RETURNING 1: an UPDATE that matched no row is a FAILURE, logged, and
# never recorded in the ledger or audited as a success.
# With RR_APPLIED set, each action's outcome is appended there as a JSON line
# (the ledger and the audit log are written from it).
rr_apply_actions() {
    local app=$1 json=$2 op email role db sql rc=0 outcome out rows
    case "$app" in dify) db=$DIFY_DB ;; cognee) db=$COGNEE_DB ;; synapse) db=$SYNAPSE_DB ;;
        *) print_error "unknown app: $app"; return 2 ;; esac
    # Synapse: the account behind this sub, and only when it has exactly ONE
    # link (a user id linked to two subs is the operator's to resolve).
    local syn="SELECT e.user_id FROM user_external_ids e WHERE e.auth_provider = '${RR_SYNAPSE_PROVIDER:-oidc-authentik}' AND e.external_id = :'em' AND (SELECT count(*) FROM user_external_ids x WHERE x.user_id = e.user_id AND x.auth_provider = e.auth_provider) = 1"
    while IFS=$'\t' read -r op email role; do
        [ -n "$op" ] || continue
        case "$app:$op" in
            dify:set_role)    sql="UPDATE tenant_account_joins SET role = :'role' WHERE role <> 'owner' AND role <> :'role' AND account_id IN (SELECT id FROM accounts WHERE email = :'em') RETURNING 1;" ;;
            dify:disable)     sql="UPDATE accounts SET status = 'banned' WHERE email = :'em' AND status = 'active' AND NOT EXISTS (SELECT 1 FROM tenant_account_joins j WHERE j.account_id = accounts.id AND j.role = 'owner') RETURNING 1;" ;;
            dify:enable)      sql="UPDATE accounts SET status = 'active' WHERE email = :'em' AND status = 'banned' RETURNING 1;" ;;
            cognee:set_role)  sql="UPDATE users SET is_superuser = (:'role' = 'superuser') WHERE email = :'em' AND is_superuser <> (:'role' = 'superuser') RETURNING 1;" ;;
            cognee:disable)   sql="UPDATE users SET is_active = false WHERE email = :'em' AND is_active RETURNING 1;" ;;
            cognee:enable)    sql="UPDATE users SET is_active = true WHERE email = :'em' AND NOT is_active RETURNING 1;" ;;
            synapse:set_role) sql="UPDATE users SET admin = CASE WHEN :'role' = 'admin' THEN 1 ELSE 0 END WHERE name IN (${syn}) AND admin <> CASE WHEN :'role' = 'admin' THEN 1 ELSE 0 END RETURNING 1;" ;;
            synapse:disable)  sql="UPDATE users SET admin = 0 WHERE name IN (${syn}) AND admin <> 0 RETURNING 1;" ;;
            synapse:enable)   continue ;;   # D5: Synapse accounts are never deactivated, so nothing to re-enable
            *) print_error "unknown action $op for $app"; rc=1; continue ;;
        esac
        if out="$(rr_psql "$db" "$sql" "em=$email" "role=$role")"; then
            rows=$(printf '%s\n' "$out" | grep -c '^1$' || true)
            if [ "$rows" -gt 0 ]; then
                outcome=success
                print_substep "${app}: ${op} ${email}${role:+ → ${role}}"
            else
                outcome="failure: 0 rows"
                print_error "${app}: ${op} ${email} changed NO row — the account moved since the plan; not recorded"; rc=1
            fi
        else
            outcome="failure: psql"
            print_error "${app}: ${op} ${email} FAILED"; rc=1
        fi
        if [ -n "${RR_APPLIED:-}" ]; then
            RR_OP="$op" RR_EM="$email" RR_ROLE="$role" RR_OUT="$outcome" python3 -c 'import json,os
print(json.dumps({"op": os.environ["RR_OP"], "email": os.environ["RR_EM"],
                  "role": os.environ["RR_ROLE"] or None, "outcome": os.environ["RR_OUT"]}))' >> "$RR_APPLIED"
        fi
    done < <(printf '%s' "$json" | python3 -c 'import json,sys
for a in json.load(sys.stdin):
    print("\t".join((a["op"], a["email"], a.get("role") or "")))')
    return "$rc"
}

# The current accounts, as {"accounts": {key: {...}}, "unmanaged": n,
# "unmanaged_admins": m} (no user data in these statements).
#   Dify: key e-mail, its real status (active/banned/pending/uninitialized/
#         closed); an account in several workspaces reads as its highest role
#         there (owner first), so the planner's guards see the most privileged join.
#   cognee: key e-mail.
#   Synapse: key the OIDC sub in user_external_ids; a live, non-appservice
#         account without one is unmanaged (counted, admins separately).
rr_current_sql() {
    case "$1" in
        dify) echo "SELECT json_build_object('unmanaged', 0, 'unmanaged_admins', 0, 'accounts', coalesce(json_object_agg(s.email, json_build_object('role', s.role, 'status', s.status)), '{}')) FROM (SELECT a.email, a.status::text AS status, (array_agg(j.role::text ORDER BY CASE j.role::text WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 WHEN 'editor' THEN 2 WHEN 'dataset_operator' THEN 3 ELSE 4 END))[1] AS role FROM accounts a JOIN tenant_account_joins j ON j.account_id = a.id GROUP BY a.id, a.email, a.status) s;" ;;
        cognee) echo "SELECT json_build_object('unmanaged', 0, 'unmanaged_admins', 0, 'accounts', coalesce(json_object_agg(email, json_build_object('role', CASE WHEN is_superuser THEN 'superuser' ELSE 'user' END, 'active', is_active)), '{}')) FROM users;" ;;
        synapse) echo "WITH l AS (SELECT e.user_id, array_agg(e.external_id ORDER BY e.external_id) AS subs FROM user_external_ids e WHERE e.auth_provider = '${RR_SYNAPSE_PROVIDER}' GROUP BY e.user_id), live AS (SELECT u.* FROM users u WHERE u.deactivated = 0 AND u.appservice_id IS NULL) SELECT json_build_object('accounts', coalesce((SELECT json_object_agg(l.subs[1], json_build_object('role', CASE WHEN u.admin = 1 THEN 'admin' ELSE 'user' END, 'active', u.deactivated = 0, 'user', u.name)) FROM l JOIN users u ON u.name = l.user_id WHERE cardinality(l.subs) = 1), '{}'), 'conflicts', coalesce((SELECT json_agg(json_build_object('user', u.name, 'subs', l.subs, 'role', CASE WHEN u.admin = 1 THEN 'admin' ELSE 'user' END, 'active', u.deactivated = 0) ORDER BY u.name) FROM l JOIN users u ON u.name = l.user_id WHERE cardinality(l.subs) > 1), '[]'), 'unmanaged', (SELECT count(*) FROM live u WHERE u.name NOT IN (SELECT user_id FROM l)), 'unmanaged_admins', (SELECT count(*) FROM live u WHERE u.admin = 1 AND u.name NOT IN (SELECT user_id FROM l)), 'total', (SELECT count(*) FROM live));" ;;
        *) return 2 ;;
    esac
}

rr_app_container() {
    case "$1" in dify) echo dify-api ;; cognee) echo cognee ;; synapse) echo synapse ;; *) return 2 ;; esac
}

rr_app_db() {
    case "$1" in dify) echo "$DIFY_DB" ;; cognee) echo "$COGNEE_DB" ;; synapse) echo "$SYNAPSE_DB" ;; *) return 2 ;; esac
}

# The planner, inside razzfazz-config (it mounts the role catalogue and holds
# the ledger in its data volume). Exit 3 = catalogue unusable, 4 = ledger unusable.
rr_planner() {
    docker exec -i razzfazz-config python -m app.services.downstream_roles "$@"
}

# 0 = the group exists, 1 = no such group, 2 = Authentik did not answer usably.
# A group that does not exist lists no members; the reconcile must not read
# that as "everyone left" and disable every account.
authentik_group_exists() {
    local group=$1 enc resp
    [ -n "$AUTHENTIK_TOKEN" ] && [ -n "$MAIN_DOMAIN" ] || return 2
    enc="$(printf '%s' "$group" | python3 -c 'import sys,urllib.parse; sys.stdout.write(urllib.parse.quote(sys.stdin.read()))')"
    authentik_cacert_args
    resp="$(authentik_auth_header | curl -s --connect-timeout "${AK_CONNECT_TIME:-10}" --max-time "${AK_MAX_TIME:-15}" \
        "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" -H @- -H "Accept: application/json" \
        "https://auth.${MAIN_DOMAIN}/api/v3/core/groups/?name=${enc}&include_users=false&page_size=5" 2>/dev/null || true)"
    [ -n "$resp" ] || return 2
    printf '%s' "$resp" | RR_G="$group" python3 -c 'import json,os,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
r = d.get("results") if isinstance(d, dict) else None
if not isinstance(r, list):
    sys.exit(2)
sys.exit(0 if any(isinstance(g, dict) and g.get("name") == os.environ["RR_G"] for g in r) else 1)'
}

# The first A12 build kept the ledger on the host; import it into the volume
# once, then rename it so it is never imported again.
rr_migrate_old_ledger() {
    [ -f "$RR_OLD_LEDGER" ] || return 0
    local out added dropped
    if out="$(rr_planner ledger-import < "$RR_OLD_LEDGER" 2>/dev/null)"; then
        read -r added dropped < <(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["added"], d["dropped"])')
        mv -f "$RR_OLD_LEDGER" "${RR_OLD_LEDGER}.migrated" \
            && print_info "role reconcile: imported ${added} ledger entr(y/ies) from ${RR_OLD_LEDGER} into the rbac volume"
        [ "${dropped:-0}" -eq 0 ] \
            || print_info "role reconcile: dropped ${dropped} Synapse ledger entr(y/ies) keyed by e-mail — Synapse is matched by the OIDC sub now, they could never match"
    else
        print_warning "role reconcile: the old ledger ${RR_OLD_LEDGER} could not be imported — left in place"
        return 1
    fi
}

rr_summary() {
    [ "$SUMMARY" = true ] || return 0
    local IFS=$'\t'
    printf 'RZFZ-%s\t%s\n' "$1" "${*:2}"
}

# Plan every requested app first; apply only when no app hit an unusable
# catalogue (then nothing changes anywhere). Returns 1 when an app could not
# be checked or an action failed.
reconcile_roles() {
    local work rc=0
    # Bounded Authentik calls: the first one that gets no answer ends all of
    # them (authentik_down in _reconcile_roles), so a hung Authentik costs one
    # AK_MAX_TIME, not one per group.
    local AK_CONNECT_TIME=2 AK_MAX_TIME=4
    work="$(mktemp -d)"
    _reconcile_roles "$work" "$@" || rc=$?
    rm -rf "$work"
    return "$rc"
}

# Fetch one group's member rows (username TAB email TAB name TAB uid) once per
# run into the cache; 0 ok, 1 no such group, 2 Authentik unusable.
rr_group_rows() {
    local work=$1 g=$2 key f ge=0 rows
    key="$(printf '%s' "$g" | cksum | cut -d' ' -f1)"
    f="$work/cache/$key"
    if [ -f "$f.rc" ]; then return "$(cat "$f.rc")"; fi
    mkdir -p "$work/cache"
    authentik_group_exists "$g" || ge=$?
    if [ "$ge" -eq 0 ]; then
        rows="$(list_group_members "$g")" || ge=2
        [ "$ge" -ne 0 ] || printf '%s\n' "$rows" > "$f"
    fi
    echo "$ge" > "$f.rc"
    return "$ge"
}

_reconcile_roles() {
    local work=$1; shift
    local apps=("$@") app rc=0 bad_catalogue=0 authentik_down=0 groups_json g i n db report err ge col
    local -a planned=()
    if ! check_container razzfazz-config || ! check_container postgres; then
        print_warning "role reconcile: razzfazz-config or postgres is not running — nothing reconciled"
        for app in "${apps[@]}"; do rr_summary UNCHECKED "$app" "planner or database not running"; done
        return 1
    fi
    [ "$DRY" = true ] || rr_migrate_old_ledger || rc=1
    for app in "${apps[@]}"; do
        if ! check_container "$(rr_app_container "$app")"; then
            print_info "role reconcile: ${app} is not running — skipped"
            rr_summary SKIP "$app" "not running"
            continue
        fi
        if [ "$authentik_down" -eq 1 ]; then
            rr_summary UNCHECKED "$app" "Authentik not answering (no reply within ${AK_MAX_TIME:-?}s)"; rc=1; continue
        fi
        err=0
        groups_json="$(rr_planner groups "$app" </dev/null 2>"$work/err")" || err=$?
        if [ "$err" -eq 3 ]; then bad_catalogue=1; break; fi
        if [ "$err" -ne 0 ]; then
            print_warning "role reconcile: ${app}: the planner failed ($(head -c 300 "$work/err")) — ${app} NOT reconciled"
            rr_summary UNCHECKED "$app" "planner failed"; rc=1; continue
        fi
        mkdir -p "$work/$app"
        # Synapse is keyed by the OIDC sub (column 4), the others by e-mail (2).
        col=2; [ "$app" = synapse ] && col=4
        i=0 err=0
        while IFS= read -r g; do
            [ -n "$g" ] || continue
            i=$((i + 1))
            ge=0
            rr_group_rows "$work" "$g" || ge=$?
            if [ "$ge" -ne 0 ]; then
                if [ "$ge" -eq 1 ]; then
                    print_warning "role reconcile: ${app}: Authentik has no group '${g}' — ${app} NOT reconciled"
                else
                    print_warning "role reconcile: Authentik not answering (no reply within ${AK_MAX_TIME:-?}s, group '${g}') — nothing reconciled from here on"
                    authentik_down=1
                fi
                err=1; break
            fi
            printf '%s' "$g" > "$work/$app/g$i.name"
            cut -f"$col" "$work/cache/$(printf '%s' "$g" | cksum | cut -d' ' -f1)" > "$work/$app/g$i.emails"
        done < <(printf '%s' "$groups_json" | python3 -c 'import json,sys
for g in json.load(sys.stdin):
    print(g)')
        if [ "$err" -ne 0 ] || [ "$i" -eq 0 ]; then
            rr_summary UNCHECKED "$app" "$([ "$authentik_down" -eq 1 ] && echo "Authentik not answering (no reply within ${AK_MAX_TIME:-?}s)" || echo "Authentik groups not readable")"
            rc=1; continue
        fi
        db="$(rr_app_db "$app")"
        if ! rr_psql "$db" "$(rr_current_sql "$app")" > "$work/$app/current.json" 2>"$work/err" \
           || ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d.get("accounts"), dict)' "$work/$app/current.json" 2>/dev/null; then
            print_warning "role reconcile: ${app}: accounts not readable from ${db} — ${app} NOT reconciled"
            rr_summary UNCHECKED "$app" "database not readable"; rc=1; continue
        fi
        err=0
        report="$(RR_APP="$app" RR_DIR="$work/$app" RR_SVC="razzfazz-ai-admin@${MAIN_DOMAIN}" RR_SVC_LP="razzfazz-ai-admin ${RR_ADMIN_USERNAME}" RR_DOMAIN="$MAIN_DOMAIN" python3 -c 'import glob,json,os
d = os.environ["RR_DIR"]
members = {}
for f in sorted(glob.glob(os.path.join(d, "g*.name"))):
    name = open(f, encoding="utf-8").read()
    members[name] = [l.strip() for l in open(f[:-5] + ".emails", encoding="utf-8") if l.strip()]
cur = json.load(open(os.path.join(d, "current.json"), encoding="utf-8"))
# The stack identity is never touched and counts as the break-glass admin; the
# upstream cognee dev account (#2404) is never touched and never counts.
# Synapse: the stack admin logs in to Matrix as its Authentik username (the
# localpart template is preferred_username), whatever its sub.
# cognee: the per-user memory accounts mcp-manager mints under the box domain
# belong to mcp-manager; the planner recognises them by their address shape.
print(json.dumps({"app": os.environ["RR_APP"], "members": members, "current": cur["accounts"],
                  "conflicts": cur.get("conflicts") or [],
                  "service_accounts": [os.environ["RR_SVC"]],
                  "service_localparts": os.environ["RR_SVC_LP"].split(),
                  "ignored_accounts": ["default_user@example.com"] if os.environ["RR_APP"] == "cognee" else [],
                  "mcp_domain": os.environ["RR_DOMAIN"] if os.environ["RR_APP"] == "cognee" else ""}))' \
            | rr_planner plan 2>"$work/err")" || err=$?
        if [ "$err" -eq 3 ]; then bad_catalogue=1; break; fi
        if [ "$err" -eq 4 ]; then
            print_warning "role reconcile: ${app}: $(head -c 300 "$work/err")"
            rr_summary UNCHECKED "$app" "ledger unreadable — no removals"; rc=1; continue
        fi
        if [ "$err" -ne 0 ] || ! printf '%s' "$report" > "$work/$app/report.json"; then
            print_warning "role reconcile: ${app}: the planner failed — ${app} NOT reconciled"
            rr_summary UNCHECKED "$app" "planner failed"; rc=1; continue
        fi
        planned+=("$app")
    done
    if [ "$bad_catalogue" -eq 1 ]; then
        print_warning "role reconcile: the role catalogue is unusable ($(head -c 300 "$work/err")) — NOTHING was changed in any app"
        for app in "${apps[@]}"; do rr_summary UNCHECKED "$app" "role catalogue unusable"; done
        return 1
    fi
    for app in "${planned[@]}"; do
        local f="$work/$app/report.json" actions line
        # One python pass: the counts and the human lines, TAB-separated.
        local -a counts
        read -r -a counts < <(python3 -c 'import json,sys
r = json.load(open(sys.argv[1])); c = json.load(open(sys.argv[2]))
held = [h for h in r["held"] if h.get("kind") != "ban"]
bans = [h for h in r["held"] if h.get("kind") == "ban"]
print(len(r["actions"]), len(held), len(bans), len(r["refused"]), r["matched"], c.get("unmanaged", 0), c.get("unmanaged_admins", 0),
      c.get("total", r["matched"] + c.get("unmanaged", 0)))' \
            "$f" "$work/$app/current.json")
        n=${counts[0]}
        rr_summary DRIFT "$app" "$n"
        rr_summary MATCHED "$app" "${counts[4]}"
        rr_summary ACCOUNTS "$app" "${counts[7]}"
        [ "${counts[5]}" -eq 0 ] || { rr_summary UNMANAGED "$app" "${counts[5]}" "${counts[6]}"
            print_warning "role reconcile: ${app}: ${counts[5]} account(s) (${counts[6]} admin) are not linked to Authentik — not managed"; }
        [ "${counts[1]}" -eq 0 ] || rr_summary HELD "$app" "${counts[1]}"
        [ "${counts[2]}" -eq 0 ] || rr_summary BANNED "$app" "${counts[2]}"
        [ "${counts[3]}" -eq 0 ] || rr_summary REFUSED "$app" "${counts[3]}"
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            case "$line" in
                ban*) print_info "role reconcile: ${app}: ${line#ban$'\t'}" ;;
                *)    print_warning "role reconcile: ${app}: ${line#*$'\t'}" ;;
            esac
        done < <(python3 -c 'import json,sys
r = json.load(open(sys.argv[1]))
for h in r["held"]:
    print("%s\t%s %s (%s): %s" % (h.get("kind", "role"), "held" if h.get("kind") != "ban" else "kept",
                                   h["email"], h.get("role"), h["reason"]))
for h in r["refused"]:
    print("refused\tREFUSED %s %s: %s" % (h["op"], h["email"], h["reason"]))' "$f")
        if [ "$DRY" = true ]; then
            [ "$n" -eq 0 ] || python3 -c 'import json,sys
for a in json.load(open(sys.argv[1]))["actions"]:
    print("  [dry-run] %s %s%s" % (a["op"], a["email"], (" -> " + a["role"]) if a.get("role") else ""))' "$f"
            continue
        fi
        [ "$n" -gt 0 ] || { print_success "role reconcile: ${app} matches the catalogue"; continue; }
        actions="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["actions"]))' "$f")"
        : > "$work/$app/applied.jsonl"
        RR_APPLIED="$work/$app/applied.jsonl" rr_apply_actions "$app" "$actions" || rc=1
        RR_ACTOR="$RR_ACTOR" python3 -c 'import json,os,sys
r = json.load(open(sys.argv[1]))
print(json.dumps({"app": r["app"], "default_role": r["default_role"], "actor": os.environ.get("RR_ACTOR") or None,
                  "applied": [json.loads(l) for l in open(sys.argv[2], encoding="utf-8")]}))' \
            "$f" "$work/$app/applied.jsonl" > "$work/$app/applied.json"
        rr_planner ledger-update < "$work/$app/applied.json" >/dev/null 2>"$work/err" \
            || { print_error "role reconcile: ${app}: the ledger was NOT updated ($(head -c 300 "$work/err"))"; rc=1; }
        rr_planner audit < "$work/$app/applied.json" >/dev/null 2>&1 \
            || print_warning "role reconcile: ${app}: the changes were applied but NOT written to the portal audit log"
    done
    # #2473 D6: the knowledge-scope datasets of the apps planned above. The
    # status check (--summary, always a dry run) reads them too: one SELECT per
    # system, and it reports the scope drift (RZFZ-SCOPE* lines). An Authentik
    # that stopped answering is already reported once, per app (UNCHECKED).
    if [ "$authentik_down" -eq 0 ] && [ "${#planned[@]}" -gt 0 ]; then
        rr_reconcile_datasets "$work" "${planned[@]}" || rc=1
    fi
    return "$rc"
}

# ── Knowledge scopes: Dify and cognee datasets (#2473 D6) ────────────────────
# A knowledge scope is an Authentik group with attributes.rzfz_kb_scope: true
# (named "KB: <scope>"). A Dify dataset joins it by carrying a knowledge tag
# named like the group; a cognee dataset joins by name, listed in the group's
# attributes.rzfz_kb_targets.cognee_datasets (a name several cognee datasets
# share links none of them). A dataset in a scope is readable by exactly the
# scope's members plus its owner; a dataset in no scope is left alone. Once per
# box, every untagged team-wide Dify dataset becomes owner-only (D6: nothing
# stays readable by everyone because nobody decided). Open WebUI's side is its
# group sync (modules/chat/compose.yml). The planner is
# app/services/downstream_roles.py (`datasets`), which also writes the
# Settings -> Knowledge access report.

# The scopes as JSON {group: {"cognee_targets": [...]}}; 1 = Authentik unusable.
authentik_kb_scopes() {
    local base="https://auth.${MAIN_DOMAIN}" url first guard=0 resp out
    [ -n "$AUTHENTIK_TOKEN" ] && [ -n "$MAIN_DOMAIN" ] || return 1
    first="${base}/api/v3/core/groups/?include_users=false&page_size=100&attributes=%7B%22rzfz_kb_scope%22%3A%20true%7D"
    url="$first"
    out="$(mktemp)"
    # shellcheck disable=SC2064
    trap "rm -f '${out}'" RETURN
    while [ -n "$url" ] && [ "$guard" -lt 50 ]; do
        guard=$((guard + 1))
        authentik_cacert_args
        resp="$(authentik_auth_header | curl -s --connect-timeout "${AK_CONNECT_TIME:-10}" --max-time "${AK_MAX_TIME:-30}" \
            "${RZFZ_AUTHENTIK_CACERT_ARGS[@]}" -H @- -H "Accept: application/json" "$url" 2>/dev/null || true)"
        [ -n "$resp" ] || return 1
        # The attribute is checked here again: an Authentik that ignores the
        # filter lists every group, and only a flagged one is a scope.
        url="$(printf '%s' "$resp" | AK_FIRST="$first" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
r = d.get("results") if isinstance(d, dict) else None
if not isinstance(r, list):
    sys.exit(2)
for g in r:
    a = (g.get("attributes") or {}) if isinstance(g, dict) else {}
    if isinstance(a, dict) and a.get("rzfz_kb_scope") is True and g.get("name"):
        t = (a.get("rzfz_kb_targets") or {}).get("cognee_datasets") if isinstance(a.get("rzfz_kb_targets"), dict) else []
        t = [x for x in t if isinstance(x, str) and x] if isinstance(t, list) else []
        sys.stderr.write(json.dumps({"name": g["name"], "cognee_targets": t}) + "\n")
nxt = (d.get("pagination") or {}).get("next")
if isinstance(nxt, int) and not isinstance(nxt, bool):
    print(os.environ["AK_FIRST"] + "&page=%d" % nxt if nxt > 0 else "")
else:
    print(nxt or "")
' 2>>"$out")" || return 1
    done
    [ -z "$url" ] || return 1   # cut off at the page guard: incomplete
    python3 -c 'import json,sys
print(json.dumps({d["name"]: {"cognee_targets": d["cognee_targets"]} for d in map(json.loads, open(sys.argv[1]))}))' "$out"
}

# The datasets (id, name, owner as `creator`, current readers as `members`,
# Dify: permission + knowledge tags) and e-mail -> account id, as one JSON.
# Accounts come as [e-mail, id] pairs in a fixed order; an e-mail two accounts
# share maps to neither (the planner reports it).
# Dify 1.17.1: the owner is `maintainer` (backfilled from created_by);
# cognee 1.5.3: `owner_id`, readers are the `read` rows in `acls`.
rr_dataset_sql() {
    case "$1" in
        dify) echo "SELECT json_build_object('datasets', coalesce((SELECT json_agg(json_build_object('id', d.id, 'name', d.name, 'permission', d.permission, 'creator', coalesce(d.maintainer, d.created_by), 'tags', coalesce((SELECT json_agg(t.name ORDER BY t.name) FROM tag_bindings b JOIN tags t ON t.id = b.tag_id WHERE b.target_id = d.id AND t.type = 'knowledge'), '[]'::json), 'members', coalesce((SELECT json_agg(p.account_id ORDER BY p.account_id) FROM dataset_permissions p WHERE p.dataset_id = d.id AND p.has_permission), '[]'::json)) ORDER BY d.id) FROM datasets d), '[]'::json), 'accounts', coalesce((SELECT json_agg(json_build_array(lower(a.email), a.id) ORDER BY lower(a.email), a.id) FROM accounts a), '[]'::json));" ;;
        cognee) echo "SELECT json_build_object('datasets', coalesce((SELECT json_agg(json_build_object('id', d.id, 'name', d.name, 'creator', d.owner_id, 'members', coalesce((SELECT json_agg(a.principal_id ORDER BY a.principal_id) FROM acls a JOIN permissions p ON p.id = a.permission_id WHERE a.dataset_id = d.id AND p.name = 'read'), '[]'::json)) ORDER BY d.id) FROM datasets d), '[]'::json), 'accounts', coalesce((SELECT json_agg(json_build_array(lower(u.email), u.id) ORDER BY lower(u.email), u.id) FROM users u), '[]'::json));" ;;
        *) return 2 ;;
    esac
}

# #2473 D6: a scoped dataset is readable by exactly the planned members (the
# scope's members + its owner): `restrict`. A dataset this reconcile restricted
# earlier that is in no scope any more goes back to owner-only: `unscope`
# (Dify: only_me, its member rows deleted; cognee: the read ACLs replaced by the
# owner's). One transaction per dataset, ending in a count of the dataset: 0
# (it vanished since the plan) is a failure. With RR_DS_APPLIED set, each
# outcome is appended there as `op TAB dataset TAB ok|failed`.
rr_apply_dataset_acl() {
    local system=$1 json=$2 op ds members db sql rc=0 out
    local restrict_sql unscope_sql
    case "$system" in
        dify) db=$DIFY_DB
              restrict_sql="BEGIN;
UPDATE datasets SET permission = 'partial_members' WHERE id = :'ds';
DELETE FROM dataset_permissions WHERE dataset_id = :'ds';
INSERT INTO dataset_permissions (id, dataset_id, account_id, tenant_id, has_permission)
  SELECT gen_random_uuid(), d.id, m::uuid, d.tenant_id, true
  FROM datasets d, unnest(string_to_array(:'members', ',')) AS m WHERE d.id = :'ds';
SELECT count(*) FROM datasets WHERE id = :'ds';
COMMIT;"
              unscope_sql="BEGIN;
UPDATE datasets SET permission = 'only_me' WHERE id = :'ds';
DELETE FROM dataset_permissions WHERE dataset_id = :'ds';
SELECT count(*) FROM datasets WHERE id = :'ds';
COMMIT;" ;;
        cognee) db=$COGNEE_DB
              restrict_sql="BEGIN;
DELETE FROM acls WHERE dataset_id = :'ds' AND permission_id IN (SELECT id FROM permissions WHERE name = 'read');
INSERT INTO acls (id, principal_id, permission_id, dataset_id)
  SELECT gen_random_uuid(), m::uuid, p.id, d.id
  FROM datasets d, permissions p, unnest(string_to_array(:'members', ',')) AS m WHERE d.id = :'ds' AND p.name = 'read';
SELECT count(*) FROM datasets d WHERE d.id = :'ds' AND EXISTS (SELECT 1 FROM permissions WHERE name = 'read');
COMMIT;"
              unscope_sql="BEGIN;
DELETE FROM acls WHERE dataset_id = :'ds' AND permission_id IN (SELECT id FROM permissions WHERE name = 'read');
INSERT INTO acls (id, principal_id, permission_id, dataset_id)
  SELECT gen_random_uuid(), d.owner_id, p.id, d.id
  FROM datasets d, permissions p WHERE d.id = :'ds' AND p.name = 'read' AND d.owner_id IS NOT NULL;
SELECT count(*) FROM datasets d WHERE d.id = :'ds' AND d.owner_id IS NOT NULL AND EXISTS (SELECT 1 FROM permissions WHERE name = 'read');
COMMIT;" ;;
        *) print_error "unknown system: $system"; return 2 ;;
    esac
    while IFS=$'\t' read -r op ds members; do
        [ -n "$ds" ] || continue
        case "$op" in restrict) sql=$restrict_sql ;; unscope) sql=$unscope_sql ;; *) continue ;; esac
        if out="$(rr_psql "$db" "$sql" "ds=$ds" "members=$members" 2>&1)" && printf '%s\n' "$out" | grep -qx '1'; then
            if [ "$op" = restrict ]; then
                print_substep "${system}: dataset ${ds} readable by its scope's members and its owner only (#2473)"
            else
                print_substep "${system}: dataset ${ds} is in no knowledge scope any more — owner-only again (#2473)"
            fi
            [ -z "${RR_DS_APPLIED:-}" ] || printf '%s\t%s\tok\n' "$op" "$ds" >> "$RR_DS_APPLIED"
        else
            print_error "${system}: ${op} of dataset ${ds} FAILED (#2473)"; rc=1
            [ -z "${RR_DS_APPLIED:-}" ] || printf '%s\t%s\tfailed\n' "$op" "$ds" >> "$RR_DS_APPLIED"
        fi
    done < <(printf '%s' "$json" | python3 -c 'import json,sys
for a in json.load(sys.stdin):
    if a["op"] in ("restrict", "unscope"):
        print(a["op"] + "\t" + a["dataset"] + "\t" + ",".join(a["members"]))')
    return "$rc"
}

# One portal audit entry per dataset change applied (or failed) above, from
# the plan's actions and the apply's outcome lines (`op TAB dataset TAB
# ok|failed`). The entry counts the readers, it does not list them.
rr_audit_datasets() {
    local system=$1 actions=$2 applied=$3
    [ -s "$applied" ] || return 0
    printf '%s' "$actions" | RR_ACTOR="$RR_ACTOR" python3 -c 'import json,os,sys
acts = {a["dataset"]: a for a in json.load(sys.stdin)}
out = []
for line in open(sys.argv[2], encoding="utf-8"):
    op, ds, res = line.rstrip("\n").split("\t")
    out.append({"op": op, "dataset": ds, "readers": len((acts.get(ds) or {}).get("members") or []), "outcome": res})
print(json.dumps({"system": sys.argv[1], "actor": os.environ.get("RR_ACTOR") or None, "applied": out}))' "$system" "$applied" \
        | rr_planner audit-datasets >/dev/null 2>&1 \
        || print_warning "knowledge scopes: ${system}: the changes were applied but NOT written to the portal audit log"
}

# #2473 D6, once per box: every untagged team-wide Dify dataset becomes
# owner-only. Marker DIFY_KNOWLEDGE_SCOPES_MIGRATED (the UTC date); a dataset
# shared with the team later is never touched again. A failure writes no
# marker and is retried on the next reconcile.
dify_migrate_knowledge_scopes() {
    local tagged=$1 marker
    marker=$(read_env_value "$ENV_FILE" DIFY_KNOWLEDGE_SCOPES_MIGRATED 2>/dev/null || true)
    [ -z "$marker" ] || return 0
    rr_psql "$DIFY_DB" "UPDATE datasets SET permission = 'only_me' WHERE permission = 'all_team_members' AND NOT (id::text = ANY(string_to_array(:'tagged', ',')));" \
        "tagged=$tagged" >/dev/null \
        || { print_error "Dify: team-wide datasets NOT made owner-only — retried on the next reconcile (#2473)"; return 1; }
    update_env_value "$ENV_FILE" DIFY_KNOWLEDGE_SCOPES_MIGRATED "$(date -u +%Y-%m-%d)" \
        || { print_error "Dify: datasets made owner-only, but DIFY_KNOWLEDGE_SCOPES_MIGRATED could not be written to ${ENV_FILE} (#2473)"; return 1; }
    print_substep "Dify: untagged team-wide datasets are owner-only now (once, #2473)"
}

# The dataset half of --reconcile-roles, for the apps whose role reconcile
# could run (their container and database answered). Runs with no scope too:
# a dataset restricted earlier then goes back to owner-only. Returns 1 on a
# failure; a system whose datasets could not be read changes nothing and is
# named as unreadable in the report.
rr_reconcile_datasets() {
    local work=$1; shift
    local -a systems=() unreadable=()
    local app d="$work/kb" rc=0 g i=0 ge plan sys tagged line
    for app in "$@"; do case "$app" in dify|cognee) systems+=("$app") ;; esac; done
    [ "${#systems[@]}" -gt 0 ] || return 0
    mkdir -p "$d"
    if ! authentik_kb_scopes > "$d/scopes.json"; then
        print_warning "knowledge scopes: Authentik groups not readable — no dataset changed"
        rr_summary SCOPEUNCHECKED all "Authentik groups not readable"
        return 1
    fi
    while IFS= read -r g; do
        [ -n "$g" ] || continue
        i=$((i + 1)); ge=0
        rr_group_rows "$work" "$g" || ge=$?
        if [ "$ge" -ne 0 ]; then
            print_warning "knowledge scopes: members of '${g}' not readable — no dataset changed"
            rr_summary SCOPEUNCHECKED all "the members of a knowledge scope are not readable"
            return 1
        fi
        printf '%s' "$g" > "$d/g$i.name"
        cut -f2 "$work/cache/$(printf '%s' "$g" | cksum | cut -d' ' -f1)" > "$d/g$i.emails"
    done < <(python3 -c 'import json,sys
for g in json.load(open(sys.argv[1])):
    print(g)' "$d/scopes.json")
    for sys in "${systems[@]}"; do
        if ! rr_psql "$(rr_app_db "$sys")" "$(rr_dataset_sql "$sys")" > "$d/$sys.json" 2>"$work/err" \
           || ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d["datasets"], list)' "$d/$sys.json" 2>/dev/null; then
            print_warning "knowledge scopes: ${sys} datasets not readable — no ${sys} dataset changed"
            rr_summary SCOPEUNCHECKED "$sys" "datasets not readable"
            rm -f "$d/$sys.json"; unreadable+=("$sys"); rc=1
        fi
    done
    plan="$(python3 -c 'import glob,json,os,sys
d = sys.argv[1]
targets = json.load(open(os.path.join(d, "scopes.json")))
scopes = {}
for f in sorted(glob.glob(os.path.join(d, "g*.name"))):
    name = open(f, encoding="utf-8").read()
    scopes[name] = {"members": [l.strip() for l in open(f[:-5] + ".emails", encoding="utf-8") if l.strip()],
                    "cognee_targets": targets.get(name, {}).get("cognee_targets", [])}
systems, unreadable = {}, []
for s in sys.argv[2:]:
    p = os.path.join(d, s + ".json")
    if os.path.exists(p):
        systems[s] = json.load(open(p, encoding="utf-8"))
    else:
        unreadable.append(s)
print(json.dumps({"scopes": scopes, "systems": systems, "unreadable": unreadable}))' "$d" "${systems[@]}" | tee "$d/request.json" | rr_planner datasets 2>"$work/err")" \
        || { print_warning "knowledge scopes: the planner failed ($(head -c 300 "$work/err")) — no dataset changed"
             rr_summary SCOPEUNCHECKED all "planner failed"; return 1; }
    printf '%s' "$plan" > "$d/plan.json"
    rr_summary SCOPES all "$i"
    while IFS=$'\t' read -r kind line; do
        [ -n "$kind" ] || continue
        print_warning "knowledge scopes: ${line}"
        [ "$kind" = error ] && rc=1
    done < <(python3 -c 'import json,sys
p = json.load(open(sys.argv[1]))
for scope, names in sorted(p["ambiguous"].items()):
    for n in names:
        print("warn\t%s: cognee dataset name %r belongs to several datasets — not linked; name a unique dataset" % (scope, n))
for s, errs in sorted(p["errors"].items()):
    for e in errs:
        print("error\t%s: %s" % (s, e))
for s, dups in sorted(p["duplicates"].items()):
    for e in dups:
        print("warn\t%s: e-mail %s belongs to several accounts — it gets no dataset through a scope" % (s, e))' "$d/plan.json")
    for sys in "${systems[@]}"; do
        [ -f "$d/$sys.json" ] || continue
        local actions n
        actions="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["actions"].get(sys.argv[2], [])))' "$d/plan.json" "$sys")"
        n="$(printf '%s' "$actions" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
        if [ "$SUMMARY" = true ]; then
            # n planned changes, how many of them close a leak (someone outside
            # the scope reads the dataset now), the scoped datasets, the errors.
            local -a sc
            read -r -a sc < <(python3 -c 'import json,sys
p = json.load(open(sys.argv[1])); s = sys.argv[2]
print(len(p["actions"].get(s, [])), len(p.get("widened", {}).get(s, [])), len(p["scoped"].get(s, [])),
      len(p["errors"].get(s, [])))' "$d/plan.json" "$sys")
            rr_summary SCOPEDRIFT "$sys" "${sc[0]:-x}" "${sc[1]:-x}" "${sc[2]:-x}"
            [ "${sc[3]:-0}" = 0 ] || rr_summary SCOPEERR "$sys" "${sc[3]}"
        fi
        if [ "$DRY" = true ]; then
            printf '%s' "$actions" | python3 -c 'import json,sys
for a in json.load(sys.stdin):
    if a["op"] == "restrict":
        print("  [dry-run] %s dataset %s -> readable by %d account(s)" % (sys.argv[1], a["dataset"], len(a["members"])))
    else:
        print("  [dry-run] %s dataset %s -> in no scope any more, owner-only" % (sys.argv[1], a["dataset"]))' "$sys"
            continue
        fi
        if [ "$sys" = dify ]; then
            tagged="$(python3 -c 'import json,sys; print(",".join(json.load(open(sys.argv[1]))["tagged"].get("dify", [])))' "$d/plan.json")"
            dify_migrate_knowledge_scopes "$tagged" || rc=1
        fi
        : > "$d/$sys.applied"
        if [ "$n" -gt 0 ]; then
            RR_DS_APPLIED="$d/$sys.applied" rr_apply_dataset_acl "$sys" "$actions" || rc=1
            rr_audit_datasets "$sys" "$actions" "$d/$sys.applied"
        else
            print_success "knowledge scopes: ${sys} datasets match their scopes"
        fi
        # The ledger of what this reconcile restricted, AFTER the apply: only
        # a successful restrict is recorded, only a successful unscope removed.
        python3 -c 'import json,sys
p = json.load(open(sys.argv[1])); s = sys.argv[3]
ok = {"restrict": [], "unscope": []}
for line in open(sys.argv[2], encoding="utf-8"):
    op, ds, res = line.rstrip("\n").split("\t")
    if res == "ok":
        ok[op].append(ds)
print(json.dumps({"system": s, "present": p["present"][s], "scoped": p["scoped"][s],
                  "planned_restrict": [a["dataset"] for a in p["actions"][s] if a["op"] == "restrict"],
                  "restrict_ok": ok["restrict"], "unscope_ok": ok["unscope"]}))' "$d/plan.json" "$d/$sys.applied" "$sys" \
            | rr_planner scoped-update >/dev/null 2>"$work/err" \
            || { print_error "knowledge scopes: ${sys}: the ledger of restricted datasets was NOT updated ($(head -c 300 "$work/err"))"; rc=1; }
    done
    if [ "$DRY" = false ]; then
        rr_write_knowledge_report "$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["report"]))' "$d/plan.json")" || true
    fi
    return "$rc"
}

# Settings -> Knowledge access reads what the last real reconcile wrote (an
# empty scope list writes an empty report, so a deleted last scope does not
# linger on the page).
rr_write_knowledge_report() {
    printf '%s' "$1" | rr_planner knowledge-report >/dev/null 2>&1 \
        || { print_warning "knowledge scopes: the Knowledge access report was NOT written"; return 1; }
}

usage() { sed -n '/^# ── Usage/,/^# Only apps whose container/p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# ── arg parse ─────────────────────────────────────────────────────────────────
APP="" GROUP="" PASSWORD="" DRY=false LISTONLY=false RECONCILE=false SUMMARY=false
while [ $# -gt 0 ]; do
    case "$1" in
        --app)      APP="${2:?--app needs a name}"; shift 2 ;;
        --group)    GROUP="${2:?--group needs a value}"; shift 2 ;;
        --password) PASSWORD="${2:?--password needs a value}"; shift 2 ;;
        --dry-run)  DRY=true; shift ;;
        --list)     LISTONLY=true; shift ;;
        --reconcile-roles) RECONCILE=true; shift ;;
        --summary)  SUMMARY=true; shift ;;
        -h|--help)  usage 0 ;;
        *) print_error "Unknown option: $1"; usage 1 ;;
    esac
done

[ -n "$APP" ] || { print_error "--app is required (dify | cognee | all)."; usage 1; }

# #2473 D5: the role reconcile. Its own app set (Synapse too); the create-path
# options do not apply to it.
if [ "$RECONCILE" = true ]; then
    [ -z "$GROUP" ] && [ -z "$PASSWORD" ] && [ "$LISTONLY" = false ] \
        || { print_error "--reconcile-roles takes only --app, --dry-run and --summary."; exit 1; }
    case "$APP" in
        dify|cognee|synapse) RR_APPS=("$APP") ;;
        all) RR_APPS=(dify cognee synapse) ;;
        *) print_error "Unknown app: $APP (dify|cognee|synapse|all)."; exit 1 ;;
    esac
    rc=0
    reconcile_roles "${RR_APPS[@]}" || rc=$?
    if [ "$DRY" = true ]; then print_info "Done (dry run, no changes made)."
    elif [ "$rc" -eq 0 ]; then print_success "Role reconcile complete."
    else print_error "Role reconcile finished with errors or unchecked apps (see above)."; fi
    exit "$rc"
fi
[ "$SUMMARY" = false ] || { print_error "--summary needs --reconcile-roles."; exit 1; }
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
