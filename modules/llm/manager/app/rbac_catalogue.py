# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The role catalogue loader, bind-mounted from core/common/razzfazz_common/rbac.py.

The LLM Manager has no razzfazz_common (FastAPI image, build context
modules/llm/manager), so compose mounts the ONE loader file at
/opt/razzfazz/rbac/razzfazz_rbac.py and the catalogue at
/etc/razzfazz/rbac/roles.yaml. A missing loader mount is a RuntimeError at
import (fail loud). A missing or malformed CATALOGUE raises CatalogueError from
catalogue(); app.config.tier_groups turns that into empty tiers (fail closed).
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time

_log = logging.getLogger(__name__)
#: seconds a FAILED load is remembered: one re-read and one CRITICAL line per
#: window, not per get_settings() call (the completion hot path calls it per request).
FAILURE_TTL_S = 30.0
_now = time.monotonic
_failure: tuple | None = None  # (deadline, CatalogueError)

_MODULE = os.environ.get("RZFZ_RBAC_MODULE", "/opt/razzfazz/rbac/razzfazz_rbac.py")


def _load():
    if not os.path.isfile(_MODULE):
        raise RuntimeError(f"role catalogue loader missing at {_MODULE} — compose must mount "
                           f"core/common/razzfazz_common/rbac.py there")
    spec = importlib.util.spec_from_file_location("razzfazz_rbac", _MODULE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["razzfazz_rbac"] = mod
    spec.loader.exec_module(mod)
    return mod


_rbac = _load()
CatalogueError = _rbac.CatalogueError


def catalogue():
    """The loaded catalogue. A good load is cached for the process (the loader's
    own cache); a failed one is cached for FAILURE_TTL_S so a broken mount costs
    one re-read and one CRITICAL line per window. The same error is re-raised
    inside the window; a repaired mount is picked up when the window ends."""
    global _failure
    if _failure is not None:
        if _now() < _failure[0]:
            raise _failure[1]
        _failure = None
    try:
        cat = _rbac.catalogue()
        for permission in ("superadmin", "admin", "user"):  # the tiers we serve must exist
            cat.groups_for("llm-manager", permission)
        return cat
    except CatalogueError as exc:
        _failure = (_now() + FAILURE_TTL_S, exc)
        _log.critical("role catalogue unusable, every llm-manager tier without a legacy "
                      ".env override has NO groups (fail closed): %s", exc)
        raise


def reset_cache() -> None:
    global _failure
    _failure = None
    _rbac.reset_cache()


def available() -> bool:
    """True if the catalogue loads (or is cached). Feeds the /healthz field."""
    try:
        catalogue()
        return True
    except CatalogueError:
        return False
