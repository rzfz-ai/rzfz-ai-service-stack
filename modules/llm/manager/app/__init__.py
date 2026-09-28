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
        # Liveness only — readiness (DB/Valkey reachability) is a separate
        # probe added later so a transient datastore blip doesn't nuke the
        # container.
        return {"status": "ok", "version": __version__}

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
