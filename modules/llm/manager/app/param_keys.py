# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3498 — ONE spelling per engine parameter.

The catalogue and the CLI store llama.cpp's long flag names without the dashes
(`ctx-size`, `parallel`, `temp`, `top-p`); the console's editor stored its own
(`ctx_size`, `n_parallel`, `temperature`). The node turned both into the same
CLI flag and emitted it twice, and llama.cpp keeps the LAST one — by JSONB key
order the editor's. Measured: a deployment stored `parallel: 2` + `n_parallel: 1`
ran one slot.

The canonical key is the flag name the node emits: dashes, lowercase, no
leading `--`, with the editor's/llama.cpp-API aliases mapped. When two stored
spellings name one flag, the one ALREADY in canonical spelling wins (it is
what the catalogue, the CLI and every API caller write); an alias only counts
when no canonical key exists. The node agent applies the identical rule when it
builds the command line (drivers/base.py `canonical_params`), pinned by
tests/unit/llm-manager/test_3498_one_param_key_set.py.
"""
from __future__ import annotations

#: alias (already dash-normalised) -> canonical flag name
ALIASES = {
    "n-parallel": "parallel",
    "temperature": "temp",
    "n-ctx": "ctx-size",
    # llama.cpp's short flags (`-c 8192`, `-np 4`, ...): the same option
    "c": "ctx-size",
    "np": "parallel",
    "ngl": "n-gpu-layers",
    "gpu-layers": "n-gpu-layers",
    "b": "batch-size",
    "ub": "ubatch-size",
    "fa": "flash-attn",
    "ctk": "cache-type-k",
    "ctv": "cache-type-v",
}


def canonical_key(key) -> str:
    k = str(key).strip().lstrip("-").replace("_", "-").lower()
    return ALIASES.get(k, k)


def canonical_params(params) -> dict:
    """``params`` with one key per flag, every key canonical. The canonical
    spelling wins a collision; among aliases only, the LAST in JSONB key order wins —
    what llama.cpp did with the repeated flag, so such a row launches exactly
    as before."""
    out: dict = {}
    exact: dict = {}
    # PostgreSQL's JSONB key order (byte length, then bytes): the order the
    # node reads a stored row in, whatever order a request carried — so the
    # "last alias" here is the one the node launches.
    for k, v in sorted((params or {}).items(),
                       key=lambda kv: (len(str(kv[0]).encode()), str(kv[0]).encode())):
        c = canonical_key(k)
        is_exact = str(k) == c
        if c not in out or is_exact or not exact[c]:
            out[c] = v
            exact[c] = is_exact
    return out


def alias_report(params) -> dict:
    """What a stored row carries besides canonical keys: ``aliases`` (keys not
    in canonical spelling) and ``conflicts`` (flags stored under more than one
    spelling — the ones the node used to emit twice)."""
    seen: dict = {}
    aliases = []
    for k in (params or {}):
        c = canonical_key(k)
        seen.setdefault(c, []).append(str(k))
        if str(k) != c:
            aliases.append(str(k))
    return {"aliases": sorted(aliases),
            "conflicts": sorted(c for c, ks in seen.items() if len(ks) > 1)}


def _jsonb_last_wins(params) -> dict:
    """What the node launched before 2026.11-ga.3: every spelling emitted, and
    llama.cpp kept the LAST in JSONB key order."""
    out: dict = {}
    for k, v in sorted((params or {}).items(),
                       key=lambda kv: (len(str(kv[0]).encode()), str(kv[0]).encode())):
        out[canonical_key(k)] = v
    return out


def effective_slots(params, *, before: bool = False):
    """(parallel, ctx-size, context per slot) the engine launches with, from the
    canonical rule; None where the row does not set it (the engine default).
    ``before=True``: what it launched with under the old last-spelling rule —
    a row storing two spellings changes at its next launch."""
    p = _jsonb_last_wins(params) if before else canonical_params(params)

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    par, ctx = _int(p.get("parallel")), _int(p.get("ctx-size"))
    unified = p.get("kv-unified") in (True, "true", "on", 1)
    per_slot = ctx if (ctx and unified) else (ctx // par if ctx and par else None)
    return {"parallel": par, "ctx_size": ctx, "ctx_per_slot": per_slot}
