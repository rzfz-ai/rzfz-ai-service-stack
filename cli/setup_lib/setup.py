#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import argparse
import sys
import os

# #22: the CLI backend now lives in cli/setup_lib/ and runs on the HOST
# (the razzfazz-setup web container that used to host it was removed).
# config_manager / checksum_manager / log_manager are siblings here.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from config_manager import ConfigManager, STACK_ROOT
except ImportError:
    print("Error: Could not import ConfigManager. Run this script from the correct environment.")
    sys.exit(1)

CERTS_DIR = os.path.join(STACK_ROOT, "certs")

try:
    from checksum_manager import ChecksumManager
except ImportError:
    ChecksumManager = None

try:
    from log_manager import LogManager
except ImportError:
    LogManager = None

try:
    from checksum_manager import ChecksumManager
except ImportError:
    ChecksumManager = None

try:
    from log_manager import LogManager
except ImportError:
    LogManager = None

def main():
    parser = argparse.ArgumentParser(description="razzfazz.ai CLI Setup Tool")
    # M030-S5: per-user agent volume migration (anonymous → named)
    parser.add_argument('--migrate-agent-volumes', action='store_true',
                        help="M030: detect per-user agent instances using anonymous volumes "
                             "(pre-M030 catalog) and migrate them to named volumes losslessly. "
                             "Per-instance brief downtime (~30s each). Use --dry-run first to preview.")
    parser.add_argument('--dry-run', action='store_true',
                        help="Preview-only mode for --migrate-agent-volumes. No changes made.")

    parser.add_argument('--status', action='store_true', help="Show current configuration status")
    parser.add_argument('--init', action='store_true', help="Run initialization wizard (interactive)")
    parser.add_argument('--reset', action='store_true', help="Factory Reset (DANGER)")
    parser.add_argument('--regenerate-secrets', action='store_true', help="Regenerate all secrets and passwords")
    parser.add_argument('--secrets-status', action='store_true', help="Show status of all secrets")
    parser.add_argument('--set-gpustack-api-key', metavar='KEY', help="Set the GPUStack API key for model sync")
    parser.add_argument('--gpustack-api-key-status', action='store_true', help="Show GPUStack API key status")
    parser.add_argument('--smtp-status', action='store_true', help="Show SMTP relay configuration status")
    parser.add_argument('--set-smtp-mode', metavar='MODE', choices=['relay', 'direct'], help="Set SMTP mode (relay or direct)")
    parser.add_argument('--set-smtp-relay', nargs=3, metavar=('HOST', 'USER', 'PASS'), help="Set SMTP relay credentials (host username password)")
    parser.add_argument('--set-tls-mode', metavar='MODE', choices=['letsencrypt', 'selfsigned', 'certificate'], help="Set TLS mode (letsencrypt, selfsigned, or certificate)")
    parser.add_argument('--install-certificate', nargs=2, metavar=('CERT_FILE', 'KEY_FILE'), help="Install custom TLS certificate files (cert.pem and key.pem)")
    parser.add_argument('--certificate-status', action='store_true', help="Show TLS certificate status")
    parser.add_argument('--set-backup-encryption-password', metavar='PASSWORD', nargs='?', const='__PROMPT__', help="Set a custom backup encryption password (omit value to clear)")
    parser.add_argument('--backup-encryption-status', action='store_true', help="Show backup encryption status")
    # Entra ID SSO [EXPERIMENTAL]
    parser.add_argument('--set-entra-credentials', nargs='+', metavar='VALUE',
                        help="Set Microsoft Entra ID SSO credentials: CLIENT_ID CLIENT_SECRET TENANT_ID [DOMAIN] [EXPERIMENTAL]")
    # Open WebUI OIDC [EXPERIMENTAL]
    parser.add_argument('--set-openwebui-oidc', nargs=2, metavar=('CLIENT_ID', 'CLIENT_SECRET'),
                        help="Set Open WebUI OIDC credentials and enable native Authentik OIDC login [EXPERIMENTAL]")
    parser.add_argument('--disable-openwebui-oidc', action='store_true',
                        help="Disable Open WebUI native OIDC (revert to Caddy forward-auth only) [EXPERIMENTAL]")
    # Gitea Authentik OIDC [EXPERIMENTAL]
    parser.add_argument('--set-gitea-oidc', nargs=2, metavar=('CLIENT_ID', 'CLIENT_SECRET'),
                        help="Set Gitea Authentik OIDC credentials and enable native OIDC login [EXPERIMENTAL]")
    parser.add_argument('--disable-gitea-oidc', action='store_true',
                        help="Disable Gitea native Authentik OIDC [EXPERIMENTAL]")
    # LightRAG [EXPERIMENTAL]
    parser.add_argument('--set-lightrag-models', nargs='+', metavar='MODEL', help="Set LightRAG models: LLM_MODEL EMBEDDING_MODEL [RERANK_MODEL]")
    parser.add_argument('--lightrag-status', action='store_true', help="Show LightRAG model configuration")
    # Cognee [EXPERIMENTAL]
    parser.add_argument('--set-cognee-models', nargs='+', metavar='MODEL', help="Set Cognee models: LLM_MODEL EMBEDDING_MODEL [EMBEDDING_DIM]")
    parser.add_argument('--cognee-status', action='store_true', help="Show Cognee model configuration")
    # Agentic AI stack (experimental)
    parser.add_argument('--paperclip-status', action='store_true', help="Show Paperclip profile status [EXPERIMENTAL]")
    parser.add_argument('--moltis-status', action='store_true', help="Show Moltis profile status [EXPERIMENTAL]")
    parser.add_argument('--hermes-status', action='store_true', help="Show Hermes Agent profile status [EXPERIMENTAL]")
    parser.add_argument('--matrix-status', action='store_true', help="Show Matrix (Synapse + Element Web) profile status [EXPERIMENTAL]")
    # Checksum Governance
    parser.add_argument('--checksum-take', metavar='COMMENT', nargs='?', const='Manual snapshot via CLI', help="Take a checksum governance snapshot")
    parser.add_argument('--checksum-history', action='store_true', help="Show checksum governance history")
    parser.add_argument('--checksum-detail', metavar='SET_ID', type=int, help="Show details of a checksum set")
    parser.add_argument('--checksum-diff', nargs=2, metavar=('ID_A', 'ID_B'), type=int, help="Show diff between two checksum sets")
    parser.add_argument('--checksum-status', action='store_true', help="Show current governance fingerprint")
    # Log Snapshots
    parser.add_argument('--logs-take', metavar='REASON', nargs='?', const='Manual snapshot via CLI', help="Create a log snapshot")
    parser.add_argument('--logs-list', action='store_true', help="List available log snapshots")
    
    args = parser.parse_args()
    cm = ConfigManager()

    if args.status:
        config = cm.get_all_config()
        print("\n=== razzfazz.ai Configuration Status ===")
        print(f"Domain:   {config['domain']}")
        print(f"Timezone: {config['timezone']}")
        print(f"Profiles: {', '.join(config['profiles'])}")
        
        # GPUStack API Key status
        api_key_status = cm.get_gpustack_api_key_status()
        if api_key_status['is_valid']:
            print(f"GPUStack API Key: ✓ {api_key_status['current_value_masked']}")
        elif api_key_status['needs_configuration']:
            print(f"GPUStack API Key: ⚠ Not configured (model-sync won't work)")
        else:
            print(f"GPUStack API Key: ⚠ {api_key_status['current_value_masked']}")
        
        # Backup encryption status
        enc_status = cm.get_backup_encryption_status()
        if enc_status['encryption_active']:
            source = 'custom password' if enc_status['custom_password_set'] else 'admin password'
            print(f"Backup Enc: 🔒 Active ({source})")
        else:
            print(f"Backup Enc: ⚠ No encryption password available")

        # SMTP status
        config_full = cm.get_editable_config()
        smtp_mode = config_full['global'].get('SMTP_MODE', 'relay')
        smtp_from = config_full['global'].get('SMTP_FROM', '')
        smtp_host = config_full['global'].get('SMTP_RELAY_HOST', '')
        smtp_user = config_full['global'].get('SMTP_RELAY_USERNAME', '')
        if smtp_mode == 'direct':
            print(f"SMTP:     ✉ Direct mode (from: {smtp_from or 'default'})")
        elif smtp_user:
            print(f"SMTP:     ✓ Relay via {smtp_host} (from: {smtp_from or 'default'})")
        else:
            print(f"SMTP:     ⚠ Relay not configured (emails won't send)")
        
        print("========================================")

    elif args.init:
        print("\n=== Initialization Wizard ===")
        current = cm.get_editable_config()
        
        # Simple interactive prompts
        domain = input(f"Domain [{current['global']['MAIN_DOMAIN']}]: ") or current['global']['MAIN_DOMAIN']
        tz = input(f"Timezone [{current['global']['TZ']}]: ") or current['global']['TZ']

        # Profiles
        available_profiles = list(current['profiles'].keys())
        active_profiles = set([p for p, enabled in current['profiles'].items() if enabled])
        
        # Helper to toggle
        def toggle_profile(p, active_set):
            if p in active_set:
                active_set.remove(p)
            else:
                active_set.add(p)
            
            # Mutuality Constraints
            if p == 'llm-cpu' and 'llm-cpu' in active_set:
                active_set.discard('llm-box')
            elif p == 'llm-box' and 'llm-box' in active_set:
                active_set.discard('llm-cpu')

        while True:
            # Clear screen (optional based on terminal support, simplistic here)
            print("\n--- Profile Selection ---")
            print("Select profiles by entering their number. Type 'd' when done.")
            
            for i, p in enumerate(available_profiles):
                mark = "[X]" if p in active_profiles else "[ ]"
                print(f"{i+1}. {mark} {p}")
            
            choice = input(f"Toggle [1-{len(available_profiles)}] or 'd' to done: ").strip().lower()
            
            if choice == 'd':
                break
            
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(available_profiles):
                    toggle_profile(available_profiles[idx], active_profiles)
                else:
                    print("Invalid selection.")
            else:
                print("Invalid input.")

        # Just mock implementation for CLI brevity - full wizard logic here would follow app.py logic
        update_data = {
            'MAIN_DOMAIN': domain,
            'TZ': tz,
        }
        
        # Add profile flags for config_manager.update_config
        # Logic in update_config looks for 'profile_NAME' = 'on'
        # First reset all potential profiles to ensure mutual exclusion works cleanly if logic supported it
        # But here we just pass the 'on' ones
        for p in available_profiles:
            if p in active_profiles:
                update_data[f'profile_{p}'] = 'on'
        
        print("\nUpdating config...")
        cm.update_config(update_data)
        print("Configuration saved.") 
        
        # Disabled auto-restart from within container due to path issues
        # cm.apply_changes_and_restart()
        print("\nIMPORTANT: Please restart the stack manually to apply changes:")
        print("  docker compose down && docker compose up -d")

    elif args.regenerate_secrets:
        print("\n=== Regenerate All Secrets ===")
        print("This will generate new cryptographically secure secrets for:")
        print("  • WEBUI_SECRET_KEY")
        print("  • AUTHENTIK_SECRET_KEY")
        print("  • POSTGRES_PASSWORD")
        print("  • VALKEY_PASSWORD")
        print("  • GPUSTACK_SECRET_KEY")
        print("  • PIPELINES_API_KEY")
        print("  • KOMODO_PASSKEY")
        print("  • Dify SECRET_KEY")
        print("")
        print("WARNING: After regenerating secrets, you must:")
        print("  1. Restart the entire stack")
        print("  2. Re-initialize any services that store these secrets")
        print("")
        
        confirm = input("Type 'REGENERATE' to confirm: ").strip()
        if confirm == "REGENERATE":
            print("\nRegenerating secrets...")
            regenerated = cm.regenerate_all_secrets()
            print("\n✓ Secrets regenerated successfully!")
            print("\nNew secrets have been written to .env and .env.dify")
            print("\nIMPORTANT: Restart the stack to apply changes:")
            print("  docker compose down && docker compose up -d --force-recreate")
        else:
            print("Aborted.")

    elif args.secrets_status:
        print("\n=== Secrets Status ===")
        status = cm.get_secrets_status()
        
        needs_regen = []
        for key, info in status.items():
            status_icon = "✓" if not info['needs_regeneration'] else "⚠"
            set_status = "SET" if info['is_set'] else "NOT SET"
            print(f"  {status_icon} {key}: {set_status} (length: {info['current_length']})")
            if info['needs_regeneration']:
                needs_regen.append(key)
        
        if needs_regen:
            print(f"\n⚠ {len(needs_regen)} secret(s) may need regeneration.")
            print("  Run: rzfz setup --regenerate-secrets")
        else:
            print("\n✓ All secrets appear to be properly configured.")

    elif args.reset:
        confirm = input("TYPE 'DELETE' TO CONFIRM FACTORY RESET: ")
        if confirm == "DELETE":
            print("Initiating Factory Reset...")
            cm.start_factory_reset()
        else:
            print("Aborted.")

    elif args.set_gpustack_api_key:
        key = args.set_gpustack_api_key.strip()
        if not key:
            print("Error: API key cannot be empty.")
            sys.exit(1)
        if not key.startswith('gpustack_'):
            print("Warning: GPUStack API keys typically start with 'gpustack_'.")
            confirm = input("Continue anyway? [y/N]: ").strip().lower()
            if confirm != 'y':
                print("Aborted.")
                sys.exit(0)
        if cm.set_gpustack_api_key(key):
            print("✓ GPUStack API key saved to .env")
            print("\nRecreate the model-sync container to apply (restart is not enough):")
            print("  docker compose up -d --force-recreate model-sync")
        else:
            print("Error: Failed to save API key.")
            sys.exit(1)

    elif args.gpustack_api_key_status:
        status = cm.get_gpustack_api_key_status()
        print("\n=== GPUStack API Key Status ===")
        if status['is_valid']:
            print(f"  ✓ Key: {status['current_value_masked']}")
            print("  Status: Configured and looks valid")
        elif status['needs_configuration']:
            print("  ⚠ Key: Not configured or placeholder")
            print("  Status: Model sync will NOT work")
            print("\n  To set the key:")
            print("    1. Open GPUStack UI → API Keys")
            print("    2. Create a new API key")
            print("    3. Run: rzfz setup --set-gpustack-api-key <YOUR_KEY>")
        else:
            print(f"  ⚠ Key: {status['current_value_masked']}")
            print("  Status: Set but may not be valid")
        print()

    elif args.smtp_status:
        print("\n=== SMTP Relay Status ===")
        config_full = cm.get_editable_config()
        smtp_mode = config_full['global'].get('SMTP_MODE', 'relay')
        smtp_from = config_full['global'].get('SMTP_FROM', '')
        smtp_host = config_full['global'].get('SMTP_RELAY_HOST', '')
        smtp_port = config_full['global'].get('SMTP_RELAY_PORT', '587')
        smtp_user = config_full['global'].get('SMTP_RELAY_USERNAME', '')
        smtp_pass = config_full['global'].get('SMTP_RELAY_PASSWORD', '')
        
        print(f"  Mode:     {smtp_mode}")
        print(f"  From:     {smtp_from or '(default: razzfazz-ai-box-1@<domain>)'}")
        
        if smtp_mode == 'relay':
            print(f"  Host:     {smtp_host or '(not set) ⚠'}")
            print(f"  Port:     {smtp_port}")
            print(f"  Username: {smtp_user or '(not set) ⚠'}")
            print(f"  Password: {'●' * 8 + ' (set)' if smtp_pass else '(not set) ⚠'}")
            
            if not smtp_host or not smtp_user or not smtp_pass:
                print("\n  ⚠ SMTP relay is not fully configured. Emails won't be sent.")
                print("  Set credentials via:")
                print("    rzfz setup --set-smtp-relay <host> <user> <password>")
                print("    Or use the Configuration Portal at https://config.<domain>/")
            else:
                print("\n  ✓ SMTP relay is configured and ready.")
        elif smtp_mode == 'direct':
            print("\n  ✉ Direct mode: emails sent directly to recipient MX servers.")
            print("  Ensure SPF, DKIM, DMARC DNS records are configured.")
        print()

    elif args.set_smtp_mode:
        mode = args.set_smtp_mode
        cm.update_config({'SMTP_MODE': mode})
        print(f"✓ SMTP mode set to: {mode}")
        if mode == 'direct':
            print("  Relay credentials cleared.")
        print("\nRestart smtp-relay to apply: docker compose up -d --force-recreate smtp-relay")

    elif args.set_smtp_relay:
        host, user, password = args.set_smtp_relay
        from config_manager import ENV_FILE
        cm._write_env_file(ENV_FILE, {
            'SMTP_MODE': 'relay',
            'SMTP_RELAY_HOST': host,
            'SMTP_RELAY_USERNAME': user,
            'SMTP_RELAY_PASSWORD': password,
        })
        print(f"✓ SMTP relay configured: {host} (user: {user})")
        print("\nRestart smtp-relay to apply: docker compose up -d --force-recreate smtp-relay")

    elif args.set_tls_mode:
        mode = args.set_tls_mode
        if mode == 'certificate':
            cert_path = os.path.join(CERTS_DIR, 'cert.pem')
            key_path = os.path.join(CERTS_DIR, 'key.pem')
            if not os.path.isfile(cert_path) or not os.path.isfile(key_path):
                print("⚠ Warning: Certificate files not found in ./certs/")
                print("  Expected: ./certs/cert.pem (full chain) and ./certs/key.pem (private key)")
                print("  Install them first with: rzfz setup --install-certificate <cert.pem> <key.pem>")
                confirm = input("Continue anyway? [y/N]: ").strip().lower()
                if confirm != 'y':
                    print("Aborted.")
                    sys.exit(0)
        cm.update_config({'TLS_MODE': mode})
        mode_labels = {'letsencrypt': "Let's Encrypt", 'selfsigned': 'Self-Signed', 'certificate': 'Custom Certificate'}
        print(f"✓ TLS mode set to: {mode_labels.get(mode, mode)}")
        print("\nRestart Caddy to apply: docker compose up -d --force-recreate caddy")

    elif args.install_certificate:
        import shutil
        cert_src, key_src = args.install_certificate
        certs_dir = CERTS_DIR
        cert_dst = os.path.join(certs_dir, 'cert.pem')
        key_dst = os.path.join(certs_dir, 'key.pem')

        # Validate source files exist
        if not os.path.isfile(cert_src):
            print(f"Error: Certificate file not found: {cert_src}")
            sys.exit(1)
        if not os.path.isfile(key_src):
            print(f"Error: Key file not found: {key_src}")
            sys.exit(1)

        # Basic PEM validation
        with open(cert_src, 'r') as f:
            cert_content = f.read()
        if '-----BEGIN CERTIFICATE-----' not in cert_content:
            print("Error: Certificate file does not appear to be a valid PEM certificate.")
            sys.exit(1)
        with open(key_src, 'r') as f:
            key_content = f.read()
        if '-----BEGIN' not in key_content or 'PRIVATE KEY' not in key_content:
            print("Error: Key file does not appear to be a valid PEM private key.")
            sys.exit(1)

        # Create certs directory if needed
        os.makedirs(certs_dir, exist_ok=True)

        # Copy files
        shutil.copy2(cert_src, cert_dst)
        shutil.copy2(key_src, key_dst)
        os.chmod(cert_dst, 0o644)
        os.chmod(key_dst, 0o600)
        print(f"✓ Certificate installed: {cert_dst}")
        print(f"✓ Private key installed: {key_dst}")

        # Auto-set TLS mode to certificate
        cm.update_config({'TLS_MODE': 'certificate'})
        print("✓ TLS mode set to: Custom Certificate")
        print("\nRestart Caddy to apply: docker compose up -d --force-recreate caddy")

    elif args.certificate_status:
        print("\n=== TLS Certificate Status ===")
        config = cm.get_all_config()
        mode = config.get('tls_mode', 'letsencrypt')
        mode_labels = {'letsencrypt': "🌐 Let's Encrypt", 'selfsigned': '🔐 Self-Signed', 'certificate': '📜 Custom Certificate'}
        print(f"  TLS Mode: {mode_labels.get(mode, mode)}")

        cert_path = os.path.join(CERTS_DIR, 'cert.pem')
        key_path = os.path.join(CERTS_DIR, 'key.pem')
        cert_exists = os.path.isfile(cert_path)
        key_exists = os.path.isfile(key_path)

        if cert_exists:
            cert_size = os.path.getsize(cert_path)
            print(f"  cert.pem: ✓ Found ({cert_size} bytes)")
        else:
            print("  cert.pem: ✗ Not found")

        if key_exists:
            key_size = os.path.getsize(key_path)
            print(f"  key.pem:  ✓ Found ({key_size} bytes)")
        else:
            print("  key.pem:  ✗ Not found")

        if mode == 'certificate' and (not cert_exists or not key_exists):
            print("\n  ⚠ TLS mode is 'certificate' but files are missing!")
            print("  Install them with: rzfz setup --install-certificate <cert.pem> <key.pem>")
        elif mode != 'certificate' and cert_exists and key_exists:
            print("\n  ℹ Certificate files are present but TLS mode is not 'certificate'.")
            print("  Switch with: rzfz setup --set-tls-mode certificate")
        print()

    elif args.set_backup_encryption_password is not None:
        password = args.set_backup_encryption_password
        if password == '__PROMPT__':
            # No value given — clear the custom password
            password = ''
        if cm.set_backup_encryption_password(password):
            if password:
                print("✓ Custom backup encryption password saved to .env")
                print("\nRestart backup-service to apply: docker compose up -d --force-recreate backup-service")
            else:
                print("✓ Custom backup encryption password cleared.")
                print("  Backups will be encrypted with the admin password (AUTHENTIK_BOOTSTRAP_PASSWORD).")
                print("\nRestart backup-service to apply: docker compose up -d --force-recreate backup-service")
        else:
            print("Error: Failed to save backup encryption password.")
            sys.exit(1)

    elif args.backup_encryption_status:
        status = cm.get_backup_encryption_status()
        print("\n=== Backup Encryption Status ===")
        if status['encryption_active']:
            print(f"  🔒 Encryption: Active")
            print(f"  Password source: {status['password_source']}")
        else:
            print(f"  ⚠ Encryption: No password available")
            print(f"  Backups will not include encrypted .env files.")
        if status['custom_password_set']:
            print(f"  Custom password: Set")
        else:
            print(f"  Custom password: Not set (using admin password as default)")
        print()

    elif args.set_entra_credentials:
        vals = args.set_entra_credentials
        if len(vals) < 3:
            print("Error: CLIENT_ID, CLIENT_SECRET, and TENANT_ID are required.")
            print("Usage: --set-entra-credentials CLIENT_ID CLIENT_SECRET TENANT_ID [DOMAIN]")
            sys.exit(1)
        client_id, client_secret, tenant_id = vals[0], vals[1], vals[2]
        domain = vals[3] if len(vals) > 3 else ""
        from config_manager import ENV_FILE
        cm._write_env_file(ENV_FILE, {
            'ENABLE_ENTRA_OAUTH': 'true',
            'ENTRA_CLIENT_ID': client_id,
            'ENTRA_CLIENT_SECRET': client_secret,
            'ENTRA_TENANT_ID': tenant_id,
            'ENTRA_OAUTH_DOMAIN': domain,
        })
        print("\n=== Microsoft Entra ID SSO Configured ===")
        print(f"  Client ID:  {client_id}")
        print(f"  Tenant ID:  {tenant_id}")
        print(f"  Domain:     {domain or '(none — all tenant accounts allowed)'}")
        print(f"\n  ✓ ENABLE_ENTRA_OAUTH=true written to .env")
        print(f"  Restart Authentik to apply blueprint changes:")
        print(f"    docker compose restart authentik-server authentik-worker")
        print()

    elif args.set_openwebui_oidc:
        client_id, client_secret = args.set_openwebui_oidc
        from config_manager import ENV_FILE
        cm._write_env_file(ENV_FILE, {
            'ENABLE_OPENWEBUI_OIDC': 'true',
            'OPENWEBUI_OIDC_CLIENT_ID': client_id,
            'OPENWEBUI_OIDC_CLIENT_SECRET': client_secret,
            'ENABLE_OAUTH_SIGNUP': 'true',
        })
        print("\n=== Open WebUI OIDC Configured [EXPERIMENTAL] ===")
        print(f"  Client ID: {client_id[:8]}...{client_id[-4:] if len(client_id) > 12 else ''}")
        print(f"\n  ✓ ENABLE_OPENWEBUI_OIDC=true written to .env")
        print(f"  Recreate Open WebUI to apply:")
        print(f"    docker compose up -d --force-recreate openwebui")
        print(f"\n  ⚠ [EXPERIMENTAL] Native OIDC login is experimental.")
        print(f"  Caddy forward-auth remains active as a defence-in-depth layer.")
        print()

    elif args.disable_openwebui_oidc:
        from config_manager import ENV_FILE
        cm._write_env_file(ENV_FILE, {
            'ENABLE_OPENWEBUI_OIDC': 'false',
            'OPENWEBUI_OIDC_CLIENT_ID': '',
            'OPENWEBUI_OIDC_CLIENT_SECRET': '',
        })
        print("\n=== Open WebUI OIDC Disabled ===")
        print(f"  ✓ ENABLE_OPENWEBUI_OIDC=false written to .env")
        print(f"  Recreate Open WebUI to apply:")
        print(f"    docker compose up -d --force-recreate openwebui")
        print()

    elif args.set_gitea_oidc:
        client_id, client_secret = args.set_gitea_oidc
        from config_manager import ENV_FILE
        cm._write_env_file(ENV_FILE, {
            'ENABLE_GITEA_AUTHENTIK_OIDC': 'true',
            'GITEA_OIDC_CLIENT_ID': client_id,
            'GITEA_OIDC_CLIENT_SECRET': client_secret,
        })
        print("\n=== Gitea Authentik OIDC Configured [EXPERIMENTAL] ===")
        print(f"  Client ID: {client_id[:8]}...{client_id[-4:] if len(client_id) > 12 else ''}")
        print(f"\n  ✓ ENABLE_GITEA_AUTHENTIK_OIDC=true written to .env")
        print(f"  Recreate Gitea to apply:")
        print(f"    docker compose up -d --force-recreate gitea")
        print(f"\n  ⚠ [EXPERIMENTAL] Native OIDC login is experimental.")
        print(f"  Caddy forward-auth remains active as a defence-in-depth layer.")
        print()

    elif args.disable_gitea_oidc:
        from config_manager import ENV_FILE
        cm._write_env_file(ENV_FILE, {
            'ENABLE_GITEA_AUTHENTIK_OIDC': 'false',
            'GITEA_OIDC_CLIENT_ID': '',
            'GITEA_OIDC_CLIENT_SECRET': '',
        })
        print("\n=== Gitea OIDC Disabled ===")
        print(f"  ✓ ENABLE_GITEA_AUTHENTIK_OIDC=false written to .env")
        print(f"  Recreate Gitea to apply:")
        print(f"    docker compose up -d --force-recreate gitea")
        print()

    elif args.lightrag_status:
        config = cm.get_lightrag_config()
        models = cm.get_gpustack_models()
        print("\n=== LightRAG Model Configuration ===")
        print(f"  LLM Model:       {config['llm_model'] or '⚠ Not configured'}")
        print(f"  Embedding Model: {config['embedding_model'] or '⚠ Not configured'}")
        print(f"  Reranking:       {config['rerank_binding']}")
        if config['rerank_binding'] != 'null':
            print(f"  Reranker Model:  {config['rerank_model'] or '⚠ Not configured'}")
        print(f"\n  Available GPUStack models: {', '.join(models) if models else '(could not fetch)'}")
        print()

    elif args.set_lightrag_models:
        models_arg = args.set_lightrag_models
        if len(models_arg) < 2:
            print("Error: At least LLM_MODEL and EMBEDDING_MODEL are required.")
            print("Usage: --set-lightrag-models <LLM_MODEL> <EMBEDDING_MODEL> [RERANK_MODEL]")
            sys.exit(1)
        llm_model = models_arg[0]
        embedding_model = models_arg[1]
        rerank_model = models_arg[2] if len(models_arg) > 2 else ""
        rerank_binding = "cohere" if rerank_model else "null"
        cm.set_lightrag_models(llm_model, embedding_model, rerank_binding, rerank_model)
        print(f"✓ LightRAG models updated:")
        print(f"  LLM:       {llm_model}")
        print(f"  Embedding: {embedding_model}")
        if rerank_model:
            print(f"  Reranker:  {rerank_model}")
        print(f"\nRestart lightrag container to apply: docker compose up -d --force-recreate lightrag")

    elif args.cognee_status:
        config = cm.get_cognee_config()
        models = cm.get_gpustack_models()
        print("\n=== Cognee Model Configuration [EXPERIMENTAL] ===")
        print(f"  LLM Model:       {config['llm_model'] or '⚠ Not configured'}")
        print(f"  Embedding Model: {config['embedding_model'] or '⚠ Not configured'}")
        print(f"  Embedding Dim:   {config['embedding_dim']}")
        print(f"\n  Available GPUStack models: {', '.join(models) if models else '(could not fetch)'}")
        print()

    elif args.set_cognee_models:
        models_arg = args.set_cognee_models
        if len(models_arg) < 2:
            print("Error: At least LLM_MODEL and EMBEDDING_MODEL are required.")
            print("Usage: --set-cognee-models <LLM_MODEL> <EMBEDDING_MODEL> [EMBEDDING_DIM]")
            sys.exit(1)
        llm_model = models_arg[0]
        embedding_model = models_arg[1]
        embedding_dim = models_arg[2] if len(models_arg) > 2 else "768"
        cm.set_cognee_models(llm_model, embedding_model, embedding_dim)
        print(f"✓ Cognee models updated:")
        print(f"  LLM:           {llm_model}")
        print(f"  Embedding:     {embedding_model}")
        print(f"  Embedding Dim: {embedding_dim}")
        print(f"\nRestart cognee container to apply: docker compose up -d --force-recreate cognee")

    elif args.paperclip_status:
        env_config = cm._read_env_file(cm.ENV_FILE)
        active = 'paperclip' in env_config.get('COMPOSE_PROFILES', '').split(',')
        print(f"Paperclip [EXPERIMENTAL]: {'ENABLED' if active else 'DISABLED'}")
        print(f"  Domain: {env_config.get('PAPERCLIP_DOMAIN', '(not set)')}")
        print(f"  Port:   {env_config.get('PAPERCLIP_PORT', '3100')}")

    elif args.moltis_status:
        env_config = cm._read_env_file(cm.ENV_FILE)
        active = 'moltis' in env_config.get('COMPOSE_PROFILES', '').split(',')
        print(f"Moltis [EXPERIMENTAL]: {'ENABLED' if active else 'DISABLED'}")
        print(f"  Domain: {env_config.get('MOLTIS_DOMAIN', '(not set)')}")
        print(f"  Matrix: {env_config.get('MOLTIS_MATRIX_HOMESERVER', '(not set)')}")

    elif args.hermes_status:
        env_config = cm._read_env_file(cm.ENV_FILE)
        active = 'hermes' in env_config.get('COMPOSE_PROFILES', '').split(',')
        print(f"Hermes Agent [EXPERIMENTAL]: {'ENABLED' if active else 'DISABLED'}")
        print(f"  Domain: {env_config.get('HERMES_DOMAIN', '(not set)')}")
        print(f"  Port:   {env_config.get('HERMES_PORT', '3101')}")

    elif args.matrix_status:
        env_config = cm._read_env_file(cm.ENV_FILE)
        active = 'matrix' in env_config.get('COMPOSE_PROFILES', '').split(',')
        print(f"Matrix [EXPERIMENTAL]: {'ENABLED' if active else 'DISABLED'}")
        print(f"  Matrix API:  https://{env_config.get('MATRIX_DOMAIN', '(not set)')} (Synapse homeserver)")
        print(f"  Matrix UI:   https://{env_config.get('ELEMENT_WEB_DOMAIN', '(not set)')} (Element Web client)")
        print(f"  Server name: {env_config.get('SYNAPSE_SERVER_NAME', '(not set)')} (PERMANENT)")

    elif args.checksum_take is not None:
        if not ChecksumManager:
            print("Error: checksum_manager not available."); sys.exit(1)
        csm = ChecksumManager()
        comment = args.checksum_take
        print(f"Taking checksum snapshot: {comment}")
        result = csm.take_checksum(comment, source="cli")
        print(f"✓ Checksum set #{result['set_id']} created")
        print(f"  Files:   {result['file_count']}")
        print(f"  Overall: {result['overall_sha256']}")

    elif args.checksum_history:
        if not ChecksumManager:
            print("Error: checksum_manager not available."); sys.exit(1)
        csm = ChecksumManager()
        history = csm.get_history(limit=50)
        if not history:
            print("No checksum snapshots yet.")
        else:
            print(f"\n{'ID':>4}  {'Timestamp':<20}  {'Source':<8}  {'Files':>5}  {'Comment'}")
            print("-" * 80)
            for h in history:
                print(f"{h['id']:>4}  {h['timestamp']:<20}  {h['source']:<8}  {h['file_count']:>5}  {h['comment']}")

    elif args.checksum_detail is not None:
        if not ChecksumManager:
            print("Error: checksum_manager not available."); sys.exit(1)
        csm = ChecksumManager()
        detail = csm.get_set_detail(args.checksum_detail)
        if not detail:
            print(f"Checksum set #{args.checksum_detail} not found."); sys.exit(1)
        print(f"\n=== Checksum Set #{detail['set_id']} ===")
        print(f"  Timestamp: {detail['timestamp']}")
        print(f"  Comment:   {detail['comment']}")
        print(f"  Source:    {detail['source']}")
        print(f"  Overall:   {detail['overall_sha256']}")
        print(f"\n{'File':<60}  {'SHA256':<18}  {'Size':>8}")
        print("-" * 90)
        for f in detail['files']:
            h = f['sha256'][:16] + '…' if len(f['sha256']) > 16 else f['sha256']
            s = f"{f['file_size'] / 1024:.1f}K" if f['file_size'] > 0 else "—"
            print(f"{f['filepath']:<60}  {h:<18}  {s:>8}")

    elif args.checksum_diff is not None:
        if not ChecksumManager:
            print("Error: checksum_manager not available."); sys.exit(1)
        csm = ChecksumManager()
        id_a, id_b = args.checksum_diff
        diff = csm.get_diff(id_a, id_b)
        if not diff:
            print("Could not compute diff. Check that both sets exist."); sys.exit(1)
        status_label = "CHANGED" if diff['overall_changed'] else "IDENTICAL"
        print(f"\n=== Diff: Set #{id_a} → #{id_b} ({status_label}) ===")
        print(f"  Set #{id_a}: {diff['set_a']['timestamp']} — {diff['set_a']['comment']}")
        print(f"  Set #{id_b}: {diff['set_b']['timestamp']} — {diff['set_b']['comment']}")
        if diff['changes']:
            print(f"\n  {'Status':<10}  {'File'}")
            print("  " + "-" * 70)
            for ch in diff['changes']:
                print(f"  {ch['status'].upper():<10}  {ch['filepath']}")
        else:
            print("\n  No file-level changes detected.")

    elif args.checksum_status:
        if not ChecksumManager:
            print("Error: checksum_manager not available."); sys.exit(1)
        csm = ChecksumManager()
        current = csm.get_current_overall()
        if current:
            print(f"Current governance fingerprint: {current}")
        else:
            print("No checksum baseline found. Take one with: --checksum-take")

    elif args.logs_take is not None:
        if not LogManager:
            print("Error: log_manager not available."); sys.exit(1)
        lm = LogManager()
        reason = args.logs_take
        print(f"Creating log snapshot: {reason}")
        print("Collecting container logs (this may take a moment)...")
        result = lm.create_snapshot(reason=reason)
        print(f"✓ Snapshot created: {result['filename']} ({result['size_human']})")
        print(f"  Containers: {result['container_count']}")
        print(f"  Location:   {result['filepath']}")

    elif args.logs_list:
        if not LogManager:
            print("Error: log_manager not available."); sys.exit(1)
        lm = LogManager()
        snapshots = lm.list_snapshots()
        if not snapshots:
            print("No log snapshots yet.")
        else:
            print(f"\n{'Filename':<40}  {'Created':<24}  {'Size':>10}")
            print("-" * 78)
            for s in snapshots:
                print(f"{s['filename']:<40}  {s['created']:<24}  {s['size_human']:>10}")

    elif args.migrate_agent_volumes:
        # M030-S5: minimal migration tooling. Operator clarified that no
        # prod instances need migration in this rollout (the operator's
        # 5 agents on prod will be re-provisioned manually with the new
        # M030 catalog, accepting the data loss for those test instances).
        # This command exists for completeness and future-proofing — when
        # an actual M030 deployment over a non-trivial install needs to
        # migrate, the tooling is here.
        #
        # Implementation lives in agents/manager (it has the docker SDK +
        # DB access already). We delegate via docker exec.
        import subprocess as _sp
        dry = "--dry-run" if args.dry_run else ""
        try:
            r = _sp.run(
                ["docker", "exec", "-w", "/app", "-e", "PYTHONPATH=/app",
                 "agent-manager", "python3", "-m", "app.services.volume_migrator",
                 *( [dry] if dry else [] )],
                check=False,
            )
            sys.exit(r.returncode)
        except FileNotFoundError:
            print("Error: docker CLI not available in this container.")
            sys.exit(1)

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
