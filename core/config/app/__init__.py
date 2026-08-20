# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import os
import yaml

from razzfazz_common.flask_app import create_base_app
from razzfazz_common.env_utils import read_env_key


def create_app():
    # M026 S05 #5 + #148 followup: shared base app provides /healthz,
    # session security flags, the {razzfazz_version, stack_version,
    # main_domain, brand_color} template context, and (via context_extras)
    # this container's nav-data injection. Cookie name passed via the new
    # cookie_name kwarg (was a local app.config override pre-#148).
    # require_auth=False because this container runs its OWN before_request
    # gate in app/auth.py. As of #18 (Option A) that gate TRUSTS the
    # Authentik SSO it sits behind (X-Authentik-* headers, admin-group
    # check) — same model as the Help UI — and keeps the static
    # ADMIN_PASSWORD only as a break-glass fallback for direct host-port
    # access. We can't just set require_auth=True because we still need the
    # break-glass password path when SSO/Authentik is down.
    app = create_base_app(
        __name__,
        service_name='razzfazz-config',
        secret_key_env='CONFIG_SECRET_KEY',
        cookie_name='razzfazz_config',
        require_auth=False,
        context_extras=lambda: _nav_extras(app),
        # Flask defaults to static_url_path='/static' but this app's
        # templates, CSS (`background: url('/branding/media/razzfazz.png')`),
        # auth middleware exemption (auth.py line ~30 checks both '/static/'
        # and '/branding/'), and the cycle release-notes markdown (every
        # `<img src="/branding/media/X.png">`) all assume `/branding`. The
        # path was lost when the shared `create_base_app` factory replaced
        # the per-container Flask() call. Restoring it makes /branding/<rel>
        # serve /app/app/static/<rel> — fixes broken icons in the Release
        # Notes / What's New modals (v2026.05-ga.4 hotfix).
        static_url_path='/branding',
    )

    # Load stack root path (mounted volume)
    app.config['STACK_ROOT'] = os.environ.get('STACK_ROOT', '/stack')
    app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD', '')
    app.config['AUDIT_LOG_PATH'] = os.environ.get('AUDIT_LOG_PATH', '/var/log/razzfazz/config-audit.log')

    # Load profiles manifest
    manifest_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'profiles.yaml')
    with open(manifest_path) as f:
        app.config['PROFILES'] = yaml.safe_load(f)

    # Initialize services (stored on app for access from blueprints)
    from app.services.audit_logger import AuditLogger
    from app.services.profile_manager import ProfileManager
    from app.services.resource_monitor import ResourceMonitor
    from app.services.apply_manager import ApplyManager
    from app.services.config_manager import ConfigManager
    from app.services.checksum_manager import ChecksumManager
    from app.services.image_checker import ImageChecker

    app.audit_logger = AuditLogger(app.config['AUDIT_LOG_PATH'])
    app.profile_manager = ProfileManager(
        manifest_path,
        env_path=os.path.join(app.config['STACK_ROOT'], '.env')
    )
    app.config_manager = ConfigManager(app.config['STACK_ROOT'])
    app.resource_monitor = ResourceMonitor(app.profile_manager, app.config_manager)
    app.resource_monitor.start()
    app.apply_manager = ApplyManager(
        app.config['STACK_ROOT'],
        app.profile_manager,
        app.resource_monitor,
        app.audit_logger,
        config_manager=app.config_manager,
    )
    app.checksum_manager = ChecksumManager()
    app.apply_manager.checksum_manager = app.checksum_manager
    app.image_checker = ImageChecker(app.profile_manager)

    # Register auth middleware (#18: Authentik-SSO trust + admin-group gate
    # via razzfazz_common.auth, with the static ADMIN_PASSWORD demoted to a
    # break-glass fallback). Kept as an app-wide before_request rather than
    # per-route decorators because this container gates the whole portal.
    from app.auth import auth_bp, register_auth_middleware
    app.register_blueprint(auth_bp)
    register_auth_middleware(app)

    # Register blueprints
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.modules import modules_bp
    from app.blueprints.api import api_bp
    from app.blueprints.settings import settings_bp
    from app.blueprints.backup import backup_bp
    from app.blueprints.docs import docs_bp
    from app.blueprints.governance import governance_bp
    from app.blueprints.logs import logs_bp
    from app.blueprints.licenses import licenses_bp
    from app.blueprints.security import security_bp
    from app.blueprints.offline import offline_bp
    from app.blueprints.network_policy import network_policy_bp
    from app.blueprints.mac_backends import mac_backends_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(modules_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(governance_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(licenses_bp)
    app.register_blueprint(security_bp)
    app.register_blueprint(offline_bp)
    app.register_blueprint(network_policy_bp)
    # #168 P2 (2026.08) — Mac LLM backends panel (mac-llm profile). The blueprint
    # itself guards on the profile being enabled; the nav link is likewise gated.
    app.register_blueprint(mac_backends_bp, url_prefix='/mac-backends')

    # Read MAIN_DOMAIN from .env (cached at startup — only changes on reinstall).
    # Uses razzfazz_common.read_env_key so the parser semantics match
    # razzfazz-upgrade.sh's read_env_value (rc6.2+).
    env_path = os.path.join(app.config['STACK_ROOT'], '.env')
    main_domain = read_env_key(env_path, 'MAIN_DOMAIN')
    if main_domain:
        app.config['MAIN_DOMAIN'] = main_domain

    # Nav-data extras (#148): wired in above via create_base_app's
    # context_extras kwarg. razzfazz_common.register_context_processor
    # already injects `razzfazz_version`, `stack_version`,
    # `main_domain`, and `brand_color`; this just adds the bits config
    # templates additionally rely on.

    # Jinja2 filter: country code → flag emoji (e.g. "US" → "🇺🇸")
    @app.template_filter('flag')
    def country_flag(code):
        if not code or code.lower() == 'razzfazz':
            return code or ''
        code = code.upper()
        # EU is not a country code but widely recognized
        if len(code) == 2 and code.isalpha():
            return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code)
        return code

    return app


def _nav_extras(app):
    """Return the per-request template extras config UI templates expect.

    Pulled out of create_app() so it can be passed to create_base_app's
    context_extras kwarg (#148). Closure over `app` lets us reach
    profile_manager + resource_monitor at request time.
    """
    profiles = app.profile_manager.get_all_profiles()
    enabled = app.profile_manager.get_enabled_profiles()
    profile_resources = app.resource_monitor.get_profile_resources()

    total = len(profiles)
    enabled_count = sum(1 for pid in profiles if pid in enabled or pid == 'core')
    running_count = sum(1 for pid in profiles
                       if pid in profile_resources and profile_resources[pid].get('total_mb', 0) > 0)
    disabled_count = total - enabled_count

    # ga.1 (Issue E): the Security Documentation nav link 404s on public/Codeberg
    # boxes because docs/security-architecture.md is culled from the public export
    # (only docs/community ships). Gate the nav entry on the doc being present so
    # those boxes don't advertise a link that dead-ends in a 404. Checked per-request
    # (cheap os.path.exists) so an on-box generate/overlay-sync lights it up live.
    # #171: also count the box-local Enterprise overlay copy — customer/Codeberg
    # boxes render the current gated doc via the security blueprint's overlay
    # fallback (see blueprints/security.py:_resolve_doc_path), so the nav link must
    # light up when EITHER the tracked doc OR the overlay copy is present.
    stack_root = app.config['STACK_ROOT']
    security_doc_path = os.path.join(stack_root, 'docs', 'security-architecture.md')
    security_overlay_path = os.path.join(
        stack_root, 'overlay', 'enterprise', 'security-architecture.md')

    return {
        'nav_module_counts': {
            'total': total,
            'enabled': enabled_count,
            'running': running_count,
            'disabled': disabled_count,
        },
        'enabled_profiles': enabled,
        'security_doc_available': (
            os.path.isfile(security_doc_path) or os.path.isfile(security_overlay_path)),
    }
