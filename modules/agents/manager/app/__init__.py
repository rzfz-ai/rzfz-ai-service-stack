# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Agent Manager — per-user agent instance lifecycle management."""

import os

from flask import Flask


def create_app():
    app = Flask(__name__,
                template_folder='templates',
                static_folder='static')

    app.secret_key = os.environ['WEBUI_SECRET_KEY']
    app.config['SESSION_COOKIE_NAME'] = 'razzfazz_agents'
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    # Stack config from environment
    app.config['DATABASE_URL'] = os.environ['AGENT_MANAGER_DATABASE_URL']
    app.config['DOCKER_HOST'] = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
    app.config['CADDY_ADMIN_URL'] = os.environ.get('CADDY_ADMIN_URL', 'http://caddy:2019')
    app.config['MAIN_DOMAIN'] = os.environ.get('MAIN_DOMAIN', 'localhost')
    app.config['AGENTS_DOMAIN'] = os.environ.get('AGENTS_DOMAIN', f'agents.{app.config["MAIN_DOMAIN"]}')
    app.config['AGENT_NETWORK'] = os.environ.get('AGENT_NETWORK', 'agent-network')

    # Resource governance
    app.config['AGENT_MAX_INSTANCES'] = int(os.environ.get('AGENT_MAX_INSTANCES', '20'))
    app.config['AGENT_IDLE_TIMEOUT_LIGHTWEIGHT'] = int(os.environ.get('AGENT_IDLE_TIMEOUT_LIGHTWEIGHT', '1800'))
    app.config['AGENT_IDLE_TIMEOUT_HEAVY'] = int(os.environ.get('AGENT_IDLE_TIMEOUT_HEAVY', '7200'))
    app.config['AGENT_CLEANUP_AFTER_DAYS'] = int(os.environ.get('AGENT_CLEANUP_AFTER_DAYS', '30'))
    # #36 / PR #84 — agent memory governance. The per-instance max + default are
    # operator env (rarely changed); the global budget + per-user cap are
    # DB-backed (agent_settings) so they apply live and dodge the Config-UI .env
    # inode-write bug. AGENT_CORE_STACK_RESERVE_MB is the RAM held back for the
    # core stack when deriving the dynamic host ceiling for the budget.
    app.config['AGENT_MEM_PER_INSTANCE_MAX_GB'] = int(os.environ.get('AGENT_MEM_PER_INSTANCE_MAX_GB', '16'))
    app.config['AGENT_MEM_DEFAULT_GB'] = int(os.environ.get('AGENT_MEM_DEFAULT_GB', '2'))
    app.config['AGENT_CORE_STACK_RESERVE_MB'] = int(os.environ.get('AGENT_CORE_STACK_RESERVE_MB', '8192'))
    # rc6.7 #93: idle auto-stop is OFF by default — per-user agents are
    # designed to do background work (matrix bots, scheduled jobs, sandbox
    # callbacks) and shouldn't get reaped just because the user is away
    # from the UI. Set AGENT_IDLE_AUTO_STOP_ENABLED=true to re-enable on
    # memory-bound boxes.
    app.config['AGENT_IDLE_AUTO_STOP_ENABLED'] = (
        os.environ.get('AGENT_IDLE_AUTO_STOP_ENABLED', 'false').strip().lower()
        in ('true', '1', 'yes', 'on')
    )

    # Initialize database
    from app.services.database import Database
    app.db = Database(app.config['DATABASE_URL'])
    app.db.migrate()

    # Initialize Docker client
    from app.services.docker_client import AgentDockerClient
    app.docker_client = AgentDockerClient(
        base_url=app.config['DOCKER_HOST'],
        network=app.config['AGENT_NETWORK'],
    )

    # Initialize Caddy client
    from app.services.caddy_client import CaddyClient
    app.caddy_client = CaddyClient(
        admin_url=app.config['CADDY_ADMIN_URL'],
        agents_domain=app.config['AGENTS_DOMAIN'],
    )

    # Initialize Authentik client (PR #84 C1): registers/deregisters the
    # per-instance forward_single forward-auth provider so the embedded outpost
    # authenticates each coding-agent subdomain. No-ops (with a warning) when
    # AUTHENTIK_BOOTSTRAP_TOKEN is unset.
    from app.services.authentik_client import AuthentikClient
    app.authentik_client = AuthentikClient()

    # Load agent type catalog
    from app.services.catalog import AgentCatalog
    app.catalog = AgentCatalog(app.db)

    # Initialize provisioning engine
    from app.services.provisioner import Provisioner
    app.provisioner = Provisioner(
        db=app.db,
        docker_client=app.docker_client,
        caddy_client=app.caddy_client,
        catalog=app.catalog,
        config=app.config,
        authentik_client=app.authentik_client,
    )

    # Start idle scheduler
    from app.services.lifecycle import LifecycleManager
    app.lifecycle = LifecycleManager(
        db=app.db,
        docker_client=app.docker_client,
        caddy_client=app.caddy_client,
        config=app.config,
        provisioner=app.provisioner,
    )
    app.lifecycle.start()

    # Register blueprints
    from app.blueprints.health import health_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.api import api_bp
    from app.blueprints.admin import admin_bp
    from app.blueprints.proxy import proxy_bp
    from app.blueprints.terminal import terminal_bp, init_terminal

    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(terminal_bp)
    init_terminal(app)

    # Proxy blueprint handles subdomain requests — register with lowest priority
    # by using before_request to check if this is a subdomain request
    from app.blueprints.proxy import proxy_bp, init_proxy_ws
    app.register_blueprint(proxy_bp)
    # Per-instance WebSocket proxy for the coding-agent terminal (item A, PR
    # #84). Registered as a real flask-sock route so the /terminal WS upgrade is
    # handled by ws_terminal, NOT hijacked by the HTTP route_subdomains shim.
    init_proxy_ws(app)

    @app.before_request
    def route_subdomains():
        """Route per-instance subdomain requests to the proxy blueprint."""
        from flask import request as req
        host = req.host.split(':')[0]
        agents_domain = app.config.get('AGENTS_DOMAIN', '')
        # If this is a subdomain of agents_domain (not agents_domain itself),
        # let the proxy blueprint handle it
        if agents_domain and host != agents_domain and host.endswith(f'.{agents_domain}'):
            # Skip dashboard/api/admin routes — proxy handles everything.
            # ANY WebSocket-upgrade request on a per-instance subdomain is the
            # agent's WS (coding-agent `/terminal`, moltis `/ws/chat`, hermes's
            # own, or a user-defined path). It MUST reach the flask-sock
            # ws_terminal bridge (which upgrades + proxies to the container),
            # NOT be swallowed by the HTTP proxy_to_agent (httpx can't upgrade a
            # WS → the moltis "Loading LLMs…" hang / `/ws/chat` reconnect loop,
            # #36). Detect the upgrade via the `Upgrade: websocket` header (RFC
            # 6455) — case-insensitively — and let normal routing dispatch it to
            # the flask-sock route (exact `/terminal` or the generic
            # `<path:ws_path>`). Security is unchanged: ws_terminal re-runs the
            # SAME proxy-proof (source-IP) + ownership checks before opening the
            # upstream socket, so a non-owner / non-Caddy caller is still 403'd.
            if req.headers.get('Upgrade', '').lower() == 'websocket':
                return None
            # PR #84 fix (browser gate): on an INSTANCE subdomain, `/static/*`
            # belongs to the proxied agent container (the coding-agent web UI
            # serves its vendored xterm.min.js / xterm.min.css / addon under
            # `/static/vendor/…`). It must be proxied — NOT served from the
            # manager's own `static_folder` (which holds only the dashboard's
            # styles.css → a Flask 404 for the agent's assets). That 404 left
            # `window.Terminal` undefined and broke the terminal in the browser
            # (curl only ever fetched `/`, so it never caught this). The
            # manager's own dashboard `/static/styles.css` is served on the bare
            # agents.<domain> host, which this branch already excludes
            # (host != agents_domain), so proxying `/static/` here is safe.
            if not req.path.startswith('/healthz'):
                from app.blueprints.proxy import proxy_to_agent
                return proxy_to_agent(req.path.lstrip('/'))

    # Template context processor — inject user info from Authentik headers.
    # M033 A4: parse via the shared lib instead of reading headers inline.
    # NB: is_admin now checks exact group membership against the parsed list
    # (vs the prior substring check against the raw header) — equivalent for
    # these two group names and more correct.
    @app.context_processor
    def inject_user():
        from razzfazz_common.auth import parse_authentik_headers
        info = parse_authentik_headers()
        admin_groups = ('authentik Admins', 'razzfazz.ai Super Admins')
        return {
            'current_user': info['username'] or 'anonymous',
            'current_groups': info['groups'],
            'is_admin': any(g in admin_groups for g in info['groups']),
            'config': app.config,
        }

    # M030-S5: opportunistic detection of per-user agent instances whose
    # volumes don't match the current catalog's named-volume layout. Logs
    # a one-time warning + remediation hint at startup so operators can't
    # silently sit on pre-M030 anonymous-volume instances.
    try:
        from app.services.volume_migrator import detect_candidates
        candidates = detect_candidates(app.db, app.docker_client, app.catalog)
        if candidates:
            import logging
            log = logging.getLogger(__name__)
            log.warning(
                "M030: detected %d agent instance(s) with anonymous volumes "
                "that should be named per the current catalog. Run "
                "`razzfazz-setup.sh --migrate-agent-volumes --dry-run` to "
                "preview migration, then re-run without --dry-run to apply.",
                len(candidates),
            )
            for c in candidates:
                log.warning(
                    "  - %s (type=%s slug=%s) missing named volumes: %s",
                    c['container_name'], c['agent_type'], c['user_slug'],
                    ', '.join(c['missing_volumes']),
                )
    except Exception as e:
        # Don't block startup on detection failures
        import logging
        logging.getLogger(__name__).warning(
            "M030-S5 startup detection failed (non-fatal): %s", e)

    return app
