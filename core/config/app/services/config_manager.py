# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Config manager — .env reader/writer ported from core/setup/app/config_manager.py."""

import logging
import base64
import os
import re
import secrets
import string
import subprocess
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
    ('SECRET_KEY', 42, 'dify', 'base64'),
]


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
        """Update the main domain and all derived *_DOMAIN variables."""
        env = self.read_env()
        old_domain = env.get('MAIN_DOMAIN', '')
        if new_domain == old_domain:
            return {}
        updates = {'MAIN_DOMAIN': new_domain}
        # Update all derived domain vars that reference MAIN_DOMAIN
        domain_vars = [k for k in env if k.endswith('_DOMAIN') and k != 'MAIN_DOMAIN']
        for var in domain_vars:
            val = env.get(var, '')
            if old_domain and old_domain in val:
                updates[var] = val.replace(old_domain, new_domain)
        self._write_env_file(self.env_path, updates)
        return updates

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

    def get_auth_config(self):
        env = self.read_env()
        return {
            'google_enabled': env.get('ENABLE_GOOGLE_OAUTH', 'false') == 'true',
            'google_client_id': env.get('GOOGLE_CLIENT_ID', ''),
            'google_client_secret_set': bool(env.get('GOOGLE_CLIENT_SECRET', '')),
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
        }

    def update_auth(self, data):
        updates = {}
        for key, val in data.items():
            updates[key] = val
        if updates:
            self._write_env_file(self.env_path, updates)
        return updates

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
        if env_updates:
            self._write_env_file(self.env_path, env_updates)
        if dify_updates:
            self._write_env_file(self.dify_env_path, dify_updates)
        return len(env_updates) + len(dify_updates)

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
        if env_updates:
            self._write_env_file(self.env_path, env_updates)
        if dify_updates:
            self._write_env_file(self.dify_env_path, dify_updates)
        return len(env_updates) + len(dify_updates)

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
