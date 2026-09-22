# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Resource monitor — polls Docker stats, system memory, and GPU sysfs."""

import glob
import json
import os
import subprocess
import threading
import time


class ResourceMonitor:
    def __init__(self, profile_manager, config_manager=None):
        self._profile_manager = profile_manager
        # Optional — used to fetch GPUSTACK_API_KEY when augmenting the local
        # GPU readings with worker GPUs from gpustack /v1/gpu-devices. None is
        # tolerated for tests / older callers; it just degrades to local-only.
        self._config_manager = config_manager
        self._cache = {}
        self._system = {}
        self._gpus = []
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self, interval=30):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _poll_loop(self, interval):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(interval)

    def _update(self):
        system = self._read_meminfo()
        local_gpus = self._read_gpus()
        remote_gpus = self._read_remote_gpus()
        # Merge: local first (live busy_pct from sysfs is more current than
        # gpustack's poll-snapshot), then remote workers. Dedupe by GPU name —
        # on single-box installs the sysfs entry and the gpustack self-report
        # cover the same physical card, so we drop the gpustack copy when a
        # local one already names it. On multi-host installs (master-cpu +
        # worker boxes) there are no local entries, so every remote shows.
        local_names = {g.get('name', '').lower() for g in local_gpus}
        have_local = bool(local_gpus)
        # A single-box install has exactly ONE gpustack worker — the embedded
        # one co-located with this host — and its self-reported GPUs ARE the
        # physical cards we already read via sysfs. So when there is only one
        # distinct remote worker AND we have local sysfs GPUs, every remote GPU
        # is this box's own card; drop them all. Real multi-host installs have
        # >=2 distinct worker hosts, so the per-GPU name/host match below still
        # applies and genuine remote workers surface. (#config-ui GPU dedupe:
        # the embedded worker's host is its container hostname — never empty —
        # so the loopback-host check (b) alone missed it and the one card showed
        # as two.)
        remote_hosts = {(g.get('host') or '').strip().lower() for g in remote_gpus}
        single_embedded = have_local and len(remote_hosts) <= 1
        gpus = list(local_gpus)
        for g in remote_gpus:
            rn = (g.get('name') or '').lower()
            rhost = (g.get('host') or '').strip().lower()
            # Drop a remote gpustack GPU that is really THIS box's card (already
            # read via sysfs, which has live busy_pct). Two independent signals,
            # either suffices:
            #   (a) its name overlaps a local name; or
            #   (b) it has no distinct remote host (None/empty/loopback) — i.e. it
            #       belongs to the embedded/local worker.
            # (b) fixes the single-box DOUBLE-COUNT: sysfs names the card from
            # product_name/'card0' (e.g. "AMD Radeon Graphics") while gpustack
            # reports the marketing name ("AMD Radeon 8060S"), so the name match
            # (a) alone misses it and the one card showed as two workers. Real
            # remote workers carry a distinct host, so they still surface; on a
            # master-cpu box with no local GPU (have_local=False) every remote
            # shows as before.
            if have_local and (
                single_embedded
                or any(rn and (rn in ln or ln in rn) for ln in local_names)
                or rhost in ('', 'none', 'local', 'localhost', '127.0.0.1')
            ):
                continue
            gpus.append(g)
        containers = self._read_docker_stats()

        # Group by profile — prefer enabled profiles for shared container names
        # (e.g. gpustack belongs to llm-legacy — #1447 folded llm-cpu in and
        # removed llm-box/llm-experimental)
        profile_map = {}
        profiles = self._profile_manager.get_all_profiles()
        enabled = self._profile_manager.get_enabled_profiles()
        container_to_profile = {}
        # First pass: all profiles (sets defaults)
        for pid, p in profiles.items():
            for c in p.get('containers', []):
                container_to_profile[c['name']] = pid
        # Second pass: enabled profiles override (so shared containers map correctly)
        for pid, p in profiles.items():
            if pid in enabled or pid == 'core':
                for c in p.get('containers', []):
                    container_to_profile[c['name']] = pid

        for cname, stats in containers.items():
            pid = container_to_profile.get(cname, '_untracked')
            profile_map.setdefault(pid, {'containers': {}, 'total_mb': 0})
            profile_map[pid]['containers'][cname] = stats
            profile_map[pid]['total_mb'] += stats.get('memory_mb', 0)

        with self._lock:
            self._cache = profile_map
            self._system = system
            self._gpus = gpus

    def _read_gpus(self):
        """Read AMD GPU stats from sysfs (kernel-driver native, no rocm-smi/amdsmi
        needed; same source the M022 monkey-patch uses for the gpustack
        dashboard). Each card surfaces gpu_busy_percent (0-100) and VRAM
        used/total in MiB. Cards without these sysfs nodes (NVIDIA, no GPU,
        DRM render-only) are skipped — they'll need a dedicated reader.
        """
        out = []
        for card_path in sorted(glob.glob('/sys/class/drm/card[0-9]*')):
            device = os.path.join(card_path, 'device')
            busy_path = os.path.join(device, 'gpu_busy_percent')
            if not os.path.exists(busy_path):
                continue
            try:
                with open(busy_path) as f:
                    busy_pct = int(f.read().strip())
            except Exception:
                busy_pct = None
            vram_used_mib = None
            vram_total_mib = None
            try:
                with open(os.path.join(device, 'mem_info_vram_used')) as f:
                    vram_used_mib = int(f.read().strip()) // (1024 * 1024)
            except Exception:
                pass
            try:
                with open(os.path.join(device, 'mem_info_vram_total')) as f:
                    vram_total_mib = int(f.read().strip()) // (1024 * 1024)
            except Exception:
                pass
            # #1978: GTT is GPU memory too. amdgpu splits the GPU's memory into
            # the BIOS-pinned carve-out (mem_info_vram_*) and GTT, pages lent
            # from host RAM; a buffer object lives in exactly one of them. On a
            # small-carve-out box (0.175: 2 GB carve-out, 123.5 GB GTT) the
            # carve-out alone is a window that is always nearly full and never
            # moves — measured 87.6 % while the GPU held ~62 of ~125 GB.
            # Both sides must grow together: adding the used figures to a
            # carve-out-sized total would read ~3000 %, a broken gauge instead
            # of a misleading one. So only when BOTH GTT files are readable.
            gtt_used_mib = gtt_total_mib = None
            for fname, key in (('mem_info_gtt_used', 'used'), ('mem_info_gtt_total', 'total')):
                try:
                    with open(os.path.join(device, fname)) as f:
                        value = int(f.read().strip()) // (1024 * 1024)
                except Exception:
                    continue
                if key == 'used':
                    gtt_used_mib = value
                else:
                    gtt_total_mib = value
            if (gtt_used_mib is not None and gtt_total_mib is not None
                    and vram_used_mib is not None and vram_total_mib is not None):
                vram_used_mib += gtt_used_mib
                vram_total_mib += gtt_total_mib
            # Friendly name from /sys/class/drm/card{N}/device/uevent if available
            name = os.path.basename(card_path)
            try:
                with open(os.path.join(device, 'uevent')) as f:
                    for line in f:
                        if line.startswith('PCI_ID=') and 'AMD' not in line:
                            pass  # not vendored, but keep going
                # Try product_name shortcut
                pname = os.path.join(device, 'product_name')
                if os.path.exists(pname):
                    with open(pname) as f:
                        n = f.read().strip()
                        if n:
                            name = n
            except Exception:
                pass
            out.append({
                'card': os.path.basename(card_path),
                'name': name,
                'host': 'local',
                'busy_pct': busy_pct,
                'vram_used_mib': vram_used_mib,
                'vram_total_mib': vram_total_mib,
            })
        return out

    def _read_remote_gpus(self):
        """Pull worker-side GPU stats from gpustack /v1/gpu-devices and shape
        them like _read_gpus() output, so multi-host stacks (master-cpu +
        worker boxes) surface their GPUs in the same dashboard table.

        On a single-box install, gpustack reports the local GPU(s) here too.
        We dedupe by `name`+`vendor` so they don't double up against the
        sysfs read; the sysfs reading wins because it includes live
        gpu_busy_percent that the gpustack snapshot may lag on.
        """
        if not self._config_manager:
            return []
        try:
            from app.services.gpustack_client import get_gpu_devices
            api_key = self._config_manager.read_env().get('GPUSTACK_API_KEY', '')
            return get_gpu_devices(api_key)
        except Exception:
            return []

    def _read_meminfo(self):
        try:
            with open('/proc/meminfo') as f:
                info = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(':')
                        info[key] = int(parts[1])
                total_mb = info.get('MemTotal', 0) // 1024
                available_mb = info.get('MemAvailable', 0) // 1024
                return {
                    'total_mb': total_mb,
                    'available_mb': available_mb,
                    'used_mb': total_mb - available_mb,
                }
        except Exception:
            return {'total_mb': 0, 'available_mb': 0, 'used_mb': 0}

    def _read_docker_stats(self):
        try:
            result = subprocess.run(
                ['docker', 'stats', '--no-stream', '--format',
                 '{"name":"{{.Name}}","memory":"{{.MemUsage}}","cpu":"{{.CPUPerc}}"}'],
                capture_output=True, text=True, timeout=30
            )
            containers = {}
            for line in result.stdout.strip().splitlines():
                try:
                    data = json.loads(line)
                    name = data['name']
                    mem_str = data['memory'].split('/')[0].strip()
                    memory_mb = self._parse_mem(mem_str)
                    cpu_str = data['cpu'].rstrip('%')
                    containers[name] = {
                        'memory_mb': memory_mb,
                        'cpu_percent': float(cpu_str) if cpu_str else 0.0,
                    }
                except (json.JSONDecodeError, ValueError, IndexError):
                    continue
            return containers
        except Exception:
            return {}

    def _parse_mem(self, mem_str):
        mem_str = mem_str.strip()
        if mem_str.endswith('GiB'):
            return float(mem_str[:-3]) * 1024
        if mem_str.endswith('MiB'):
            return float(mem_str[:-3])
        if mem_str.endswith('KiB'):
            return float(mem_str[:-3]) / 1024
        if mem_str.endswith('B'):
            return float(mem_str[:-1]) / (1024 * 1024)
        return 0.0

    def get_system_resources(self):
        with self._lock:
            return dict(self._system)

    def get_profile_resources(self):
        with self._lock:
            return dict(self._cache)

    def get_gpus(self):
        with self._lock:
            return list(self._gpus)

    def get_all(self):
        with self._lock:
            system = dict(self._system)
            profiles = dict(self._cache)
            gpus = list(self._gpus)
        # The background poll thread populates _system asynchronously; until its
        # first _update() lands (the seconds right after container start, or if a
        # round is delayed) _system is empty, which would make the dashboard's
        # memory widget raise UndefinedError on `system.used_mb`. Read meminfo
        # synchronously here so callers ALWAYS get the memory keys. Lock is
        # released first — _read_meminfo doesn't touch shared state.
        if not system:
            system = self._read_meminfo()
        return {
            'system': system,
            'profiles': profiles,
            'gpus': gpus,
        }
