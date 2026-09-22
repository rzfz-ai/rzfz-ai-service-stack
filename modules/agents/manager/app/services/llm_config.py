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

When an operator wants the whole hermes/moltis/coding-tools/openhands/
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
        "openhands": {"role": "coding", "prefix": "openai/"},
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
        "agents": {**_BASELINE["agents"], **(loaded.get("agents") or {})},
        "models": loaded.get("models") or {},
    }
    return merged


_spec = _load_yaml()


def reload() -> None:
    """Re-read the YAML. Mostly for tests and the optional reload endpoint."""
    global _spec
    _spec = _load_yaml()


def model_for(agent_type: str) -> str:
    """Return the LLM alias an agent should use, with any per-agent prefix applied.

    Examples
    --------
    >>> model_for("hermes")
    'qwen3.6'
    >>> model_for("openhands")
    'openai/qwen3.6'  # openhands.prefix is 'openai/'
    """
    agent = _spec.get("agents", {}).get(agent_type, {})
    role = agent.get("role")
    if not role:
        logger.warning("agent_type %r not in standard-models.yaml — using baseline.", agent_type)
        role = _BASELINE["agents"].get(agent_type, {}).get("role", "chat")

    alias = _spec.get("defaults", {}).get(role)
    if not alias:
        logger.warning("role %r has no default in standard-models.yaml — using baseline.", role)
        alias = _BASELINE["defaults"].get(role, "qwen3.6")

    prefix = agent.get("prefix", "")
    return f"{prefix}{alias}"


def embedding_for(agent_type: str) -> str:
    """Return the embedding-model alias for an agent, with optional per-agent prefix.

    Defaults to `defaults.embedding`. Per-agent override is `agents.<type>.embedding_role`.
    """
    agent = _spec.get("agents", {}).get(agent_type, {})
    role = agent.get("embedding_role", "embedding")
    alias = _spec.get("defaults", {}).get(role, _BASELINE["defaults"]["embedding"])
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
    return m.get("auto_start", True) is not False


def _chat_models() -> list[tuple[str, dict[str, Any]]]:
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
    for alias, m in (_spec.get("models") or {}).items():
        roles = set(m.get("roles") or [])
        if roles & _CHAT_ROLES and _is_running(m):
            out.append((alias, m))

    # Put the resolved chat default first (if it survived the filter).
    default_alias = _spec.get("defaults", {}).get("chat")
    if default_alias:
        for i, (alias, _m) in enumerate(out):
            if alias == default_alias and i != 0:
                out.insert(0, out.pop(i))
                break
    return out


def alias_list() -> list[str]:
    """Return the picker allow-list of model aliases (default first).

    Used by consumers like moltis (`MOLTIS_PROVIDERS__LOCAL__MODELS`) and the
    coding-agents (`AGENT_MODEL_ALIASES`) that need a static allow-list of model
    IDs. Reflects only running, chat-capable models — see `_chat_models` — so a
    stopped/downloaded-only model (gemma4, qwen3-coder-next) never appears in the
    picker (selecting it would 503). The resolved `defaults.chat` alias is first.
    """
    return [alias for alias, _m in _chat_models()]


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

    models = []
    for alias, m in _chat_models():
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
    models_obj: dict[str, dict[str, Any]] = {}
    for alias, m in chat:
        display = m.get("display_name") or alias
        models_obj[alias] = {
            "name": f"{display} ({_backend_display(base_url)})",
            "limit": {
                "context": int(m.get("per_slot_context") or 0),
                "output": int(m.get("max_completion_tokens") or 0),
            },
        }
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
