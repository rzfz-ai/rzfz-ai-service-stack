# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Per-profile provisioning for runtime profile enablement.

razzfazz-init.sh generates per-profile secrets at first install (gated
on the profile being in COMPOSE_PROFILES). razzfazz-upgrade.sh's
`_gen_if_empty` re-runs that same set on every upgrade for back-fill.
Profiles enabled via the config UI *after* install never went through
either path — the operator hit empty SSO_CLIENT_SECRET, missing
per-profile DB, etc. — so this module runs the same
generation/derivation logic just before apply_manager's `compose up`.

Keep the mapping below in sync with razzfazz-init.sh's
generate_secrets() block and razzfazz-upgrade.sh's _gen_if_empty calls.
"""

import base64
import re
import secrets
import string


# (env_key, generator_kind). Generator kinds:
#   hex16    -> secrets.token_hex(16)        (32 hex chars)
#   hex32    -> secrets.token_hex(32)        (64 hex chars)
#   secret42 -> secrets.token_urlsafe(42)    (~56 url-safe chars)
#   secret64 -> secrets.token_urlsafe(64)    (~86 url-safe chars)
#   b64_32   -> base64(secrets.token_bytes(32)) (44 chars, STANDARD alphabet +
#               padding — matches `openssl rand -base64 32`; use for keys a
#               consumer decodes with the standard base64 alphabet)
#   pw24     -> 24-char alphanumeric password (matches generate_password 24)
#   pw32     -> 32-char alphanumeric password (matches generate_password 32).
#               Alphanumeric matters: the Wazuh API rejects base64 padding
#               characters, which is why cli/init.sh uses generate_password
#               rather than generate_secret for those (#855).
PROFILE_SECRETS = {
    'matrix': [
        ('SYNAPSE_DB_PASSWORD', 'hex16'),
        ('SYNAPSE_REGISTRATION_SHARED_SECRET', 'hex32'),
        ('SYNAPSE_MACAROON_SECRET_KEY', 'hex32'),
        ('SYNAPSE_FORM_SECRET', 'hex16'),
        ('SYNAPSE_CLIENT_SECRET', 'hex32'),
        ('MATRIX_CLIENT_ID', 'hex16'),
        ('ELEMENT_WEB_CLIENT_SECRET', 'hex32'),
    ],
    'paperless-ngx': [
        ('PAPERLESS_SECRET_KEY', 'secret64'),
        ('PAPERLESS_CLIENT_SECRET', 'hex32'),
        # PAPERLESS_DB_PASSWORD: handled below by the *_DB_PASSWORD-when-USER-set
        # block — the .env.example pre-fills PAPERLESS_DB_USER, so the password
        # MUST be generated to keep the per-service role in init-db.sh.
    ],
    'vaultwarden': [
        # VAULTWARDEN_ADMIN_TOKEN intentionally NOT provisioned — empty = /admin server
        # panel disabled by default (shared-secret backdoor; all admin work is via SSO
        # Org-Owner roles). Enable for break-glass by setting an Argon2 `vaultwarden hash`.
        ('VAULTWARDEN_CLIENT_SECRET', 'hex32'),
    ],
    'infisical': [
        ('INFISICAL_ENCRYPTION_KEY', 'hex16'),
        ('INFISICAL_AUTH_SECRET', 'secret42'),
        ('INFISICAL_CLIENT_SECRET', 'hex32'),
        # INFISICAL_DB_PASSWORD handled by USER-aware block below.
    ],
    'onyx': [
        ('ONYX_SECRET', 'secret42'),
        ('ONYX_CLIENT_SECRET', 'hex32'),
        # ONYX_DB_PASSWORD handled by USER-aware block below.
    ],
    'openhands': [
        ('OPENHANDS_CLIENT_SECRET', 'hex32'),
    ],
    'agents': [
        ('AGENTS_CLIENT_SECRET', 'hex32'),
    ],
    'lightrag': [
        ('LIGHTRAG_CLIENT_SECRET', 'hex32'),
        # LightRAG refuses to start with a default TOKEN_SECRET when
        # AUTH_ACCOUNTS is configured: "TOKEN_SECRET must be explicitly set
        # to a non-default value". Caught when enabling lightrag post-install.
        ('LIGHTRAG_TOKEN_SECRET', 'hex32'),
        # API key for LightRAG's /api endpoints (X-API-Key header). razzfazz-init.sh
        # and razzfazz-upgrade.sh both generate this; mirror here for
        # post-install profile-enable.
        ('LIGHTRAG_API_KEY', 'hex32'),
    ],
    'cognee': [
        ('COGNEE_CLIENT_SECRET', 'hex32'),
        # (#16) the FalkorDB graph-password secret was removed here — cognee uses
        # the embedded Kuzu graph DB (GRAPH_DATABASE_PROVIDER=kuzu, no graph
        # container), so that secret was dead.
        # Admin password for cognee's fastapi-users (no SSO). razzfazz-upgrade.sh
        # has this; init.sh doesn't. Mirror here so post-install profile-enable
        # gets a usable initial admin (operator can override in .env).
        ('COGNEE_ADMIN_PASSWORD', 'pw24'),
    ],
    'docling': [
        ('DOCLING_CLIENT_SECRET', 'hex32'),
    ],
    'stirling-pdf': [
        ('STIRLING_CLIENT_SECRET', 'hex32'),
    ],
    'paperclip': [
        ('PAPERCLIP_DB_PASSWORD', 'hex16'),
        ('PAPERCLIP_BETTER_AUTH_SECRET', 'hex32'),
        ('PAPERCLIP_CLIENT_SECRET', 'hex32'),
    ],
    'observability': [
        ('OBSERVABILITY_CLIENT_SECRET', 'hex32'),
        # CLICKHOUSE_PASSWORD intentionally omitted: clickhouse uses
        # CLICKHOUSE_USER=default with no password by default, and our
        # init script's clickhouse-client invocation now omits --password
        # when the env var is empty. Setting one without the matching
        # users.xml override breaks the healthcheck.
    ],
    'gitea': [
        ('GITEA_SECRET_KEY', 'hex32'),
        ('GITEA_CLIENT_SECRET', 'hex32'),
        # Per-repo internal token Gitea uses for SSH-keyed background jobs;
        # razzfazz-init.sh and razzfazz-upgrade.sh both generate this.
        ('GITEA_INTERNAL_TOKEN', 'hex32'),
        # GITEA_DB_PASSWORD handled by USER-aware block below.
    ],
    'crawl4ai': [
        # (#191) crawl4ai's SSO is a Caddy forward_auth proxy provider
        # (core/Authentik/blueprints/base/29-crawl4ai.yaml uses
        # client_secret: "${CRAWL4AI_CLIENT_SECRET}", forward_single) — exactly
        # like docling/stirling, which mint their *_CLIENT_SECRET. crawl4ai's
        # was the ONE forward_auth module minted NOWHERE (init.sh, upgrade.sh
        # AND here all missed it), so the proxy provider applied with an empty
        # client_secret → the outpost's forward_auth token exchange for
        # crawl4ai fails. Mint it here (and the two shell paths) for parity.
        ('CRAWL4AI_CLIENT_SECRET', 'hex32'),
        # (#191) The crawl4ai API's OWN Bearer credential — distinct from the
        # forward_auth client_secret above, and not optional. The container
        # binds 0.0.0.0 (Caddy and the published host port cannot reach a
        # loopback-only listener), and the image's _resolve_auth() calls
        # sys.exit(1) on a non-loopback bind with no credential — a supervisord
        # crash-loop that never reaches healthy. Caddy also forwards this as the
        # upstream Authorization header, which is what makes /playground
        # reachable for an SSO'd operator at all.
        #
        # The earlier note here said a token alone "doesn't help, the image then
        # 401s /monitor/health". That was right about the symptom and wrong
        # about the cause: /monitor/health is not crawl4ai's health endpoint.
        # The auth gate's public set is {observability.health_check.endpoint,
        # "/token"} and that endpoint is "/health" — measured on 0.9.0,
        # /health returns 200 with no credential. The healthcheck and the Caddy
        # exemption now use it, so the token is sufficient.
        ('CRAWL4AI_API_TOKEN', 'hex32'),
    ],
    # (#191) mcp-manager: razzfazz-init.sh mints these on a fresh box and
    # razzfazz-post-install.sh's ensure_mcp_manager_secret back-fills them on
    # upgrade — but a profile enabled from the Config Portal went through
    # NEITHER path, so MCP_* stayed empty → DB-auth fail + /internal 401.
    # Mirror ensure_mcp_manager_secret here. MCP_MANAGER_DB_PASSWORD is handled
    # by the USER-aware DB block below (MCP_MANAGER_DB_USER is pre-filled in
    # config/.env.example).
    #
    # MCP_MANAGER_SECRET_KEY MUST be b64_32 (STANDARD base64), not secret42
    # (url-safe): mcp-manager's crypto.py derives its AES-256-GCM master key
    # via base64.b64decode(key, validate=True), which REJECTS the url-safe
    # `-`/`_` chars token_urlsafe emits → "MCP_MANAGER_SECRET_KEY is not valid
    # base64" and the service refuses to operate. Matches init.sh
    # (`generate_secret 32`) and post-install's ensure_mcp_manager_secret
    # (`openssl rand -base64 32`), which both emit standard base64. (#191)
    'mcp': [
        ('MCP_MANAGER_SECRET_KEY', 'b64_32'),
        ('MCP_INTERNAL_TOKEN', 'hex32'),
        ('MCP_CLIENT_SECRET', 'hex32'),
    ],
    # (#1075) OpenUEM — same third-site gap as Wazuh below. cli/init.sh mints
    # these three on a fresh box and cli/upgrade.sh back-fills them
    # profile-scoped to `openuem`, but enabling the module from the Config
    # Portal on an EXISTING box runs neither shell path: the operator got an
    # empty OPENUEM_DB_PASSWORD (the DSN in modules/apps/openuem/compose.yml is
    # `postgres://${OPENUEM_DB_USER}:${OPENUEM_DB_PASSWORD}@postgres:5432/...`,
    # so openuem-certs and every worker die with "password authentication
    # failed"), an empty console JWT_KEY and an empty Authentik proxy secret.
    #
    # pw24 (not secret*/b64) for the DB password, matching `generate_password
    # 24` in cli/init.sh: the value is interpolated into that postgres:// URL
    # and base64's `/ + =` would corrupt it. hex32 for the console signing key
    # (64 hex chars — upstream's own docs want >= 32 and ship a demo key we
    # never adopt) and for the forward-auth ProxyProvider secret that
    # core/Authentik/blueprints/base/35-openuem.yaml consumes.
    #
    # OPENUEM_DB_USER needs no DB_USER_DEFAULTS row: config/.env.example ships
    # `openuem_user` and config/migrations/env-changes.json adds it with that
    # same non-empty default, so it is never empty on a box that reaches here.
    'openuem': [
        ('OPENUEM_CONSOLE_JWT_KEY', 'hex32'),
        ('OPENUEM_CLIENT_SECRET', 'hex32'),
        ('OPENUEM_DB_PASSWORD', 'pw24'),
    ],
    # (#855 rev-B, review HIGH 7) Wazuh was the THIRD secret-generation site
    # this module never reached — cli/init.sh mints on a fresh box and
    # cli/upgrade.sh back-fills on upgrade, but enabling the profile from the
    # Config Portal on an existing box went through NEITHER, so all six
    # credentials stayed empty and Apply started a SIEM with no working
    # credential anywhere. That also violated this module's own docstring
    # contract ("keep in sync with init.sh + upgrade.sh").
    #
    # pw32 (not hex/urlsafe) for the three indexer/manager passwords, matching
    # `generate_password 32` in cli/init.sh: the Wazuh API enforces a password
    # policy and rejects base64 padding characters, which token_urlsafe emits.
    'wazuh': [
        ('WAZUH_INDEXER_PASSWORD', 'pw32'),
        ('WAZUH_DASHBOARD_PASSWORD', 'pw32'),
        ('WAZUH_API_PASSWORD', 'pw32'),
        ('WAZUH_AUTHD_PASSWORD', 'hex32'),
        # Two Authentik secrets, two Authentik objects (rev-B blocker 4): the
        # forward-auth ProxyProvider on app slug `wazuh` and the native OIDC
        # provider on hidden app slug `wazuh-oidc`. An empty proxy secret makes
        # wazuh.<domain> 404 for everyone; an empty OIDC secret breaks the
        # dashboard login only.
        ('WAZUH_CLIENT_SECRET', 'hex32'),
        ('WAZUH_OIDC_CLIENT_SECRET', 'hex32'),
    ],
}

def _generate(kind):
    if kind == 'hex16':
        return secrets.token_hex(16)
    if kind == 'hex32':
        return secrets.token_hex(32)
    if kind == 'secret32':
        return secrets.token_urlsafe(32)
    if kind == 'secret42':
        return secrets.token_urlsafe(42)
    if kind == 'secret64':
        return secrets.token_urlsafe(64)
    if kind == 'b64_32':
        # STANDARD (not url-safe) base64 of 32 random bytes — 44 chars incl.
        # padding, same as `openssl rand -base64 32`. Required by consumers that
        # decode with the standard alphabet (mcp-manager crypto.py's
        # base64.b64decode(..., validate=True)); url-safe `-`/`_` would be
        # rejected there. (#191)
        return base64.b64encode(secrets.token_bytes(32)).decode('ascii')
    if kind in ('pw24', 'pw32'):
        # Alphanumeric password — 24 chars for 'pw24', 32 for 'pw32' (see
        # `length` below). URL-safe alphabet only; symbols are
        # deliberately omitted: many services compose DSNs from env vars
        # (e.g. Infisical's DB_CONNECTION_URI) and an unescaped '#' or '@'
        # breaks URL parsing — pg-connection-string's URL() throws
        # "Invalid URL". Alphanumeric is safe everywhere.
        alphabet = string.ascii_letters + string.digits
        length = 32 if kind == 'pw32' else 24
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    raise ValueError(f'unknown generator kind: {kind}')


# ---------------------------------------------------------------------------
# #245 — Dify's native OTEL export, stated instead of inherited
# ---------------------------------------------------------------------------
# The four .env.dify keys that decide whether a Dify workflow run reaches OUR
# collector. Two of them used to be left at their upstream value, and both of
# those values are wrong for a single box:
#
#   OTEL_EXPORTER_OTLP_PROTOCOL — `modules/dify/compose.yml` gives dify-api /
#     dify-worker / dify-worker-beat BOTH env files (`../../.env` and then
#     `../../.env.dify`). The stack's own `.env` carries
#     OTEL_EXPORTER_OTLP_PROTOCOL=grpc since migration v67 — that value belongs
#     to the collector's EXTERNAL log-export leg (#197), not to Dify. Dify reads
#     the same NAME (api/extensions/ext_otel.py): `protocol == "grpc"` builds a
#     GRPCSpanExporter against OTLP_BASE_ENDPOINT — which is
#     http://otel-collector:4318, the collector's HTTP receiver (gRPC listens on
#     4317). Every span would be dropped silently, in the background, with no
#     error anywhere in Dify's UI. Today that does not happen ONLY because
#     config/.env.dify.example carries an empty `OTEL_EXPORTER_OTLP_PROTOCOL=`
#     line copied verbatim from upstream, and the second env_file wins. Nothing
#     said that out loud and nothing held it: tidying the empty upstream keys
#     out of that example would have flipped Dify to gRPC-against-HTTP. So the
#     hook that pins the ENDPOINT now pins the PROTOCOL that must pair with it.
#
#   OTEL_SAMPLING_RATE — upstream ships 0.1, sized for SaaS volume. Dify turns
#     it into ParentBasedTraceIdRatio(0.1) and a workflow run is ONE root
#     trace, so nine runs out of ten produce nothing. On a box that is not
#     sampling, it is an acceptance test that fails at random.
_DIFY_OTEL_COLLECTOR = 'http://otel-collector:4318'
_DIFY_OTEL_PROTOCOL = 'http/protobuf'
_DIFY_OTEL_SAMPLING_RATE = '1.0'
# The value upstream ships. Only THIS one is raised — an operator who chose a
# rate of their own keeps it.
_DIFY_OTEL_UPSTREAM_SAMPLING_RATE = '0.1'


# #2003 — the LLM Manager's own OTLP seam. `.env.example` carried this key with
# a comment telling the reader to set it when the observability profile is on,
# and nothing set it: `grep -rn LLM_MANAGER_OTEL_ENDPOINT cli/ core/config/app/
# scripts/` returned nothing at all. Since the 2026.09 cutover every LLM call in
# the stack goes through the manager, so the one surface that sees all of them
# emitted nothing while the dashboards were being called empty.
#
# One value, two consumers: modules/llm/manager/compose.yml forwards it to the
# manager AND to llm-manager-router, and app/config.py turns a non-empty value
# into the router's `otel` callback — so setting it CHANGES the router config,
# which the entrypoint watcher turns into a LiteLLM restart (#1955).
_LLM_MANAGER_OTEL_ENDPOINT = _DIFY_OTEL_COLLECTOR


def llm_manager_otel_updates(env):
    """The `.env` write that points the LLM Manager and its router at the
    collector. Returns {} when it is already right, so a re-run writes nothing.

    Shared with cli/post-install.sh's wire_observability_consumers() — the two
    paths (Config Portal toggle vs. CLI) must agree, and
    tests/unit/consistency/test_2003_the_manager_seam_agrees.py pins that by
    RUNNING both.
    """
    if env.get('LLM_MANAGER_OTEL_ENDPOINT', '') != _LLM_MANAGER_OTEL_ENDPOINT:
        return {'LLM_MANAGER_OTEL_ENDPOINT': _LLM_MANAGER_OTEL_ENDPOINT}
    return {}


_OWUI_OTEL_ENDPOINT = _DIFY_OTEL_COLLECTOR + '/v1/traces'
_AGENTS_OTEL_ENDPOINT = _DIFY_OTEL_COLLECTOR


def owui_otel_updates(env):
    """The `.env` write that points Open WebUI's pipelines filter at the
    collector (#2015). Shared with cli/post-install.sh's OWUI-OTEL block;
    test_2015 runs both."""
    if env.get('OPENLIT_OTLP_ENDPOINT', '') != _OWUI_OTEL_ENDPOINT:
        return {'OPENLIT_OTLP_ENDPOINT': _OWUI_OTEL_ENDPOINT}
    return {}


def agents_otel_updates(env):
    """The `.env` write that gives every new/recreated agent instance its
    OTLP endpoint (#2015 — was wired by post-install only, so a toggle-enabled
    observability profile produced no agent spans). Shared with
    cli/post-install.sh's AGENTS-OTEL block; test_2015 runs both."""
    if env.get('OBSERVABILITY_OTEL_AGENTS_ENDPOINT', '') != _AGENTS_OTEL_ENDPOINT:
        return {'OBSERVABILITY_OTEL_AGENTS_ENDPOINT': _AGENTS_OTEL_ENDPOINT}
    return {}


def observability_recreate_targets(env, dify_changed=False, owui_changed=False,
                                   agents_changed=False, manager_changed=False):
    """The containers that read the OTLP env at CREATE time and therefore have
    to be recreated for a write to take effect — restricted to the profiles
    the box actually runs (#2015). cli/post-install.sh recreates the same set;
    the Portal toggle does so through apply_manager since #2015."""
    profiles = {p.strip() for p in (env.get('COMPOSE_PROFILES') or '').split(',') if p.strip()}
    out = []
    if manager_changed and 'llm-manager' in profiles:
        out += ['llm-manager', 'llm-manager-router']
    if owui_changed and 'chat' in profiles:
        out.append('pipelines')
    if dify_changed and 'dify' in profiles:
        out += ['dify-api', 'dify-worker']
    if agents_changed and 'agents' in profiles:
        out.append('agent-manager')
    return out


def dify_otel_updates(dify_env):
    """The .env.dify writes that turn Dify's native OTEL export on and aim it
    at our collector. Returns only the keys whose CURRENT value is wrong, so a
    re-run on an already-wired box writes nothing.

    Shared with cli/post-install.sh's wire_observability_consumers() — the two
    paths (Config Portal toggle vs. CLI) must agree, and
    tests/unit/consistency/test_245_dify_otel_wiring_paths_agree.py pins that.
    """
    updates = {}
    if dify_env.get('ENABLE_OTEL', '').lower() != 'true':
        updates['ENABLE_OTEL'] = 'true'
    if dify_env.get('OTLP_BASE_ENDPOINT', '') != _DIFY_OTEL_COLLECTOR:
        updates['OTLP_BASE_ENDPOINT'] = _DIFY_OTEL_COLLECTOR
    if dify_env.get('OTEL_EXPORTER_OTLP_PROTOCOL', '') != _DIFY_OTEL_PROTOCOL:
        updates['OTEL_EXPORTER_OTLP_PROTOCOL'] = _DIFY_OTEL_PROTOCOL
    rate = (dify_env.get('OTEL_SAMPLING_RATE') or '').strip()
    if rate in ('', _DIFY_OTEL_UPSTREAM_SAMPLING_RATE):
        updates['OTEL_SAMPLING_RATE'] = _DIFY_OTEL_SAMPLING_RATE
    return updates


#: #2149 — the LLM Manager consumers post-install wires (cli/post-install.sh,
#: RAZZFAZZ_LLM_CONSUMERS): service -> (profile, env prefix, endpoint keys,
#: key keys). The manager requires a `rzfz-sk-…` bearer on /v1; post-install
#: mints `stack/<svc>` and writes the consumer's key variables on --preset and
#: --refresh, and NOTHING did so when the module was enabled from the Config
#: Portal — LightRAG (in no preset, so the Portal is its normal enable path)
#: then talks to the manager with an empty key and dies on its first embedding
#: call (`KeyError: 'OPENAI_API_KEY'`; QA RZFZAI-1961). The two paths must stay
#: identical: a consistency guard parses post-install's table against this one.
LLM_MANAGER_CONSUMERS = {
    'cognee':       ('cognee',      'COGNEE',       ('LLM_ENDPOINT', 'EMBEDDING_ENDPOINT'),                    ('LLM_API_KEY', 'EMBEDDING_API_KEY')),
    'lightrag':     ('lightrag',    'LIGHTRAG',     ('LLM_ENDPOINT', 'EMBEDDING_ENDPOINT', 'RERANK_ENDPOINT'), ('LLM_API_KEY', 'EMBEDDING_API_KEY', 'RERANK_API_KEY')),
    'openhands':    ('openhands',   'OPENHANDS',    ('LLM_ENDPOINT',),                                         ('LLM_API_KEY',)),
    'paperclip':    ('paperclip',   'PAPERCLIP',    ('LLM_ENDPOINT',),                                         ('LLM_API_KEY',)),
    'ollama-proxy': ('llm-manager', 'OLLAMA_PROXY', (),                                                        ('LLM_API_KEY',)),
}
LLM_CANONICAL_ENDPOINT = 'http://llm:8080/v1'
REFRESH_HINT = "run `rzfz post-install --refresh` once the LLM Manager is up to mint it"


def llm_manager_consumer_updates(profile_id, env, mint_service_key=None):
    """The .env writes that give a consumer enabled by ``profile_id`` its
    LLM Manager wiring: the canonical endpoints and the `stack/<svc>` key.

    ``mint_service_key(svc) -> plaintext`` is the caller's handle on the
    manager (the Portal runs post-install's own mint function through docker
    exec); ``None`` or an empty return means the key cannot be minted HERE,
    and the line says so and names the refresh — the consumer then starts on
    the compose default (empty key) exactly as before, but the operator is
    told instead of finding out from a 401 in the container log.

    An already-stored `LLM_MANAGER_<PREFIX>_KEY` is reused, never re-minted
    (a second key would orphan the first in the manager's key list).
    Returns ``(updates, lines)``."""
    updates, lines = {}, []
    for svc, (profile, prefix, ep_keys, key_keys) in LLM_MANAGER_CONSUMERS.items():
        if profile != profile_id:
            continue
        for ek in ep_keys:
            want = LLM_CANONICAL_ENDPOINT + ('/rerank' if 'RERANK' in ek else '')
            var = f'{prefix}_{ek}'
            if (env.get(var) or '').strip() != want:
                updates[var] = want
        key_var = f'LLM_MANAGER_{prefix}_KEY'
        key = (env.get(key_var) or '').strip()
        if not key:
            if mint_service_key is None:
                lines.append(f'  Warning: stack/{svc} service key not minted — no LLM Manager handle here; {REFRESH_HINT}')
                continue
            try:
                key = (mint_service_key(svc) or '').strip()
            except Exception as e:  # the mint must never take the toggle down
                key = ''
                lines.append(f'  Warning: minting the stack/{svc} service key failed ({e}); {REFRESH_HINT}')
            if not key:
                if not lines or 'failed' not in lines[-1]:
                    lines.append(f'  Warning: the LLM Manager did not return a stack/{svc} service key; {REFRESH_HINT}')
                continue
            updates[key_var] = key
            lines.append(f'  minted stack/{svc} service key ({key_var})')
        for kk in key_keys:
            var = f'{prefix}_{kk}'
            if (env.get(var) or '').strip() != key:
                updates[var] = key
        if any(k.startswith(prefix + '_') for k in updates):
            lines.append(f'  {svc} wired to the LLM Manager at {LLM_CANONICAL_ENDPOINT} (#2149)')
    return updates, lines


def provision_profile(profile_id, config_manager, action=None, mint_service_key=None):
    """Generate any empty secrets and derive any host-dependent env vars
    needed before a profile's containers can start. Idempotent — re-runs
    are safe because every step is a "fill-if-empty" check.

    Returns a list of human-readable lines describing what changed (or
    nothing changed). The caller appends these to its action log.
    """
    lines = []
    env = config_manager.read_env()
    updates = {}

    for key, kind in PROFILE_SECRETS.get(profile_id, []):
        if not env.get(key):
            value = _generate(kind)
            updates[key] = value
            lines.append(f'  generated {key} ({kind})')

    # NB: CADDY_IP detection retired — public hostnames that internal
    # services need to resolve (AUTHENTIK_DOMAIN, MATRIX_DOMAIN, …) are
    # registered as Docker network aliases on the caddy container, so
    # embedded DNS handles the lookup dynamically. See core/compose.yml.

    # LightRAG: AUTH_ACCOUNTS must be `user:password,user:password,...`.
    # razzfazz-init.sh sets `admin:$CONFIG_ADMIN_PASSWORD` at install. Two
    # failure modes to repair here before lightrag boots:
    #   1. Malformed value (operator hand-edit, or an older code path that
    #      wrote only the bare 'admin') — the server crash-loops on it.
    #   2. (M033 S17) Format-valid but the admin password DIVERGED from the
    #      bootstrap password — e.g. the upstream default `admin:admin123`
    #      that the image ships, or a --no-secrets install where
    #      CONFIG_ADMIN_PASSWORD was never set. The old format-only check
    #      let these through, so the operator's one remembered password
    #      ("the bootstrap password") didn't actually log in to LightRAG.
    # Repair preserves any additional accounts; only the `admin:` entry is
    # realigned to the bootstrap password.
    # #1973: enabling GPUStack (llm-legacy) on a box that never had it. A
    # manager-only box has no device overlay in COMPOSE_FILE — it never needed
    # one — and gpustack then dies in a start loop with the #1448 FATAL
    # ("HARDWARE=amd but /dev/kfd is not mounted"). rzfz upgrade repairs the
    # chain; the Portal toggle did not, so "enable" alone never produced a
    # usable GPUStack (measured 0.79, 2026-09-12). The overlay is picked from
    # HARDWARE, exactly as init and upgrade pick it; an unknown HARDWARE is
    # SAID, not guessed — the wrong overlay is worse than none.
    if profile_id == 'llm-legacy':
        hw = (env.get('HARDWARE') or '').strip()
        overlay = f'modules/llm/compose.devices.{hw}.yml'
        chain = [e for e in (env.get('COMPOSE_FILE') or '').split(':') if e]
        if hw not in ('amd', 'nvidia', 'cpu'):
            lines.append(f'  Warning: HARDWARE={hw or "<unset>"} — cannot pick a device overlay for gpustack; '
                         'it will refuse to start (#1448) until HARDWARE is set and rzfz upgrade has run (#1973).')
        elif overlay in chain:
            lines.append(f'  device overlay already in COMPOSE_FILE ({overlay}).')
        else:
            if not chain:
                chain = ['compose.yml']
            # One device overlay at a time: a stale one for another hardware
            # line goes, operator-added overlays stay where they are.
            chain = [e for e in chain if not re.match(r'^(modules/)?llm/compose\.devices\.\w+\.yml$', e)]
            chain.insert(1, overlay)
            updates['COMPOSE_FILE'] = ':'.join(chain)
            lines.append(f'  COMPOSE_FILE gains the device overlay for HARDWARE={hw}: {updates["COMPOSE_FILE"]} (#1973)')

    if profile_id == 'lightrag':
        accounts = env.get('LIGHTRAG_AUTH_ACCOUNTS', '')
        admin_pw = env.get('AUTHENTIK_BOOTSTRAP_PASSWORD', '')
        pairs = [p for p in accounts.split(',') if p] if accounts else []
        valid = bool(pairs) and all(':' in p for p in pairs)
        if not valid:
            if admin_pw:
                updates['LIGHTRAG_AUTH_ACCOUNTS'] = f'admin:{admin_pw}'
                lines.append('  repaired LIGHTRAG_AUTH_ACCOUNTS '
                             '(invalid format; set to admin:<bootstrap_pw>)')
        elif admin_pw:
            # Format is valid — realign the admin entry's password if it
            # diverged from the bootstrap password, keeping other accounts.
            rebuilt = []
            changed = False
            for p in pairs:
                user, _, pw = p.partition(':')
                if user == 'admin' and pw != admin_pw:
                    rebuilt.append(f'admin:{admin_pw}')
                    changed = True
                else:
                    rebuilt.append(p)
            if changed:
                updates['LIGHTRAG_AUTH_ACCOUNTS'] = ','.join(rebuilt)
                lines.append('  realigned LIGHTRAG_AUTH_ACCOUNTS admin '
                             'password to bootstrap password (had diverged)')

    # Cognee + LightRAG: model env vars are empty in .env.example because
    # the model name depends on what's deployed on GPUStack. razzfazz-post-install.sh
    # makes qwen3.6 the always-on default LLM (gemma4 is pre-downloaded but
    # scaled to 0 replicas, so it must NOT be the default — connecting to it
    # would time out) and qwen3-embedding the embedding (switched from
    # nomic-embed-text 2026-06-19: nomic's hard 2048-tok context cap can't embed
    # cognee/lightrag graph-summary chunks ~3000 tok; qwen3-embedding is 32K ctx,
    # dim 2560), so default to those when empty. Without this the
    # services boot but every /add request runs an LLM connection test that
    # times out after 30s with empty model name → 500. Operator can still override.
    # Cognee uses LLM_PROVIDER=custom → litellm, which requires a `<provider>/<model>`
    # prefix on the model name; without it litellm raises BadRequestError("LLM
    # Provider NOT provided"). LightRAG uses LLM_BINDING=openai (direct OpenAI SDK),
    # which takes the bare model name. Embedding paths use openai_compatible /
    # openai bindings respectively — bare names everywhere.
    MODEL_DEFAULTS = {
        'cognee': [
            ('COGNEE_LLM_MODEL', 'openai/qwen3.6'),
            ('COGNEE_EMBEDDING_MODEL', 'qwen3-embedding'),
            # #413 — the SAME dim rule as LightRAG below, which #70 fixed there
            # and missed here. Cognee's pgvector tables are keyed to this
            # number; leave it at .env.example's stale 768 and every insert
            # fails with "expected 768 dimensions, not 4096" while the
            # container still reports healthy and the API still answers 200.
            # The only visible symptom is "the knowledge graph is empty", which
            # reads like the user forgot to run cognify. Observed on 0.97 after
            # enabling cognee from the Config Portal — the path that skips
            # core/llm/sync.py, which is what writes the correct value on boxes
            # that had cognee on at install.
            ('COGNEE_EMBEDDING_DIM', '4096'),
        ],
        'lightrag': [
            ('LIGHTRAG_LLM_MODEL', 'qwen3.6'),
            ('LIGHTRAG_EMBEDDING_MODEL', 'qwen3-embedding'),
            # EMBEDDING_DIM must match the embedding model's ACTUAL native output
            # dim. The GPUStack/llama-box embeddings endpoint IGNORES the OpenAI
            # `dimensions` param (measured on 0.91: requesting 768/2560/4096 all
            # return 4096), so LightRAG can't truncate — the pgvector tables are
            # keyed to this dim and MUST equal what the endpoint emits or every
            # document insert fails on a dimension mismatch. qwen3-embedding
            # (Qwen3-Embedding-8B GGUF via the v0.7.1 llama-box runner) emits
            # 4096-dim vectors; the old 768 was nomic-embed-text's dim, left stale
            # after the model switch. #70.
            ('LIGHTRAG_EMBEDDING_DIM', '4096'),
            # Endpoint ignores `dimensions`, so don't send it (misleading). #70.
            ('LIGHTRAG_EMBEDDING_SEND_DIM', 'false'),
        ],
    }
    for key, default in MODEL_DEFAULTS.get(profile_id, []):
        if not env.get(key):
            updates[key] = default
            lines.append(f'  defaulted {key}={default}')

    # #2149: the LLM Manager key and endpoints — post-install's wiring, on the
    # Portal's enable path too. Reads env as of this call: a key stored by an
    # earlier post-install run is reused, never re-minted.
    c_updates, c_lines = llm_manager_consumer_updates(profile_id, {**env, **updates}, mint_service_key)
    updates.update(c_updates)
    lines.extend(c_lines)

    # (#184 / #191) Per-service DB *user*: config/.env.example pre-fills these
    # (onyx_user, paperless_user, …), but a box whose .env predates that
    # addition — an older install, or an upgrade whose env-migration didn't
    # back-fill them — has the *_DB_USER EMPTY. On a toggle-enable the
    # per-service DSN then evaluates as "<empty>:<POSTGRES_PASSWORD>@postgres"
    # and connects as the GLOBAL docker user (or fails auth) — the "*_DB_USER
    # empty-on-upgrade trap" (#184; hit on the 0.208 demo box enabling onyx).
    # Default the USER to its canonical per-service role name when empty so
    # (a) the *_DB_PASSWORD block below fires and (b) init-db.sh /
    # postgres-db-reconcile create the dedicated role. Matches
    # config/.env.example exactly. Idempotent: only fills when empty.
    DB_USER_DEFAULTS = {
        'paperless-ngx': ('PAPERLESS_DB_USER', 'paperless_user'),
        'infisical':     ('INFISICAL_DB_USER', 'infisical_user'),
        'onyx':          ('ONYX_DB_USER',      'onyx_user'),
        'gitea':         ('GITEA_DB_USER',     'gitea_user'),
        'mcp':           ('MCP_MANAGER_DB_USER', 'mcp_manager_user'),
    }
    ud = DB_USER_DEFAULTS.get(profile_id)
    if ud:
        user_key, default_user = ud
        if not env.get(user_key) and not updates.get(user_key):
            updates[user_key] = default_user
            lines.append(f'  defaulted {user_key}={default_user}')

    # Per-service DB passwords: when *_DB_USER is non-empty (the .env.example
    # default for paperless / infisical / onyx / gitea / mcp, or just
    # defaulted above), generate *_DB_PASSWORD if missing. Without this,
    # init-db.sh's role-create gate (USER+PASS+DB all required) doesn't fire,
    # the per-service role never exists, and the compose DSN evaluates as
    # "<user_role>:<POSTGRES_PASSWORD>@postgres" → postgres rejects with
    # "role does not exist". This is the actual common case for
    # profile-enable-after-install, since .env was bootstrapped from
    # .env.example which ships the USER values.
    DB_PASSWORD_BY_PROFILE = {
        'paperless-ngx': ('PAPERLESS_DB_USER', 'PAPERLESS_DB_PASSWORD'),
        'infisical':     ('INFISICAL_DB_USER', 'INFISICAL_DB_PASSWORD'),
        'onyx':          ('ONYX_DB_USER',      'ONYX_DB_PASSWORD'),
        'gitea':         ('GITEA_DB_USER',     'GITEA_DB_PASSWORD'),
        'mcp':           ('MCP_MANAGER_DB_USER', 'MCP_MANAGER_DB_PASSWORD'),
    }
    pair = DB_PASSWORD_BY_PROFILE.get(profile_id)
    if pair:
        user_key, pass_key = pair
        # Re-read env including just-applied updates so the USER flag we just
        # might have set in this same call is visible.
        live_env = {**env, **updates}
        if live_env.get(user_key) and not live_env.get(pass_key):
            value = _generate('hex16')
            updates[pass_key] = value
            lines.append(f'  generated {pass_key} (hex16; {user_key} is set)')

    # (#245) Observability wiring hook: enabling the profile brings up
    # OpenLIT + ClickHouse + otel-collector, but the LLM surfaces must be
    # pointed at the collector or the dashboards stay empty ("up but not
    # wired"). Two fixes, both idempotent:
    #   1. OWUI span emitter (.env): heal the stale OPENLIT_OTLP_ENDPOINT that
    #      pointed at openlit:4318 — openlit ships NO OTLP receiver (only its
    #      :3000 UI), so every OWUI chat span was silently dropped. The real
    #      receiver is otel-collector:4318.
    #   2. Dify native OTEL (.env.dify): turn ENABLE_OTEL on, target the same
    #      collector, and — see dify_otel_updates() above — also STATE the
    #      transport and the sampling rate instead of inheriting them, because
    #      what Dify inherits is wrong on both counts.
    if profile_id == 'observability':
        collector = _DIFY_OTEL_COLLECTOR
        # CFG-11: the containers that CONSUME these keys are not the ones the
        # toggle recreates. `_execute_toggle` recreates the profile's own
        # services plus authentik-init / authentik-worker /
        # razzfazz-start-portal — never `pipelines`, `dify-api` or
        # `dify-worker`, which read env at container-CREATE time
        # (`env_file: ../../.env`). So the write below is inert until some
        # unrelated recreate happens, and the operator sees a green toggle
        # with permanently empty dashboards. We cannot recreate them from
        # here (this module has no docker handle), so the action log names
        # the exact follow-up instead of implying the wiring is live.
        needs_recreate = []
        # #2003: the manager seam. Same CFG-11 caveat as the rest — this module
        # has no docker handle, and both consumers read the value at container
        # CREATE time, so the write is inert until they are recreated. Named in
        # the follow-up line below rather than implied to be live.
        mgr_updates = llm_manager_otel_updates(env)
        if mgr_updates:
            updates.update(mgr_updates)
            lines.append('  set LLM_MANAGER_OTEL_ENDPOINT -> otel-collector '
                         '(was empty; every /v1 call goes through the manager)')
            needs_recreate += ['llm-manager', 'llm-manager-router']
        owui_updates = owui_otel_updates(env)
        if owui_updates:
            updates.update(owui_updates)
            lines.append('  set OPENLIT_OTLP_ENDPOINT -> otel-collector '
                         f'(was {env.get("OPENLIT_OTLP_ENDPOINT") or "<empty>"}; openlit:4318 has no OTLP receiver)')
            needs_recreate.append('pipelines')
        # #2015: the agents seam — post-install wired it, the toggle did not,
        # so a toggle-enabled observability profile produced no agent spans.
        agents_updates = agents_otel_updates(env)
        if agents_updates:
            updates.update(agents_updates)
            lines.append('  set OBSERVABILITY_OTEL_AGENTS_ENDPOINT -> otel-collector '
                         '(new/recreated agent instances emit traces)')
            needs_recreate.append('agent-manager')
        # CFG-11: only touch .env.dify when Dify is actually deployed —
        # otherwise this created a two-line .env.dify on boxes that have no
        # Dify at all, which then looks like a half-configured install.
        profiles = {p.strip() for p in
                    (env.get('COMPOSE_PROFILES') or '').split(',') if p.strip()}
        if 'dify' in profiles:
            dify_env = config_manager.read_dify_env()
            dify_updates = dify_otel_updates(dify_env)
            for k, v in dify_updates.items():
                config_manager.update_dify_env_var(k, v)
            if dify_updates:
                lines.append(f'  enabled Dify OTEL export -> otel-collector '
                             f'({len(dify_updates)} .env.dify update(s))')
                needs_recreate += ['dify-api', 'dify-worker']
        if needs_recreate and action:
            action.add_line(
                f'  NOTE: spans will NOT flow until these containers are '
                f'recreated — they read the OTLP env at create time: '
                f'{", ".join(needs_recreate)}. Run '
                f'`docker compose up -d --no-deps --force-recreate '
                f'{" ".join(needs_recreate)}` (CFG-11).')

    if updates:
        for k, v in updates.items():
            config_manager.update_env_var(k, v)
        if action:
            action.add_line(f'Provisioning {profile_id} ({len(updates)} env update(s))...')
            for line in lines:
                action.add_line(line)
    return lines
