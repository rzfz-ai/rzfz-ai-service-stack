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
from pydantic import BaseModel

from app import settings_store
from app.authz import Role, capabilities_for, require_authenticated, require_role
from app.config import _METERING_MODES, get_settings

logger = logging.getLogger(__name__)


class SettingsPatch(BaseModel):
    metering_mode: Optional[str] = None


def _effective_settings() -> dict:
    st = get_settings()
    overrides = settings_store.list_overrides()
    mm_override = overrides.get(settings_store.METERING_MODE_KEY)
    return {
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
        return _effective_settings()

    @router.delete("/api/settings/{key}")
    def clear_setting(key: str, request: Request,
                      identity: dict = Depends(_super)):
        """Remove an override so the .env value governs again (#358).

        Idempotent: deleting an absent override is a 200 with removed=false, not
        a 404 — the caller's intent ("make env win") is satisfied either way, and
        a UI that reverts twice should not see an error.
        """
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
        return {**_effective_settings(), "removed": removed}

    app.include_router(me_router)
    app.include_router(router)
