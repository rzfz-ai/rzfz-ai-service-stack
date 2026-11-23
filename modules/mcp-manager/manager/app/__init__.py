# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Personal MCP Manager (#36) — per-user MCP-proxy provisioning + credential vault.

Mirrors agent-manager's app factory. Wires the encrypted credential store, the
personal-MCP catalog, the per-user proxy provisioner, the Caddy route client,
the OAuth flow, and the boot route-reconcile scheduler.

SECURITY: this service holds the master encryption key (MCP_MANAGER_SECRET_KEY)
in memory only; credentials are AES-GCM encrypted at rest in mcp_manager_db.
Identity is always the Authentik forward-auth headers (only-own). Talks to
docker via docker-socket-proxy (same constrained grants as agent-manager).
"""

import logging
import os

from flask import Flask

logger = logging.getLogger(__name__)


def _configure_app_logging() -> None:
    """#3127: nothing configured a log level, so the app's INFO lines (the
    instance-roll summary and 16 more `logger.info` sites) never reached
    `docker logs mcp-manager`: gunicorn sets up only its own loggers —
    `gunicorn.error`/`gunicorn.access`, both `propagate = False` — and
    leaves the root logger handlerless, so the app's records fell to
    Python's last-resort handler, which emits WARNING and above only. Give
    the `app` loggers ONE INFO handler on stderr — the stream `docker logs`
    already shows gunicorn's own lines on. The handler is the app logger's
    own, never the root logger's: a root handler would carry every
    library's INFO too and invite double emission, and `propagate = False`
    keeps the app off the root even if something later configures it, so
    no line can appear twice. Idempotent, and it stands down when the host
    process already configured logging (a test runner, a dev server): the
    app's records then keep flowing to the host's handlers by propagation.
    WARNING/ERROR lines are unchanged — the same bare text, now through
    this handler instead of the last-resort one."""
    app_logger = logging.getLogger("app")
    if app_logger.handlers or logging.getLogger().handlers:
        return
    app_logger.addHandler(logging.StreamHandler())
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False


def create_app():
    # #3127: before anything below logs, so every app line takes this path.
    _configure_app_logging()
    app = Flask(__name__, template_folder="templates", static_folder="static")

    app.secret_key = os.environ.get("WEBUI_SECRET_KEY", os.environ["MCP_MANAGER_SECRET_KEY"])
    app.config["SESSION_COOKIE_NAME"] = "razzfazz_mcp"
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.config["DATABASE_URL"] = os.environ["MCP_MANAGER_DATABASE_URL"]
    app.config["DOCKER_HOST"] = os.environ.get("DOCKER_HOST", "tcp://docker-socket-proxy:2375")
    app.config["MAIN_DOMAIN"] = os.environ.get("MAIN_DOMAIN", "localhost")
    app.config["MCP_DOMAIN"] = os.environ.get("MCP_DOMAIN", f"mcp.{app.config['MAIN_DOMAIN']}")
    app.config["MCP_NETWORK"] = os.environ.get("MCP_NETWORK", "mcp-network")
    app.config["MCP_MAX_INSTANCES"] = int(os.environ.get("MCP_MAX_INSTANCES", "50"))
    # HIGH-1 (#61): shared service-to-service secret guarding /internal/* (the
    # agent-manager presents it). Empty => /internal fails CLOSED.
    app.config["MCP_INTERNAL_TOKEN"] = os.environ.get("MCP_INTERNAL_TOKEN", "")

    master_key = os.environ["MCP_MANAGER_SECRET_KEY"]

    # Database
    from app.services.database import Database
    app.db = Database(app.config["DATABASE_URL"])
    app.db.migrate()

    # Encryption + credential store
    from app.services.crypto import CredentialCipher
    from app.services.credential_store import CredentialStore
    app.cipher = CredentialCipher(master_key)
    from app.services.openbao_oauth import build_oauth_backend
    # #2472 B22: OAuth tokens in OpenBao when the openbao profile is on.
    app.cred_store = CredentialStore(app.db, app.cipher, oauth_backend=build_oauth_backend())

    # #2472: the read-only Authentik directory (design record §7, ruling 2).
    # Built from MCP_MANAGER_AUTHENTIK_READONLY_TOKEN ONLY — never from
    # AUTHENTIK_BOOTSTRAP_TOKEN, which this container does not receive
    # (modules/mcp-manager/compose.yml has no env_file).
    # None while the key is empty: no email resolves to a person and no
    # pre-upgrade row is re-keyed or quarantined (fail closed).
    from app.services.gateway_identity import AuthentikDirectory
    app.authentik_directory = AuthentikDirectory.from_env()
    if app.authentik_directory is None:
        logger.warning("MCP_MANAGER_AUTHENTIK_READONLY_TOKEN is empty: Open WebUI callers "
                       "stay unresolved and pre-upgrade rows stay unattributed")

    # #2472 system_oauth: while Open WebUI signs in through Authentik
    # (OWUI_OIDC_ACTIVE), its gateway connections forward the chat user's
    # Authentik access token; this checks it with Authentik's public keys.
    # None otherwise (the Open WebUI session path alone).
    from app.services.owui_oidc import AuthentikTokenVerifier
    app.owui_oidc = AuthentikTokenVerifier.from_env()
    if app.owui_oidc is not None:
        # Read the key set once now, off the start-up path, so the first chat
        # calls do not race for it (only one thread ever fetches).
        import threading
        threading.Thread(target=app.owui_oidc.keys_reachable, name="owui-oidc-jwks",
                         daemon=True).start()
    if app.owui_oidc is not None and app.authentik_directory is None:
        logger.warning("Open WebUI forwards Authentik tokens, but MCP_MANAGER_AUTHENTIK_READONLY_TOKEN "
                       "is empty: no chat user is confirmed active, so none gets a personal credential")

    # Catalog
    from app.services.catalog import PersonalMCPCatalog
    app.catalog = PersonalMCPCatalog()

    # #2472: the MCP gateway's server registry (`gateway_servers:` in
    # core/mcp/mcp-servers.yaml). An invalid registry loads as EMPTY.
    from app.services.gateway_registry import load_gateway_limits, load_gateway_servers
    app.gateway_servers = load_gateway_servers()
    app.gateway_limits = load_gateway_limits()

    # Docker + Caddy clients
    from app.services.docker_client import MCPDockerClient
    app.docker_client = MCPDockerClient(
        base_url=app.config["DOCKER_HOST"], network=app.config["MCP_NETWORK"])
    from app.services.caddy_client import CaddyClient
    app.caddy_client = CaddyClient(mcp_domain=app.config["MCP_DOMAIN"])

    # Provisioner
    from app.services.provisioner import MCPProvisioner
    app.provisioner = MCPProvisioner(
        db=app.db, docker_client=app.docker_client, caddy_client=app.caddy_client,
        catalog=app.catalog, cred_store=app.cred_store, config=app.config)

    # OAuth flow (P3)
    from app.services.oauth import OAuthFlow, load_providers
    try:
        app.oauth_flow = OAuthFlow(master_key, load_providers())
    except Exception:
        logger.exception("OAuthFlow init failed — OAuth integrations disabled")
        app.oauth_flow = None
    # B9 fix round 1 (I2): a launch renews an OAuth proxy's token first.
    app.provisioner.oauth_flow = app.oauth_flow

    # #619: the lifecycle scheduler is GONE with the dynamic routes it
    # existed to heal — the static *.MCP_DOMAIN wildcard cannot lose an
    # instance host, so there is nothing to reconcile.

    # Blueprints
    from app.blueprints.health import health_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.api import api_bp
    from app.blueprints.oauth import oauth_bp
    from app.blueprints.internal import internal_bp

    # #619: the *.mcp wildcard bearer-proxy hook MUST be installed before
    # any request runs — it fully handles instance-subdomain hosts so the
    # manager's own routes are unreachable there (CRITICAL #1).
    from app.blueprints.wildcard_proxy import init_wildcard_proxy
    init_wildcard_proxy(app)

    # #639: every header-authenticated surface is Caddy-anchored — see
    # blueprints/caddy_anchor.py for the threat model and the not-anchored
    # exceptions (health, internal).
    from app.blueprints.caddy_anchor import install_caddy_anchor
    install_caddy_anchor(dashboard_bp, api_bp, oauth_bp)
    # #2472: remember who each SSO user is, and attribute their pre-upgrade
    # credentials. Installed AFTER the anchor, so it only ever sees
    # Caddy-delivered identity headers.
    from app.services.gateway_identity import install_record_sso_user
    install_record_sso_user(dashboard_bp, api_bp, oauth_bp)

    # #2472: re-key pre-upgrade instances/bindings by sub (or quarantine them).
    # Decides nothing without the directory or on any directory failure;
    # record_sso_user retries until it has completed.
    try:
        from app.services.identity_migration import ensure_instances_rekeyed
        ensure_instances_rekeyed(app)
    except Exception:
        logger.exception("identity re-key at start failed; retried on the next visit")
    # #2677: a departed person's stored credentials are deleted; rows left by
    # people who left before this existed go here, on the start that follows
    # an upgrade. The count is logged and every purge is audited.
    try:
        from app.services.identity_migration import ensure_departed_swept
        ensure_departed_swept(app)
    except Exception:
        logger.exception("departed-person credential sweep at start failed; retried later")

    # Running per-user instances follow an upgraded image (recreated with
    # their state; see services/instance_roll.py). In the background, so a
    # slow docker never delays the start.
    from app.services.instance_roll import start_instance_roll
    start_instance_roll(app)

    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(oauth_bp)
    app.register_blueprint(internal_bp)
    # #2472: the MCP gateway. NOT Caddy-anchored: it trusts no identity
    # header, only a credential it can verify (blueprints/gateway.py).
    from app.blueprints.gateway import gateway_bp
    app.register_blueprint(gateway_bp)
    # A cognee instance renews its one-hour cognee token. NOT Caddy-anchored:
    # its only credential is the instance's own renewal secret.
    from app.blueprints.instance_auth import instance_auth_bp
    app.register_blueprint(instance_auth_bp)

    @app.context_processor
    def inject_user():
        from razzfazz_common.auth import parse_authentik_headers
        info = parse_authentik_headers()
        return {"current_user": info["username"] or "anonymous",
                "current_email": info["email"]}

    return app
