# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Profile manifest loader and status queries."""

import os
import yaml


class ProfileManager:
    def __init__(self, manifest_path, env_path=None):
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
        self._profiles = data.get('profiles', {})
        self._categories = data.get('categories', {})
        self._env_path = env_path

    def get_all_profiles(self):
        return self._profiles

    def get_profile(self, profile_id):
        return self._profiles.get(profile_id)

    def get_enabled_profiles(self):
        """Read COMPOSE_PROFILES from .env and return set of enabled profile IDs."""
        if not self._env_path or not os.path.exists(self._env_path):
            return set()
        # rc6.7: shared env_utils (strips inline ` # comment`).
        from .env_utils import read_env_key
        value = read_env_key(self._env_path, 'COMPOSE_PROFILES')
        if not value:
            return set()
        return set(p.strip() for p in value.split(',') if p.strip())

    def get_profiles_by_category(self):
        """Group profiles by their category for nav rendering."""
        by_cat = {}
        for pid, profile in self._profiles.items():
            cat = profile.get('category', 'Other')
            by_cat.setdefault(cat, []).append({'id': pid, **profile})
        return by_cat

    def get_container_status(self, profile_id):
        """Query Docker API for running/health state of a profile's containers.

        Returns dict: {container_name: {status, health, memory_mb}}
        """
        try:
            import docker
            client = docker.from_env()
        except Exception:
            return {}

        profile = self._profiles.get(profile_id)
        if not profile:
            return {}

        result = {}
        for c in profile.get('containers', []):
            name = c['name']
            # #538 follow-up: a `deploy.replicas` service (marked `replicated:
            # true`) has NO container named after it — Docker names its
            # containers <project>-<service>-1, -2, ... So `containers.get(name)`
            # always raises NotFound, which would show the worker permanently
            # missing on the module card. Resolve it by the compose-service
            # label across all its replicas and report the aggregate.
            if c.get('replicated'):
                result[name] = self._replicated_status(client, name)
                continue
            try:
                container = client.containers.get(name)
                health = container.attrs.get('State', {}).get('Health', {}).get('Status', 'none')
                result[name] = {
                    'status': container.status,
                    'health': health,
                }
            except docker.errors.NotFound:
                result[name] = {'status': 'not_found', 'health': 'none'}
            except Exception:
                result[name] = {'status': 'error', 'health': 'none'}

        return result

    @staticmethod
    def _replicated_status(client, service):
        """Aggregate status of a compose service's replica containers.

        `running` when every replica is running, `partial` when some are,
        `not_found` when none exist. Health is the worst across replicas — a
        module is only as healthy as its unhealthiest worker.
        """
        try:
            replicas = client.containers.list(
                all=True,
                filters={'label': f'com.docker.compose.service={service}'},
            )
        except Exception:
            return {'status': 'error', 'health': 'none'}
        if not replicas:
            return {'status': 'not_found', 'health': 'none'}
        states = [r.status for r in replicas]
        if all(s == 'running' for s in states):
            status = 'running'
        elif any(s == 'running' for s in states):
            status = 'partial'
        else:
            status = states[0]
        healths = [(r.attrs.get('State', {}).get('Health', {}) or {}).get('Status', 'none')
                   for r in replicas]
        health = ('unhealthy' if 'unhealthy' in healths
                  else 'starting' if 'starting' in healths
                  else 'healthy' if 'healthy' in healths else 'none')
        return {'status': status, 'health': health, 'replicas': len(replicas)}

    def get_profile_health(self, profile_id):
        """Aggregate health for a profile: green/yellow/gray."""
        statuses = self.get_container_status(profile_id)
        if not statuses:
            return 'gray'
        # Only consider persistent containers (ignore init/one-shot that exited or don't exist)
        known = {k: v for k, v in statuses.items()
                 if v['status'] not in ('not_found', 'exited')}
        if not known:
            return 'gray'
        states = [s['status'] for s in known.values()]
        if all(s == 'running' for s in states):
            return 'green'
        if any(s == 'running' for s in states):
            return 'yellow'
        return 'gray'
