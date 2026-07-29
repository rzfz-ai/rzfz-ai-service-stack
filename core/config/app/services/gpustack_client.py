# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""GPUStack API client — queries loaded models and worker info."""

import fnmatch
import json
import os
import urllib.error
import urllib.request


GPUSTACK_API = 'http://gpustack:9090'
HF_API = 'https://huggingface.co/api/models'


def _is_offline():
    """True when the box is in offline network mode (RAZZFAZZ_NETWORK_MODE=offline,
    or the legacy RAZZFAZZ_OFFLINE boolean). #184 P1 / WS7b.

    Reads STACK_ROOT/.env WITHOUT sourcing it (operator-edited values carry
    spaces/metachars — project memory feedback_dotenv_no_source.md). Fail-open:
    any read error → False (keep the pre-#184 online behaviour) so a broken .env
    never suppresses a legitimate VRAM lookup — an actually-offline box always
    has the key readable, so the HF gate holds there."""
    try:
        from razzfazz_common.env_utils import read_env_key
        env_path = os.path.join(os.environ.get('STACK_ROOT', '/stack'), '.env')
        mode = read_env_key(env_path, 'RAZZFAZZ_NETWORK_MODE')
        if mode in ('online', 'proxied', 'offline'):
            return mode == 'offline'
        return read_env_key(env_path, 'RAZZFAZZ_OFFLINE').lower() in (
            '1', 'true', 'yes', 'on')
    except Exception:
        return False


def _get_json(api_key, path):
    """GET a gpustack API path, return parsed JSON or None on failure."""
    try:
        req = urllib.request.Request(
            f'{GPUSTACK_API}{path}',
            headers={'Authorization': f'Bearer {api_key}'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_running_instances(api_key):
    """Return list of running model instances. Tries /v1 (gpustack 0.7.x)
    then /v2 (gpustack 2.x); whichever responds first wins. Returns the
    raw item dicts so callers can pull worker_name, computed_resource_claim,
    huggingface_*, etc. without a second round-trip."""
    for path in ('/v1/model-instances', '/v2/model-instances'):
        data = _get_json(api_key, path)
        if data is not None:
            return [i for i in data.get('items', []) if i.get('state') == 'running']
    return []


def _get_running_model_records(api_key):
    """Return [(name, model_record_dict)]. The model record is the parent
    /models entry (carries `meta`, `huggingface_*`); when /models isn't
    queryable (older gpustack), fall back to using the instance dict
    itself which has the same huggingface_* fields. Either is enough for
    the HF GGUF-size lookup."""
    insts = _get_running_instances(api_key)
    running_names = {i.get('model_name') for i in insts}
    instance_by_name = {i.get('model_name'): i for i in insts}

    for path in ('/v1/models', '/v2/models'):
        data = _get_json(api_key, path)
        if data is None:
            continue
        out = []
        for m in data.get('items', []):
            name = m.get('name')
            if name in running_names:
                out.append((name, m))
        return out

    # /models not reachable — synthesize records from the instance dicts.
    return [(name, inst) for name, inst in instance_by_name.items()]


def _aggregate_instance_vram(api_key):
    """Roll the running model-instances up into a per-model summary so
    the dashboard can show actual scheduler-claimed VRAM and the worker
    name. Sums vram across distributed_servers and gpu_indexes for the
    common case where one model occupies a single GPU on a single worker.

    Returns: {model_name: {'vram_bytes': int, 'workers': set, 'gpus': int}}
    """
    summary = {}
    for inst in _get_running_instances(api_key):
        name = inst.get('model_name')
        if not name:
            continue
        s = summary.setdefault(name, {'vram_bytes': 0, 'workers': set(), 'gpus': 0})
        worker = inst.get('worker_name')
        if worker:
            s['workers'].add(worker)
        claim = inst.get('computed_resource_claim') or {}
        vram = claim.get('vram') or {}
        if isinstance(vram, dict):
            for v in vram.values():
                try:
                    s['vram_bytes'] += int(v or 0)
                except (TypeError, ValueError):
                    pass
            s['gpus'] += len(vram)
        elif isinstance(vram, (int, float)):
            s['vram_bytes'] += int(vram)
            s['gpus'] += 1
    return summary


def _gguf_size_via_hf(repo_id, filename_pattern):
    """Sum sizes of HuggingFace files matching the glob pattern in
    `filename_pattern` (typically a single GGUF or a multi-part shard
    pattern like `Foo-Q4_K_M-*.gguf`). Mirrors the M022 Patch 2 helper
    in usercustomize.py — that one runs inside the gpustack container
    via huggingface_hub; we can't import the patched helper from here so
    we hit the public HF API directly. Returns 0 on any failure.

    #184 P1 / WS7b: on an OFFLINE box this is the one call in the client that
    reaches the internet (huggingface.co). Gate it — offline returns 0 (the
    existing '—' display path already handles a 0 VRAM figure) rather than block
    5 s per model on a connection that can't succeed and would trip the firewall.
    """
    if _is_offline():
        return 0
    if not repo_id or not filename_pattern:
        return 0
    try:
        url = f'{HF_API}/{repo_id}/tree/main?recursive=true'
        with urllib.request.urlopen(url, timeout=5) as resp:
            files = json.loads(resp.read())
        total = 0
        for f in files:
            if f.get('type') != 'file':
                continue
            path = f.get('path', '')
            if fnmatch.fnmatch(path, filename_pattern):
                total += int(f.get('size', 0) or 0)
        return total
    except Exception:
        return 0


def get_models(api_key):
    """Get loaded models from GPUStack with actual VRAM consumption per
    model and the worker(s) running them. VRAM source priority:
      1. `computed_resource_claim.vram` summed across all running
         instances of the model — what the scheduler actually claimed
         on the worker GPU. Truthful on stock gpustack (v0.7.x stable
         + v2.x without the M022 monkey-patch). Reported as `—` when
         the claim is 0/missing so we don't show a misleading number.
      2. `meta.size` (GGUF file size) — fallback when no live claim is
         visible (older API, model loaded but instances filtered out).
      3. HuggingFace API GGUF size lookup — last resort for models
         created via raw POST that skipped metadata extraction.

    Worker source: `worker_name` from each running instance, deduped.
    """
    if not api_key:
        return []
    data = _get_json(api_key, '/v1-openai/models')
    if data is None:
        return []

    records = dict(_get_running_model_records(api_key))
    inst_summary = _aggregate_instance_vram(api_key)
    out = []
    for m in data.get('data', []):
        mid = m['id']
        record = records.get(mid) or {}
        meta = record.get('meta') or {}
        summary = inst_summary.get(mid) or {}
        # Prefer the live scheduler claim (sum across instances). Fall back
        # to GGUF file size from meta, then to HF API.
        vram = int(summary.get('vram_bytes') or 0)
        if vram == 0:
            vram = int(meta.get('size') or 0)
        if vram == 0:
            vram = _gguf_size_via_hf(
                record.get('huggingface_repo_id'),
                record.get('huggingface_filename'),
            )
        workers = sorted(summary.get('workers') or [])
        out.append({
            'id': mid,
            'owned_by': m.get('owned_by', ''),
            'vram_bytes': vram,
            'workers': workers,
            'gpu_count': summary.get('gpus', 0),
        })
    return out


def get_gpu_devices(api_key):
    """Get every GPU known to GPUStack — local + every worker — as a list
    of dicts shaped to match resource_monitor._read_gpus() so the dashboard
    can merge them into one table.

    Each item:
      {
        'name':              'AMD Radeon 8060S',
        'vendor':            'AMD'|'Apple'|'NVIDIA'|...,
        'host':              'razzfazz-ai-worker-box-1',
        'busy_pct':          0..100 or None,
        'vram_used_mib':     int or None,
        'vram_total_mib':    int or None,
        'vram_allocated_mib':int or None,    # bytes scheduled (may exceed used)
        'type':              'rocm'|'cuda'|'mps'|'cpu'|...,
        'card':              gpustack_id (e.g. 'razzfazz-ai-worker-box-1:rocm:0'),
      }

    Returns [] on any failure — the dashboard already handles an empty
    GPU list gracefully (renders the host-system card alone).
    """
    if not api_key:
        return []
    try:
        req = urllib.request.Request(
            f'{GPUSTACK_API}/v1/gpu-devices',
            headers={'Authorization': f'Bearer {api_key}'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    out = []
    for d in data.get('items', []):
        mem = d.get('memory') or {}
        core = d.get('core') or {}
        total = int(mem.get('total') or 0)
        used = int(mem.get('used') or 0)
        allocated = int(mem.get('allocated') or 0)
        out.append({
            'name': d.get('name') or 'GPU',
            'vendor': d.get('vendor') or '',
            'host': d.get('worker_name') or '',
            'busy_pct': int(round(core.get('utilization_rate') or 0)),
            'vram_used_mib': used // (1024 * 1024) if total else None,
            'vram_total_mib': total // (1024 * 1024) if total else None,
            'vram_allocated_mib': allocated // (1024 * 1024) if allocated else None,
            'type': d.get('type') or '',
            'card': d.get('id') or '',
        })
    return out


def register_local_model(api_key, payload):
    """Register a model with GPUStack from a payload (POST /vN/models).

    Used by the Config-UI model sideload (#184 P1 / WS7d): ``payload`` carries a
    ``source: local_path`` spec (built by model_source via
    model_sideload.build_register_payload) so GPUStack loads the GGUF from the
    volume, never HuggingFace. Tries /v1/models (gpustack v0.7.x — the stable
    default, live-confirmed to accept source=local_path) then /v2/models
    (gpustack 2.x). Returns ``(model_id, error)`` — exactly one is truthy.
    """
    if not api_key:
        return None, 'No GPUStack API key is configured.'
    body = json.dumps(payload).encode('utf-8')
    last_err = 'GPUStack did not accept the model registration.'
    for path in ('/v1/models', '/v2/models'):
        try:
            req = urllib.request.Request(
                f'{GPUSTACK_API}{path}',
                data=body,
                method='POST',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            model_id = data.get('id') if isinstance(data, dict) else None
            if model_id:
                return model_id, None
            last_err = 'GPUStack accepted the request but returned no model id.'
        except urllib.error.HTTPError as exc:
            # 404 on /v1 → this is a v2.x box; fall through to /v2. Any other
            # HTTP error is terminal for this endpoint — surface its body.
            if exc.code == 404 and path == '/v1/models':
                continue
            try:
                detail = exc.read().decode('utf-8', 'replace')[:300]
            except Exception:
                detail = ''
            last_err = f'GPUStack rejected the model (HTTP {exc.code}): {detail}'
        except Exception as exc:  # noqa: BLE001 — surface a readable message
            last_err = f'Could not reach GPUStack to register the model: {exc}'
    return None, last_err


def get_workers(api_key):
    """Get GPUStack workers. Returns list of worker dicts."""
    if not api_key:
        return []
    try:
        req = urllib.request.Request(
            f'{GPUSTACK_API}/api/workers',
            headers={'Authorization': f'Bearer {api_key}'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get('items', [])
    except Exception:
        return []
