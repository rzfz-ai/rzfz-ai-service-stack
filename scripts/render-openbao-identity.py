#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 — render the OpenBao identity reconcile as a shell script (no secrets in it).

cli/lib-openbao.sh::openbao_reconcile_identity runs it inside the `openbao`
container as `sh -c "<script>"`, logged in with the rzfz-config AppRole from
the read-only /openbao/cred mount (the host's management role; the
Configuration Portal never holds it). The OIDC client secret is the exec's
only stdin: `read -r OIDC_SECRET` takes it, and bao reads it back from stdin
(`oidc_client_secret=-`). Per-user paths are keyed by the OIDC sub (the alias
name: user_claim=sub, the provider's sub_mode hashed_user_id; decision record
D8/D9). Technical users live under secret/apps/<svc>/ only.

What it reconciles, idempotently: the `oidc/` auth mount and its `default`
role; the policies rzfz-admin, rzfz-user (templated on the alias name),
svc-mcp-manager and team-<slug>; one external group per Authentik group that
a catalogue role or the team flag gives an OpenBao policy, its policies
rewritten each run (a group that lost them is removed); the svc-mcp-manager
AppRole, whose credentials are minted
once into /openbao/mcp-cred and kept; the token role rzfz-agent and the
svc-agent-manager AppRole (A21), whose credentials go into
/openbao/agent-cred once and again when they no longer log in.

Usage (inside razzfazz-config, the catalogue mounted): a JSON request on
stdin, {"team_groups": [...], "oidc": {"discovery_url", "client_id",
"redirect_ui"}}; the script on stdout.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

#: The external identity groups this script owns (it removes the ones it no
#: longer renders): one per Authentik group that a catalogue role or the
#: team flag gives an OpenBao policy.
GROUP_PREFIX = "rzfz-grp-"
#: Group names of an earlier layout, removed when found.
LEGACY_PREFIXES = "rzfz-role-*|team-*"
#: The team policies (team-<slug>, secret/teams/<slug>/).
TEAM_PREFIX = "team-"
#: A21: where the agent manager's AppRole may log in from: openbao-net's
#: DYNAMIC range (core/compose.yml ip_range), which excludes Caddy's fixed
#: address (CADDY_STACK_IPV4, the technical users' /32). Guarded equal.
AGENT_MANAGER_CIDR = "10.213.76.128/25"
#: #2472 B22: the MCP manager joins the same network, so the same range.
MCP_MANAGER_CIDR = AGENT_MANAGER_CIDR


def group_name(group: str) -> str:
    import hashlib
    return f"{GROUP_PREFIX}{_slug(group) or 'group'}-{hashlib.sha256(group.encode()).hexdigest()[:8]}"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.split(":", 1)[-1].strip().lower()).strip("-")


def _q(s: str) -> str:
    """A double-quoted shell word. Every character the shell treats specially
    inside double quotes is escaped; a control character is refused (a newline
    would end the command)."""
    if not isinstance(s, str) or re.search(r"[\x00-\x1f\x7f]", s):
        raise ValueError("refusing a value with a control character")
    return '"' + re.sub(r'([\\"$`])', r"\\\1", s) + '"'


#: The Portal's policy, one source with the bootstrap (openbao-init.sh).
PORTAL_POLICY = Path(__file__).resolve().parents[1] / "modules" / "apps" / "openbao" / "config" / "policies" / \
    "rzfz-portal.hcl"
#: The management role's policy, one source with the bootstrap too.
CONFIG_POLICY = PORTAL_POLICY.with_name("rzfz-config.hcl")
#: The stack's AppRoles the refresh re-asserts (the bootstrap writes the same,
#: test_2476_openbao_identity_render.py holds the two equal).
STACK_ROLE_WRITES = (
    "bao write auth/approle/role/rzfz-portal token_policies=rzfz-portal token_ttl=15m token_max_ttl=1h >/dev/null",
    "bao write auth/approle/role/rzfz-breakglass token_policies=rzfz-config secret_id_ttl=10m "
    "secret_id_num_uses=1 token_ttl=15m token_max_ttl=1h >/dev/null",
)


def _hcl_body(text: str) -> str:
    body = "\n".join(l for l in text.splitlines() if not l.startswith("#")).strip("\n")
    if "\nHCL\n" in f"\n{body}\n":
        raise ValueError("a policy must not contain a line 'HCL'")
    return body


def _stack_policies(portal_policy: str, config_policy: str) -> list:
    """M-c: what the run-once bootstrap sets up for the Portal and the
    break-glass, re-asserted idempotently on every refresh, so a store set up
    before (or a later tightening of rzfz-portal.hcl) converges. The Portal's
    CREDENTIAL stays the bootstrap's (/openbao/cred is read-only here)."""
    body = _hcl_body(portal_policy)
    return ["bao policy write rzfz-config - >/dev/null <<'HCL'", _hcl_body(config_policy), "HCL",
            "bao auth enable -path=approle-apps approle >/dev/null 2>&1 || bao auth list | grep -q '^approle-apps/'",
            "APPS_ACC=$(bao auth list | awk '$1==\"approle-apps/\"{print $3}')",
            '[ -n "$APPS_ACC" ] || { echo "openbao: the approle-apps mount has no accessor" >&2; exit 1; }',
            # #2472 B22: a technical user's own AppRole never reads the raw
            # upstream OAuth token the MCP manager keeps under oauth/ (that
            # token is used through the gateway only)
            'printf \'%s\\n%s\\n\' "path \\"secret/data/apps/{{identity.entity.aliases.$APPS_ACC.metadata.role_name}}/*\\" '
            '{ capabilities = [\\"read\\"] }" '
            '"path \\"secret/data/apps/{{identity.entity.aliases.$APPS_ACC.metadata.role_name}}/oauth/*\\" '
            '{ capabilities = [\\"deny\\"] }" | bao policy write rzfz-app - >/dev/null',
            "bao policy write rzfz-portal - >/dev/null <<'HCL'", body, "HCL",
            *STACK_ROLE_WRITES]


def render(cat, team_groups, oidc, portal_policy: str | None = None, config_policy: str | None = None) -> str:
    if portal_policy is None:
        portal_policy = PORTAL_POLICY.read_text(encoding="utf-8")
    if config_policy is None:
        config_policy = CONFIG_POLICY.read_text(encoding="utf-8")
    L = ["set -eu", "read -r OIDC_SECRET",
         'export BAO_ADDR="${BAO_ADDR:-http://127.0.0.1:8200}"',
         'CRED="${OPENBAO_CRED_DIR:-/openbao/cred}"',
         'BAO_TOKEN=$(bao write -field=token auth/approle/login role_id=@"$CRED/config.role_id" '
         'secret_id=@"$CRED/config.secret_id"); export BAO_TOKEN',
         # the token goes whatever happens below (set -e stops at the first failure)
         "trap 'bao token revoke -self >/dev/null 2>&1 || true' EXIT",
         *_stack_policies(portal_policy, config_policy),
         "bao auth enable oidc 2>/dev/null || true",
         # no oidc_discovery_ca_pem: OpenBao verifies auth.<domain> against its
         # system roots, which compose points at the stack's CA superset
         # (SSL_CERT_FILE), so a CA regeneration is followed by a restart
         f'printf \'%s\' "$OIDC_SECRET" | bao write auth/oidc/config oidc_discovery_url={_q(oidc["discovery_url"])} '
         f'oidc_client_id={_q(oidc["client_id"])} oidc_client_secret=- default_role=default >/dev/null',
         "unset OIDC_SECRET",
         # D9/A17 owner rule: the Authentik pk rides in the alias metadata
         # (ak_pk), so the purge confirms a deletion by a direct lookup.
         # The UI callback only: the CLI is used on the box (rzfz openbao
         # admin-shell, docker exec), never through bao.<domain>.
         f'bao write auth/oidc/role/default role_type=oidc user_claim=sub groups_claim=groups '
         f'claim_mappings="rzfz_pk=ak_pk" oidc_scopes="openid,email,profile,groups,rzfz_pk" '
         f'token_ttl=1h token_max_ttl=8h allowed_redirect_uris={_q(oidc["redirect_ui"])} >/dev/null',
         "OIDC_ACC=$(bao auth list | awk '$1==\"oidc/\"{print $3}')",
         "TOKEN_ACC=$(bao auth list | awk '$1==\"token/\"{print $3}')",
         '[ -n "$OIDC_ACC" ] && [ -n "$TOKEN_ACC" ] || { echo "openbao: auth mount accessors not found" >&2; exit 1; }',
         "bao policy write rzfz-admin - >/dev/null <<'HCL'",
         'path "secret/*" { capabilities = ["create","read","update","delete","list"] }',
         'path "sys/policies/acl" { capabilities = ["list"] }',
         "HCL",
         # D8/D9: keyed by the alias NAME, which is the immutable OIDC sub on
         # both mounts (A21's token-role tokens carry the sub as their alias).
         "bao policy write rzfz-user - >/dev/null <<HCL",
         'path "secret/data/users/{{identity.entity.aliases.${OIDC_ACC}.name}}/*" { capabilities = ["create","read","update","delete"] }',
         'path "secret/metadata/users/{{identity.entity.aliases.${OIDC_ACC}.name}}/*" { capabilities = ["list","read","delete"] }',
         'path "secret/data/users/{{identity.entity.aliases.${TOKEN_ACC}.name}}/*" { capabilities = ["read","list"] }',
         # A21: an agent may list its owner's keys (kv list), never read metadata
         'path "secret/metadata/users/{{identity.entity.aliases.${TOKEN_ACC}.name}}/*" { capabilities = ["list"] }',
         # #2472 B22: the owner's OAuth grants (MCP manager's store) are never
         # an agent's: the more specific deny wins over the reads above. The
         # person (OIDC alias) keeps them.
         'path "secret/data/users/{{identity.entity.aliases.${TOKEN_ACC}.name}}/oauth/*" { capabilities = ["deny"] }',
         'path "secret/metadata/users/{{identity.entity.aliases.${TOKEN_ACC}.name}}/oauth/*" { capabilities = ["deny"] }',
         "HCL",
         # D8: the MCP manager's AppRole — OAuth tokens only: a person's under
         # users/<sub>/oauth/, a technical user's under apps/<svc>/oauth/;
         # credentials minted once.
         "bao policy write svc-mcp-manager - >/dev/null <<'HCL'",
         'path "secret/data/users/+/oauth/*" { capabilities = ["create","read","update","delete"] }',
         'path "secret/metadata/users/+/oauth/*" { capabilities = ["list","read","delete"] }',
         'path "secret/data/apps/+/oauth/*" { capabilities = ["create","read","update","delete"] }',
         'path "secret/metadata/apps/+/oauth/*" { capabilities = ["list","read","delete"] }',
         "HCL",
         # bound to openbao-net's dynamic range (where mcp-manager's address
         # comes from; never Caddy's fixed one), re-bound on every refresh
         "bao write auth/approle/role/svc-mcp-manager token_policies=svc-mcp-manager token_ttl=15m token_max_ttl=1h "
         f"token_bound_cidrs={MCP_MANAGER_CIDR} secret_id_bound_cidrs={MCP_MANAGER_CIDR} >/dev/null",
         'MCP_CRED="${OPENBAO_MCP_CRED_DIR:-/openbao/mcp-cred}"',
         'if [ ! -s "$MCP_CRED/secret_id" ]; then',
         "  umask 077",
         '  rm -f "$MCP_CRED/role_id.tmp" "$MCP_CRED/secret_id.tmp"',
         '  bao read -field=role_id auth/approle/role/svc-mcp-manager/role-id > "$MCP_CRED/role_id.tmp"',
         '  bao write -f -field=secret_id auth/approle/role/svc-mcp-manager/secret-id > "$MCP_CRED/secret_id.tmp"',
         '  [ -s "$MCP_CRED/role_id.tmp" ] && [ -s "$MCP_CRED/secret_id.tmp" ]',
         '  chmod 0400 "$MCP_CRED/role_id.tmp" "$MCP_CRED/secret_id.tmp"',
         '  mv -f "$MCP_CRED/role_id.tmp" "$MCP_CRED/role_id"',
         '  mv -f "$MCP_CRED/secret_id.tmp" "$MCP_CRED/secret_id"',
         "fi",
         # A21: the agent manager mints each agent its OWNER's token (alias =
         # the owner's OIDC sub, so rzfz-user's templated read path applies).
         # Its role can create rzfz-agent tokens and revoke a token by
         # accessor; OpenBao cannot narrow revoke-accessor to one role, so it
         # could revoke ANY token whose accessor it knows (the manager only
         # revokes the accessors it recorded). Logins are accepted only from
         # openbao-net's dynamic range, which never holds Caddy's fixed
         # address. Credentials are minted once, and again when the stored
         # pair is no longer this role's (a restored store).
         'bao write auth/token/roles/rzfz-agent allowed_policies=rzfz-user allowed_entity_aliases="*" '
         'orphan=true token_period=4h >/dev/null',
         "bao policy write svc-agent-manager - >/dev/null <<'HCL'",
         'path "auth/token/create/rzfz-agent" { capabilities = ["update"] }',
         'path "auth/token/revoke-accessor" { capabilities = ["update"] }',
         "HCL",
         "bao write auth/approle/role/svc-agent-manager token_policies=svc-agent-manager token_ttl=15m "
         f"token_max_ttl=1h token_bound_cidrs={AGENT_MANAGER_CIDR} secret_id_bound_cidrs={AGENT_MANAGER_CIDR} >/dev/null",
         'AGENT_CRED="${OPENBAO_AGENT_CRED_DIR:-/openbao/agent-cred}"',
         'AGENT_OK=',
         # checked by lookup, not by a login: this script runs inside the
         # openbao container, outside the role's bound range
         'if [ -s "$AGENT_CRED/role_id" ] && [ -s "$AGENT_CRED/secret_id" ] && '
         '[ "$(bao read -field=role_id auth/approle/role/svc-agent-manager/role-id)" = "$(cat "$AGENT_CRED/role_id")" ] && '
         # an unknown secret_id answers exit 0 and a "Success!" line, no data
         '_A=$(bao write -field=secret_id_accessor auth/approle/role/svc-agent-manager/secret-id/lookup '
         'secret_id=@"$AGENT_CRED/secret_id" 2>/dev/null) && case "$_A" in ""|*[!0-9a-f-]*) false ;; esac; then',
         "  AGENT_OK=1",
         "fi",
         "unset _A",
         'if [ -z "$AGENT_OK" ]; then',
         "  umask 077",
         '  rm -f "$AGENT_CRED/role_id.tmp" "$AGENT_CRED/secret_id.tmp"',
         '  bao read -field=role_id auth/approle/role/svc-agent-manager/role-id > "$AGENT_CRED/role_id.tmp"',
         '  bao write -f -field=secret_id auth/approle/role/svc-agent-manager/secret-id > "$AGENT_CRED/secret_id.tmp"',
         '  [ -s "$AGENT_CRED/role_id.tmp" ] && [ -s "$AGENT_CRED/secret_id.tmp" ]',
         '  chmod 0400 "$AGENT_CRED/role_id.tmp" "$AGENT_CRED/secret_id.tmp"',
         '  mv -f "$AGENT_CRED/role_id.tmp" "$AGENT_CRED/role_id"',
         '  mv -f "$AGENT_CRED/secret_id.tmp" "$AGENT_CRED/secret_id"',
         "fi"]
    # One external group per AUTHENTIK group (OpenBao 2.6 holds one alias per
    # external group, and an alias is never moved): its policies are the
    # union of what the catalogue's openbao roles and the team flag give that
    # group, rewritten on every run, so a group moved from admin to user
    # loses rzfz-admin on the next refresh. Named by the group's slug plus a
    # digest of its exact name (unique, stable).
    policies: dict[str, set] = {}
    role_policy = {"admin": "rzfz-admin", "user": "rzfz-user"}
    for perm, policy in role_policy.items():
        for role in cat.components.get("openbao", {}).get(perm, ()):
            for group in cat.role_groups(role):
                policies.setdefault(group, set()).add(policy)
    slugs: dict[str, str] = {}
    for team in sorted(team_groups):
        s = _slug(team)
        if not s:
            print(f"openbao: team group {team!r} has no usable name; no team path", file=sys.stderr)
            continue
        if s in slugs:
            print(f"openbao: team groups {slugs[s]!r} and {team!r} share the path secret/teams/{s}/; "
                  f"only {slugs[s]!r} gets it - rename one", file=sys.stderr)
            continue
        slugs[s] = team
        policies.setdefault(team, set()).add(f"{TEAM_PREFIX}{s}")
        L += [f"bao policy write {TEAM_PREFIX}{s} - >/dev/null <<'HCL'",
              f'path "secret/data/teams/{s}/*" {{ capabilities = ["create","read","update","delete"] }}',
              f'path "secret/metadata/teams/{s}/*" {{ capabilities = ["list","read"] }}',
              "HCL"]
    keep = []
    for group in sorted(policies):
        gname = group_name(group)
        keep.append(gname)
        q = _q(group)
        L += [f'bao write identity/group name="{gname}" type=external policies="{",".join(sorted(policies[group]))}" >/dev/null',
              f'GID=$(bao read -field=id identity/group/name/{gname})',
              # an absent alias answers without an id; a lookup error stops the run
              f'HAVE=$(bao write -field=id identity/lookup/group alias_name={q} alias_mount_accessor="$OIDC_ACC")',
              'case "$HAVE" in',
              '  "$GID") ;;',
              f'  ""|*[!0-9a-f-]*) bao write identity/group-alias name={q} mount_accessor="$OIDC_ACC" canonical_id="$GID" >/dev/null ;;',
              f'  *) echo "openbao: the Authentik group "{q}" is aliased to another OpenBao group; nothing was moved" >&2; exit 1 ;;',
              'esac']
    # A team whose rzfz_bao_team flag was removed (or a role that left the
    # catalogue's openbao component) loses its group, and a team its policy.
    # A listing that fails is said and skips the prune (exit 3 at the end:
    # the caller warns); "No value found" is an empty list, not a failure.
    L += ["PRUNE_SKIPPED=",
          "_list() { _out=$(\"$@\" 2>&1) && { printf '%s\\n' \"$_out\"; return 0; }; "
          "case \"$_out\" in *'No value found'*) return 0 ;; esac; "
          "echo \"openbao: WARNING: '$*' failed, stale entries were NOT pruned: $_out\" >&2; PRUNE_SKIPPED=1; return 1; }",
          f'KEEP=" {" ".join(sorted(keep))} "',
          "GROUPS_LIST=$(_list bao list identity/group/name) || PRUNE_SKIPPED=1",
          "[ -n \"$PRUNE_SKIPPED\" ] || for g in $(printf '%s\\n' \"$GROUPS_LIST\" | sed '1,2d'); do",
          '  case "$g" in',
          f'    {GROUP_PREFIX}*|{LEGACY_PREFIXES}) ;;',
          "    *) continue ;;",
          "  esac",
          '  case "$KEEP" in *" $g "*) continue ;; esac',
          '  bao delete "identity/group/name/$g" >/dev/null',
          "done",
          f'KEEP_TEAMS=" {" ".join(sorted(TEAM_PREFIX + x for x in slugs))} "',
          "POLICY_LIST=$(_list bao policy list) || PRUNE_SKIPPED=1",
          "[ -n \"$PRUNE_SKIPPED\" ] || for p in $POLICY_LIST; do",
          f'  case "$p" in {TEAM_PREFIX}*) ;; *) continue ;; esac',
          '  case "$KEEP_TEAMS" in *" $p "*) continue ;; esac',
          '  bao policy delete "$p" >/dev/null',
          "done",
          '[ -z "$PRUNE_SKIPPED" ] || exit 3']
    return "\n".join(L) + "\n"


def main() -> int:
    import json
    from razzfazz_common.rbac import catalogue
    req = json.load(sys.stdin)
    teams = [t for t in req.get("team_groups") or [] if isinstance(t, str)]
    sys.stdout.write(render(catalogue(), teams, req["oidc"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
