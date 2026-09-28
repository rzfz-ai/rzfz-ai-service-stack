# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Provisioning engine for per-user MCP proxies (#36).

Mirrors agent-manager's Provisioner. For a given (user, mcp_id) it:
  1. decrypts ONLY that user's credentials (via CredentialStore)
  2. renders the catalog env_map (injecting creds + user identity + settings)
  3. launches a per-user proxy container holding those creds in env/tmpfs
     (no docker socket, no host-mounted disk for creds)
  4. records the instance. #619: NO route registration and NO boot
     reconcile anymore — the static *.MCP_DOMAIN wildcard proxies every
     instance host to the manager's bearer proxy (wildcard_proxy.py),
     which enforces the per-instance bearer from the DB row per request.

SECURITY: each proxy receives ONLY its owner's creds. Decrypted plaintext lives
only in this process during launch and in the proxy container's memory. Nothing
here logs credential values.
"""

import logging
import re
import secrets
import uuid

# #61 NEW-1: SINGLE source of truth for the user slug, shared with agent-manager.
# Both services bind-mount core/common/razzfazz_common at /app/common and set
# PYTHONPATH=/app/common. The api blueprint re-exports from here. The two
# services' derivations MUST agree byte-for-byte (``<=14>-<6hex>``, fits
# user_slug VARCHAR(24)) or agent-manager's /internal/agent-wiring/<slug> lookup
# misses these instances and the per-proxy bearer never reaches the agent.
from razzfazz_common.user_slug import make_user_slug  # noqa: F401

logger = logging.getLogger(__name__)

# #36 follow-up: catalog `reach_backend` -> the SCOPED docker network(s) a proxy
# joins (besides mcp-network) so it can dial a shared backend by container name.
# Only backends that live on a non-`default`, non-`mcp-network` net qualify — so
# the proxy never gains a path to mcp-manager:5000/internal.
# #36 BLOCKER-1 fix: cognee proxies join a DEDICATED `cognee-backend` net (cognee
# is dual-homed on it) — NOT the shared `coding-agents` bridge. Sandboxes are on
# `coding-agents` and CANNOT reach a per-user proxy by container name any more
# (defence-in-depth on top of the per-instance auth-shim bearer gate).
_BACKEND_NETWORKS = {
    "cognee": ["cognee-backend"],
}


class MCPProvisioner:
    def __init__(self, db, docker_client, caddy_client, catalog, cred_store, config):
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._catalog = catalog
        self._creds = cred_store
        self._config = config

    # ------------------------------------------------------------------

    def _render_env(self, integ: dict, user_slug: str, user_email: str,
                    decrypted_creds: dict, settings: dict,
                    extra: dict | None = None) -> dict:
        """Resolve the catalog env_map placeholders to concrete values.

        Placeholders:
          {{cred:<key>}}          -> decrypted_creds[<key>]
          {{oauth_access_token}}  -> decrypted_creds['oauth_access']
          {{user_slug}}           -> user_slug
          {{user_email}}          -> user_email
          {{setting:<key>}}       -> settings[<key>]
          {{cognee_user_api_key}} -> extra['cognee_user_api_key'] (auto-provisioned)
          {{cognee_company_dataset}} -> extra['cognee_company_dataset']
        Unresolved placeholders resolve to '' (never left literal in the env).
        """
        extra = extra or {}
        env_map = integ.get("env_map", {}) or {}
        out: dict[str, str] = {}
        for var, template in env_map.items():
            val = str(template)
            # {{cred:<key>}}
            for m in set(re.findall(r"\{\{cred:([a-zA-Z0-9_]+)\}\}", val)):
                val = val.replace(f"{{{{cred:{m}}}}}", str(decrypted_creds.get(m, "")))
            # {{setting:<key>}}
            for m in set(re.findall(r"\{\{setting:([a-zA-Z0-9_]+)\}\}", val)):
                val = val.replace(f"{{{{setting:{m}}}}}", str(settings.get(m, "")))
            val = val.replace("{{oauth_access_token}}",
                              str(decrypted_creds.get("oauth_access", "")))
            val = val.replace("{{user_slug}}", user_slug)
            val = val.replace("{{user_email}}", user_email or "")
            val = val.replace("{{cognee_user_api_key}}",
                              str(extra.get("cognee_user_api_key", "")))
            val = val.replace("{{cognee_company_dataset}}",
                              str(extra.get("cognee_company_dataset", "")))
            out[var] = val
        return out

    # ------------------------------------------------------------------

    def _cognee_user_email(self, user_slug: str, user_email: str) -> str:
        """The per-user cognee account email. Prefer the real user email; else a
        deterministic slug-based address under the box's real MAIN_DOMAIN (cognee's
        email validator rejects reserved/special-use domains like `.local`, so we
        namespace under a real domain the box owns)."""
        if user_email:
            return user_email
        import os as _os
        domain = (self._config.get("MAIN_DOMAIN")
                  or _os.environ.get("MAIN_DOMAIN", "example.com"))
        return f"cognee-{user_slug}@{domain}"

    def _cognee_user_password(self, user_slug: str) -> str:
        """Deterministic per-user cognee password (HMAC of the slug under the
        manager master key). Stable across re-provisions so ensure_user can log
        in as the existing account; never logged, never returned to the user."""
        import hashlib
        import hmac as _hmac
        secret = (self._config.get("MCP_MANAGER_SECRET_KEY")
                  or __import__("os").environ.get("MCP_MANAGER_SECRET_KEY", "mcp"))
        if isinstance(secret, str):
            secret = secret.encode()
        return "Cg1!" + _hmac.new(secret, user_slug.encode(),
                                  hashlib.sha256).hexdigest()[:32]

    def _provision_cognee_identity(self, integ: dict, user_slug: str,
                                   username: str, user_email: str,
                                   groups: list) -> tuple:
        """Ensure the per-user cognee identity + key (fail-closed). Returns
        (ok, extra_dict, error_msg)."""
        import os as _os
        from app.services import cognee_identity
        base = _os.environ.get("COGNEE_BASE_URL", "http://cognee:8000")
        admin_email = _os.environ.get("COGNEE_ADMIN_EMAIL", "")
        admin_pw = _os.environ.get("COGNEE_ADMIN_PASSWORD", "")
        if not admin_email or not admin_pw:
            return False, {}, ("Cognee admin credentials are not configured on "
                               "this box; cannot provision personal memory.")
        # FAIL-CLOSED: require real backend access control for the private tier.
        if integ.get("requires_backend_access_control") and \
                not cognee_identity.backend_access_control_on(base, admin_email, admin_pw):
            return False, {}, (
                "Cognee backend access control is OFF — personal memory would "
                "not be isolated, so provisioning is refused (no masquerade).")
        cu_email = self._cognee_user_email(user_slug, user_email)
        cu_pw = self._cognee_user_password(user_slug)
        try:
            res = cognee_identity.ensure_user(base, admin_email, admin_pw,
                                              cu_email, cu_pw)
        except Exception as e:
            logger.exception("cognee identity provisioning failed for %s", user_slug)
            return False, {}, f"Cognee identity provisioning failed: {e}"
        # cognee-mcp presents API_TOKEN as `Authorization: Bearer` for self-hosted;
        # cognee accepts a per-user JWT there (an api-key is rejected on that
        # header). The proxy re-provisions on relaunch, refreshing the token.
        token = res.get("token") or res.get("api_key", "")
        if not token:
            return False, {}, "Cognee per-user token could not be obtained."
        extra = {"cognee_user_api_key": token,
                 "cognee_user_id": res.get("user_id", "")}
        # Company-brain tier: resolve which shared dataset this user is scoped to.
        if integ.get("cognee_autoprovision") == "company":
            ds = ""
            try:
                gov = self._db.get_governance(integ["id"]) if hasattr(self._db, "get_governance") else None
                cfg = (gov or {}).get("config") if isinstance(gov, dict) else None
                if isinstance(cfg, dict):
                    ds = cfg.get("company_dataset", "")
            except Exception:
                ds = ""
            extra["cognee_company_dataset"] = ds or "company_brain"
        return True, extra, ""

    def launch(self, mcp_id: str, user_id: str, username: str,
               groups: list, user_email: str = "") -> tuple:
        """Launch the caller's own per-user MCP proxy. Returns (instance_id, msg)."""
        integ = self._catalog.get(mcp_id)
        if not integ:
            return None, f"Unknown integration: {mcp_id}"

        user_slug = make_user_slug(username)

        # dedupe / relaunch. There is at most ONE non-destroyed instance per
        # (mcp_id, user_slug) — enforced by the partial-unique index
        # idx_mcp_instance_user. If it's already running, no-op. If it exists in
        # ANY other live state (stopped / error / provisioning), we must REUSE
        # that same row on relaunch (#66): a fresh create_instance() INSERT would
        # collide with that index and 500. `relaunch_of` carries the existing id
        # through to the reuse path below.
        existing = self._db.get_instance_by_mcp_and_user(mcp_id, user_slug)
        relaunch_of = None
        if existing:
            if existing.get("state") == "running":
                return str(existing["id"]), "Already running."
            relaunch_of = existing["id"]

        # Fetch ONLY this user's decrypted creds.
        decrypted = self._creds.get_decrypted(user_slug, mcp_id)
        cred_model = self._catalog.cred_model(mcp_id)
        if cred_model == "pat" and not decrypted:
            return None, ("No credentials stored for this integration yet. "
                          "Add your credentials first, then provision.")
        if cred_model == "oauth" and "oauth_access" not in decrypted:
            return None, ("Not authorized yet. Complete the OAuth flow first, "
                          "then provision.")

        # #36 two-tier cognee: auto-provision the per-user cognee identity (no
        # user-supplied key). FAIL-CLOSED — if this can't mint a real per-user
        # key (backend access-control off / login fails), we refuse rather than
        # fall back to a shared key that would leak across users.
        extra: dict = {}
        if integ.get("cognee_autoprovision"):
            ok, extra, err = self._provision_cognee_identity(
                integ, user_slug, username, user_email, groups)
            if not ok:
                return None, err

        # Non-secret settings live in the instance config (if any prior instance).
        settings = {}
        if existing and existing.get("config"):
            cfg = existing["config"]
            if isinstance(cfg, dict):
                settings = cfg.get("settings", {})

        global_max = int(self._config.get("MCP_MAX_INSTANCES", 50))
        try:
            if len(self._db.get_all_instances()) >= global_max:
                return None, "System-wide MCP instance limit reached."
        except Exception:
            pass

        container_name = f"mcp-{mcp_id}-{user_slug}"
        # CRITICAL-1 (#61): the route secret is the REAL access control on the
        # per-user proxy (which holds THIS user's live creds). It is the bearer
        # the owning user's agents present; the opaque subdomain is only defense
        # in depth. >=128-bit entropy: token_urlsafe(32) = 256 bits.
        route_secret = secrets.token_urlsafe(32)
        instance_config = {"settings": settings, "_route_secret": route_secret}
        if relaunch_of is not None:
            # Reuse the existing row (stopped/error/provisioning → provisioning)
            # instead of INSERTing a duplicate (which the partial-unique index
            # would reject — #66). Also tear down any stale container left by the
            # prior stop() (stop leaves the container present-but-stopped; a
            # create with the same name would otherwise conflict) and drop its
            # old Caddy route so we re-register cleanly with the new bearer.
            instance_id = relaunch_of
            # #619: no per-instance route to drop — the wildcard proxy reads
            # the CURRENT _route_secret from the DB row on every request, so
            # a relaunch's new bearer takes effect immediately.
            try:
                self._docker.remove_container(container_name)
            except Exception:
                logger.warning("relaunch: no stale container %s to remove (ok)",
                               container_name)
            self._db.reset_instance_for_relaunch(instance_id, container_name,
                                                 instance_config)
        else:
            instance_id = self._db.create_instance(mcp_id, user_id, user_slug,
                                                   container_name, instance_config)

        try:
            env = self._render_env(integ, user_slug, user_email, decrypted,
                                   settings, extra=extra)
            # Inject the bearer into the proxy container so the proxy itself can
            # REQUIRE `Authorization: Bearer <PROXY_AUTH_TOKEN>` (the test-echo
            # reference proxy enforces this). Caddy ALSO enforces it on the route
            # (below), so even an image that ignores the env still gets gated.
            env["PROXY_AUTH_TOKEN"] = route_secret
            labels = {
                "razzfazz.managed": "true",
                "razzfazz.mcp.managed": "true",
                "razzfazz.mcp.id": mcp_id,
                "razzfazz.mcp.user": user_slug,
                "razzfazz.mcp.instance": str(instance_id),
            }
            # #36 follow-up: a proxy that must reach a shared backend (cognee-mcp
            # in API mode -> cognee:8000) joins a SCOPED extra network the backend
            # is on. #36 BLOCKER-1 / #785: `reach_backend: cognee` maps to the
            # DEDICATED `cognee-backend` net, NOT `coding-agents` — this comment
            # described the pre-BLOCKER-1 behaviour and said the opposite of what
            # `_BACKEND_NETWORKS` above does. On the sandbox bridge a peer
            # sandbox could name another user's proxy container directly. NEVER
            # `default` either (docker_client refuses it) so the /internal
            # isolation invariant holds.
            extra_networks = _BACKEND_NETWORKS.get(integ.get("reach_backend"), [])
            container_id = self._docker.create_container(
                name=container_name,
                image=integ["image"],
                environment=env,
                mem_limit=integ.get("mem_limit", "256m"),
                cpu_limit=float(integ.get("cpu_limit", 0.5)),
                labels=labels,
                command=integ.get("command") or None,
                extra_networks=extra_networks,
            )
            self._docker.start_container(container_id)
            # #619: no Caddy route registration — the static *.MCP_DOMAIN
            # wildcard proxies every instance host to the manager's bearer
            # proxy, which re-creates the CRITICAL-1 gate + host-rewrite from
            # the DB row/catalog per request.

            self._db.update_instance_state(instance_id, "running",
                                           container_id=container_id)
            self._db.log_audit(user_slug, "provision", mcp_id,
                               {"container": container_name})
            return str(instance_id), f"{integ['display_name']} proxy provisioned."
        except Exception as e:
            logger.exception("Failed to launch MCP proxy %s", container_name)
            self._db.update_instance_state(instance_id, "error",
                                           error_message=str(e)[:500])
            self._db.log_audit(user_slug, "provision_failed", mcp_id,
                               {"error": str(e)[:200]})
            return str(instance_id), f"Provisioning failed: {e}"

    def stop(self, instance_id, username: str) -> tuple:
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)
        inst = self._db.get_instance(instance_id)
        if not inst:
            return None, "Instance not found."
        self._docker.stop_container(inst["container_name"])
        self._db.update_instance_state(instance_id, "stopped")
        self._db.log_audit(inst["user_slug"], "stop", inst["mcp_id"])
        return str(instance_id), "Stopped."

    def delete(self, instance_id, username: str) -> tuple:
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)
        inst = self._db.get_instance(instance_id)
        if not inst:
            return None, "Instance not found."
        self._docker.remove_container(inst["container_name"])
        self._db.delete_instance(instance_id)
        self._db.log_audit(inst["user_slug"], "delete", inst["mcp_id"])
        return str(instance_id), "Deleted."
