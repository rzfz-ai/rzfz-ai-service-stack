# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Agent Manager — per-user agent instance lifecycle management."""

import os

from flask import Flask


def create_app(*, background_jobs: bool = True):
    """Build the agent-manager app.

    `background_jobs=False` is the read-only/CLI mode used by the in-container
    `rzfz agents` entrypoint (`cli_main`): no LifecycleManager scheduler, no
    schema migrations, no mutating startup backfills — so `list`, `--dry-run`
    and the upgrade-summary probe run in a second process that can never start
    a background recreate or race the running manager's (#3187 review).
    """
    app = Flask(__name__,
                template_folder='templates',
                static_folder='static')

    # #959 Task A1: wire the shared razz_base.html shell onto this app's
    # Jinja loader — the same thing razzfazz_common.flask_app's
    # create_base_app() does for config/help/setup/backup. Agent Manager
    # builds its Flask app directly (not via create_base_app), so it never
    # picked this up; without it, base_shell.html's `{% extends
    # "razz_base.html" %}` raises TemplateNotFound at render time. The
    # original (local) loader stays first so per-container template names
    # always win; only un-resolved names (razz_base.html) fall through.
    import razzfazz_common
    from jinja2 import ChoiceLoader, FileSystemLoader
    _shared_template_dir = os.path.join(os.path.dirname(razzfazz_common.__file__), 'templates')
    if os.path.isdir(_shared_template_dir) and app.jinja_loader is not None:
        app.jinja_loader = ChoiceLoader([app.jinja_loader, FileSystemLoader(_shared_template_dir)])

    app.secret_key = os.environ['WEBUI_SECRET_KEY']
    app.config['SESSION_COOKIE_NAME'] = 'razzfazz_agents'
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    # Stack config from environment
    app.config['DATABASE_URL'] = os.environ['AGENT_MANAGER_DATABASE_URL']
    app.config['DOCKER_HOST'] = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock')
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

    # #3187: auto-update instances left on an old image once they are idle.
    # Enabled by default. The SUPPORTED configuration path is DB-backed
    # (agent_settings via the admin /api/config endpoint, resolved live per pass
    # — see app.services.agent_updates.auto_update_enabled); the env vars below
    # are the container-level fallback when no DB value is set. The idle window
    # is NOT defaulted here on purpose: an explicit override wins when set,
    # otherwise the lifecycle derives it from the instance's own idle_timeout /
    # the AGENT_IDLE_TIMEOUT_* fallbacks (the same "idle" the reaper uses).
    app.config['AGENT_AUTO_UPDATE_ENABLED'] = (
        os.environ.get('AGENT_AUTO_UPDATE_ENABLED', 'true').strip().lower()
        in ('true', '1', 'yes', 'on')
    )
    _update_window = os.environ.get('AGENT_UPDATE_IDLE_WINDOW_SECONDS', '').strip()
    app.config['AGENT_UPDATE_IDLE_WINDOW_SECONDS'] = (
        int(_update_window) if _update_window else None)

    # Initialize database
    from app.services.database import Database
    app.db = Database(app.config['DATABASE_URL'])
    if background_jobs:
        app.db.migrate()
    # else: read-only CLI mode — the running manager owns the schema; this
    # process must not migrate or backfill anything (#3187 review).

    # Initialize Docker client
    from app.services.docker_client import AgentDockerClient
    app.docker_client = AgentDockerClient(
        base_url=app.config['DOCKER_HOST'],
        network=app.config['AGENT_NETWORK'],
    )

    # Initialize Caddy client
    from app.services.caddy_client import CaddyClient
    app.caddy_client = CaddyClient(agents_domain=app.config['AGENTS_DOMAIN'])

    # Initialize Authentik client (PR #84 C1): registers/deregisters the
    # per-instance forward_single forward-auth provider so the embedded outpost
    # authenticates each coding-agent subdomain. No-ops (with a warning) when
    # AUTHENTIK_BOOTSTRAP_TOKEN is unset.
    from app.services.authentik_client import AuthentikClient
    app.authentik_client = AuthentikClient()

    # Load agent type catalog
    from app.services.catalog import AgentCatalog
    app.catalog = AgentCatalog(app.db)

    # #2476 (A21): the owner-scoped OpenBao token broker (None without
    # OPENBAO_ADDR). Mints nothing until OPENBAO_SUB_IS_UID=true (A19's gate).
    from app.services.openbao_tokens import build_broker
    try:
        token_broker = build_broker(os.environ, app.docker_client)
    except Exception as e:  # noqa: BLE001 — no broker, no tokens; never fatal
        import logging
        logging.getLogger(__name__).warning("openbao token broker not wired: %s", e)
        token_broker = None

    # Initialize provisioning engine
    from app.services.provisioner import Provisioner
    app.provisioner = Provisioner(
        db=app.db,
        docker_client=app.docker_client,
        caddy_client=app.caddy_client,
        catalog=app.catalog,
        config=app.config,
        authentik_client=app.authentik_client,
        # #2482 C6: `running` only once a web pane's first load would work.
        readiness_gate=True,
        token_broker=token_broker,
    )

    # E1 (#2518): record the volume names each pre-migration-14 instance's
    # running container ACTUALLY mounts, once (never overwrites; only reads
    # Mounts). Recreate + delete then bind to those names instead of
    # recomputing them from the row slug. Docker unreachable → skipped, logged,
    # retried on the next start. Never fatal. Skipped in read-only CLI mode.
    if background_jobs:
        try:
            from app.services.slug_migration import run_startup_backfill
            run_startup_backfill(app.db, app.docker_client, app.db.get_agent_type)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "#2518 mounted_volumes back-fill failed (non-fatal): %s", e)

        # #2529/#1039: per-instance databases — persist their names (and, from
        # C2 on, repair a missing password) BEFORE the scheduler or a request can
        # recreate an instance from its row. Never fatal. Skipped in read-only
        # CLI mode.
        try:
            from app.services.instance_db import run_startup_db_backfill
            run_startup_db_backfill(app.db, app.docker_client, app.provisioner)
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "#2529 per-instance DB back-fill failed (non-fatal): %s", e)

    # Start idle scheduler — only in the serving process. The CLI read path
    # (background_jobs=False) must never start a second scheduler with its own
    # instance locks in a second process (#3187 review).
    if background_jobs:
        from app.services.lifecycle import LifecycleManager
        app.lifecycle = LifecycleManager(
            db=app.db,
            docker_client=app.docker_client,
            caddy_client=app.caddy_client,
            config=app.config,
            provisioner=app.provisioner,
        )
        app.lifecycle.start()
    else:
        app.lifecycle = None

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

    # #2476 (A21): the agents' read-only, token-forwarding way to OpenBao.
    from app.blueprints.bao import bao_bp
    app.register_blueprint(bao_bp)
    app.bao_proxy = None
    if token_broker is not None and os.environ.get('OPENBAO_ADDR'):
        from app.services.bao_proxy import BaoProxy
        app.bao_proxy = BaoProxy(os.environ['OPENBAO_ADDR'], app.db, app.docker_client.container_ips)

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
            # #2597 review I1: `/branding/*` is not an agent path. The base
            # agents site serves it from Caddy (branding_static); on an
            # instance host nothing serves it, and Caddy used to exempt it from
            # forward_auth, so it arrived here unauthenticated. Answer it HERE,
            # before the WebSocket and proxy branches: never resolved, never
            # lazy-started, never forwarded into a container, whatever the
            # method or headers.
            if req.path.startswith('/branding/'):
                from flask import Response
                return Response('Not found', status=404,
                                content_type='text/plain; charset=utf-8')
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
        from razzfazz_common.rbac_guard import allows
        info = parse_authentik_headers()
        return {
            'current_user': info['username'] or 'anonymous',
            'current_groups': info['groups'],
            'is_admin': allows('agent-manager', 'admin', info['groups']),  # #2473
            'config': app.config,
            # #959 Task A1: cache-bust base_shell.html's styles.css link
            # across upgrades (same pattern as core/config/app/__init__.py).
            'stack_version': os.environ.get('RAZZFAZZ_VERSION', '0'),
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
