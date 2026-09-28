# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Lifecycle manager — idle detection, auto-stop, and cleanup scheduler."""

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)


class LifecycleManager:
    def __init__(self, db, docker_client, caddy_client, config: dict,
                 provisioner=None):
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._config = config
        # Optional — used for the periodic Caddy-route reconcile. The
        # provisioner owns the route logic (catalog + companion handling);
        # we only schedule it here. None tolerated for older callers/tests.
        self._provisioner = provisioner
        self._scheduler = BackgroundScheduler()

    def start(self):
        """Start the background scheduler for idle detection and cleanup."""
        # rc6.7 #93: per-user agents are designed to do background work
        # (matrix bots, hermes long-running tasks, openhands sandbox
        # callbacks). Auto-stopping them on inactivity defeats that —
        # disabled by default. Operator can re-enable via
        # AGENT_IDLE_AUTO_STOP_ENABLED=true if the box is memory-bound.
        if self._config.get('AGENT_IDLE_AUTO_STOP_ENABLED', False):
            self._scheduler.add_job(
                self._check_idle,
                'interval',
                seconds=60,
                id='idle_check',
                replace_existing=True,
            )
            logger.info("Idle auto-stop enabled (AGENT_IDLE_AUTO_STOP_ENABLED=true)")
        else:
            logger.info("Idle auto-stop disabled — agents stay running until explicitly stopped")
        self._scheduler.add_job(
            self._sync_states,
            'interval',
            seconds=120,
            id='state_sync',
            replace_existing=True,
        )
        # Self-healing sweep (#606: Authentik-only — per-instance Caddy
        # routes no longer exist; the static *.agents wildcard carries every
        # instance host). 120s cadence with an immediate first run heals
        # Authentik providers on boot and for late-starting containers,
        # container-checked first (#516 ghost guard). Idempotent.
        if self._provisioner is not None:
            self._scheduler.add_job(
                self._reconcile_routes,
                'interval',
                seconds=120,
                id='route_reconcile',
                replace_existing=True,
                next_run_time=datetime.now(timezone.utc),
            )
            # #606: the H2 caddy watch (+canary) is gone with the dynamic
            # routes it healed — the static wildcard cannot lose an instance
            # host, so there is nothing for a watch to restore.
        cleanup_days = self._config.get('AGENT_CLEANUP_AFTER_DAYS', 30)
        if cleanup_days > 0:
            self._scheduler.add_job(
                self._cleanup_stale,
                'cron',
                hour=3, minute=0,
                id='cleanup',
                replace_existing=True,
            )
        self._scheduler.start()
        logger.info("Lifecycle scheduler started")

    def stop(self):
        self._scheduler.shutdown(wait=False)

    def _check_idle(self):
        """Stop instances that have been idle longer than their timeout."""
        try:
            instances = self._db.get_all_instances()
            now = datetime.now(timezone.utc)

            for inst in instances:
                if inst['state'] != 'running':
                    continue

                timeout = inst.get('idle_timeout')
                if timeout is None:
                    timeout = (self._config['AGENT_IDLE_TIMEOUT_LIGHTWEIGHT']
                               if inst['tier'] == 'lightweight'
                               else self._config['AGENT_IDLE_TIMEOUT_HEAVY'])

                last = inst.get('last_accessed')
                if not last:
                    continue

                # Ensure timezone-aware comparison
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)

                idle_seconds = (now - last).total_seconds()
                if idle_seconds > timeout:
                    logger.info(
                        f"Auto-stopping idle instance {inst['container_name']} "
                        f"(idle {idle_seconds:.0f}s > timeout {timeout}s)"
                    )
                    self._docker.stop_container(inst['container_name'])
                    self._db.update_instance_state(inst['id'], 'stopped')
                    self._db.log_audit(
                        inst['user_id'], 'auto_stop', inst['agent_type'],
                        inst['id'], {'idle_seconds': int(idle_seconds)}
                    )
        except Exception:
            logger.exception("Error in idle check")

    def _reconcile_routes(self):
        """Authentik-provider self-heal sweep (#606: no Caddy routes anymore)."""
        try:
            self._provisioner.reconcile_routes()
        except Exception:
            logger.exception("Error in route reconcile")

    def _sync_states(self):
        """Sync DB state with actual Docker container states."""
        try:
            instances = self._db.get_all_instances()
            for inst in instances:
                if inst['state'] in ('provisioning', 'destroyed'):
                    continue

                docker_state = self._docker.get_container_state(inst['container_name'])
                if docker_state is None:
                    # Container doesn't exist but DB says it should
                    if inst['state'] != 'error':
                        self._db.update_instance_state(
                            inst['id'], 'error',
                            error_message='Container not found'
                        )
                elif docker_state == 'running' and inst['state'] != 'running':
                    self._db.update_instance_state(inst['id'], 'running')
                elif docker_state == 'exited' and inst['state'] != 'stopped':
                    self._db.update_instance_state(inst['id'], 'stopped')
        except Exception:
            logger.exception("Error in state sync")

    def _cleanup_stale(self):
        """Remove stopped instances older than AGENT_CLEANUP_AFTER_DAYS.

        AGM-6 (#1039): the teardown is delegated to `Provisioner.delete` — the
        one place the FULL sequence lives. The hand-rolled version here did
        `remove_container(primary)` + `delete_instance()` and nothing else, so
        an auto-reaped instance leaked everything `delete()` also does:

          * the companion container (hermes-workspace) kept running;
          * the per-instance Authentik forward-auth provider stayed registered;
          * the minted `rzfz-sk` LLM-Manager key was NEVER revoked — `delete()`
            is its only revoke site, and unlike the Authentik provider there is
            no `sweep_orphans` pass to catch it later, so a live, unattributed
            per-user key survived in llm-manager forever;
          * named volumes and the per-instance database were left behind.

        The fallback path is kept for the `provisioner=None` construction
        (older callers/tests) — degraded, but better than skipping the sweep.
        """
        try:
            days = self._config.get('AGENT_CLEANUP_AFTER_DAYS', 30)
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            instances = self._db.get_all_instances()

            for inst in instances:
                if inst['state'] != 'stopped':
                    continue
                stopped_at = inst.get('stopped_at')
                if not (stopped_at and stopped_at < cutoff):
                    continue
                logger.info(
                    f"Cleaning up stale instance {inst['container_name']} "
                    f"(stopped {days}+ days ago)"
                )
                # Per-instance try: one instance whose teardown throws must not
                # abort the sweep for every instance behind it.
                try:
                    if self._provisioner is not None:
                        self._provisioner.delete(inst['id'], 'system')
                    else:
                        logger.warning(
                            "Stale cleanup of %s running WITHOUT a provisioner "
                            "— partial teardown only (no key revoke / Authentik "
                            "deregister / volume + DB removal)",
                            inst['container_name'])
                        self._docker.remove_container(inst['container_name'])
                        self._db.delete_instance(inst['id'])
                    self._db.log_audit(
                        'system', 'cleanup', inst['agent_type'], inst['id'],
                        {'stopped_at': str(stopped_at)}
                    )
                except Exception:
                    logger.exception(
                        "Stale cleanup failed for %s", inst['container_name'])
        except Exception:
            logger.exception("Error in stale cleanup")
