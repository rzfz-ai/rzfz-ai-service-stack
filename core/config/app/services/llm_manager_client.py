# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""LLM Manager client — the served-model summary for the Config UI dashboard.

On a box running the ``llm-manager`` profile the LLM Manager (not GPUStack) is
the LLM backend, so the dashboard's "LLM Models" panel must read from it. The
manager fronts every worker behind a metered OpenAI surface; its ``/v1/models``
is a plain key-authed READ (not metered) that returns the models it currently
serves. #976.

Authentication (EXO-13 / CFG-14): the portal uses its OWN key,
``LLM_MANAGER_CONFIG_KEY`` (cost centre ``stack/config-portal``), minted by
``cli/post-install.sh::wire_llm_manager_consumers``. It used to BORROW
``LLM_MANAGER_OWUI_KEY``, which was wrong on three counts — the admin
dashboard's reads were metered against the OWUI tenant and polluted the very
usage series it renders; rotating or rate-limiting the OWUI key silently
darkened the ADMIN panel; and ``/v1/models`` applies the calling key's
``allowed_models`` filter (``modules/llm/manager/app/proxy.py``, #329), so a
model-restricted consumer key made the panel show a FILTERED list as if it
were everything the box serves. The OWUI/Dify keys stay a documented FALLBACK
for boxes whose ``post-install`` predates the dedicated key — with the
filtering caveat surfaced to the caller, never silently.

Latency (EXO-8): the dashboard fragment is polled every 15 s and the models
fragment every 30 s, so a *hung* (not refused) manager would otherwise pin a
Flask worker for the full timeout on every poll. Two mitigations here: a short
timeout (``DASHBOARD_TIMEOUT``) and a small module-level TTL memo, so N
concurrent pollers collapse onto one upstream call.

The manager's ``/api/workers`` (same base, same key) additionally exposes the
per-worker GPU/VRAM/CPU sample. A manager-only box has no GPUStack, so the
inline GPU/VRAM readout the dashboard used to source from GPUStack now comes
from here — ``get_workers`` shapes each worker into the compact "GPU & workers"
view-model the dashboard fragment renders next to the served-model list.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

# In-container address of the metered manager gateway (Docker DNS).
LLM_MANAGER_BASE = 'http://llm-manager:8080'

#: EXO-8 — the dashboard is a POLLED surface; it must never wait 5 s on a
#: hung gateway. Long enough for a healthy in-network round-trip, short
#: enough that a stuck manager cannot hold a Flask worker.
DASHBOARD_TIMEOUT = 1.5

#: EXO-8 — memo lifetime. Shorter than the 15 s live-fragment poll, so the
#: data a poller sees is at most this stale, while N *concurrent* pollers
#: (several browsers, or a slow response overlapping the next poll) still
#: collapse onto one upstream call.
CACHE_TTL_SECONDS = 10.0

#: The portal's OWN key first (EXO-13); the consumer keys are a back-compat
#: fallback for boxes that have not re-run post-install yet.
_CONFIG_KEY_VAR = 'LLM_MANAGER_CONFIG_KEY'
_FALLBACK_KEY_VARS = ('LLM_MANAGER_OWUI_KEY', 'LLM_MANAGER_DIFY_KEY')

#: Appended to a successful /v1/models read made with a borrowed consumer
#: key: that key may carry an `allowed_models` restriction (#329), so a short
#: list must not be read as "the box serves little".
BORROWED_KEY_NOTE = (
    'showing what the Open WebUI/Dify service key may see — run '
    '`rzfz post-install --refresh` to mint the Config Portal its own key'
)

_cache_lock = threading.Lock()
_cache = {}          # key -> (expires_at, value)


def _service_key(env: dict):
    """Return ``(key, is_dedicated)`` for the manager reads.

    ``is_dedicated`` is False when we fell back to a consumer key, which the
    callers surface rather than hide (EXO-13/CFG-14)."""
    env = env or {}
    v = (env.get(_CONFIG_KEY_VAR) or '').strip()
    if v:
        return v, True
    for k in _FALLBACK_KEY_VARS:
        v = (env.get(k) or '').strip()
        if v:
            return v, False
    return '', False


def _memo(cache_key, ttl, producer):
    """Tiny TTL memo (EXO-8).

    The upstream call happens OUTSIDE the lock: holding it across a hung
    request would serialise every poller behind the timeout — the very thing
    this exists to prevent. A concurrent duplicate call in the (rare) race is
    the acceptable cost; the steady state is one call per TTL.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[cache_key] = (time.monotonic() + ttl, value)
    return value


def reset_cache():
    """Drop the TTL memo. For tests and for an explicit operator refresh."""
    with _cache_lock:
        _cache.clear()


def get_served_models(env: dict, base_url: str = LLM_MANAGER_BASE,
                      timeout: float = DASHBOARD_TIMEOUT, use_cache: bool = True):
    """Return the models the LLM Manager currently serves, shaped for the
    dashboard fragment: ``[{'id': str, 'workers': [], 'vram_bytes': 0}, ...]``.

    Returns ``([], reason)`` on any failure — reason is a short human string the
    fragment shows instead of a stack trace ('' on success). On success with a
    BORROWED consumer key the reason carries ``BORROWED_KEY_NOTE``, because
    the manager filters ``/v1/models`` by the calling key's ``allowed_models``
    (#329) and a short list would otherwise read as "not deployed" (CFG-14).

    Memoised for ``CACHE_TTL_SECONDS`` (EXO-8); pass ``use_cache=False`` to
    force a fresh read."""
    key, dedicated = _service_key(env)
    if not key:
        return [], 'no LLM Manager service key in .env yet'

    def _fetch():
        req = urllib.request.Request(
            f'{base_url.rstrip("/")}/v1/models',
            headers={'Authorization': f'Bearer {key}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            # CFG-25: HTTPError SUBCLASSES URLError, so without this clause a
            # 401/403 from a revoked or wrong service key — the most likely
            # real failure — was reported as the manager being down.
            return [], f'LLM Manager rejected the request (HTTP {exc.code})'
        except urllib.error.URLError:
            return [], 'LLM Manager not reachable'
        except Exception:
            return [], 'LLM Manager returned an unexpected response'
        data = body.get('data') if isinstance(body, dict) else None
        if not isinstance(data, list):
            return [], 'LLM Manager returned no model list'
        models = []
        for m in data:
            if isinstance(m, dict) and m.get('id'):
                models.append({'id': m['id'], 'workers': [], 'vram_bytes': 0})
        return models, ('' if dedicated else BORROWED_KEY_NOTE)

    if not use_cache:
        return _fetch()
    return _memo(('models', base_url, key), CACHE_TTL_SECONDS, _fetch)


def _num(v):
    """Coerce a JSON scalar to float; return None for null / non-numeric /
    bool (JSON ``true`` must not read as ``1.0``)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def worker_view(w: dict) -> dict:
    """Shape one ``/api/workers`` object into the dashboard GPU view-model.

    Tolerates a null ``metrics`` sample (worker hasn't reported yet) and any
    missing field — every numeric slot is either a rounded float or ``None``,
    and the template renders ``None`` as an em-dash. Never raises.

    Derived fields:
      * ``gpu_pct``  — GPU utilization as a percentage. ``gpu_util`` may arrive
        as a 0..1 fraction or an already-0..100 percentage; values ``<= 1`` are
        treated as a fraction (``*100``), larger values as a raw percentage.
      * ``cpu_pct``  — ``load / ncpu * 100`` (host load normalised to cores).
      * ``vram_pct`` — ``vram_used_gb / vram_total_gb * 100`` when both known.
    """
    if not isinstance(w, dict):
        w = {}
    metrics = w.get('metrics')
    if not isinstance(metrics, dict):
        metrics = {}

    # VRAM used: prefer the live metrics sample, fall back to the top-level.
    vram_total = _num(w.get('vram_total_gb'))
    vram_used = _num(metrics.get('vram_used_gb'))
    if vram_used is None:
        vram_used = _num(w.get('vram_used_gb'))

    gpu_util = _num(metrics.get('gpu_util'))
    mem_used = _num(metrics.get('mem_used_gb'))
    load = _num(metrics.get('load'))
    ncpu = _num(metrics.get('ncpu'))

    gpu_pct = None
    if gpu_util is not None:
        gpu_pct = round(gpu_util * 100, 1) if gpu_util <= 1.0 else round(gpu_util, 1)

    cpu_pct = None
    if load is not None and ncpu:
        cpu_pct = round(load / ncpu * 100, 1)

    vram_pct = None
    if vram_total and vram_used is not None:
        vram_pct = round(vram_used / vram_total * 100, 1)

    return {
        'name': (w.get('name') or '').strip() or '—',
        'hardware': (w.get('hardware') or '').strip(),
        'status': (w.get('status') or '').strip(),
        'has_metrics': bool(metrics),
        'gpu_pct': gpu_pct,
        'vram_used_gb': round(vram_used, 1) if vram_used is not None else None,
        'vram_total_gb': round(vram_total, 1) if vram_total is not None else None,
        'vram_pct': vram_pct,
        'mem_used_gb': round(mem_used, 1) if mem_used is not None else None,
        'load': round(load, 2) if load is not None else None,
        'ncpu': int(ncpu) if ncpu is not None else None,
        'cpu_pct': cpu_pct,
    }


def parse_workers(body) -> list:
    """Shape a raw ``/api/workers`` response body into the GPU view-model list.

    Accepts a bare JSON list or a dict wrapper (``{'workers': [...]}`` /
    ``{'data': [...]}``). Non-list / unexpected bodies yield ``[]``. Pure —
    no I/O — so it unit-tests directly against a sample payload."""
    if isinstance(body, list):
        raw = body
    elif isinstance(body, dict):
        raw = body.get('workers')
        if not isinstance(raw, list):
            raw = body.get('data')
    else:
        raw = None
    if not isinstance(raw, list):
        return []
    return [worker_view(w) for w in raw if isinstance(w, dict)]


def get_workers(env: dict, base_url: str = LLM_MANAGER_BASE,
                timeout: float = DASHBOARD_TIMEOUT, use_cache: bool = True):
    """Return ``(workers, reason)`` from the LLM Manager's ``GET /v1/workers``.

    ``workers`` is a list of :func:`worker_view` dicts (empty on any failure);
    ``reason`` is a short human string the fragment shows instead of a stack
    trace ('' on success). Same base URL + bearer key as
    :func:`get_served_models` — no new auth path.

    Uses ``/v1/workers`` (the service-key hot path), NOT ``/api/workers``: the
    ``/api/*`` routes are ingress/SSO-gated and reject a container-to-container
    service-key call with 403 "request did not originate from the ingress
    proxy". ``/v1/workers`` serves the same live GPU/VRAM summary under the same
    ``rzfz-sk`` auth ``/v1/models`` uses. Never 500s the dashboard: any error
    (no key, manager down, auth reject, junk body) degrades to ``([], reason)``.
    ``HTTPError`` is caught separately from ``URLError`` so an auth/permission
    rejection reads as such instead of a misleading 'not reachable'.

    Memoised for ``CACHE_TTL_SECONDS`` (EXO-8) — this one is called from BOTH
    the 15 s live fragment and the 30 s models fragment, so the memo is what
    keeps a single dashboard from making two upstream calls per cycle."""
    key, _dedicated = _service_key(env)
    if not key:
        return [], 'no LLM Manager service key in .env yet'

    def _fetch():
        req = urllib.request.Request(
            f'{base_url.rstrip("/")}/v1/workers',
            headers={'Authorization': f'Bearer {key}'},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            return [], f'LLM Manager rejected the request (HTTP {exc.code})'
        except urllib.error.URLError:
            return [], 'LLM Manager not reachable'
        except Exception:
            return [], 'LLM Manager returned an unexpected response'
        return parse_workers(body), ''

    if not use_cache:
        return _fetch()
    return _memo(('workers', base_url, key), CACHE_TTL_SECONDS, _fetch)
