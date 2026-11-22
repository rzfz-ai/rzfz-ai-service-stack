# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Lifecycle manager — idle detection, auto-stop, auto-update, and cleanup scheduler."""

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.services import agent_updates, sessions

logger = logging.getLogger(__name__)

#: #3187 review round 4 — server-owned renewal of `last_accessed` for every
#: attached session. Must stay well below Provisioner.SESSION_FRESHNESS_SECONDS
#: (90 s) so a quiet attached PTY (no inbound frames) never ages out while
#: attached; even a missed sweep or two keeps the row inside the window.
SESSION_HEARTBEAT_INTERVAL_SECONDS = 20


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
        # (matrix bots, hermes long-running tasks). Auto-stopping them on inactivity defeats that —
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
        # #3187: instances left on an old image after an upgrade are recreated
        # automatically once they are IDLE (no session / no terminal attach
        # within the idle window); busy ones only get the badge. Runs with the
        # state sync cadence, first run immediately (an upgrade restarts this
        # manager, so the first pass IS the post-upgrade pass). The job is
        # always scheduled — the enabled flag is resolved LIVE on every pass
        # (agent_settings DB override > AGENT_AUTO_UPDATE_ENABLED env), so an
        # operator opt-out takes effect without a restart. The window defaults
        # to the instance's own idle_timeout (existing config) — see
        # agent_updates.
        self._scheduler.add_job(
            self._auto_update_idle,
            'interval',
            seconds=120,
            id='auto_update_idle',
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
        )
        logger.info("Idle auto-update scheduled (#3187; enabled=%s)",
                    agent_updates.auto_update_enabled(self._db, self._config))
        # #3187 review round 4: server-owned periodic renewal of
        # last_accessed for every registered attachment — independent of
        # browser input/output, so a quiet attached terminal stays visible to
        # other processes (the docker-exec CLI) for the FULL attachment
        # lifetime. On disconnect/crash the registry entry is gone, renewal
        # stops, and the row ages out within SESSION_FRESHNESS_SECONDS
        # (bounded cleanup preserved). Serving process only: this scheduler
        # is never started in read-only CLI mode (background_jobs=False).
        self._scheduler.add_job(
            self._session_heartbeat,
            'interval',
            seconds=SESSION_HEARTBEAT_INTERVAL_SECONDS,
            id='session_heartbeat',
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
                    # #2476 (A21): a stopped agent holds no live token.
                    if self._provisioner is not None:
                        self._provisioner._revoke_openbao_token(inst)
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

    def _auto_update_idle(self):
        """#3187: recreate instances that are behind on their image once they
        are idle — via provisioner.upgrade(), the SAME volumes-preserving
        recreate the one-click Update uses (never delete+launch). An instance
        is skipped while a start/recreate is in flight (instance_busy), while
        a terminal/session is ATTACHED (session_active — the in-process
        registry, race-checked again inside _upgrade under the instance lock),
        or while it has been accessed within the idle window (a running session
        or an attached terminal bumps last_accessed). Enabled is resolved live
        per pass: agent_settings > AGENT_AUTO_UPDATE_ENABLED env > default on.
        Per-instance try: one failing recreate must not abort the pass."""
        if self._provisioner is None:
            return
        if not agent_updates.auto_update_enabled(self._db, self._config):
            return
        try:
            now = datetime.now(timezone.utc)
            session_active = getattr(self._provisioner, 'session_active', None)
            for inst in self._db.get_all_instances():
                try:
                    if inst.get('state') in ('destroyed', 'provisioning'):
                        continue
                    if self._provisioner.instance_busy(inst['id']) is True:
                        continue
                    if callable(session_active) and session_active(inst['id']):
                        # busy user: badge only, never a recreate under them
                        continue
                    type_info = self._provisioner._catalog.get_type(inst['agent_type'])
                    if not type_info:
                        continue
                    if not self._provisioner.update_available(inst, type_info,
                                                              use_cache=False):
                        continue
                    window = agent_updates.update_window_seconds(
                        self._config, inst, db=self._db)
                    if window is None:
                        # underivable window — do not guess, only badge
                        continue
                    if not agent_updates.is_idle(inst, now=now, window=window):
                        continue
                    logger.info(
                        "#3187 auto-update: %s is idle (window %ss) and behind on its "
                        "image — recreating on the current one",
                        inst['container_name'], window)
                    iid, msg = self._provisioner.upgrade(inst['id'], inst['user_slug'])
                    self._db.log_audit(
                        inst['user_id'], 'auto_update', inst['agent_type'], inst['id'],
                        {'idle_window_seconds': window, 'message': (msg or '')[:200],
                         'ok': bool(iid)})
                except Exception:  # noqa: BLE001
                    logger.exception("#3187 auto-update failed for %s",
                                     inst.get('container_name'))
        except Exception:
            logger.exception("Error in auto-update pass")

    def _session_heartbeat(self):
        """#3187 review round 4: renew `last_accessed` for every attachment
        in this (serving) process's registry. Frame-driven beats alone cannot
        keep a quiet attached PTY visible past the freshness window — the
        portal terminal only beats on inbound frames and the output reader
        never beats — so the renewal is server-owned and periodic."""
        try:
            renewed = sessions.heartbeat(self._db)
            if renewed:
                logger.debug("session heartbeat: renewed %d attached session(s)",
                             renewed)
        except Exception:
            logger.exception("Error in session heartbeat")

    def _sync_states(self):
        """Sync DB state with actual Docker container states."""
        try:
            instances = self._db.get_all_instances()
            for inst in instances:
                if inst['state'] == 'destroyed':
                    continue
                # #2589: a start or recreate is in flight — the container may
                # be gone for a moment; that is not "Container not found".
                if (self._provisioner is not None
                        and self._provisioner.instance_busy(inst['id']) is True):
                    continue
                if inst['state'] == 'provisioning':
                    # #2482 C6: a row the readiness gate left "starting" (its
                    # web UI was still booting) is promoted here once its
                    # container runs and the gates pass. Nothing else touches
                    # a provisioning row: a launch may still be in flight.
                    if (self._provisioner is not None
                            and self._docker.get_container_state(inst['container_name']) == 'running'
                            and self._provisioner.instance_host_ready(inst)):
                        self._db.update_instance_state(inst['id'], 'running')
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
                    # #2482: an instance with a web pane is `running` only
                    # once its pane's first load would work (the outpost and
                    # UI gates); until then the row keeps what it says and
                    # the next sync asks again.
                    if (self._provisioner is not None
                            and not self._provisioner.instance_host_ready(inst)):
                        continue
                    self._db.update_instance_state(inst['id'], 'running')
                elif docker_state == 'exited' and inst['state'] != 'stopped':
                    if inst['state'] == 'running' and self._revive_stranded(inst):
                        continue
                    self._db.update_instance_state(inst['id'], 'stopped')
        except Exception:
            logger.exception("Error in state sync")

    def _revive_stranded(self, inst) -> bool:
        """#2589: an instance the store says is running, whose container
        exited and is pinned to networks that no longer exist, was stopped by
        a backup restore or `rzfz down` that then recreated the stack's
        networks. Nobody stopped it on purpose, and its own Start cannot work
        on the old network IDs, so it is brought back through the provisioner's
        Start, which recreates it on the live networks (same named volumes).

        Returns True when the start was attempted (the start owns the row from
        here); False leaves the plain demotion to 'stopped' to the caller."""
        if self._provisioner is None:
            return False
        try:
            stranded = self._provisioner.stranded_networks(
                inst, self._provisioner._catalog.get_type(inst['agent_type']))
        except Exception:  # noqa: BLE001
            logger.exception("stale-network check failed for %s",
                             inst['container_name'])
            return False
        if not stranded:
            return False
        logger.warning(
            "Instance %s was running and is stranded on removed networks (%s) "
            "— starting it again (#2589)", inst['container_name'],
            ', '.join(stranded))
        try:
            # the owner's slug as the acting name: the recreate templates the
            # container from it, exactly as the owner's own Start would
            iid, msg = self._provisioner.start(inst['id'], inst['user_slug'])
            logger.info("Revive of %s: %s", inst['container_name'], msg)
        except Exception:  # noqa: BLE001
            logger.exception("Revive of %s failed", inst['container_name'])
        return True

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
