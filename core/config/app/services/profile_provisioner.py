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
#   pw24     -> 24-char alphanumeric+symbol password (matches generate_password)
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
        # NB (#191): crawl4ai ALSO needs a browseable-health / SSO-exempt
        # config.yml + a host-port/bind fix before its container comes up
        # docker-*healthy* — those are compose/vendored-config changes that
        # require a live-stack test, tracked separately. A raw API token alone
        # doesn't help (the image then 401s /monitor/health), so only the
        # forward_auth client_secret is minted here.
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
    if kind == 'pw24':
        # Length-24 password from URL-safe alphanumeric only. Symbols are
        # deliberately omitted: many services compose DSNs from env vars
        # (e.g. Infisical's DB_CONNECTION_URI) and an unescaped '#' or '@'
        # breaks URL parsing — pg-connection-string's URL() throws
        # "Invalid URL". Alphanumeric is safe everywhere.
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(24))
    raise ValueError(f'unknown generator kind: {kind}')


def provision_profile(profile_id, config_manager, action=None):
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

    if updates:
        for k, v in updates.items():
            config_manager.update_env_var(k, v)
        if action:
            action.add_line(f'Provisioning {profile_id} ({len(updates)} env update(s))...')
            for line in lines:
                action.add_line(line)
    return lines
