# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""LLM-config bridge between `core/llm/standard-models.yaml` and `catalog.py`.

Purpose
-------
`core/llm/standard-models.yaml` is the canonical source-of-truth for which
LLM each agent type uses (alias, role, optional per-agent prefix like
`openai/`). This module loads that YAML and exposes simple helpers so
`catalog.py` no longer needs to hardcode `'LLM_MODEL': 'gemma4'`-style
literals scattered through every agent's `env_template` block.

When an operator wants the whole hermes/moltis/coding-tools/
paperclip fleet to track a new model (e.g. `defaults.chat: gemma4 →
qwen3.5`), they edit one YAML key — the next agent provisioned from
catalog picks up the new value automatically.

Bind-mount
----------
The YAML is read from `/standard-models.yaml` inside the container by
default (the path is overridable via `STANDARD_MODELS_YAML`). The
agent-manager compose entry bind-mounts `core/llm/standard-models.yaml`
to that target so operator edits propagate on the next
`docker restart agent-manager` — no image rebuild required.

Resilience
----------
The YAML is loaded at module import. If it's missing or malformed the
loader logs a warning and falls back to a frozen baseline that matches
the GA-time defaults. The agent-manager keeps booting in that case;
worst case is that newly-provisioned agents get the baseline models
instead of operator-edited ones, which is exactly the pre-S1 behaviour.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Fallback baseline used when the YAML can't be read. These match the
# values that were hardcoded in catalog.py at the time of the M031 S1
# cut — they keep the agent-manager working when the operator-mountable
# YAML isn't reachable.
_BASELINE: dict[str, Any] = {
    "defaults": {
        "chat": "qwen3.6",
        "coding": "qwen3.6",
        "general": "qwen3.6",
        "vision": "qwen3.6",
        "embedding": "qwen3-embedding",
        "reranker": "qwen3-reranker",
    },
    "agents": {
        "hermes": {"role": "chat"},
        "moltis": {"role": "coding"},
        "coding-tools": {"role": "coding"},
        "paperclip": {"role": "chat"},
    },
    "models": {},
}

_YAML_PATH = Path(os.environ.get("STANDARD_MODELS_YAML", "/standard-models.yaml"))

_spec: dict[str, Any] = _BASELINE


def _load_yaml() -> dict[str, Any]:
    """Read the YAML once and return a merged spec (yaml-on-top-of-baseline).

    The merge keeps `_BASELINE` as a floor: if the YAML is missing keys
    (e.g. operator deletes `agents.paperclip` by accident), we still
    provision paperclip with the baseline role.
    """
    if not _YAML_PATH.exists():
        logger.warning(
            "standard-models.yaml not at %s — falling back to baseline; "
            "is the bind-mount missing from agents/compose.yml?",
            _YAML_PATH,
        )
        return dict(_BASELINE)

    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed in agent-manager — falling back to baseline.")
        return dict(_BASELINE)

    try:
        loaded = yaml.safe_load(_YAML_PATH.read_text()) or {}
    except Exception:
        logger.exception("standard-models.yaml unreadable — falling back to baseline.")
        return dict(_BASELINE)

    merged: dict[str, Any] = {
        "defaults": {**_BASELINE["defaults"], **(loaded.get("defaults") or {})},
        "defaults_by_hardware": loaded.get("defaults_by_hardware") or {},
        "agents": {**_BASELINE["agents"], **(loaded.get("agents") or {})},
        "models": loaded.get("models") or {},
    }
    return merged


_spec = _load_yaml()


# ── #2158 / #2512: the catalogue's hardware conditionality ───────────────────
# core/llm/hardware_catalog.py is the rule; it is not mounted into this
# container, so the family rule is mirrored here byte-for-byte and a guard
# (tests/unit/agent-manager/test_2512_agent_model_lists_follow_the_role.py)
# runs both over the same strings. The container reads the box's .env through
# `env_file:`, so HARDWARE and LLM_MASTER_ROLE are in os.environ.
_KNOWN_FAMILIES = ("amd", "nvidia", "apple", "cpu")
_GPU_FAMILIES = ("amd", "nvidia", "apple")
_DEFAULT_FAMILY = "amd"


def _hardware_family(value) -> str:
    """Byte-for-byte hardware_catalog.hardware_family (the manager's rule)."""
    h = (value or "").lower()
    if ("amd" in h) or ("gfx" in h) or ("rocm" in h) or ("vulkan" in h):
        return "amd"
    if ("nvidia" in h) or ("cuda" in h):
        return "nvidia"
    if ("apple" in h) or ("metal" in h) or ("mlx" in h):
        return "apple"
    if h == "cpu" or h.endswith("-cpu") or "cpu" in h.split("-"):
        return "cpu"
    return h


def _catalog_family() -> str:
    """The family this box's agents are served by.

    RZFZ_CATALOG_HARDWARE (#2460) wins; a `fleet` master (#2512) is served by
    its GPU workers, never by its own CPU — so never `cpu` there; else the
    box's HARDWARE. Unknown -> the default family, like hardware_catalog."""
    forced = os.environ.get("RZFZ_CATALOG_HARDWARE") or ""
    fam = _hardware_family(forced or os.environ.get("HARDWARE") or "")
    role = str(os.environ.get("LLM_MASTER_ROLE") or "").strip().lower()
    if not forced and role == "fleet":
        # #2536 item 16: the joined GPU workers' family, as post-install
        # resolved it (hardware_catalog.fleet_family) and wrote to .env.
        fleet = _hardware_family(os.environ.get("LLM_FLEET_FAMILY") or "")
        if fleet in _GPU_FAMILIES:
            return fleet
        return fam if fam in _GPU_FAMILIES else _DEFAULT_FAMILY
    if not forced and fam == "cpu" and role != "standalone":
        # #2512 fix round 1: a CPU master whose role is not decided yet (the
        # upgrade deferred it) is UNKNOWN, not "a standalone CPU box": the
        # catalogue default — so a fleet master waiting for its derivation
        # never hands new agents the 4B. `rzfz init` writes `standalone` for
        # testvm-cpu explicitly.
        return _DEFAULT_FAMILY
    return fam if fam in _KNOWN_FAMILIES else _DEFAULT_FAMILY


def _applies_here(m: dict[str, Any]) -> bool:
    """An entry's `hardware:` list (hardware_catalog.entry_families) admits
    this box's family; no list = every family."""
    hw = (m or {}).get("hardware")
    if hw is None:
        return True
    if isinstance(hw, str):
        hw = [hw]
    return _catalog_family() in [_hardware_family(h) for h in hw]


def _defaults() -> dict[str, Any]:
    """`defaults` with this family's `defaults_by_hardware` overrides —
    hardware_catalog.defaults_for. A CPU standalone box's agents get the 4B;
    a fleet master's agents never do (#2512)."""
    base = dict(_spec.get("defaults") or {})
    fam = _catalog_family()
    for key, overrides in (_spec.get("defaults_by_hardware") or {}).items():
        if _hardware_family(key) == fam and isinstance(overrides, dict):
            base.update(overrides)
    return base


def reload() -> None:
    """Re-read the YAML. Mostly for tests and the optional reload endpoint."""
    global _spec
    _spec = _load_yaml()


# ── #3052: the coding default follows the fleet ─────────────────────────────
# The coding default is the best model the fleet
# actually SERVES. A master whose own .env says HARDWARE=cpu /
# LLM_MASTER_ROLE=standalone resolves `defaults_by_hardware.cpu.coding` (the
# 4B, 16k context) above — although a joined GPU worker serves qwen3.6. The
# family rule cannot see that worker; the LLM Manager's /v1/models can. So the
# provisioner hands the served aliases in (`served`) and:
#   * a configured default that is GPU-class stays as it is (a GPU box or a
#     fleet master is unchanged);
#   * a CPU-class default gives way to the first GPU-class coding model the
#     fleet serves, in the YAML's role order: the catalogue's own
#     `defaults.coding` first, then every `roles: [coding …]` entry in
#     declaration order — deterministic, whatever order the server lists;
#   * nothing GPU-class served -> the CPU default stays;
#   * served list unknown (None) -> today's behaviour, byte for byte.

def _is_gpu_class(m: dict[str, Any] | None) -> bool:
    """An entry that runs on a GPU family: no `hardware:` list (every family)
    or a list naming at least one GPU family. `hardware: [cpu]` is CPU-class."""
    if not isinstance(m, dict):
        return False
    hw = m.get("hardware")
    if hw is None:
        return True
    if isinstance(hw, str):
        hw = [hw]
    return any(_hardware_family(h) in _GPU_FAMILIES for h in hw)


def _role_candidates(role: str) -> list[str]:
    """GPU-class aliases for `role`, in the YAML's precedence order."""
    models = _spec.get("models") or {}
    order: list[str] = []
    first = (_spec.get("defaults") or {}).get(role)
    if first:
        order.append(first)
    for alias, m in models.items():
        if role in set((m or {}).get("roles") or []) and alias not in order:
            order.append(alias)
    return [a for a in order if a in models and _is_gpu_class(models[a])]


def fleet_default(role: str, served) -> str | None:
    """The served model that replaces a CPU-class default for `role`, or None
    when the configured default stands (see the block comment above)."""
    if served is None:
        return None
    current = _defaults().get(role)
    entry = (_spec.get("models") or {}).get(current)
    if entry is None or _is_gpu_class(entry):
        return None
    served_set = {str(s) for s in served}
    for alias in _role_candidates(role):
        if alias in served_set:
            return alias
    return None


def role_of(agent_type: str) -> str:
    """The role an agent key resolves through (`agents.<key>.role`)."""
    agent = _spec.get("agents", {}).get(agent_type, {})
    return agent.get("role") or _BASELINE["agents"].get(agent_type, {}).get("role", "chat")


def alias_for(agent_type: str, served=None) -> str:
    """`model_for` without the per-agent prefix."""
    agent = _spec.get("agents", {}).get(agent_type, {})
    role = agent.get("role")
    if not role:
        logger.warning("agent_type %r not in standard-models.yaml — using baseline.", agent_type)
        role = _BASELINE["agents"].get(agent_type, {}).get("role", "chat")

    alias = _defaults().get(role)
    if not alias:
        logger.warning("role %r has no default in standard-models.yaml — using baseline.", role)
        alias = _BASELINE["defaults"].get(role, "qwen3.6")
    if role == "coding":
        alias = fleet_default("coding", served) or alias
    return alias


def model_for(agent_type: str, served=None) -> str:
    """Return the LLM alias an agent should use, with any per-agent prefix applied.

    `served` (#3052): the aliases the LLM Manager reports as served; a coding
    agent on a CPU-class default then gets the fleet's GPU-class coding model.
    None keeps the configured default.

    Examples
    --------
    >>> model_for("hermes")
    'qwen3.6'
    """
    agent = _spec.get("agents", {}).get(agent_type, {})
    prefix = agent.get("prefix", "")
    return f"{prefix}{alias_for(agent_type, served)}"


def egress_flag(model: str | None) -> str | None:
    """#2962: the catalogue's model risk flag for `model` (an alias, with or
    without a per-agent prefix such as `openai/`), e.g. "denied-only"; None
    when the model carries none or is not in the catalogue."""
    alias = str(model or "").strip().split("/")[-1]
    m = (_spec.get("models") or {}).get(alias)
    if not isinstance(m, dict):
        return None
    flag = str(m.get("egress") or "").strip()
    return flag or None


def embedding_for(agent_type: str) -> str:
    """Return the embedding-model alias for an agent, with optional per-agent prefix.

    Defaults to `defaults.embedding`. Per-agent override is `agents.<type>.embedding_role`.
    """
    agent = _spec.get("agents", {}).get(agent_type, {})
    role = agent.get("embedding_role", "embedding")
    alias = _defaults().get(role, _BASELINE["defaults"]["embedding"])
    prefix = agent.get("prefix", "")
    return f"{prefix}{alias}"


# Roles a chat-style coding/dev tool (gsd, opencode) will offer in its
# model picker. Embedding / reranker / vision-only models are deliberately
# excluded — they don't make sense in a "pick a chat model" dropdown.
_CHAT_ROLES = {"chat", "coding", "general"}


def _is_running(m: dict[str, Any]) -> bool:
    """True unless the model's YAML entry marks it `auto_start: false`.

    A model with `auto_start: false` is downloaded/registered in gpustack but
    NOT loaded (e.g. gemma4, qwen3-coder-next as of 2026-06-13 — qwen3.6 is the
    single always-on default). Offering an un-loaded model in a picker means the
    user can select it and get a 503 on first inference. So the picker list must
    exclude these; only always-on models belong in a "pick a model" dropdown.

    `auto_start` absent (or any value other than the literal `False`) counts as
    running — matching gpustack's default-on behaviour.
    """
    # #2158/#2512: `auto_start` applies only on the families an entry names —
    # elsewhere it is an on-demand spare, not a running model. Without this
    # the CPU 4B (`hardware: [cpu]`) was offered on every GPU box and on a
    # fleet master, where it runs nowhere.
    return m.get("auto_start", True) is not False and _applies_here(m)


def _chat_models(served=None, first: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Return (alias, model-dict) pairs for the models a picker should offer.

    A model is included only if it is BOTH:
      • chat-capable — has at least one of `_CHAT_ROLES` (chat/coding/general);
        embedding / reranker / vision-document-conversion models are excluded.
      • running / always-on — its YAML entry is not `auto_start: false`
        (see `_is_running`); a downloaded-but-not-loaded model would 503.

    The resolved chat default (`defaults.chat`) is placed FIRST when present in
    the filtered set, so consumers that treat the head of the list as the
    pre-selected option surface the always-on default. Remaining models keep
    YAML declaration order.

    With the shipped standard-models.yaml this yields exactly `["qwen3.6"]`
    (gemma4 + qwen3-coder-next are auto_start:false; the embeddings/reranker
    are non-chat). If the operator ever marks another chat model auto_start:true
    it appears here automatically — nothing is hardcoded.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    if served is not None:
        # #3052: what the fleet serves is what a picker can use — whichever
        # box family the entry names and whatever its auto_start says.
        served_set = {str(s) for s in served}
        out = [(alias, m) for alias, m in (_spec.get("models") or {}).items()
               if set(m.get("roles") or []) & _CHAT_ROLES and alias in served_set]
    if not out:
        for alias, m in (_spec.get("models") or {}).items():
            roles = set(m.get("roles") or [])
            if roles & _CHAT_ROLES and _is_running(m):
                out.append((alias, m))

    # Put the resolved chat default first (if it survived the filter) — or
    # the alias the caller names (#3052: the agent's own default).
    default_alias = first or _defaults().get("chat")
    if default_alias:
        for i, (alias, _m) in enumerate(out):
            if alias == default_alias and i != 0:
                out.insert(0, out.pop(i))
                break
    return out


def alias_list(served=None, first: str | None = None) -> list[str]:
    """Return the picker allow-list of model aliases (default first).

    Used by consumers like moltis (`MOLTIS_PROVIDERS__OPENAI__MODELS`) and the
    coding-agents (`AGENT_MODEL_ALIASES`) that need a static allow-list of model
    IDs. Reflects only running, chat-capable models — see `_chat_models` — so a
    stopped/downloaded-only model (gemma4, qwen3-coder-next) never appears in the
    picker (selecting it would 503). The resolved `defaults.chat` alias is first.
    """
    return [alias for alias, _m in _chat_models(served, first)]


def context_windows(served=None, first: str | None = None) -> dict[str, int]:
    """{alias: per-slot context in tokens} for the picker's models.

    `served` (#3073): the same list the picker follows (#3052), so a model the
    fleet serves has its entry on whichever box family the entry names; None
    keeps the configured models. The number is the manifest's per_slot_context
    (ctx-size / parallel), which is what the manager deploys.

    A model whose YAML states no `per_slot_context` is left out, so a consumer
    falls back to its own conservative default instead of trusting a 0.
    """
    out: dict[str, int] = {}
    for alias, m in _chat_models(served, first):
        try:
            ctx = int(m.get("per_slot_context") or 0)
        except (TypeError, ValueError):
            ctx = 0
        if ctx > 0:
            out[alias] = ctx
    return out


#: Hosts that mean "this box's own LLM Manager". `llm` is the canonical service
#: name the cutover put every consumer on (#979/#1445); `llm-manager` is the
#: container behind it.
_MANAGER_HOSTS = ("llm", "llm-manager")


def _backend_display(base_url: str) -> str:
    """The operator-facing NAME of whatever `base_url` reaches (#1445 part b).

    The provider ID stays `gpustack` on purpose: it is a KEY. It is written into
    seeded-once agent config files, `opencode_pipe.py` sends it back as
    `PROVIDER_ID`, and opencode stores the active model as `gpustack/<alias>`.
    Renaming it would fork every existing agent's stored selection for nothing.

    The display name is not a key, and since the cutover it was simply wrong: on
    a 2026.09 box every model in the agent's picker reads "… (GPUStack)" while
    the box runs no GPUStack at all — the base URL points at the manager. An
    operator comparing that list against `rzfz status` sees two different
    stacks.
    """
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return "LLM Manager" if host in _MANAGER_HOSTS else "GPUStack"


def gsd_models(base_url: str = "http://llm:8080/v1", served=None,
               first: str | None = None) -> list[dict[str, Any]]:
    """The `models` list of gsd's / Pi's provider block (see gsd_models_json)."""
    models = []
    for alias, m in _chat_models(served, first):
        display = m.get("display_name") or alias
        models.append({
            "id": alias,
            "name": f"{display} ({_backend_display(base_url)})",
            "reasoning": False,
            "input": ["text"],
            "contextWindow": int(m.get("per_slot_context") or 0),
            "maxTokens": int(m.get("max_completion_tokens") or 0),
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        })
    return models


def gsd_models_json(api_key_placeholder: str = "{{GPUSTACK_API_KEY}}",
                    base_url: str = "http://llm:8080/v1") -> str:
    """Return the JSON content for gsd's `~/.gsd/agent/models.json`.

    Single GPUStack-provider block, one entry per chat-capable model.
    Per-model context_window + max_tokens come straight from the YAML's
    `per_slot_context` + `max_completion_tokens` so a YAML edit propagates
    to gsd's model picker on the next agent provision.

    Use {{GPUSTACK_API_KEY}}-style placeholders for the api_key argument
    when this string will be passed back through the provisioner — the
    provisioner runs its own substitution pass before the value lands in
    the container env.
    """
    import json as _json

    models = gsd_models(base_url)
    payload = {
        "providers": {
            "gpustack": {
                "baseUrl": base_url,
                "api": "openai-completions",
                "apiKey": api_key_placeholder,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": models,
            }
        }
    }
    return _json.dumps(payload, indent=2)


def opencode_models(base_url: str = "http://llm:8080/v1", served=None,
                    first: str | None = None) -> dict[str, dict[str, Any]]:
    """The `models` object of opencode's provider block (opencode_config_json).
    With `served` (#3052) it lists every chat model the fleet serves, so
    `opencode run -m gpustack/<alias>` finds each of them."""
    models_obj: dict[str, dict[str, Any]] = {}
    for alias, m in _chat_models(served, first):
        display = m.get("display_name") or alias
        models_obj[alias] = {
            "name": f"{display} ({_backend_display(base_url)})",
            "limit": {
                "context": int(m.get("per_slot_context") or 0),
                "output": int(m.get("max_completion_tokens") or 0),
            },
        }
    return models_obj


def opencode_config_json(api_key_placeholder: str = "{{GPUSTACK_API_KEY}}",
                         base_url: str = "http://llm:8080/v1",
                         default_alias: str | None = None) -> str:
    """Return the JSON content for opencode's `~/.config/opencode/config.json`.

    `default_alias` is what opencode will pick as the active model on first
    boot (`"gpustack/<alias>"` form). Defaults to the first chat-capable
    model in the YAML.
    """
    import json as _json

    chat = _chat_models()
    models_obj = opencode_models(base_url)
    if not default_alias and chat:
        default_alias = chat[0][0]

    payload = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "gpustack": {
                "npm": "@ai-sdk/openai-compatible",
                "name": _backend_display(base_url),
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key_placeholder,
                },
                "models": models_obj,
            }
        },
        "model": f"gpustack/{default_alias}" if default_alias else "",
    }
    return _json.dumps(payload, indent=2)


def opencode_provider_block(api_key_placeholder: str = "{{GPUSTACK_API_KEY}}",
                            base_url: str = "http://llm:8080/v1") -> str:
    """Return the *single-line* opencode provider block JSON used by
    paperclip's `OPENCODE_GPUSTACK_CONFIG` env var.

    This is a partial-shape variant of opencode_config_json — paperclip
    expects only the `provider` key with `env: [GPUSTACK_API_KEY]` and
    abbreviated model entries.
    """
    import json as _json

    models_obj: dict[str, dict[str, str]] = {}
    for alias, m in _chat_models():
        display = m.get("display_name") or alias
        models_obj[alias] = {"name": display}

    payload = {
        "provider": {
            "gpustack": {
                "name": _backend_display(base_url),
                "npm": "@ai-sdk/openai-compatible",
                "env": ["GPUSTACK_API_KEY"],
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key_placeholder,
                },
                "models": models_obj,
            }
        }
    }
    return _json.dumps(payload, separators=(",", ":"))
