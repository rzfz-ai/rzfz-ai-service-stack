# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#612 — service-to-service key minting for per-user agents.

On an LLM-Manager box, agent-manager provisions per-user agents (hermes) that
need an LLM backend. They talk to THIS manager's OpenAI ingress
(``llm-manager:8080/v1``) with a per-user ``rzfz-sk`` key, so metering, quota
and audit attribute every call to the agent's owner. The keys are minted here,
by agent-manager, at instance launch — and revoked at instance delete.

Auth deliberately mirrors ``authz.from_caddy``, anchored on agent-manager
instead: the request's immediate TCP peer must be the agent-manager container
(fail-closed when the name does not resolve). No shared admin token travels —
the operator decision on #612 was the anchor path, precisely so no new
long-lived secret lands in any env file. A co-resident container cannot spoof
the source IP (cap_drop ALL — no NET_RAW), same argument as the Caddy anchor.

The mint endpoint auto-creates one cost center per owner (``agents/<user>``,
team ``agents``), so the Usage page groups agent traffic by human, not by
container.
"""
from __future__ import annotations

import datetime as _dt
import logging
import socket
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.keys import _cache, _invalidate_or_503, parse_uuid
from app.auth import generate_api_key
from app.config import get_settings
from app.db import session_scope
from app.models import ApiKey, CostCenter

logger = logging.getLogger("orchestrator.agent_keys")

AGENT_MANAGER_HOST = "agent-manager"
_peer_cache: dict = {"t": 0.0, "ips": frozenset()}
_PEER_TTL = 30.0


def _agent_manager_ips() -> frozenset:
    now = time.time()
    if now - _peer_cache["t"] < _PEER_TTL and _peer_cache["ips"]:
        return _peer_cache["ips"]
    ips: set[str] = set()
    try:
        for res in socket.getaddrinfo(AGENT_MANAGER_HOST, None):
            ips.add(res[4][0])
    except Exception:  # noqa: BLE001 — unresolvable == empty == deny
        pass
    frozen = frozenset(ips)
    if frozen:
        _peer_cache["t"] = now
        _peer_cache["ips"] = frozen
    return frozen


def _require_agent_manager(request: Request) -> None:
    """Fail-closed peer anchor: only the agent-manager container may call.

    On a mismatch the cache is dropped and resolved ONCE more before denying
    (#613 review nit 1): an agent-manager recreate gets a new IP, and a stale
    30s cache would 403 the very launch that recreate was part of — the mint
    is best-effort, so that window silently produced an LLM-less agent.
    """
    remote = ((request.client.host if request.client else "") or "").strip()
    if not remote:
        raise HTTPException(
            status_code=403,
            detail="agent-key endpoints accept only the agent-manager peer")
    if remote not in _agent_manager_ips():
        _peer_cache["t"] = 0.0
        _peer_cache["ips"] = frozenset()
        if remote not in _agent_manager_ips():
            raise HTTPException(
                status_code=403,
                detail="agent-key endpoints accept only the agent-manager peer")


class AgentKeyCreate(BaseModel):
    username: str
    instance_id: str
    # LLMM-13: optional per-key ceilings, so an agent key can be capped like any
    # other key at mint time. Omitted (the pre-LLMM-13 shape) = NULL = no limit,
    # so agent-manager's existing call is unchanged.
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    max_budget_tokens: Optional[int] = None
    # seconds, converted to the INTERVAL column exactly as keys.py::create_key
    # does — never a raw string, which the driver would not adapt.
    budget_duration_seconds: Optional[int] = None


def register_agent_keys_api(app) -> None:
    router = APIRouter()

    @router.post("/internal/agent-keys")
    def mint_agent_key(payload: AgentKeyCreate, request: Request):
        _require_agent_manager(request)
        username = payload.username.strip()
        if not username:
            raise HTTPException(status_code=422, detail="username required")
        settings = get_settings()
        plaintext, key_hash, display = generate_api_key(settings.key_prefix)
        cc_name = f"agents/{username}"
        with session_scope() as s:
            cc = s.query(CostCenter).filter(CostCenter.name == cc_name).first()
            if cc is None:
                cc = CostCenter(name=cc_name, team="agents")
                s.add(cc)
                s.flush()
            key = ApiKey(
                key_hash=key_hash,
                key_prefix=display,
                cost_center_id=cc.id,
                # LLMM-13: attribute the key to its OWNER. The module's stated
                # purpose is that "metering, quota and audit attribute every call
                # to the agent's owner", but `owner_username` (#314) was left
                # NULL here while `keys.py::create_key` populates it — so the
                # per-person usage rollup silently missed all agent traffic.
                owner_username=username,
                # …and honour the optional ceilings the caller passes (NULL =
                # unlimited, as before).
                rpm_limit=payload.rpm_limit,
                tpm_limit=payload.tpm_limit,
                max_budget_tokens=payload.max_budget_tokens,
                budget_duration=(
                    _dt.timedelta(seconds=payload.budget_duration_seconds)
                    if payload.budget_duration_seconds else None),
            )
            s.add(key)
            s.flush()
            logger.info("minted agent key %s for %s (instance %s)",
                        key.key_prefix, cc_name, payload.instance_id)
            return {
                "id": str(key.id),
                "key": plaintext,   # shown ONCE — goes straight into the
                                    # instance env, never persisted here
                "key_prefix": key.key_prefix,
                "cost_center_id": str(cc.id),
            }

    @router.post("/internal/agent-keys/{key_id}/revoke")
    def revoke_agent_key(key_id: str, request: Request):
        _require_agent_manager(request)
        with session_scope() as s:
            key = s.get(ApiKey, parse_uuid(key_id, "key_id"))
            if key is None:
                # revoke-on-delete must be idempotent — a re-run of a failed
                # delete finds the key already gone and that is success
                return {"revoked": False, "missing": True}
            key.status = "revoked"
            key_hash = bytes(key.key_hash)
        # #327 discipline: a revocation whose cache-invalidate fails must NOT
        # report success — the key would keep authenticating for the TTL.
        _invalidate_or_503(_cache(request), key_hash, key_id, "revoked")
        return {"revoked": True}

    app.include_router(router)
