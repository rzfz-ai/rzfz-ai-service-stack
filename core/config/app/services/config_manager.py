# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Config manager — .env reader/writer ported from core/setup/app/config_manager.py."""

import json
import logging
import base64
import os
import re
import secrets
import string
import subprocess
import sys
from .env_mount import env_write_guard

logger = logging.getLogger(__name__)


ENV_FILE_NAME = '.env'
DIFY_ENV_FILE_NAME = '.env.dify'

TLS_MODES = {
    'letsencrypt': '',
    'selfsigned': 'internal',
    'certificate': 'certificate',
}

# #1450 (C10b / D7) re-review, finding 5: GPUSTACK_MODES (the standalone /
# master / worker → bind-address map) is gone. Nothing under core/ used it once
# the page lost its mode section. The mapping itself still exists where it is
# still true — cli/init.sh, which is the thing that actually writes
# GPUSTACK_MODE and GPUSTACK_BIND on a `worker-box` preset.

SMTP_MODES = ['relay', 'direct']

SECRETS_CONFIG = [
    ('WEBUI_SECRET_KEY', 32, 'env', 'hex'),
    ('AUTHENTIK_SECRET_KEY', 42, 'env', 'base64'),
    ('AUTHENTIK_BOOTSTRAP_TOKEN', 48, 'env', 'base64'),
    ('POSTGRES_PASSWORD', 24, 'env', 'password'),
    ('VALKEY_PASSWORD', 24, 'env', 'password'),
    ('GPUSTACK_SECRET_KEY', 32, 'env', 'base64'),
    ('PIPELINES_API_KEY', 32, 'env', 'base64'),
    ('KOMODO_PASSKEY', 24, 'env', 'password'),
    ('CHAT_CLIENT_SECRET', 64, 'env', 'base64'),
    ('DIFY_CLIENT_SECRET', 64, 'env', 'base64'),
    ('ADMIN_CLIENT_SECRET', 64, 'env', 'base64'),
    ('LLM_CLIENT_SECRET', 64, 'env', 'base64'),
    ('BACKUP_CLIENT_SECRET', 32, 'env', 'hex'),
    ('PLUGIN_DAEMON_KEY', 42, 'env', 'base64'),
    ('PLUGIN_DIFY_INNER_API_KEY', 42, 'env', 'base64'),
    ('GITEA_SECRET_KEY', 32, 'env', 'hex'),
    ('GITEA_INTERNAL_TOKEN', 64, 'env', 'base64'),
    ('GITEA_CLIENT_SECRET', 32, 'env', 'hex'),
    # LLM Manager (#254) — LiteLLM router master key, manager-only.
    ('LITELLM_INTERNAL_KEY', 32, 'env', 'hex'),
    # #3301: bearer key callers of the Ollama-protocol proxy send.
    ('OLLAMA_PROXY_CLIENT_KEY', 32, 'env', 'hex'),
    ('SECRET_KEY', 42, 'dify', 'base64'),
]


# #2927: GOOGLE_OAUTH_DOMAIN is a list of allowed Google Workspace domains (comma
# or space separated). It is rendered into an Authentik policy expression, so an
# entry must be a plain host name; anything else is refused, never written.
_DOMAIN_LABEL = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')


def normalize_domain_list(value):
    """'A.com, b.com' -> 'a.com,b.com'; ValueError on an entry that is no domain name."""
    out = []
    for raw in re.split(r'[,\s]+', (value or '').strip()):
        d = raw.strip().lower().lstrip('@')
        if not d:
            continue
        labels = d.split('.')
        if len(labels) < 2 or not all(_DOMAIN_LABEL.match(x) for x in labels):
            raise ValueError(f"'{raw}' is not a valid domain name (use e.g. example.com)")
        if d not in out:
            out.append(d)
    return ','.join(out)


class ConfigManager:
    def __init__(self, stack_root):
        self.stack_root = stack_root
        self.env_path = os.path.join(stack_root, ENV_FILE_NAME)
        self.dify_env_path = os.path.join(stack_root, DIFY_ENV_FILE_NAME)

    def read_env(self):
        return self._read_env_file(self.env_path)

    def get_all_config(self):
        env = self.read_env()
        profiles = [p.strip() for p in env.get('COMPOSE_PROFILES', '').split(',') if p.strip()]
        tls_raw = env.get('TLS_MODE', '')
        if tls_raw == 'internal':
            tls_mode = 'selfsigned'
        elif tls_raw == 'certificate':
            tls_mode = 'certificate'
        else:
            tls_mode = 'letsencrypt'

        return {
            'domain': env.get('MAIN_DOMAIN', 'Unknown'),
            'timezone': env.get('TZ', 'UTC'),
            'profiles': profiles,
            'tls_mode': tls_mode,
            'gpustack_mode': env.get('GPUSTACK_MODE', 'standalone'),
            'smtp_mode': env.get('SMTP_MODE', 'relay'),
            'smtp_relay_host': env.get('SMTP_RELAY_HOST', ''),
            'smtp_relay_username': env.get('SMTP_RELAY_USERNAME', ''),
            'smtp_from': env.get('SMTP_FROM', ''),
            'google_oauth_enabled': env.get('ENABLE_GOOGLE_OAUTH', 'false') == 'true',
            'entra_oauth_enabled': env.get('ENABLE_ENTRA_OAUTH', 'false') == 'true',
            'openwebui_oidc_enabled': env.get('ENABLE_OPENWEBUI_OIDC', 'false') == 'true',
            'gitea_oidc_enabled': env.get('ENABLE_GITEA_AUTHENTIK_OIDC', 'false') == 'true',
            'letsencrypt_email': env.get('LETSENCRYPT_EMAIL', ''),
        }

    def update_domain(self, new_domain):
        """Update the main domain and every derived stack host (#2376).

        The same whole-host rule as `rzfz setup --set-domain`
        (razzfazz_common.redomain): `<old>` or `<label>.<old>` moves,
        `myold.example` does not; `*_OAUTH_DOMAIN` (the IdP allow-list) and a
        key still templating `${MAIN_DOMAIN}` are left alone. The .env.dify
        `*_URL` / `*_ORIGINS` keys move too, and are written FIRST: MAIN_DOMAIN
        in .env is the "work remains" marker, so a failure between the two
        writes leaves .env on the old domain and a re-run finishes the job.
        Returns {KEY: value}, .env.dify keys as ".env.dify:KEY"."""
        from razzfazz_common.redomain import matrix_pin_updates, redomain_dify_values, redomain_env_domains
        env = self.read_env()
        raw = self._read_env_file_raw(self.env_path)
        old_domain = env.get('MAIN_DOMAIN', '')
        if not new_domain or new_domain == old_domain:
            return {}
        updates = {'MAIN_DOMAIN': new_domain}
        dify = {}
        if old_domain:
            updates.update(redomain_env_domains(env, raw, old_domain, new_domain))
            # #2981: the Matrix server name is permanent; pin it before MAIN_DOMAIN moves.
            updates.update(matrix_pin_updates(env, old_domain, os.path.dirname(os.path.abspath(self.env_path))))
            dify = redomain_dify_values(
                self._read_env_file_raw(self.dify_env_path), old_domain, new_domain)
            if dify:
                self._write_env_file(self.dify_env_path, dify)
        self._write_env_file(self.env_path, updates)
        for key, value in dify.items():
            updates[f'.env.dify:{key}'] = value
        return updates

    def _read_env_file_raw(self, filepath):
        """KEY -> raw value (quotes and `${VAR}` kept), comments skipped: the
        re-domain must tell a templated value from a literal and keep quoting."""
        config = {}
        if not os.path.exists(filepath):
            return config
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                config[key.strip()] = val.strip()
        return config

    def get_domain_tls_config(self):
        env = self.read_env()
        tls_raw = env.get('TLS_MODE', '')
        return {
            'domain': env.get('MAIN_DOMAIN', ''),
            'tls_mode': 'selfsigned' if tls_raw == 'internal' else ('certificate' if tls_raw == 'certificate' else 'letsencrypt'),
            'letsencrypt_email': env.get('LETSENCRYPT_EMAIL', ''),
        }

    def update_domain_tls(self, data):
        updates = {}
        if 'tls_mode' in data:
            mode = data['tls_mode']
            if mode in TLS_MODES:
                updates['TLS_MODE'] = TLS_MODES[mode]
                updates['TLS_DIRECTIVE'] = 'tls internal' if mode == 'selfsigned' else ''
        if 'letsencrypt_email' in data:
            updates['LETSENCRYPT_EMAIL'] = data['letsencrypt_email'].strip()
        if updates:
            self._write_env_file(self.env_path, updates)
        return updates

    # #3044: the relay hostname Vaultwarden dials (same Docker network).
    VAULTWARDEN_SMTP_RELAY = 'smtp-relay'

    def smtp_mail_can_leave(self, env=None):
        """True when the relay can deliver: direct mode, or relay mode with a
        smarthost. Relay mode without a host (the init's "skip") is mail off."""
        env = self.read_env() if env is None else env
        mode = env.get('SMTP_MODE', '')
        if mode == 'direct':
            return True
        return mode == 'relay' and bool(env.get('SMTP_RELAY_HOST', '').strip())

    def sync_vaultwarden_smtp(self):
        """Write VAULTWARDEN_SMTP_HOST from the SMTP config and return it:
        `smtp-relay` when mail can leave, empty (= Vaultwarden mail off) when not.
        Writes only on a change, so an unchanged value leaves .env alone."""
        env = self.read_env()
        want = self.VAULTWARDEN_SMTP_RELAY if self.smtp_mail_can_leave(env) else ''
        if env.get('VAULTWARDEN_SMTP_HOST', '') != want:
            self._write_env_file(self.env_path, {'VAULTWARDEN_SMTP_HOST': want})
        return want

    def get_auth_config(self):
        env = self.read_env()
        return {
            'google_enabled': env.get('ENABLE_GOOGLE_OAUTH', 'false') == 'true',
            'google_client_id': env.get('GOOGLE_CLIENT_ID', ''),
            'google_client_secret_set': bool(env.get('GOOGLE_CLIENT_SECRET', '')),
            'google_domain': env.get('GOOGLE_OAUTH_DOMAIN', ''),
            'entra_enabled': env.get('ENABLE_ENTRA_OAUTH', 'false') == 'true',
            'entra_client_id': env.get('ENTRA_CLIENT_ID', ''),
            'entra_tenant_id': env.get('ENTRA_TENANT_ID', ''),
            'entra_domain': env.get('ENTRA_OAUTH_DOMAIN', ''),
            'entra_client_secret_set': bool(env.get('ENTRA_CLIENT_SECRET', '')),
            'openwebui_oidc_enabled': env.get('ENABLE_OPENWEBUI_OIDC', 'false') == 'true',
            'openwebui_oidc_client_id': env.get('OPENWEBUI_OIDC_CLIENT_ID', ''),
            'openwebui_oidc_secret_set': bool(env.get('OPENWEBUI_OIDC_CLIENT_SECRET', '')),
            'gitea_oidc_enabled': env.get('ENABLE_GITEA_AUTHENTIK_OIDC', 'false') == 'true',
            'gitea_oidc_client_id': env.get('GITEA_OIDC_CLIENT_ID', ''),
            'gitea_oidc_secret_set': bool(env.get('GITEA_OIDC_CLIENT_SECRET', '')),
            'findings': self.sso_switch_findings(env),
        }

    #: #2535 — the bash twin is scripts/lib.sh::derive_owui_sso_env; test_2535 holds them equal.
    OWUI_DERIVED_SSO_KEYS = ('OWUI_GOOGLE_CLIENT_ID', 'OWUI_GOOGLE_CLIENT_SECRET', 'OWUI_OAUTH_AUTO_REDIRECT',
                             'OWUI_OIDC_ACTIVE')

    #: switch -> the keys it cannot work without
    SSO_SWITCH_REQUIRES = {
        'ENABLE_GOOGLE_OAUTH': ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET'),
        'ENABLE_ENTRA_OAUTH': ('ENTRA_CLIENT_ID', 'ENTRA_CLIENT_SECRET', 'ENTRA_TENANT_ID'),
        'ENABLE_OPENWEBUI_OIDC': ('OPENWEBUI_OIDC_CLIENT_ID', 'OPENWEBUI_OIDC_CLIENT_SECRET'),
        'ENABLE_GITEA_AUTHENTIK_OIDC': ('GITEA_OIDC_CLIENT_ID', 'GITEA_OIDC_CLIENT_SECRET'),
    }

    @staticmethod
    def derive_owui_sso_env(env):
        if env.get('ENABLE_OPENWEBUI_OIDC', '') == 'true':
            return {'OWUI_GOOGLE_CLIENT_ID': '', 'OWUI_GOOGLE_CLIENT_SECRET': '',
                    'OWUI_OAUTH_AUTO_REDIRECT': 'true', 'OWUI_OIDC_ACTIVE': 'true'}
        return {'OWUI_GOOGLE_CLIENT_ID': env.get('GOOGLE_CLIENT_ID', ''),
                'OWUI_GOOGLE_CLIENT_SECRET': env.get('GOOGLE_CLIENT_SECRET', ''),
                'OWUI_OAUTH_AUTO_REDIRECT': 'false', 'OWUI_OIDC_ACTIVE': ''}

    @classmethod
    def sso_switch_findings(cls, env):
        out = []
        for switch, needs in cls.SSO_SWITCH_REQUIRES.items():
            if env.get(switch, '') == 'true':
                missing = [k for k in needs if not env.get(k, '')]
                if missing:
                    out.append((switch, f"on, but {', '.join(missing)} empty \u2014 the switch has no effect"))
        want = cls.derive_owui_sso_env(env)
        drift = [k for k, v in want.items() if env.get(k) != v]
        if drift:
            out.append(('OWUI_*', f"Open WebUI's login keys ({', '.join(drift)}) disagree with the switches \u2014 not applied yet"))
        return out

    #: Auth-form keys Open WebUI reads directly from its environment
    #: (modules/chat/compose.yml OAUTH_CLIENT_ID/SECRET). The rest reach it
    #: through the derived OWUI_* keys.
    OWUI_READ_INPUTS = ('OPENWEBUI_OIDC_CLIENT_ID', 'OPENWEBUI_OIDC_CLIENT_SECRET')

    def update_auth(self, data):
        """Write the Auth keys AND the derived OWUI_* keys in ONE in-place write.

        Returns written, derived_changed (derived keys to write) and owui_changed
        (keys Open WebUI reads whose VALUE changed: a recreate is needed only then).
        """
        written = dict(data)
        env = self.read_env()
        merged = {**env, **written}
        want = self.derive_owui_sso_env(merged)
        changed = {k: v for k, v in want.items() if merged.get(k) != v or k not in merged}
        owui = {k for k, v in want.items() if env.get(k, '') != v}
        owui |= {k for k in self.OWUI_READ_INPUTS if k in written and env.get(k, '') != written[k]}
        if written or changed:
            self._write_env_file(self.env_path, {**written, **changed})
        return {'written': written, 'derived_changed': sorted(changed), 'owui_changed': sorted(owui)}

    def get_smtp_config(self):
        env = self.read_env()
        return {
            'mode': env.get('SMTP_MODE', 'relay'),
            'from': env.get('SMTP_FROM', ''),
            'relay_host': env.get('SMTP_RELAY_HOST', ''),
            'relay_port': env.get('SMTP_RELAY_PORT', '587'),
            'relay_username': env.get('SMTP_RELAY_USERNAME', ''),
            'relay_password_set': bool(env.get('SMTP_RELAY_PASSWORD', '')),
        }

    def update_smtp(self, data):
        updates = {}
        mode = data.get('mode', 'relay')
        if mode in SMTP_MODES:
            updates['SMTP_MODE'] = mode
        if 'from' in data:
            updates['SMTP_FROM'] = data['from'].strip()
        if mode == 'relay':
            for key in ('relay_host', 'relay_port', 'relay_username'):
                if key in data:
                    env_key = 'SMTP_' + key.upper()
                    updates[env_key] = data[key].strip()
            if data.get('relay_password', '').strip():
                updates['SMTP_RELAY_PASSWORD'] = data['relay_password'].strip()
        elif mode == 'direct':
            updates['SMTP_RELAY_HOST'] = ''
            updates['SMTP_RELAY_USERNAME'] = ''
            updates['SMTP_RELAY_PASSWORD'] = ''
        if updates:
            self._write_env_file(self.env_path, updates)
        return updates

    # #2521 follow-up (K4): Open WebUI's user-header forwarding. The compose
    # line is `ENABLE_FORWARD_USER_INFO_HEADERS=${OWUI_FORWARD_USER_INFO_HEADERS:-true}`
    # (modules/chat/compose.yml), so an absent or EMPTY value means ON; and
    # Open WebUI 0.11.3 (backend/open_webui/env.py) turns it on only for
    # `.lower() == "true"` — so `1`/`yes`/`on` are OFF there. The page must
    # show what the container will actually do.
    OWUI_FORWARD_HEADERS_KEY = 'OWUI_FORWARD_USER_INFO_HEADERS'

    def get_chat_config(self):
        raw = self.read_env().get(self.OWUI_FORWARD_HEADERS_KEY, '')
        value = str(raw).strip().lower()
        return {
            'forward_user_info_headers': value in ('', 'true'),
            'forward_user_info_headers_raw': raw,
        }

    def set_chat_forward_user_info_headers(self, enabled):
        """Write the key as an explicit `true`/`false` — never empty, which the
        compose fallback would read as ON."""
        self._write_env_file(self.env_path, {
            self.OWUI_FORWARD_HEADERS_KEY: 'true' if enabled else 'false'})

    # Code and tools in Open WebUI (code interpreter, direct tool servers, user
    # tools): read-only. The values live in Open WebUI's own config table and
    # are changed in Open WebUI (Admin Settings); a new install starts with all
    # of them off (modules/chat/compose.yml). The one reader is
    # scripts/owui_config_reconcile.py code-tools, which the upgrade report uses too.
    def get_owui_code_tools(self, timeout=20):
        env = self.read_env()
        script = os.path.join(self.stack_root, 'scripts', 'owui_config_reconcile.py')
        argv = [sys.executable, script, 'code-tools', '--json',
                '--db', env.get('OPENWEBUI_DB') or 'openwebui_db',
                '--pg-user', env.get('POSTGRES_USER') or 'docker']
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {'items': [], 'error': 'Open WebUI settings could not be read (timeout).'}
        if proc.returncode != 0:
            return {'items': [], 'error': 'Open WebUI settings could not be read: is the Chat module running?'}
        try:
            items = json.loads(proc.stdout.strip().splitlines()[-1]).get('items') or []
        except (ValueError, IndexError, AttributeError):
            return {'items': [], 'error': 'Open WebUI settings could not be read (unexpected answer).'}
        return {'items': items, 'error': ''}

    def get_gpustack_config(self):
        env = self.read_env()
        key = env.get('GPUSTACK_API_KEY', '')
        # #1450 (C10b / D7) re-review, finding 5: `mode` and `master_url` used
        # to be part of this payload. No template reads them any more — the
        # page lost its mode section — and handing a view fields it cannot show
        # invites the next author to put the section back. GPUSTACK_MODE itself
        # is NOT dead on the box (modules/llm/compose.yml still reads it, and
        # cli/init.sh still writes it on a `worker-box` preset); it is dead in
        # the PORTAL, which is what this method serves.
        return {
            'api_key_set': bool(key),
            'api_key_masked': f'{key[:12]}...{key[-4:]}' if len(key) > 16 else ('(not set)' if not key else key),
            # rc6.7 #11: full key surfaced for the copy-to-clipboard button.
            # The page is already Authentik-gated to admins; the masked
            # form stays on-screen, only the clipboard copy uses the full
            # value. Empty string when no key is set so the JS can detect
            # the no-op case.
            'api_key_full': key,
        }

    def update_gpustack(self, data):
        """Write the GPUStack backend settings the portal still offers.

        #1450 (C10b / D7) review: the handler stopped PASSING `mode`,
        `master_url` and `master_token`, but this function still accepted them
        — so the refusal lived one level above the write. Measured: a single
        line in the handler (`data.update(request.form.to_dict())`) brought all
        three back, and every test stayed green. The mode question is gone in
        2026.09: GPUStack is an optional BACKEND behind the LLM Manager
        (#1442), and a master/worker choice here would compete with the
        manager's own worker enrolment. So the refusal belongs HERE, where the
        write happens, and the ignored keys are NAMED IN THE LOG rather than
        dropped silently — a caller that still sends them is a caller that has
        not noticed the cutover.

        Re-review: this said "named in the return value". They are not, and
        must not be: the return value is the set of keys that were WRITTEN, and
        `test_update_gpustack_writes_the_key_and_nothing_else` pins it to
        exactly `{'GPUSTACK_API_KEY': …}`. Putting the refusal in there would
        have made a caller's "what did you change?" answer include things that
        were not changed.
        """
        updates = {}
        retired = [k for k in ('mode', 'master_url', 'master_token') if k in data]
        if retired:
            logger.warning(
                "update_gpustack: ignoring retired key(s) %s — GPUStack is a "
                "backend behind the LLM Manager since 2026.09 (#1450/#1442); "
                "worker enrolment happens in the manager's console",
                ", ".join(sorted(retired)))
        if 'api_key' in data and data['api_key'].strip():
            updates['GPUSTACK_API_KEY'] = data['api_key'].strip()
        if updates:
            self._write_env_file(self.env_path, updates)
        return updates

    # ------------------------------------------------------------------
    # Personal Agents (global limits — stored in .env)
    # ------------------------------------------------------------------

    def get_agents_config(self):
        env = self.read_env()
        return {
            'max_instances': env.get('AGENT_MAX_INSTANCES', '20'),
            'idle_timeout_lightweight': env.get('AGENT_IDLE_TIMEOUT_LIGHTWEIGHT', '1800'),
            'idle_timeout_heavy': env.get('AGENT_IDLE_TIMEOUT_HEAVY', '7200'),
            'cleanup_after_days': env.get('AGENT_CLEANUP_AFTER_DAYS', '30'),
        }

    def update_agents_config(self, data):
        updates = {}
        for form_key, env_key in (
            ('max_instances', 'AGENT_MAX_INSTANCES'),
            ('idle_timeout_lightweight', 'AGENT_IDLE_TIMEOUT_LIGHTWEIGHT'),
            ('idle_timeout_heavy', 'AGENT_IDLE_TIMEOUT_HEAVY'),
            ('cleanup_after_days', 'AGENT_CLEANUP_AFTER_DAYS'),
        ):
            val = data.get(form_key, '').strip()
            if val:
                updates[env_key] = val
        if updates:
            self._write_env_file(self.env_path, updates)
        return updates

    # ------------------------------------------------------------------
    # RAG model configuration (Cognee + LightRAG) — migrated from the
    # deprecated razzfazz-setup web container (#22). Both engines pick
    # their LLM/embedding/reranker models from .env; this is the portal
    # surface for editing them. Cognee uses LLM_PROVIDER=custom → litellm,
    # so its LLM model needs a `<provider>/<model>` prefix (e.g.
    # `openai/qwen3.6`); LightRAG uses the OpenAI SDK directly (bare name).
    # ------------------------------------------------------------------

    def get_rag_config(self):
        env = self.read_env()
        return {
            'cognee': {
                'llm_model': env.get('COGNEE_LLM_MODEL', ''),
                'embedding_model': env.get('COGNEE_EMBEDDING_MODEL', ''),
                'embedding_dim': env.get('COGNEE_EMBEDDING_DIM', '4096'),
                'is_configured': bool(env.get('COGNEE_LLM_MODEL')) and bool(env.get('COGNEE_EMBEDDING_MODEL')),
            },
            'lightrag': {
                'llm_model': env.get('LIGHTRAG_LLM_MODEL', ''),
                'embedding_model': env.get('LIGHTRAG_EMBEDDING_MODEL', ''),
                'embedding_dim': env.get('LIGHTRAG_EMBEDDING_DIM', '768'),
                'rerank_binding': env.get('LIGHTRAG_RERANK_BINDING', 'null'),
                'rerank_model': env.get('LIGHTRAG_RERANK_MODEL', ''),
                'is_configured': bool(env.get('LIGHTRAG_LLM_MODEL')) and bool(env.get('LIGHTRAG_EMBEDDING_MODEL')),
            },
        }

    def set_cognee_models(self, llm_model, embedding_model, embedding_dim='768'):
        self._write_env_file(self.env_path, {
            'COGNEE_LLM_MODEL': llm_model.strip(),
            'COGNEE_EMBEDDING_MODEL': embedding_model.strip(),
            'COGNEE_EMBEDDING_DIM': str(embedding_dim).strip() or '4096',
        })

    def set_lightrag_models(self, llm_model, embedding_model,
                            rerank_binding='null', rerank_model=''):
        self._write_env_file(self.env_path, {
            'LIGHTRAG_LLM_MODEL': llm_model.strip(),
            'LIGHTRAG_EMBEDDING_MODEL': embedding_model.strip(),
            'LIGHTRAG_RERANK_BINDING': (rerank_binding or 'null').strip(),
            'LIGHTRAG_RERANK_MODEL': rerank_model.strip(),
        })

    def update_env_var(self, key, value):
        """Update a single .env variable."""
        self._write_env_file(self.env_path, {key: value})

    def update_env_vars(self, updates):
        """#3430: several .env variables in one in-place write. Raises
        ValueError, writing nothing, when a key or value could break out of
        its line (callers validate shapes; this is the last line of defence)."""
        for key, value in updates.items():
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', str(key)) or re.search(r'[\s"\'$#\\]', str(value)):
                raise ValueError(f'refusing to write {key!r}: unsafe key or value')
        if updates:
            self._write_env_file(self.env_path, dict(updates))

    def read_dify_env(self):
        """Read .env.dify as a dict (inline comments stripped)."""
        return self._read_env_file(self.dify_env_path)

    def update_dify_env_var(self, key, value):
        """Update a single .env.dify variable."""
        self._write_env_file(self.dify_env_path, {key: value})

    DB_SECRETS = {'POSTGRES_PASSWORD', 'VALKEY_PASSWORD'}

    def get_secrets_status(self):
        env = self.read_env()
        dify_env = self._read_env_file(self.dify_env_path)
        placeholder_indicators = ['changeme', 'replace', 'example', 'default', 'your-', 'generate']
        status = {}
        for key, length, target, fmt in SECRETS_CONFIG:
            value = env.get(key, '') if target == 'env' else dify_env.get(key, '')
            is_placeholder = not value or len(value) < 16 or any(i in value.lower() for i in placeholder_indicators)
            status[key] = {
                'length': len(value),
                'needs_regeneration': is_placeholder,
                'is_set': bool(value),
                'format': fmt,
                'is_db': key in self.DB_SECRETS,
            }
        return status

    @staticmethod
    def _dify_valkey_copies(env_updates):
        """The .env.dify keys that carry a copy of VALKEY_PASSWORD.

        Dify's REDIS_PASSWORD and the password inside CELERY_BROKER_URL follow the
        stack value; rotating one without the others leaves the Dify workers
        crash-looping on "invalid username-password pair".
        """
        pw = env_updates.get('VALKEY_PASSWORD')
        if not pw:
            return {}
        return {'REDIS_PASSWORD': pw,
                'CELERY_BROKER_URL': f'redis://:{pw}@valkey:6379/1'}

    def regenerate_secrets(self, scope='application'):
        """Regenerate secrets by scope: 'application' (safe), 'database' (danger), or 'all'.

        Fix for S03-BUG-02 (MEDIUM, 2026-05-15): unknown-scope guard. Previously
        any scope value other than 'application' or 'database' fell through
        the two skip-clauses below and rotated EVERY secret — including
        POSTGRES_PASSWORD and VALKEY_PASSWORD — without coordinated
        DB-side change, leaving the stack inoperable. A typo from the UI
        (e.g. 'dataase') silently triggered the catastrophic path.
        """
        if scope not in ('application', 'database', 'all'):
            raise ValueError(
                f"unknown scope: {scope!r}; expected 'application', "
                "'database', or 'all'"
            )
        env_updates = {}
        dify_updates = {}
        for key, length, target, fmt in SECRETS_CONFIG:
            is_db = key in self.DB_SECRETS
            if scope == 'application' and is_db:
                continue
            if scope == 'database' and not is_db:
                continue
            # scope == 'all' falls through to rotate everything (intentional).
            if fmt == 'hex':
                val = secrets.token_hex(length)
            elif fmt == 'password':
                alphabet = string.ascii_letters + string.digits
                val = ''.join(secrets.choice(alphabet) for _ in range(length))
            else:
                val = base64.b64encode(secrets.token_bytes(length)).decode()
            if target == 'env':
                env_updates[key] = val
            else:
                dify_updates[key] = val
        count = len(env_updates) + len(dify_updates)  # rotated secrets, not the synced copies
        dify_updates.update(self._dify_valkey_copies(env_updates))
        if env_updates:
            self._write_env_file(self.env_path, env_updates)
        if dify_updates:
            self._write_env_file(self.dify_env_path, dify_updates)
        return count

    def regenerate_all_secrets(self):
        env_updates = {}
        dify_updates = {}
        for key, length, target, fmt in SECRETS_CONFIG:
            if fmt == 'hex':
                val = secrets.token_hex(length)
            elif fmt == 'password':
                alphabet = string.ascii_letters + string.digits
                val = ''.join(secrets.choice(alphabet) for _ in range(length))
            else:
                val = base64.b64encode(secrets.token_bytes(length)).decode()
            if target == 'env':
                env_updates[key] = val
            else:
                dify_updates[key] = val
        count = len(env_updates) + len(dify_updates)  # rotated secrets, not the synced copies
        dify_updates.update(self._dify_valkey_copies(env_updates))
        if env_updates:
            self._write_env_file(self.env_path, env_updates)
        if dify_updates:
            self._write_env_file(self.dify_env_path, dify_updates)
        return count

    def regenerate_single_secret(self, key_name):
        """Regenerate a single secret by key name. Returns 1 on success, 0 if not found."""
        for key, length, target, fmt in SECRETS_CONFIG:
            if key != key_name:
                continue
            if fmt == 'hex':
                val = secrets.token_hex(length)
            elif fmt == 'password':
                alphabet = string.ascii_letters + string.digits
                val = ''.join(secrets.choice(alphabet) for _ in range(length))
            else:
                val = base64.b64encode(secrets.token_bytes(length)).decode()
            filepath = self.env_path if target == 'env' else self.dify_env_path
            self._write_env_file(filepath, {key: val})
            if key == 'VALKEY_PASSWORD':
                self._write_env_file(self.dify_env_path,
                                     self._dify_valkey_copies({key: val}))
            return 1
        return 0

    def _read_env_file(self, filepath):
        # rc6.7: route through shared env_utils (strips inline comments).
        from .env_utils import parse_env_file
        config = parse_env_file(filepath)
        def _expand(val, cfg):
            def replacer(m):
                inner = m.group(1)
                if ':-' in inner:
                    var, default = inner.split(':-', 1)
                    return cfg.get(var, default)
                return cfg.get(inner, m.group(0))
            return re.sub(r'\$\{([^}]+)\}', replacer, val)
        return {k: _expand(v, config) for k, v in config.items()}

    def _write_env_file(self, filepath, updates):
        if not os.path.exists(filepath):
            lines = []
        else:
            with open(filepath) as f:
                lines = f.readlines()
        new_lines = []
        keys_updated = set()
        for line in lines:
            if line.strip().startswith('#') or '=' not in line:
                new_lines.append(line)
                continue
            key = line.split('=')[0].strip()
            if key in updates:
                new_lines.append(f'{key}={updates[key]}\n')
                keys_updated.add(key)
            else:
                new_lines.append(line)
        for key, val in updates.items():
            if key not in keys_updated:
                new_lines.append(f'{key}={val}\n')
        # Truncate IN PLACE (same inode): the file is a single-file bind mount
        # in the razzfazz-config container; a tmp+rename would fail here and,
        # on the host, would break the container's bind (#1189).
        with env_write_guard(filepath):
            with open(filepath, 'w') as f:
                f.writelines(new_lines)
