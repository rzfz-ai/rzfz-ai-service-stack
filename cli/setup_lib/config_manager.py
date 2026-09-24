# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import fcntl
import os
import re
import shutil
import subprocess
import threading
import time
import secrets
import base64
import string

# #22: STACK_ROOT is configurable so this CLI backend runs on the HOST
# (after the razzfazz-setup web container was removed). Defaults to /stack
# for any legacy in-container invocation; cli/setup.sh exports
# RAZZFAZZ_STACK_ROOT=<repo root> when run on the host.
STACK_ROOT = os.environ.get("RAZZFAZZ_STACK_ROOT", "/stack")
ENV_FILE = os.path.join(STACK_ROOT, ".env")
DIFY_ENV_FILE = os.path.join(STACK_ROOT, ".env.dify")
LOG_FILE = "/var/log/setup.log"

# Profiles to show/edit
# #446/#449: PROFILES is DERIVED from the compose files, not hand-maintained.
# The 26-entry literal this replaces offered five phantoms (llm-box,
# llm-experimental, hermes, moltis, coding-tools) and was missing 32 real
# profiles — and update_config rebuilt COMPOSE_PROFILES from it, silently
# dropping anything not listed (agents, llm-legacy, observability, ...).
# Deriving at import keeps every surface honest with zero maintenance; the
# authority test (test_profile_authority.py) pins the parity.
import re as _re


def _discover_compose_profiles(stack_root=None):
    """Every profile token any tracked compose file declares."""
    root = stack_root or STACK_ROOT
    found = set()
    patterns = ("compose*.yml", "core/compose*.yml", "modules/**/compose*.yml",
                "llm/compose*.yml")
    import glob as _glob
    files = []
    for pat in patterns:
        files += _glob.glob(os.path.join(root, pat), recursive=True)
    for f in files:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        # inline form: profiles: ["a", "b"] / ['a']
        for m in _re.finditer(r'profiles:\s*\[([^\]]*)\]', text):
            for tok in m.group(1).split(','):
                tok = tok.strip().strip('"\'')
                if tok:
                    found.add(tok)
        # block form:
        #   profiles:
        #     - a
        for m in _re.finditer(r'profiles:\s*\n((?:\s+-\s+[^\n]+\n)+)', text):
            for line in m.group(1).splitlines():
                tok = line.strip().lstrip('-').strip().strip('"\'')
                if tok:
                    found.add(tok)
    return sorted(found)


PROFILES = _discover_compose_profiles()
if not PROFILES:
    # Fallback ONLY for environments without the compose tree (unit shims).
    # #642 review: loud, never silent — on a real box an empty discovery
    # means the scan broke, and a quiet 6-entry list would resurrect the
    # #449 silent-drop class through the back door.
    import sys as _sys
    print("WARNING: compose-profile discovery found NOTHING under "
          f"{STACK_ROOT!r} — falling back to a minimal profile list. "
          "On a real box this is a bug; profile toggles may be incomplete.",
          file=_sys.stderr)
    PROFILES = ["chat", "dify", "llm-legacy", "monitor"]


def _load_mutual_exclusions(stack_root=None):
    """profile -> [excluded profiles], from core/config/profiles.yaml — the
    SAME source the Configuration Portal enforces (#465): one authority, no
    CLI/Portal drift."""
    root = stack_root or STACK_ROOT
    path = os.path.join(root, "core", "config", "profiles.yaml")
    excl = {}
    try:
        import yaml as _yaml
        doc = _yaml.safe_load(open(path, encoding="utf-8")) or {}
        for name, cfg in (doc.get("profiles") or {}).items():
            if isinstance(cfg, dict) and cfg.get("mutual_exclusion"):
                excl[name] = list(cfg["mutual_exclusion"])
    except Exception:
        pass   # no yaml / no file: no exclusions (portal-less shim envs)
    return excl

# Valid TLS modes
TLS_MODES = {
    "letsencrypt": "",           # Empty = Let's Encrypt in Caddy
    "selfsigned": "internal",    # internal = self-signed in Caddy
    "certificate": "certificate", # Custom wildcard certificate
}

# Valid GPUStack modes (bind address per mode, must match razzfazz-init.sh)
GPUSTACK_MODES = {
    "standalone": "127.0.0.1",
    "master": "0.0.0.0",
    "worker": "0.0.0.0",
}

# Valid authentication scenarios
AUTH_SCENARIOS = ["base", "google"]

# Valid SMTP modes
SMTP_MODES = ["relay", "direct"]

# *_DOMAIN keys that are NOT stack subdomains and must survive `--set-domain`.
# These hold the customer's IdP tenant / login allow-list domain; rewriting one
# to the new stack domain locks every SSO user out. The `_OAUTH_DOMAIN` suffix
# rule in update_domain covers future additions; this set is the explicit,
# greppable record of the two that ship today.
NON_STACK_DOMAIN_VARS = {
    "GOOGLE_OAUTH_DOMAIN",
    "ENTRA_OAUTH_DOMAIN",
}

# Secrets configuration: (key_name, length_bytes, target_file, format)
# format: 'base64' (openssl rand -base64), 'hex' (openssl rand -hex), 'password' (alphanumeric)
SECRETS_CONFIG = [
    # .env secrets
    ("WEBUI_SECRET_KEY", 32, ENV_FILE, "hex"),
    ("AUTHENTIK_SECRET_KEY", 42, ENV_FILE, "base64"),
    ("AUTHENTIK_BOOTSTRAP_TOKEN", 48, ENV_FILE, "base64"),
    ("POSTGRES_PASSWORD", 24, ENV_FILE, "password"),
    ("VALKEY_PASSWORD", 24, ENV_FILE, "password"),
    ("GPUSTACK_SECRET_KEY", 32, ENV_FILE, "base64"),
    ("PIPELINES_API_KEY", 32, ENV_FILE, "base64"),
    ("KOMODO_PASSKEY", 24, ENV_FILE, "password"),
    ("CHAT_CLIENT_SECRET", 64, ENV_FILE, "base64"),
    ("DIFY_CLIENT_SECRET", 64, ENV_FILE, "base64"),
    ("ADMIN_CLIENT_SECRET", 64, ENV_FILE, "base64"),
    ("LLM_CLIENT_SECRET", 64, ENV_FILE, "base64"),
    ("BACKUP_CLIENT_SECRET", 32, ENV_FILE, "hex"),
    ("COGNEE_JWT_SECRET", 32, ENV_FILE, "hex"),  # #1806: cognee fastapi-users JWT signing secret
    ("PLUGIN_DAEMON_KEY", 42, ENV_FILE, "base64"),
    ("PLUGIN_DIFY_INNER_API_KEY", 42, ENV_FILE, "base64"),
    ("GITEA_SECRET_KEY", 32, ENV_FILE, "hex"),
    ("GITEA_INTERNAL_TOKEN", 64, ENV_FILE, "base64"),
    ("GITEA_CLIENT_SECRET", 32, ENV_FILE, "hex"),
    # LLM Manager (#254) — LiteLLM router master key, manager-only.
    ("LITELLM_INTERNAL_KEY", 32, ENV_FILE, "hex"),
    # .env.dify secrets
    ("SECRET_KEY", 42, DIFY_ENV_FILE, "base64"),
    # INIT_PASSWORD is excluded from security checks - it's only used during
    # initial Dify setup and is irrelevant after the first login.
]

class ConfigManager:
    def __init__(self):
        self._last_snapshot_time = 0
    
    def _snapshot_env(self, filepath, updates):
        """Create encrypted .env snapshot before modification (max once per 5 seconds)."""
        import time as _time
        now = _time.time()
        # Debounce: skip if snapshot was taken less than 5 seconds ago
        if now - self._last_snapshot_time < 5:
            return
        self._last_snapshot_time = now
        
        try:
            reason = f"pre-change: {','.join(list(updates.keys())[:5])}"
            snapshot_dir = os.path.join(STACK_ROOT, "backups", "env-snapshots")
            if not os.access(os.path.join(STACK_ROOT, "backups"), os.W_OK):
                snapshot_dir = os.path.join(STACK_ROOT, ".gsd", "env-snapshots")
            os.makedirs(snapshot_dir, exist_ok=True)
            
            env_config = self._read_env_file(ENV_FILE)
            password = env_config.get("BACKUP_ENCRYPTION_PASSWORD") or env_config.get("AUTHENTIK_BOOTSTRAP_PASSWORD", "")
            
            if not password:
                return
            
            timestamp = _time.strftime("%Y%m%d-%H%M%S")
            snapshot_name = f"env-{timestamp}"
            snapshot_file = f"{snapshot_dir}/{snapshot_name}.tar.gz.enc"
            
            # Build tar of .env files and encrypt with piped subprocesses
            tar_args = ['tar', 'czf', '-', '-C', STACK_ROOT, '.env']
            if os.path.exists(DIFY_ENV_FILE):
                tar_args.append('.env.dify')
            tar_proc = subprocess.Popen(
                tar_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            result = subprocess.run(
                ['openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2',
                 '-iter', '100000', '-pass', f'pass:{password}',
                 '-out', snapshot_file],
                stdin=tar_proc.stdout, capture_output=True, timeout=10
            )
            tar_proc.stdout.close()
            tar_proc.wait(timeout=5)
            
            if result.returncode == 0:
                with open(f"{snapshot_dir}/{snapshot_name}.reason", "w") as f:
                    f.write(reason)
        except Exception:
            pass  # Never fail the actual config write because of snapshot

    @staticmethod
    def _generate_secret(length_bytes: int) -> str:
        """Generate a cryptographically secure base64-encoded secret."""
        return base64.b64encode(secrets.token_bytes(length_bytes)).decode('utf-8')

    @staticmethod
    def _generate_hex_secret(length_bytes: int) -> str:
        """Generate a cryptographically secure hex-encoded secret."""
        return secrets.token_hex(length_bytes)

    @staticmethod
    def _generate_password(length: int = 24) -> str:
        """Generate a secure alphanumeric password."""
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def regenerate_all_secrets(self) -> dict:
        """
        Regenerate all secrets and passwords in .env and .env.dify files.
        Returns a dict with the regenerated secrets for display/confirmation.
        """
        regenerated = {}
        env_updates = {}
        dify_updates = {}

        for key_name, length_bytes, target_file, fmt in SECRETS_CONFIG:
            # Generate secret in the correct format matching razzfazz-init.sh
            if fmt == 'hex':
                new_value = self._generate_hex_secret(length_bytes)
            elif fmt == 'password':
                new_value = self._generate_password(length_bytes)
            else:
                new_value = self._generate_secret(length_bytes)
            
            regenerated[key_name] = new_value
            
            if target_file == ENV_FILE:
                env_updates[key_name] = new_value
            elif target_file == DIFY_ENV_FILE:
                dify_updates[key_name] = new_value

        # Write updates to respective files
        if env_updates:
            self._write_env_file(ENV_FILE, env_updates)
        if dify_updates:
            self._write_env_file(DIFY_ENV_FILE, dify_updates)

        return regenerated

    def get_secrets_status(self) -> dict:
        """
        Check if secrets appear to be default/placeholder values.
        Returns dict with secret names and whether they need regeneration.
        """
        env_config = self._read_env_file(ENV_FILE)
        dify_config = self._read_env_file(DIFY_ENV_FILE)
        
        status = {}
        placeholder_indicators = ['changeme', 'replace', 'example', 'default', 'your-', 'generate']
        
        for key_name, _, target_file, _ in SECRETS_CONFIG:
            if target_file == ENV_FILE:
                value = env_config.get(key_name, '')
            else:
                value = dify_config.get(key_name, '')
            
            # Check if value looks like a placeholder
            is_placeholder = (
                not value or 
                len(value) < 16 or
                any(indicator in value.lower() for indicator in placeholder_indicators)
            )
            status[key_name] = {
                'current_length': len(value),
                'needs_regeneration': is_placeholder,
                'is_set': bool(value)
            }
        
        return status

    def _read_env_file_raw(self, filepath):
        """Same parse as _read_env_file WITHOUT the ${VAR} expansion.

        update_domain needs to tell `chat.${MAIN_DOMAIN}` (already propagating;
        must not be rewritten) from a hard-coded `chat.old.example.com`, and the
        expanded view cannot: both come back as the same literal.
        """
        config = {}
        if not os.path.exists(filepath):
            return config
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    config[key.strip()] = val.strip()
        return config

    def _read_env_file(self, filepath):
        config = {}
        if not os.path.exists(filepath):
            return config
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    config[key.strip()] = val.strip()
        # Second pass: expand ${VAR} references using values in the same file
        def _expand(val, cfg):
            def replacer(m):
                inner = m.group(1)
                if ':-' in inner:
                    var, default = inner.split(':-', 1)
                    return cfg.get(var, default)
                return cfg.get(inner, m.group(0))
            return re.sub(r'\$\{([^}]+)\}', replacer, val)
        config = {k: _expand(v, config) for k, v in config.items()}
        return config

    def _write_env_file(self, filepath, updates):
        # Create encrypted snapshot before modifying .env
        self._snapshot_env(filepath, updates)

        # Use file locking to prevent TOCTOU races on concurrent writes
        lock_path = filepath + '.lock'
        with open(lock_path, 'w') as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                # Read existing to preserve comments/order
                if not os.path.exists(filepath):
                    lines = []
                else:
                    with open(filepath, 'r') as f:
                        lines = f.readlines()

                new_lines = []
                keys_updated = set()

                for line in lines:
                    if line.strip().startswith('#') or '=' not in line:
                        new_lines.append(line)
                        continue

                    key = line.split('=')[0].strip()
                    if key in updates:
                        new_lines.append(f"{key}={updates[key]}\n")
                        keys_updated.add(key)
                    else:
                        new_lines.append(line)

                # Add new keys
                for key, val in updates.items():
                    if key not in keys_updated:
                        new_lines.append(f"{key}={val}\n")

                # Stage, fsync, THEN copy over the original. `open(w)` +
                # writelines truncates in place: ENOSPC, SIGKILL or an
                # exception mid-write leaves a truncated .env, and the only
                # recovery is the encrypted snapshot whose own failure path is
                # `except Exception: pass`. The copy-over (rather than a
                # rename) is deliberate — razzfazz-config bind-mounts this
                # single file, so the INODE must be preserved; scripts/lib.sh
                # solves the same constraint the same way (`sed > tmp; cat tmp
                # > file`). Staging makes the destructive step a single
                # already-durable copy instead of the whole serialization.
                tmp_path = filepath + '.tmp'
                with open(tmp_path, 'w') as tf:
                    tf.writelines(new_lines)
                    tf.flush()
                    os.fsync(tf.fileno())
                try:
                    with open(tmp_path, 'r') as src, open(filepath, 'w') as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def get_all_config(self):
        # Read .env
        env_config = self._read_env_file(ENV_FILE)
        
        # Read Profiles
        active_profiles = env_config.get('COMPOSE_PROFILES', '').split(',')
        active_profiles = [p.strip() for p in active_profiles if p.strip()]

        return {
            "domain": env_config.get("MAIN_DOMAIN", "Unknown"),
            "timezone": env_config.get("TZ", "UTC"),
            "profiles": active_profiles,
            "dify_enabled": "dify" in active_profiles,
            "chat_enabled": "chat" in active_profiles,
            "tls_mode": "selfsigned" if env_config.get("TLS_MODE") == "internal" else ("certificate" if env_config.get("TLS_MODE") == "certificate" else "letsencrypt"),
            "gpustack_mode": env_config.get("GPUSTACK_MODE", "standalone"),
            "auth_scenario": env_config.get("DEPLOYMENT_SCENARIO", "base"),
            "google_oauth_enabled": env_config.get("ENABLE_GOOGLE_OAUTH", "false") == "true",
            "entra_oauth_enabled": env_config.get("ENABLE_ENTRA_OAUTH", "false") == "true",
            "entra_tenant_id": env_config.get("ENTRA_TENANT_ID", ""),
            "openwebui_oidc_enabled": env_config.get("ENABLE_OPENWEBUI_OIDC", "false") == "true",
            "gitea_oidc_enabled": env_config.get("ENABLE_GITEA_AUTHENTIK_OIDC", "false") == "true",
            "smtp_mode": env_config.get("SMTP_MODE", "relay"),
            "smtp_from": env_config.get("SMTP_FROM", ""),
            "smtp_relay_host": env_config.get("SMTP_RELAY_HOST", ""),
            "smtp_configured": bool(env_config.get("SMTP_RELAY_USERNAME")) if env_config.get("SMTP_MODE", "relay") == "relay" else True,
        }

    def get_editable_config(self):
        env_config = self._read_env_file(ENV_FILE)
        # Returns dict structure useful for the wizard
        return {
            "global": {
                "MAIN_DOMAIN": env_config.get("MAIN_DOMAIN", "localhost"),
                "TZ": env_config.get("TZ", "UTC"),
                "admin_email": env_config.get("LETSENCRYPT_EMAIL", ""),
                "TLS_MODE": "selfsigned" if env_config.get("TLS_MODE") == "internal" else ("certificate" if env_config.get("TLS_MODE") == "certificate" else "letsencrypt"),
                "GPUSTACK_MODE": env_config.get("GPUSTACK_MODE", "standalone"),
                "DEPLOYMENT_SCENARIO": env_config.get("DEPLOYMENT_SCENARIO", "base"),
                "GOOGLE_CLIENT_ID": env_config.get("GOOGLE_CLIENT_ID", ""),
                "GOOGLE_CLIENT_SECRET": env_config.get("GOOGLE_CLIENT_SECRET", ""),
                "ENABLE_ENTRA_OAUTH": env_config.get("ENABLE_ENTRA_OAUTH", "false"),
                "ENTRA_CLIENT_ID": env_config.get("ENTRA_CLIENT_ID", ""),
                "ENTRA_CLIENT_SECRET": env_config.get("ENTRA_CLIENT_SECRET", ""),
                "ENTRA_TENANT_ID": env_config.get("ENTRA_TENANT_ID", ""),
                "ENTRA_OAUTH_DOMAIN": env_config.get("ENTRA_OAUTH_DOMAIN", ""),
                "ENABLE_OPENWEBUI_OIDC": env_config.get("ENABLE_OPENWEBUI_OIDC", "false"),
                "OPENWEBUI_OIDC_CLIENT_ID": env_config.get("OPENWEBUI_OIDC_CLIENT_ID", ""),
                "OPENWEBUI_OIDC_CLIENT_SECRET": env_config.get("OPENWEBUI_OIDC_CLIENT_SECRET", ""),
                "ENABLE_GITEA_AUTHENTIK_OIDC": env_config.get("ENABLE_GITEA_AUTHENTIK_OIDC", "false"),
                "GITEA_OIDC_CLIENT_ID": env_config.get("GITEA_OIDC_CLIENT_ID", ""),
                "GITEA_OIDC_CLIENT_SECRET": env_config.get("GITEA_OIDC_CLIENT_SECRET", ""),
                "GPUSTACK_MASTER_SERVER_URL": env_config.get("GPUSTACK_MASTER_SERVER_URL", ""),
                "GPUSTACK_MASTER_SERVER_TOKEN": env_config.get("GPUSTACK_MASTER_SERVER_TOKEN", ""),
                "GPUSTACK_API_KEY": env_config.get("GPUSTACK_API_KEY", ""),
                "SMTP_MODE": env_config.get("SMTP_MODE", "relay"),
                "SMTP_FROM": env_config.get("SMTP_FROM", ""),
                "SMTP_RELAY_HOST": env_config.get("SMTP_RELAY_HOST", "smtp.gmail.com"),
                "SMTP_RELAY_PORT": env_config.get("SMTP_RELAY_PORT", "587"),
                "SMTP_RELAY_USERNAME": env_config.get("SMTP_RELAY_USERNAME", ""),
                "SMTP_RELAY_PASSWORD": env_config.get("SMTP_RELAY_PASSWORD", ""),
            },
            "security": {
                 "WEBUI_SECRET_KEY": env_config.get("WEBUI_SECRET_KEY", ""),
                 "POSTGRES_PASSWORD": env_config.get("POSTGRES_PASSWORD", ""),
            },
            "profiles": {
                # strip: an operator-edited `chat, dify` otherwise reports
                # `dify` as OFF in the editable view while it is enabled.
                p: (p in [x.strip() for x in
                          env_config.get("COMPOSE_PROFILES", "").split(",")])
                for p in PROFILES
            },
            "lightrag": {
                "LIGHTRAG_LLM_MODEL": env_config.get("LIGHTRAG_LLM_MODEL", ""),
                "LIGHTRAG_EMBEDDING_MODEL": env_config.get("LIGHTRAG_EMBEDDING_MODEL", ""),
                "LIGHTRAG_RERANK_BINDING": env_config.get("LIGHTRAG_RERANK_BINDING", "null"),
                "LIGHTRAG_RERANK_MODEL": env_config.get("LIGHTRAG_RERANK_MODEL", ""),
            },
            "cognee": {
                "COGNEE_LLM_MODEL": env_config.get("COGNEE_LLM_MODEL", ""),
                "COGNEE_EMBEDDING_MODEL": env_config.get("COGNEE_EMBEDDING_MODEL", ""),
                "COGNEE_EMBEDDING_DIM": env_config.get("COGNEE_EMBEDDING_DIM", "4096"),
            }
        }

    def update_domain(self, new_domain):
        """#420: change MAIN_DOMAIN plus every DERIVED *_DOMAIN var.

        Writing MAIN_DOMAIN alone leaves chat/config/llm/… subdomains on the
        OLD domain: a half-renamed box. But "every key ending in _DOMAIN whose
        value contains the old domain" was too wide, in two ways:

        * ``*_OAUTH_DOMAIN`` (``GOOGLE_OAUTH_DOMAIN``, ``ENTRA_OAUTH_DOMAIN``)
          is the customer's **IdP tenant / allow-list** domain, not a stack
          subdomain. On a box where MAIN_DOMAIN equals the corporate domain,
          ``--set-domain ai.acme.com`` silently rewrote the OAuth allow-list to
          the new stack domain and locked out every SSO user.
        * a substring match also rewrote values that merely *contain* the old
          domain. Only ``<old>`` itself or ``<label>.<old>`` is a derived
          subdomain, so the suffix is matched strictly.

        A var whose RAW line is still ``…${MAIN_DOMAIN}`` is skipped entirely:
        the indirection already propagates, and writing the expanded literal
        back would de-templatize it permanently — after which editing
        MAIN_DOMAIN alone stops propagating, the exact failure this method
        exists to prevent. 26 of the 28 shipped *_DOMAIN vars are templated.
        """
        env = self._read_env_file(ENV_FILE)
        raw = self._read_env_file_raw(ENV_FILE)
        old_domain = env.get('MAIN_DOMAIN', '')
        if not new_domain or new_domain == old_domain:
            return {}
        updates = {'MAIN_DOMAIN': new_domain}
        if not old_domain:
            self._write_env_file(ENV_FILE, updates)
            return updates
        suffix = '.' + old_domain
        for var in [k for k in env if k.endswith('_DOMAIN') and k != 'MAIN_DOMAIN']:
            if var in NON_STACK_DOMAIN_VARS or var.endswith('_OAUTH_DOMAIN'):
                continue
            if '${MAIN_DOMAIN}' in raw.get(var, ''):
                continue
            val = env.get(var, '')
            if val == old_domain or val.endswith(suffix):
                updates[var] = val[:len(val) - len(old_domain)] + new_domain
        self._write_env_file(ENV_FILE, updates)
        return updates

    def update_config(self, data):
        # Process wizard form data into env updates
        updates = {}
        
        # Global
        # #420: MAIN_DOMAIN routes through update_domain so the derived
        # *_DOMAIN vars follow — the old direct write produced a half-renamed
        # .env (subdomains kept the previous domain).
        if 'MAIN_DOMAIN' in data:
            updates.update(self.update_domain(data['MAIN_DOMAIN']) or {})
        if 'TZ' in data:
            updates['TZ'] = data['TZ']
            updates['GENERIC_TIMEZONE'] = data['TZ']
        if 'admin_email' in data:
            updates['LETSENCRYPT_EMAIL'] = data['admin_email']
        
        # TLS Mode
        if 'TLS_MODE' in data:
            tls_mode = data['TLS_MODE']
            if tls_mode in TLS_MODES:
                updates['TLS_MODE'] = TLS_MODES[tls_mode]
            else:
                updates['TLS_MODE'] = ''  # Default to letsencrypt
            # Derive TLS_DIRECTIVE for Caddy (must match razzfazz-init.sh / entrypoint.sh)
            # #1594: the two WILDCARD vHosts (*.agents, *.mcp) need their own
            # issuer. A `*.<domain>` certificate covers ONE label, so it does
            # not match `<user>.agents.<domain>`; left to ACME those blocks
            # retry an impossible dns-01 challenge for thirty days.
            if updates['TLS_MODE'] == 'internal':
                updates['TLS_DIRECTIVE'] = 'tls internal'
                updates['TLS_WILDCARD_ISSUER'] = 'issuer internal'
            elif updates['TLS_MODE'] == 'certificate':
                updates['TLS_DIRECTIVE'] = ''
                updates['TLS_WILDCARD_ISSUER'] = 'issuer internal'
            else:
                updates['TLS_DIRECTIVE'] = ''
                updates['TLS_WILDCARD_ISSUER'] = 'on_demand'
        
        # Auth Scenario
        if 'DEPLOYMENT_SCENARIO' in data:
            scenario = data['DEPLOYMENT_SCENARIO']
            if scenario in AUTH_SCENARIOS:
                updates['DEPLOYMENT_SCENARIO'] = scenario
                updates['ENABLE_GOOGLE_OAUTH'] = 'true' if scenario == 'google' else 'false'
            else:
                updates['DEPLOYMENT_SCENARIO'] = 'base'
                updates['ENABLE_GOOGLE_OAUTH'] = 'false'
        
        # Google OAuth Credentials
        if 'GOOGLE_CLIENT_ID' in data:
            updates['GOOGLE_CLIENT_ID'] = data['GOOGLE_CLIENT_ID']
        if 'GOOGLE_CLIENT_SECRET' in data:
            updates['GOOGLE_CLIENT_SECRET'] = data['GOOGLE_CLIENT_SECRET']

        # Entra ID SSO [EXPERIMENTAL]
        if 'ENABLE_ENTRA_OAUTH' in data:
            updates['ENABLE_ENTRA_OAUTH'] = 'true' if data['ENABLE_ENTRA_OAUTH'] in ('true', 'on', '1') else 'false'
        if 'ENTRA_CLIENT_ID' in data:
            updates['ENTRA_CLIENT_ID'] = data['ENTRA_CLIENT_ID'].strip()
        if 'ENTRA_CLIENT_SECRET' in data:
            val = data['ENTRA_CLIENT_SECRET'].strip()
            if val:  # Only update if non-empty (preserve existing)
                updates['ENTRA_CLIENT_SECRET'] = val
        if 'ENTRA_TENANT_ID' in data:
            updates['ENTRA_TENANT_ID'] = data['ENTRA_TENANT_ID'].strip()
        if 'ENTRA_OAUTH_DOMAIN' in data:
            updates['ENTRA_OAUTH_DOMAIN'] = data['ENTRA_OAUTH_DOMAIN'].strip()

        # Open WebUI OIDC [EXPERIMENTAL]
        if 'ENABLE_OPENWEBUI_OIDC' in data:
            enabled = data['ENABLE_OPENWEBUI_OIDC'] in ('true', 'on', '1')
            updates['ENABLE_OPENWEBUI_OIDC'] = 'true' if enabled else 'false'
        if 'OPENWEBUI_OIDC_CLIENT_ID' in data:
            updates['OPENWEBUI_OIDC_CLIENT_ID'] = data['OPENWEBUI_OIDC_CLIENT_ID'].strip()
        if 'OPENWEBUI_OIDC_CLIENT_SECRET' in data:
            val = data['OPENWEBUI_OIDC_CLIENT_SECRET'].strip()
            if val:
                updates['OPENWEBUI_OIDC_CLIENT_SECRET'] = val

        # Gitea Authentik OIDC [EXPERIMENTAL]
        if 'ENABLE_GITEA_AUTHENTIK_OIDC' in data:
            enabled = data['ENABLE_GITEA_AUTHENTIK_OIDC'] in ('true', 'on', '1')
            updates['ENABLE_GITEA_AUTHENTIK_OIDC'] = 'true' if enabled else 'false'
        if 'GITEA_OIDC_CLIENT_ID' in data:
            updates['GITEA_OIDC_CLIENT_ID'] = data['GITEA_OIDC_CLIENT_ID'].strip()
        if 'GITEA_OIDC_CLIENT_SECRET' in data:
            val = data['GITEA_OIDC_CLIENT_SECRET'].strip()
            if val:
                updates['GITEA_OIDC_CLIENT_SECRET'] = val
        
        # GPUStack Mode
        if 'GPUSTACK_MODE' in data:
            gpustack_mode = data['GPUSTACK_MODE']
            if gpustack_mode in GPUSTACK_MODES:
                updates['GPUSTACK_MODE'] = gpustack_mode
                updates['GPUSTACK_BIND'] = GPUSTACK_MODES[gpustack_mode]
            else:
                updates['GPUSTACK_MODE'] = 'standalone'
                updates['GPUSTACK_BIND'] = '127.0.0.1'
            
            # Clear mode-specific vars that don't apply (must match razzfazz-init.sh)
            if gpustack_mode != 'worker':
                updates['GPUSTACK_MASTER_SERVER_URL'] = ''
                updates['GPUSTACK_MASTER_SERVER_TOKEN'] = ''
            if gpustack_mode in ('standalone', 'master'):
                updates['GPUSTACK_WORKER_IP'] = ''
                updates['GPUSTACK_WORKER_NAME'] = ''
        
        # GPUStack Worker settings (only applied when mode is worker)
        if 'GPUSTACK_MASTER_SERVER_URL' in data and data.get('GPUSTACK_MODE') == 'worker':
            updates['GPUSTACK_MASTER_SERVER_URL'] = data['GPUSTACK_MASTER_SERVER_URL']
        if 'GPUSTACK_MASTER_SERVER_TOKEN' in data and data.get('GPUSTACK_MODE') == 'worker':
            updates['GPUSTACK_MASTER_SERVER_TOKEN'] = data['GPUSTACK_MASTER_SERVER_TOKEN']
        
        # GPUStack API Key (for model-sync container)
        if 'GPUSTACK_API_KEY' in data:
            updates['GPUSTACK_API_KEY'] = data['GPUSTACK_API_KEY'].strip()
        
        # SMTP Relay Configuration
        if 'SMTP_MODE' in data:
            smtp_mode = data['SMTP_MODE']
            if smtp_mode in SMTP_MODES:
                updates['SMTP_MODE'] = smtp_mode
            else:
                updates['SMTP_MODE'] = 'relay'
            
            # Clear relay credentials when switching to direct mode
            if smtp_mode == 'direct':
                updates['SMTP_RELAY_HOST'] = ''
                updates['SMTP_RELAY_USERNAME'] = ''
                updates['SMTP_RELAY_PASSWORD'] = ''
        
        if 'SMTP_FROM' in data:
            updates['SMTP_FROM'] = data['SMTP_FROM'].strip()
        
        if 'SMTP_RELAY_HOST' in data and data.get('SMTP_MODE') != 'direct':
            updates['SMTP_RELAY_HOST'] = data['SMTP_RELAY_HOST'].strip()
        if 'SMTP_RELAY_PORT' in data and data.get('SMTP_MODE') != 'direct':
            updates['SMTP_RELAY_PORT'] = data['SMTP_RELAY_PORT'].strip()
        if 'SMTP_RELAY_USERNAME' in data and data.get('SMTP_MODE') != 'direct':
            updates['SMTP_RELAY_USERNAME'] = data['SMTP_RELAY_USERNAME'].strip()
        if 'SMTP_RELAY_PASSWORD' in data and data.get('SMTP_MODE') != 'direct':
            val = data['SMTP_RELAY_PASSWORD'].strip()
            if val:  # Only update password if non-empty (preserve existing)
                updates['SMTP_RELAY_PASSWORD'] = val
        
        # Security
        if 'WEBUI_SECRET_KEY' in data: updates['WEBUI_SECRET_KEY'] = data['WEBUI_SECRET_KEY']
        
        # Profiles — only rewrite COMPOSE_PROFILES when the caller actually
        # submitted profile toggles (any 'profile_*' key present). Targeted setup
        # ops (--set-smtp-mode / --set-tls-mode / --install-certificate) pass a
        # single unrelated key and must NOT wipe the operator's enabled modules (#274).
        _has_profile_toggles = any(k.startswith("profile_") for k in data)
        if _has_profile_toggles:
            # #449: rebuild from the CURRENT value, touching ONLY profiles the
            # caller actually mentioned. Anything not submitted is preserved —
            # the old loop rebuilt from the (incomplete) literal and silently
            # dropped every unlisted-but-enabled profile.
            current_raw = self._read_env_file(ENV_FILE).get("COMPOSE_PROFILES", "")
            # STRIP each token, don't just filter empties. For an
            # operator-edited `COMPOSE_PROFILES=chat, dify, llm` the untrimmed
            # form gives ['chat', ' dify', ' llm']: `' dify' not in submitted`
            # is true, so a profile the wizard turned OFF is preserved, and the
            # turned_on loop then appends the clean 'dify' — yielding
            # `chat, dify, llm,dify`, a profile that can never be turned off.
            # get_all_config already strips; this path did not. De-duplicate
            # while preserving order for the same reason.
            current = []
            for p in current_raw.split(","):
                p = p.strip()
                if p and p not in current:
                    current.append(p)
            submitted = {k[len("profile_"):] for k in data
                         if k.startswith("profile_")}
            turned_on = [p for p in submitted if data.get(f"profile_{p}") == "on"]
            result = [p for p in current
                      if p not in submitted or data.get(f"profile_{p}") == "on"]
            for p in turned_on:
                if p not in result:
                    result.append(p)
            # #465: mutual exclusion, same authority as the Portal
            # (profiles.yaml) — the profile being turned ON wins, its
            # excluded counterparts drop. Mirrors apply_manager._execute_toggle.
            excl_map = _load_mutual_exclusions()
            for p in turned_on:
                for e in excl_map.get(p, []):
                    if e in result and e != p:
                        result.remove(e)
            updates['COMPOSE_PROFILES'] = ",".join(result)

        # LightRAG model configuration
        if 'LIGHTRAG_LLM_MODEL' in data:
            updates['LIGHTRAG_LLM_MODEL'] = data['LIGHTRAG_LLM_MODEL']
        if 'LIGHTRAG_EMBEDDING_MODEL' in data:
            updates['LIGHTRAG_EMBEDDING_MODEL'] = data['LIGHTRAG_EMBEDDING_MODEL']
        if 'LIGHTRAG_RERANK_BINDING' in data:
            updates['LIGHTRAG_RERANK_BINDING'] = data['LIGHTRAG_RERANK_BINDING']
        if 'LIGHTRAG_RERANK_MODEL' in data:
            updates['LIGHTRAG_RERANK_MODEL'] = data['LIGHTRAG_RERANK_MODEL']
        
        self._write_env_file(ENV_FILE, updates)
        return updates

    def get_gpustack_api_key_status(self) -> dict:
        """Check if GPUSTACK_API_KEY is configured and looks valid."""
        env_config = self._read_env_file(ENV_FILE)
        key = env_config.get('GPUSTACK_API_KEY', '')
        placeholder_indicators = ['changeme', 'replace', 'example', 'default', 'your-', 'generate']
        is_placeholder = (
            not key
            or any(indicator in key.lower() for indicator in placeholder_indicators)
        )
        return {
            'is_set': bool(key),
            'is_valid': bool(key) and not is_placeholder and key.startswith('gpustack_'),
            'needs_configuration': is_placeholder,
            'current_value_masked': f"{key[:12]}...{key[-4:]}" if len(key) > 16 else ('(not set)' if not key else '(too short)'),
        }

    def set_gpustack_api_key(self, api_key: str) -> bool:
        """Set the GPUSTACK_API_KEY in .env. Returns True on success."""
        api_key = api_key.strip()
        if not api_key:
            return False
        self._write_env_file(ENV_FILE, {'GPUSTACK_API_KEY': api_key})
        return True

    def get_backup_encryption_status(self) -> dict:
        """Check if a custom backup encryption password is configured."""
        env_config = self._read_env_file(ENV_FILE)
        custom_password = env_config.get('BACKUP_ENCRYPTION_PASSWORD', '')
        admin_password = env_config.get('AUTHENTIK_BOOTSTRAP_PASSWORD', '')
        return {
            'custom_password_set': bool(custom_password),
            'admin_password_available': bool(admin_password),
            'encryption_active': bool(custom_password or admin_password),
            'password_source': 'custom' if custom_password else ('admin' if admin_password else 'none'),
        }

    def set_backup_encryption_password(self, password: str) -> bool:
        """Set or clear the BACKUP_ENCRYPTION_PASSWORD in .env."""
        self._write_env_file(ENV_FILE, {'BACKUP_ENCRYPTION_PASSWORD': password})
        return True

    def get_lightrag_config(self) -> dict:
        """Get LightRAG model configuration from .env."""
        env_config = self._read_env_file(ENV_FILE)
        return {
            "llm_model": env_config.get("LIGHTRAG_LLM_MODEL", ""),
            "embedding_model": env_config.get("LIGHTRAG_EMBEDDING_MODEL", ""),
            "rerank_binding": env_config.get("LIGHTRAG_RERANK_BINDING", "null"),
            "rerank_model": env_config.get("LIGHTRAG_RERANK_MODEL", ""),
            "is_configured": bool(env_config.get("LIGHTRAG_LLM_MODEL")) and bool(env_config.get("LIGHTRAG_EMBEDDING_MODEL")),
        }

    def set_lightrag_models(self, llm_model: str, embedding_model: str,
                            rerank_binding: str = "null", rerank_model: str = "") -> bool:
        """Set LightRAG model configuration in .env."""
        updates = {
            "LIGHTRAG_LLM_MODEL": llm_model,
            "LIGHTRAG_EMBEDDING_MODEL": embedding_model,
            "LIGHTRAG_RERANK_BINDING": rerank_binding,
            "LIGHTRAG_RERANK_MODEL": rerank_model,
        }
        self._write_env_file(ENV_FILE, updates)
        return True

    def get_cognee_config(self) -> dict:
        """Get Cognee model configuration from .env."""
        env_config = self._read_env_file(ENV_FILE)
        return {
            "llm_model": env_config.get("COGNEE_LLM_MODEL", ""),
            "embedding_model": env_config.get("COGNEE_EMBEDDING_MODEL", ""),
            "embedding_dim": env_config.get("COGNEE_EMBEDDING_DIM", "4096"),
            "is_configured": bool(env_config.get("COGNEE_LLM_MODEL")) and bool(env_config.get("COGNEE_EMBEDDING_MODEL")),
        }

    def set_cognee_models(self, llm_model: str, embedding_model: str,
                          embedding_dim: str = "768") -> bool:
        """Set Cognee model configuration in .env."""
        updates = {
            "COGNEE_LLM_MODEL": llm_model,
            "COGNEE_EMBEDDING_MODEL": embedding_model,
            "COGNEE_EMBEDDING_DIM": embedding_dim,
        }
        self._write_env_file(ENV_FILE, updates)
        return True

    @staticmethod
    def get_gpustack_models() -> list:
        """Query GPUStack for available models. Returns list of model ID strings."""
        import urllib.request
        import json as _json

        env = {}
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, val = line.partition('=')
                        env[key.strip()] = val.strip()

        # #1445: the model list comes from the canonical endpoint (the manager);
        # the stack/openwebui service key is the credential a box always has.
        api_key = env.get("LLM_MANAGER_OWUI_KEY", "") or env.get("GPUSTACK_API_KEY", "")
        if not api_key:
            return []

        try:
            req = urllib.request.Request(
                "http://llm:8080/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
                return [m["id"] for m in data.get("data", [])]
        except Exception:
            return []

    # Note: After setting a new API key, model-sync container must be
    # recreated (not just restarted) to pick up the new env var:
    #   docker compose up -d --force-recreate model-sync

    def apply_changes_and_restart(self):
        # Trigger restart in a thread
        t = threading.Thread(target=self._restart_stack)
        t.start()

    def _restart_stack(self):
        """
        CAUTION: This method was originally designed to restart the stack.
        However, since this container lives INSIDE the stack, restarting the stack
        often kills this process before it completes, leaving the stack in an inconsistent state.
        
        We now only log valid instructions. The frontend should handle the messaging.
        """
        print("Configuration saved. Automatic restart skipped for stability.")
        print("Please run 'docker compose down && docker compose up -d' manually.")
        
        # We do NOT run subprocess here anymore.
        # subprocess.run([...]) 

    def start_factory_reset(self):
        # #22: now runs on the HOST (the razzfazz-setup container that used
        # to background this is gone). Run synchronously in STACK_ROOT — a
        # host process won't be killed by `down -v`, so there's no need to
        # detach into a thread.
        self._factory_reset()

    def _factory_reset(self):
        # docker compose down -v — destroys all stack data/volumes.
        subprocess.run([
            "docker", "compose",
            "-f", "compose.yml",
            "down", "-v"
        ], cwd=STACK_ROOT)
