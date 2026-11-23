# shellcheck shell=bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# cli/lib-openbao.sh — OpenBao identity reconcile (#2476; sourced, no top-level set)
# =============================================================================
# openbao_reconcile_identity: Authentik OIDC for people (keyed by the OIDC
# sub), the role catalogue's openbao roles as policies, team paths, and the
# svc-mcp-manager and svc-agent-manager AppRoles. The script is rendered inside razzfazz-config
# (Python, the role catalogue mounted; scripts/render-openbao-identity.py) and
# run inside the openbao container, logged in with the rzfz-config AppRole.
#
# The rendered script is the `sh -c` ARGUMENT (it holds no secret); the OIDC
# client secret is the exec's only stdin. Never `sh -s` with the secret as a
# stdin line: the shell would parse it as a command.
#
# Needs scripts/lib.sh (read_env_value, print_warning). Returns non-zero with
# a warning when anything is missing; the caller keeps going.
_openbao_plain() {
    printf '%s' "$1" | grep -Eqx '[A-Za-z0-9._-]+'
}

openbao_reconcile_identity() {
    local env_file="${ENV_FILE:-.env}" domain adomain cid secret teams req script
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openbao; then
        print_warning "openbao: the openbao container is not running; identity not reconciled"
        return 1
    fi
    domain=$(read_env_value "$env_file" OPENBAO_DOMAIN)
    adomain=$(read_env_value "$env_file" AUTHENTIK_DOMAIN)
    cid=$(read_env_value "$env_file" OPENBAO_OIDC_CLIENT_ID)
    secret=$(read_env_value "$env_file" OPENBAO_OIDC_CLIENT_SECRET)
    if [ -z "$cid" ] || [ -z "$secret" ]; then
        print_warning "openbao: OPENBAO_OIDC_CLIENT_ID or OPENBAO_OIDC_CLIENT_SECRET is empty; identity not reconciled"
        return 1
    fi
    # Values that go into the JSON request unescaped: two domains and an id.
    if ! _openbao_plain "$domain" || ! _openbao_plain "$adomain" || ! _openbao_plain "$cid"; then
        print_warning "openbao: OPENBAO_DOMAIN, AUTHENTIK_DOMAIN or OPENBAO_OIDC_CLIENT_ID is not a plain name; identity not reconciled"
        return 1
    fi
    # The team groups: every Authentik group with attributes.rzfz_bao_team true.
    # No answer stops the reconcile: rendered without its teams, the script
    # would remove every team's group and policy.
    teams=$(docker exec authentik-worker ak shell -c 'import json,sys
from authentik.core.models import Group
sys.stdout.write("RZFZ-TEAMS\t"+json.dumps(sorted(g.name for g in Group.objects.filter(attributes__rzfz_bao_team=True)))+"\n")' 2>/dev/null \
        | sed -n 's/^RZFZ-TEAMS\t//p' | head -n 1) || teams=""
    if [ -z "$teams" ]; then
        print_warning "openbao: the team groups could not be read from Authentik; team paths not reconciled"
        return 1
    fi
    req=$(printf '{"team_groups": %s, "oidc": {"discovery_url": "https://%s/application/o/openbao-oidc/", "client_id": "%s", "redirect_ui": "https://%s/ui/vault/auth/oidc/oidc/callback"}}' \
        "$teams" "$adomain" "$cid" "$domain")
    # The renderer is in the repo, which razzfazz-config mounts read-only at
    # its STACK_ROOT (the host path).
    script=$(printf '%s' "$req" | docker exec -i razzfazz-config sh -c 'exec python "$STACK_ROOT/scripts/render-openbao-identity.py"') || {
        print_warning "openbao: rendering the identity reconcile failed (docker logs razzfazz-config)"
        return 1
    }
    [ -n "$script" ] || { print_warning "openbao: the identity reconcile rendered empty"; return 1; }
    if ! printf '%s\n' "$secret" | docker exec -i openbao sh -c "$script"; then
        print_warning "openbao: the identity reconcile failed (docker logs openbao; is https://${adomain}/application/o/openbao-oidc/ reachable?)"
        return 1
    fi
    print_info "openbao: OIDC sign-in, role and team policies, and the svc-mcp-manager and svc-agent-manager AppRoles reconciled"
    return 0
}

# openbao_show_recovery_key_once: A18's "shown once at install", on an
# interactive terminal only, and only while it has not been shown.
openbao_show_recovery_key_once() {
    [ -t 0 ] || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx openbao || return 0
    if docker exec openbao test -e /openbao/keys/recovery.shown >/dev/null 2>&1; then
        return 0
    fi
    "${SCRIPT_DIR:-.}/cli/openbao.sh" show-recovery-key
}
