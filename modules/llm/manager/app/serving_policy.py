# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""What a deployment serves, and what the proxy does about it.

The proxy forwards every OpenAI verb to the router (LiteLLM), which routes by
model name only: LiteLLM's ``model_info.mode`` decides which entries
``/v1/embeddings`` can reach, but nothing stops a chat request to an embedding
model (#2979: emb-gb10, qwen3-embedding and qwen3-embedding-0.6b all answered
``/v1/chat/completions`` with 200). The deployment's serve task is the truth the
manager owns, so the check is made here, before anything is forwarded.

``deployment_facts`` is one read of the deployments table, cached for a few
seconds per app instance, never raising: a datastore blip means "unknown", and
an unknown model is forwarded exactly as before.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from fastapi.responses import JSONResponse

logger = logging.getLogger("llm_manager.serving_policy")

#: Seconds a facts snapshot is reused. Short: a deployment that becomes ready
#: or changes its task is seen on the next window.
FACTS_TTL = 3.0

#: The verbs that generate text, and the one that embeds.
GENERATE_PATHS = frozenset({"/v1/chat/completions", "/v1/completions", "/v1/responses"})
EMBED_PATH = "/v1/embeddings"


@dataclass(frozen=True)
class DeploymentFacts:
    model_name: str
    served_model: str
    task: str          # chat | embed | rerank
    status: str        # the deployment's own status (pending|ready|failed|stopped|...)
    ready: bool        # at least one instance the router can route to


def task_kind(task: Optional[str]) -> str:
    """One spelling per serve task (same synonyms as router_config._litellm_mode)."""
    t = (task or "chat").strip().lower()
    if t in ("embed", "embedding", "embeddings"):
        return "embed"
    if t in ("rerank", "reranker", "reranking", "score"):
        return "rerank"
    return "chat"


def load_deployment_facts() -> dict[str, DeploymentFacts]:
    """Every deployment, keyed by its client-facing model name. Raises on a
    datastore error (the caller decides what unknown means)."""
    from datetime import datetime, timezone

    from app.api.inventory import _not_answering
    from app.db import session_scope
    from app.models import Deployment, DeploymentInstance, Model, Worker
    from app.router_config import _SERVEABLE_INSTANCE_STATUS

    out: dict[str, DeploymentFacts] = {}
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        workers = {w.id: w for w in s.query(Worker).all()}
        routable: set = set()
        for inst in s.query(DeploymentInstance).all():
            if inst.status not in _SERVEABLE_INSTANCE_STATUS or not inst.endpoint:
                continue
            # the rule /api/deployments counts ready_instances by: a remote
            # worker that stopped beating or reporting does not count
            if _not_answering(workers.get(getattr(inst, "worker_id", None)), now) is not None:
                continue
            routable.add(inst.deployment_id)
        for dep in s.query(Deployment).all():
            model = s.get(Model, dep.model_id)
            out[dep.model_name] = DeploymentFacts(
                model_name=dep.model_name,
                served_model=(model.name if model else dep.model_name),
                task=task_kind(getattr(dep, "task", None)),
                status=str(dep.status or ""),
                ready=dep.id in routable)
    return out


def facts_snapshot(app) -> Optional[dict[str, DeploymentFacts]]:
    """The cached facts of this app instance, refreshed every FACTS_TTL; None
    when they cannot be read. BLOCKING on a miss — call it offloaded.

    ``app.state.deployment_facts_resolver`` (a callable returning the mapping)
    replaces the datastore read: the test / DI hook."""
    resolver = getattr(app.state, "deployment_facts_resolver", None)
    if resolver is not None:
        return resolver()
    now = time.monotonic()
    cache = getattr(app.state, "_deployment_facts_cache", None)
    if cache and now - cache["t"] < FACTS_TTL:
        return cache["facts"]
    try:
        facts = load_deployment_facts()
    except Exception as exc:  # noqa: BLE001 - unknown, never a refusal
        logger.debug("deployment facts unavailable: %s", exc)
        facts = None
    app.state._deployment_facts_cache = {"t": now, "facts": facts}
    return facts


#: #3041: the box-wide ceiling on the tokens one request may generate, when
#: LLM_MANAGER_MAX_TOKENS_CEILING is unset. A model whose manifest entry names a
#: lower `max_completion_tokens` gets that lower value.
DEFAULT_MAX_TOKENS_CEILING = 32768

#: The body field that bounds the generation, per verb. Chat also accepts the
#: newer `max_completion_tokens`, which is clamped the same way.
_LIMIT_FIELDS = {"/v1/chat/completions": ("max_tokens", "max_completion_tokens"),
                 "/v1/completions": ("max_tokens",),
                 "/v1/responses": ("max_output_tokens",)}


#: llama.cpp's own spellings of the same limit. The engine reads ``n_predict``
#: ahead of ``max_tokens`` (server-common.cpp: "if n_predict is present, we
#: overwrite the value specified earlier by max_tokens"), so a body carrying it
#: decides the generation length whatever the verb's field says. From llama.cpp
#: v0.9.0 on, ``/v1/responses`` (and ``/v1/messages``) also merge an
#: ``extra_params`` object into the converted chat body AFTER
#: ``max_output_tokens`` became ``max_tokens`` — so ``extra_params.max_tokens``
#: overrides the clamped value. Every one of these is bounded like the verb's
#: own field.
_ENGINE_LIMIT_FIELDS = ("n_predict",)
_EXTRA_PARAMS_LIMIT_FIELDS = ("n_predict", "max_tokens", "max_completion_tokens",
                              "max_output_tokens")


def _clamp(holder: dict, field: str, ceiling: int, model: str) -> bool:
    """Lower ``holder[field]`` to ``ceiling``; True when the field is present.

    A negative value is llama.cpp's "no limit" (-1 infinite, -2 until the
    context is full), so it is lowered too."""
    value = holder.get(field)
    if value is None:
        return False
    try:
        asked = int(value)
    except (TypeError, ValueError):
        return True           # not ours to fix: the engine says what is wrong
    if asked > ceiling or asked < 0:
        holder[field] = ceiling
        logger.info("proxy: %s=%d for model=%s lowered to its ceiling %d",
                    field, asked, model, ceiling)
    return True


def box_ceiling() -> int:
    """``LLM_MANAGER_MAX_TOKENS_CEILING``: 0 switches the ceiling off; unset,
    empty or unreadable is the default. Never raises."""
    import os

    raw = os.environ.get("LLM_MANAGER_MAX_TOKENS_CEILING")
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_TOKENS_CEILING
    try:
        value = int(float(str(raw).strip()))
    except ValueError:
        logger.warning("LLM_MANAGER_MAX_TOKENS_CEILING=%r is not a number — using %d",
                       raw, DEFAULT_MAX_TOKENS_CEILING)
        return DEFAULT_MAX_TOKENS_CEILING
    return max(0, value)


def model_ceiling(model: str, facts: Optional[DeploymentFacts]) -> Optional[int]:
    """The most tokens one request to ``model`` may generate, or None (off).

    The box ceiling, lowered to the model's own `max_completion_tokens` from
    the model manifest when the manifest names the model (by its client-facing
    name, or the name the engine serves it under)."""
    ceiling = box_ceiling()
    if ceiling <= 0:
        return None
    try:
        from app.model_manifest import load_models

        manifest = load_models()
    except Exception:  # noqa: BLE001 - the box ceiling still holds
        manifest = {}
    for name in (model, facts.served_model if facts else None):
        entry = manifest.get(name) if name else None
        own = (entry or {}).get("max_completion_tokens") if isinstance(entry, dict) else None
        try:
            own = int(own) if own is not None else None
        except (TypeError, ValueError):
            own = None
        if own and own > 0:
            return min(ceiling, own)
    return ceiling


def apply_token_ceiling(body: dict, upstream_path: str, model: str,
                        facts: Optional[DeploymentFacts]) -> Optional[int]:
    """#3041: no request generates more than the model's ceiling.

    A larger limit is lowered to the ceiling; a request that names no limit
    gets the ceiling as its limit (a consumer that sends none, such as LightRAG,
    otherwise generates until the context is full). A smaller limit is kept.
    Returns the ceiling applied, or None when there is none for this verb."""
    fields = _LIMIT_FIELDS.get(upstream_path)
    if not fields or not isinstance(body, dict):
        return None
    ceiling = model_ceiling(model, facts)
    if ceiling is None:
        return None
    named = False
    for field in fields:
        named = _clamp(body, field, ceiling, model) or named
    for field in _ENGINE_LIMIT_FIELDS:
        _clamp(body, field, ceiling, model)
    extra = body.get("extra_params")
    if isinstance(extra, dict):
        for field in _EXTRA_PARAMS_LIMIT_FIELDS:
            _clamp(extra, field, ceiling, model)
    if not named:
        body[fields[0]] = ceiling
    return ceiling


def _refuse(status_code: int, message: str, *, code: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, headers=headers,
                        content={"error": {"message": message, "type": "invalid_request_error",
                                           "code": code}})


def wrong_verb_refusal(model: str, upstream_path: str,
                       facts: Optional[DeploymentFacts]) -> Optional[JSONResponse]:
    """#2979: a request for a verb the model does not serve, refused with 400.

    Chat, completions and responses need a chat model; embeddings need an
    embedding model. A rerank model serves neither (its verb is /v1/rerank).
    An unknown model (no deployment, or facts unreadable) is not judged here."""
    if facts is None:
        return None
    if upstream_path in GENERATE_PATHS and facts.task != "chat":
        verb = "/v1/embeddings" if facts.task == "embed" else "/v1/rerank"
        kind = "an embedding model" if facts.task == "embed" else "a rerank model"
        return _refuse(400, f"{model} is {kind}; it does not generate text. Use {verb}.",
                       code="model_does_not_serve_this_endpoint")
    if upstream_path == EMBED_PATH and facts.task != "embed":
        verb = "/v1/chat/completions" if facts.task == "chat" else "/v1/rerank"
        kind = "a chat model" if facts.task == "chat" else "a rerank model"
        return _refuse(400, f"{model} is {kind}; it does not compute embeddings. Use {verb}.",
                       code="model_does_not_serve_this_endpoint")
    return None


#: Seconds a client is told to wait when a model has no ready instance: a
#: restarting engine is back in ~10 s, a rebooting worker in about a minute.
RETRY_AFTER_SECONDS = 15


def _is_paused(facts: DeploymentFacts) -> bool:
    """The operator stopped it: not a temporary state, so not a 503."""
    return facts.status in ("stopped", "stopping")


def unavailable_response(model: str) -> JSONResponse:
    return _refuse(503, f"{model} is temporarily unavailable: none of its instances is ready "
                        "(an engine is restarting or its worker is unreachable). Retry later.",
                   code="model_temporarily_unavailable",
                   headers={"Retry-After": str(RETRY_AFTER_SECONDS)})


def no_replica_refusal(model: str, facts: Optional[DeploymentFacts]) -> Optional[JSONResponse]:
    """A deployed model with no ready instance right now answers 503 with
    Retry-After. Without this the router, which no longer lists the model,
    answered 400 "Invalid model name", which tells a client its request is
    wrong and must not be retried. Unknown and operator-stopped models are left
    to the router."""
    if facts is None or facts.ready or _is_paused(facts):
        return None
    return unavailable_response(model)


def router_unknown_model(status_code: int, data) -> bool:
    """Is this the router's "Invalid model name" 400?"""
    if status_code != 400:
        return False
    try:
        import json as _json

        text = data if isinstance(data, str) else _json.dumps(data)
    except Exception:  # noqa: BLE001
        return False
    return "Invalid model name" in text


def masks_a_deployed_model(model: str, facts: Optional[DeploymentFacts], status_code: int,
                           data) -> Optional[JSONResponse]:
    """The router's 400 for a model the manager knows is deployed: its config
    is reloaded seconds after an instance comes or goes, and in that window it
    does not list the model. That is the same temporary state -> 503."""
    if facts is None or _is_paused(facts) or not router_unknown_model(status_code, data):
        return None
    return unavailable_response(model)
