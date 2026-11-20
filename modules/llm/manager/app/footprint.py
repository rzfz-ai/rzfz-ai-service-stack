# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1648 — the resident-footprint estimate, on the manager's side.

WHY IT HAS TO EXIST HERE. `est_gb` is the basis of the whole admission math
(#227/#328/#330), and it is supplied by the REQUEST, not computed by the
manager. Only the console computes it (`ModelEditor.tsx::footprint`); the CLI
deploy path does not, and that is the path a box takes when it builds its
standard set. Measured on the fleet: all seventeen deployments carried
`est_gb = None`, so `_committed_gb` summed nothing and the gate compared it
against a budget (#1422).

#1646 made a missing estimate SAFE — an unaccounted placement now has to show
up in the worker's VRAM reading before the next one is admitted. This makes it
RARE, which is the other half: a queue of one is a brake, not a budget.

TWO IMPLEMENTATIONS, ONE FORMULA — and that is a debt, stated plainly. The
console's panel shows the same arithmetic while the operator is choosing, so it
cannot simply call this. `tests/unit/llm-manager/test_1648_footprint_parity.py`
runs BOTH over the same table and compares, so the two cannot drift silently;
when the console is moved onto a manager route, this module stays and the TS
half goes.
"""
from __future__ import annotations

from typing import Optional

#: bytes per KV element, by cache dtype. Mirrors `kvBytesPerElem` in the console.
_KV_BYTES = ((("q8", "fp8"), 1.06), (("q4",), 0.56))
_KV_DEFAULT = 2.0          # f16 / auto

#: the context the engines default to when nothing says otherwise.
_DEFAULT_CTX = 8192
#: compute/runtime overhead: 6 % of the weights, never less than this.
_COMPUTE_FLOOR_GB = 1.2
_COMPUTE_SHARE = 0.06


def kv_bytes_per_elem(kv_type: Optional[str]) -> float:
    t = (kv_type or "").lower()
    for needles, value in _KV_BYTES:
        if any(n in t for n in needles):
            return value
    return _KV_DEFAULT


def footprint_gb(engine: Optional[str], size_gb: Optional[float],
                 arch: Optional[dict], params: Optional[dict] = None) -> Optional[float]:
    """Resident footprint in GB: weights + KV cache + compute.

    Returns None when the weights are unknown — an estimate built on a zero
    weight is worse than none, because it passes the gate while claiming to
    have been measured. That is the #1422 failure with extra steps.

    `arch` is `{layers, kv_heads, head_dim}` as `hf.arch_from_config` returns
    it. Without it the KV term is omitted and the answer is a LOWER BOUND —
    still better than nothing, and the caller is told which it got by the
    `arch` it passed in.
    """
    if not size_gb or size_gb <= 0:
        return None
    values = params or {}
    is_llama = (engine or "") == "llamacpp"
    # #1572: `ctx` is the TOTAL KV pool, not a per-slot value — llama.cpp
    # divides it across `--parallel` slots. Multiplying by parallel counted
    # each slot's share as the whole pool (4x at the fleet default) on a number
    # that goes straight to the admission gate.
    raw_ctx = values.get("ctx_size") if is_llama else values.get("max_model_len")
    try:
        ctx = int(raw_ctx) if raw_ctx else _DEFAULT_CTX
    except (TypeError, ValueError):
        ctx = _DEFAULT_CTX
    kv_type = values.get("cache_type_k") if is_llama else values.get("kv_cache_dtype")
    weights = float(size_gb)
    kv = 0.0
    if is_llama and arch:
        try:
            kv = (2 * int(arch["layers"]) * int(arch["kv_heads"]) * int(arch["head_dim"])
                  * ctx * kv_bytes_per_elem(kv_type)) / 1e9
        except (KeyError, TypeError, ValueError):
            kv = 0.0
    compute = max(_COMPUTE_FLOOR_GB, weights * _COMPUTE_SHARE)
    return round(weights + kv + compute, 1)


# ── #2520: llama.cpp context checkpoints cost HOST RAM ──────────────────────
#: Host RAM one llama.cpp engine adds when it runs with context checkpoints
#: on (`--ctx-checkpoints` > 0). Measured on a fleet box on 2026-09-26 with
#: the production args and GGUF (qwen3.6 Q8_0, ctx=1M, parallel=4,
#: `--cache-ram 0`): engine RSS 3.9 GiB at `--ctx-checkpoints 0` -> 5.6 GiB at 8,
#: i.e. +1.7 GiB per engine, no extra VRAM. #2536 item 32: admission adds this
#: to `est_gb`, which is decimal GB (`footprint_gb` divides by 1e9), so the
#: constant is the same bytes in GB: 1.7 x 1024^3 / 10^9 = 1.83. What it buys:
#: the 2nd request with the same 11.6k-token prefix took 0.68 s instead of
#: 12.8 s. Charged per engine, on top of `est_gb`, ONLY where the effective
#: value is > 0 (#2520).
CTX_CHECKPOINT_HOST_RSS_GB = 1.83

#: The node driver's default when a model's params do not set the value —
#: mirrors `drivers/base.py::LLAMACPP_DEFAULT_CTX_CHECKPOINTS` (and the two
#: flag spellings below mirror `LLAMACPP_CTX_CHECKPOINT_FLAGS`); the parity is
#: pinned by tests/unit/llm-manager/test_2520_ctx_checkpoints_admission.py.
LLAMACPP_DEFAULT_CTX_CHECKPOINTS = 0
_CTX_CHECKPOINT_KEYS = frozenset({"ctx-checkpoints", "swa-checkpoints"})


def _flag(key) -> str:
    return str(key).lstrip("-").replace("_", "-").lower()


def effective_ctx_checkpoints(engine: Optional[str], params: Optional[dict]) -> int:
    """The `--ctx-checkpoints` value the node will actually launch with.

    Only llama.cpp has the option; every other engine answers 0. The node
    emits every params key in dict order (drivers/base.py params_to_cli_flags)
    and llama.cpp keeps the LAST value of a repeated option, so the last
    matching key wins here too (#2536 item 31). Both sides read the params
    back from a JSONB column, so that order is PostgreSQL's JSONB key order
    (length, then bytes), not the order the operator wrote them in. A `False`
    bool emits no flag and is skipped, and so is `None` (both count as unset;
    the node then launches with the default, #2621). No key: the driver default. A value that
    does not parse as an integer counts as ON - the conservative reading for an
    admission charge (the engine would not start with it anyway, and
    over-charging a broken row cannot overcommit a box).
    """
    if (engine or "") != "llamacpp":
        return 0
    found = None
    # PostgreSQL's JSONB key order (byte length, then bytes) is what the node
    # receives, whatever order the caller built the dict in.
    for key, value in sorted((params or {}).items(), key=lambda kv: (
            len(str(kv[0]).encode()), str(kv[0]).encode())):
        if _flag(key) in _CTX_CHECKPOINT_KEYS and value is not False and value is not None:
            found = value
    if found is None:
        return LLAMACPP_DEFAULT_CTX_CHECKPOINTS
    if isinstance(found, bool):
        return int(found)
    try:
        return int(str(found).strip())
    except (TypeError, ValueError):
        return 1


def ctx_checkpoint_host_rss_gb(engine: Optional[str], params: Optional[dict]) -> float:
    """Host RAM (GB) one engine of this deployment adds for context
    checkpoints: `CTX_CHECKPOINT_HOST_RSS_GB` when the effective value is > 0,
    else 0.0 (#2520)."""
    return CTX_CHECKPOINT_HOST_RSS_GB if effective_ctx_checkpoints(engine, params) > 0 else 0.0
