# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Background lifecycle scheduler for mcp-manager (#36).

Mirrors agent-manager's lifecycle: a periodic Caddy route-reconcile (with an
immediate first run on boot) so per-user proxy subdomains self-heal after a
Caddy restart / box reboot — Caddy boots from its static Caddyfile and drops
these dynamic admin-API routes otherwise.
"""

import logging

logger = logging.getLogger(__name__)


class LifecycleManager:
    def __init__(self, db, docker_client, caddy_client, config, provisioner):
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._config = config
        self._provisioner = provisioner
        self._scheduler = None

    def start(self):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            logger.warning("apscheduler not installed — route reconcile disabled")
            return
        from datetime import datetime

        self._scheduler = BackgroundScheduler(daemon=True)
        # Immediate first run + every 120s thereafter.
        self._scheduler.add_job(
            self._reconcile_routes, "interval", seconds=120,
            next_run_time=datetime.now(), id="mcp_reconcile_routes",
            max_instances=1, coalesce=True,
        )
        self._scheduler.start()
        logger.info("mcp-manager lifecycle scheduler started")

    def _reconcile_routes(self):
        try:
            self._provisioner.reconcile_routes()
        except Exception:
            logger.exception("mcp reconcile_routes job failed")
