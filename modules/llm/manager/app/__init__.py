# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""rzfz.ai LLM Manager (manager) — FastAPI application package.

Epic #254, Phase 1 (gateway + token chargeback MVP). The package MUST be
import-safe off-box: ``create_app()`` builds the FastAPI app without opening
a DB/Valkey connection or configuring a FileHandler (see the import-side-
effect guard in tests/unit/llm-manager).
"""
from __future__ import annotations

__version__ = "0.1.0-phase1"


def create_app():
    """Build and return the FastAPI application.

    Routers are registered incrementally as Phase-1 tasks land (proxy,
    keys/cost-centers, metrics). Imports are done INSIDE this function so a
    bare ``import app`` never drags in heavy/optional deps at import time.
    """
    from fastapi import FastAPI

    app = FastAPI(
        title="rzfz.ai LLM Manager",
        version=__version__,
        # #1195: the stock /docs and /redoc pages load Swagger UI / ReDoc from
        # a CDN and boot from an inline <script> — both blocked by the manager
        # domain's CSP (script-src 'self', #347) and dead on an air-gap box.
        # /docs is re-added below as a self-hosted page (app/docs.py); /redoc
        # is dropped (linked nowhere, never routed by Caddy).
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    from app.docs import register_docs

    register_docs(app)  # #1195: same-origin Swagger UI at /docs + /docs/static/*

    @app.get("/livez", include_in_schema=False)
    def livez():
        return {"status": "ok"}

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        # Liveness only — readiness (DB/Valkey reachability) is the separate
        # /readyz below, so a transient datastore blip doesn't nuke the
        # container.
        # role_catalogue: "unavailable" means every tier without a legacy .env
        # override is empty (fail closed). Reported, never a reason to restart.
        try:
            from app import rbac_catalogue

            state = "ok" if rbac_catalogue.available() else "unavailable"
        except RuntimeError:  # the loader mount is missing
            state = "unavailable"
        return {"status": "ok", "version": __version__, "role_catalogue": state}

    from fastapi.responses import JSONResponse

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        """#2562: readiness — can this manager reach Postgres and Valkey now?
        Liveness (/healthz) stays independent so a datastore blip never gets
        the container restarted. Asks the SAME cache the /v1 path uses.
        Reasons carry an exception class, never a message: a DSN or a Valkey
        URL (with its password) can sit in one."""
        from concurrent.futures import ThreadPoolExecutor

        from sqlalchemy import text as _t
        from app.db import session_scope

        def _postgres() -> str:
            try:
                with session_scope() as s:
                    s.execute(_t("SELECT 1"))
                return "ok"
            except Exception as exc:  # noqa: BLE001 — readiness reports, never raises
                return f"down: {type(exc).__name__}"

        cache = getattr(app.state, "cache", None)
        if cache is None:
            from app import cache as _cache_mod

            cache = _cache_mod.get_cache()

        # A refused/timed-out Valkey is the fail-open state (serving goes on);
        # a rejected password or other misconfiguration is not — /v1 fails on
        # every request, so it gets its own "error:" reason (status: FAIL).
        def _valkey() -> str:
            try:
                probe = getattr(cache, "probe", None)
                state = probe() if probe else ("ok" if cache.ping() else "down")
            except Exception:  # noqa: BLE001
                state = "down"
            return {
                "ok": "ok",
                "auth": "error: cache authentication failed — check the Valkey password",
                "misconfigured": "error: cache misconfigured — every /v1 request fails",
            }.get(state, "degraded: cache unavailable (no PING answer) — "
                         "rate/budget counters fail open")

        # Both at once: blackholed, each can take ~10 s (Postgres connect
        # timeout; Valkey 2 s x 4 attempts). One after the other that is ~20 s,
        # past `rzfz status`'s 12 s probe, and a FAIL would read "not answered".
        with ThreadPoolExecutor(max_workers=2) as pool:
            pg, vk = pool.submit(_postgres), pool.submit(_valkey)
            out = {"postgres": pg.result(), "valkey": vk.result()}
        code = 200 if all(v == "ok" for v in out.values()) else 503
        return JSONResponse(status_code=code, content=out)

    # --- Phase-1 routers (registered as each task lands) ------------------
    from app.proxy import register_proxy
    from app.api.keys import register_keys_api
    from app.api.analytics import register_analytics_api
    from app.api.agent_keys import register_agent_keys_api
    from app.api.workers import register_workers_api
    from app.api.enroll import register_enroll_api
    from app.api.install_worker import register_install_worker_api
    from app.catalog import register_catalog_api
    from app.api.registry import register_registry_api
    from app.api.inventory import register_inventory_api
    from app.api.settings import register_settings_api
    from app.api.playground import register_playground_api
    from app.api.commands import register_commands_api
    from app.api.entitlement import register_entitlement_api
    from app.api.hf import register_hf_api
    from app.api.runner_upgrade import register_runner_upgrade_api
    from app.api.hub_auth import register_hub_auth_api
    from app.metrics import register_metrics
    from app.router_config import register_router_admin
    from app.relay import RelayHub, register_relay

    register_proxy(app)
    register_keys_api(app)
    register_analytics_api(app)   # #991: cost-control aggregates over usage_events
    register_agent_keys_api(app)  # #612: peer-anchored minting for per-user agents
    register_workers_api(app)
    from app.liveness import quiet_access_log
    quiet_access_log()
    register_enroll_api(app)      # #262: worker enrollment (mint token + join)
    register_install_worker_api(app)  # #1059: GET /install-worker (open, no secret)
    register_catalog_api(app)     # #264: deployable-model catalog
    register_registry_api(app)    # #289: central Zot registry contents (read)
    register_inventory_api(app)   # Phase-3 S3: fleet + deployment READ endpoints
    register_settings_api(app)    # Phase-3 S5: /api/me + runtime settings
    register_playground_api(app)  # Phase-3 S-B: SSO chat/embeddings/rerank test
    register_commands_api(app)    # #261 C1: manager→node command channel
    register_entitlement_api(app)
    register_hf_api(app)          # #295: HuggingFace model browser for Deploy
    register_runner_upgrade_api(app)  # #549 R3: per-node runner-upgrade sequence
    register_hub_auth_api(app)    # #559: hub /v2 forward_auth backend (Basic)
    register_metrics(app)
    register_router_admin(app)
    # #262 Task 5: wire relay routes (WS ingress + HTTP demux)
    app.state.relay_hub = RelayHub()
    register_relay(app)

    @app.on_event("startup")
    def _seed_router_config():  # pragma: no cover - exercised on real boot
        # The LiteLLM router boots with `--config /config/router-config.yaml`
        # and crash-loops if that file is absent. The manager owns the file
        # (rebuild_from_state writes it from DB state), but only ever wrote it
        # on an admin rebuild / backend registration — so a FRESH deploy with
        # zero backends never produced it and the router never started. Seed an
        # initial config on manager startup (render_router_config is valid with
        # an empty model_list) so the router can boot; later rebuilds refine it.
        #
        # Registered here but only RUNS on real ASGI startup — create_app()
        # itself stays DB-free (the import-side-effect guard calls create_app()
        # off-box). Best-effort: a transient DB blip must not abort manager
        # startup — `POST /api/router/rebuild` remains the manual recovery.
        try:
            from app.router_config import rebuild_from_state

            rebuild_from_state()
        except Exception as exc:  # noqa: BLE001 - startup must never hard-fail
            import logging

            logging.getLogger("orchestrator").warning(
                "initial router-config seed failed (router will retry once a "
                "backend is registered or via POST /api/router/rebuild): %s",
                exc,
            )
        # #337: daily retention prune for usage_events/node_commands
        # (daemon thread; first pass immediate). Startup-hook only, so
        # create_app() stays DB-free for the import-side-effect guard.
        try:
            from app.retention import start_retention_loop
            start_retention_loop()
        except Exception:  # noqa: BLE001 - never block startup
            pass
        # #263 SCH2: desired-state reconcile loop (reschedule-on-node-loss).
        # ALWAYS started (mirrors the retention loop's shape), but the
        # ORCH_RECONCILE gate lives inside reconcile_once itself — default
        # "observe": it decides + logs what it WOULD reschedule but never
        # enqueues anything until an operator sets ORCH_RECONCILE=enforce.
        # A fresh/upgraded box therefore never autonomously relaunches a
        # workload on its own.
        try:
            from app.reconciler import start_reconcile_loop
            start_reconcile_loop()
        except Exception:  # noqa: BLE001 - never block startup
            pass

    return app
