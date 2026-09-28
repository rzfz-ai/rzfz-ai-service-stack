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


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    app.secret_key = os.environ.get("WEBUI_SECRET_KEY", os.environ["MCP_MANAGER_SECRET_KEY"])
    app.config["SESSION_COOKIE_NAME"] = "razzfazz_mcp"
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.config["DATABASE_URL"] = os.environ["MCP_MANAGER_DATABASE_URL"]
    app.config["DOCKER_HOST"] = os.environ.get("DOCKER_HOST", "tcp://docker-socket-proxy:2375")
    app.config["CADDY_ADMIN_URL"] = os.environ.get("CADDY_ADMIN_URL", "http://caddy:2019")
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
    app.cred_store = CredentialStore(app.db, app.cipher)

    # Catalog
    from app.services.catalog import PersonalMCPCatalog
    app.catalog = PersonalMCPCatalog()

    # Docker + Caddy clients
    from app.services.docker_client import MCPDockerClient
    app.docker_client = MCPDockerClient(
        base_url=app.config["DOCKER_HOST"], network=app.config["MCP_NETWORK"])
    from app.services.caddy_client import CaddyClient
    app.caddy_client = CaddyClient(
        admin_url=app.config["CADDY_ADMIN_URL"], mcp_domain=app.config["MCP_DOMAIN"])

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

    app.register_blueprint(health_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(oauth_bp)
    app.register_blueprint(internal_bp)

    @app.context_processor
    def inject_user():
        from razzfazz_common.auth import parse_authentik_headers
        info = parse_authentik_headers()
        return {"current_user": info["username"] or "anonymous",
                "current_email": info["email"]}

    return app
