# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Runtime settings + identity endpoints for the management UI (Phase-3 S5).

  GET   /api/me         the Authentik identity Caddy forwarded (username +
                        groups) — lets the SPA show who's signed in.
  GET   /api/settings   NON-SECRET effective settings: the live metering mode
                        (+ its source), plus booleans for node-registration /
                        rollup-signing readiness and a few display values.
  PATCH /api/settings   set the metering-mode override (available|strict). It
                        persists to runtime_settings and takes effect on the
                        hot path within the proxy's short cache window — no
                        container restart. Secrets are never returned or set
                        here (node key / rollup key rotate via .env + restart).
                        K4 (#2521 follow-up): also `chat_affinity` (bool; off =
                        every chat turn routed like a request without a chat
                        id, takes effect on the next request) and
                        `routing_strategy` (validated against
                        router_config.ROUTING_STRATEGIES; the router config is
                        rebuilt at once and the router swaps LiteLLM
                        blue-green, #2509 — no dropped requests). Every change
                        is logged with the admin's name (#357 partial).
  DELETE /api/settings/{key}
                        REMOVE an override so the .env value takes effect again
                        (#358). Without it, PATCH was a one-way door: the env
                        value stayed permanently shadowed and the only way back
                        was a hand-written DELETE against the database.

Admin-gated (require_admin: Authentik admin group + Caddy source-IP anchor),
same fail-closed gate as the key-management API.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool

from app import settings_store
from app.authz import Role, capabilities_for, require_authenticated, require_role
from app.config import _METERING_MODES, get_settings

logger = logging.getLogger(__name__)


class SettingsPatch(BaseModel):
    metering_mode: Optional[str] = None
    # K4 (#2521 follow-up): per-chat session affinity on/off, and the router's
    # load-balancing algorithm (one of router_config.ROUTING_STRATEGIES).
    # StrictBool: "false" (a string) is a 422, not silently coerced.
    chat_affinity: Optional[StrictBool] = None
    routing_strategy: Optional[str] = None


def _effective_settings() -> dict:
    from app.router_config import (DEFAULT_ROUTING_STRATEGY, ROUTING_STRATEGIES,
                                   router_state)

    st = get_settings()
    router = router_state(st.router_config_path)
    overrides = settings_store.list_overrides()
    mm_override = overrides.get(settings_store.METERING_MODE_KEY)
    return {
        # K4: per-chat session affinity (#2521) and the load-balancing
        # algorithm. `*_source` says whether an operator set it ("override")
        # or the shipped default applies ("default"). `routing_strategy_running`
        # is the algorithm of the config the SERVING LiteLLM was launched with,
        # as the router entrypoint publishes it (None = unknown), and
        # `router_config_state` is running | pending | failed | unknown
        # (router_config.router_state) — a swap that never served must not
        # look like one that did (K4 fix round 1).
        "chat_affinity": settings_store.effective_chat_affinity(),
        "chat_affinity_source": ("override" if settings_store.CHAT_AFFINITY_KEY in overrides
                                 else "default"),
        "routing_strategy": settings_store.effective_routing_strategy(),
        "routing_strategy_source": ("override" if settings_store.ROUTING_STRATEGY_KEY in overrides
                                    else "default"),
        "routing_strategy_default": DEFAULT_ROUTING_STRATEGY,
        "routing_strategies": list(ROUTING_STRATEGIES),
        "routing_strategy_running": router["running_strategy"],
        "router_config_state": router["state"],
        # billing-meter CAP behaviour actually in force right now
        "metering_mode": settings_store.effective_metering_mode(),
        "metering_mode_env": st.metering_mode,          # the .env default/floor
        "metering_mode_source": "override" if mm_override is not None else "env",
        "metering_modes": list(_METERING_MODES),        # valid choices for the UI
        # readiness booleans — never the secret values themselves
        "node_registration_enabled": bool(st.node_key),
        "rollup_signing_enabled": bool(st.rollup_key),
        # LLMM-11: node-credential POSTURE, read-only (the boundary is .env —
        # never runtime-editable here, same rule as the RBAC group lists).
        # "allow" means POST /api/workers/enroll still hands the joining node the
        # SHARED node_key and `authorize_command_node` accepts it for ANY worker
        # name — so one compromised node can register as, claim commands for, and
        # open the inference relay as any other, and rotate-key cannot contain it.
        # "enforce" is the isolated posture (fresh installs get it from
        # `rzfz init`; upgraded fleets stay on "allow" until every node is
        # re-enrolled with LLM_WORKER_COMMAND_KEY). It was invisible to an operator
        # before this — the console showed no way to tell which posture a box was
        # in, and the insecure one is the code default.
        "command_key_mode": st.command_key_mode,
        "shared_node_key_accepted": st.command_key_mode != "enforce",
        "worker_isolation_enforced": st.command_key_mode == "enforce",
        "worker_approval_mode": st.worker_approval_mode,
        # display-only, non-secret
        "litellm_base_url": st.litellm_base_url,
        "key_prefix": st.key_prefix,
        "router_config_path": st.router_config_path,
        "admin_groups": list(st.admin_groups),
        # #284 Phase 4: the console-access tier groups, READ-ONLY (the
        # access-control boundary is .env — never runtime-editable here).
        # `admin_groups` above is the super-admin tier; the two below grant
        # console admin / user access. `superadmin_groups` is an alias of
        # `admin_groups` for a clearer label in the Settings UI.
        "superadmin_groups": list(st.admin_groups),
        "llm_admin_groups": list(st.llm_admin_groups),
        "llm_user_groups": list(st.llm_user_groups),
    }


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _affinity_changed(request: Request, *, clear_pins: bool) -> None:
    from app.proxy import invalidate_chat_affinity

    invalidate_chat_affinity(request.app, clear_pins=clear_pins)


def _rebuild_router() -> dict:
    """K4: a new algorithm reaches the router through the SAME path as a fleet
    change — ``rebuild_from_state`` → ``write_router_config`` (one atomic
    replace, nothing written when the bytes are unchanged) → the router's
    blue-green swap (#2509). The setting is already stored; a failed rebuild is
    reported, not raised, and the next rebuild (any worker registration)
    carries it."""
    from app.router_config import rebuild_from_state

    try:
        config = rebuild_from_state()
    except Exception as exc:
        logger.warning("settings: router rebuild after a routing_strategy change "
                       "failed — the next rebuild applies it", exc_info=True)
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": True,
            "routing_strategy": config["router_settings"]["routing_strategy"]}


def register_settings_api(app) -> None:
    # #314: /api/me answers for ANY authenticated identity (its whole job is
    # "who am I") — it must NOT sit behind the super-admin settings gate, or a
    # signed-in non-admin gets the blank, silently-failing console the issue
    # flags. Its own router carries only `require_authenticated` (Caddy anchor +
    # identity present); it reports the resolved role, which may be NONE.
    me_router = APIRouter()

    @me_router.get("/api/me")
    def me(identity: dict = Depends(require_authenticated)):
        role = identity["role"]
        return {
            "username": identity["username"],
            "groups": identity["groups"],
            "role": role.value,
            "capabilities": capabilities_for(role),
        }

    # Global settings / secrets / router config is the SUPER-ADMIN tier (matrix:
    # admins operate WITHIN policy, they do not change the box's global knobs).
    _super = require_role(Role.SUPERADMIN)
    router = APIRouter(dependencies=[Depends(_super)])

    @router.get("/api/settings")
    def read_settings():
        return _effective_settings()

    @router.patch("/api/settings")
    def update_settings(payload: SettingsPatch, request: Request,
                        identity: dict = Depends(_super)):
        from app.router_config import ROUTING_STRATEGIES

        # Validate before writing anything: a PATCH with one bad field must not
        # half-apply the others. (metering_mode is checked in its own block,
        # which is the FIRST write; chat_affinity is typed StrictBool.)
        if (payload.routing_strategy is not None
                and payload.routing_strategy not in ROUTING_STRATEGIES):
            raise HTTPException(
                status_code=422,
                detail=f"routing_strategy must be one of {list(ROUTING_STRATEGIES)}",
            )
        if payload.metering_mode is not None:
            if payload.metering_mode not in _METERING_MODES:
                raise HTTPException(
                    status_code=422,
                    detail=f"metering_mode must be one of {list(_METERING_MODES)}",
                )
            before = settings_store.effective_metering_mode()
            settings_store.set_override(
                settings_store.METERING_MODE_KEY, payload.metering_mode
            )
            # Partial step toward #357 (which wants a real audit TABLE, not a log
            # line): record WHO changed the billing-meter mode and from what, so
            # a flip is at least attributable in the container log. This does not
            # close #357 — a log is not an audit trail and does not survive a
            # log rotation or a container rebuild.
            logger.info(
                "settings: metering_mode %s -> %s by %s",
                before, payload.metering_mode,
                (identity or {}).get("username", "?"),
            )
            # Drop the proxy's cached mode so the change is visible immediately.
            from app.proxy import invalidate_metering_mode_cache

            invalidate_metering_mode_cache(request.app)
        who = (identity or {}).get("username", "?")
        if payload.chat_affinity is not None:
            before = settings_store.effective_chat_affinity()
            settings_store.set_override(settings_store.CHAT_AFFINITY_KEY,
                                        "on" if payload.chat_affinity else "off")
            # Same attribution line as the metering mode (#357 partial).
            logger.info("settings: chat_affinity %s -> %s by %s",
                        _on_off(before), _on_off(payload.chat_affinity), who)
            # The cache is always dropped (cheap); the pins only on a real
            # change — re-saving "on" must not re-pin every running chat.
            _affinity_changed(request, clear_pins=before != payload.chat_affinity)
        rebuild = None
        if payload.routing_strategy is not None:
            before = settings_store.effective_routing_strategy()
            settings_store.set_override(settings_store.ROUTING_STRATEGY_KEY,
                                        payload.routing_strategy)
            logger.info("settings: routing_strategy %s -> %s by %s",
                        before, payload.routing_strategy, who)
            rebuild = _rebuild_router()
        out = _effective_settings()
        if rebuild is not None:
            out["router_rebuild"] = rebuild
        return out

    @router.delete("/api/settings/{key}")
    def clear_setting(key: str, request: Request,
                      identity: dict = Depends(_super)):
        """Remove an override so the .env value governs again (#358).

        Idempotent: deleting an absent override is a 200 with removed=false, not
        a 404 — the caller's intent ("make env win") is satisfied either way, and
        a UI that reverts twice should not see an error.
        """
        before_affinity = settings_store.effective_chat_affinity()
        try:
            removed = settings_store.clear_override(key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        logger.info(
            "settings: override %s cleared (removed=%s) by %s",
            key, removed, (identity or {}).get("username", "?"),
        )
        if key == settings_store.METERING_MODE_KEY:
            from app.proxy import invalidate_metering_mode_cache

            invalidate_metering_mode_cache(request.app)
        rebuild = None
        if key == settings_store.CHAT_AFFINITY_KEY:
            _affinity_changed(request, clear_pins=before_affinity
                              != settings_store.effective_chat_affinity())
        elif key == settings_store.ROUTING_STRATEGY_KEY and removed:
            rebuild = _rebuild_router()   # back to the default algorithm
        out = {**_effective_settings(), "removed": removed}
        if rebuild is not None:
            out["router_rebuild"] = rebuild
        return out

    app.include_router(me_router)
    app.include_router(router)
