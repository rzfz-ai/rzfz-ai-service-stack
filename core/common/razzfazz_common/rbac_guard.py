# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Fail-closed catalogue checks with a failure cache (#2473, A10).

Used by the Agent Manager and the MCP manager, whose checks run on every
request. Same contract as the LLM Manager's app/rbac_catalogue.py: a good load is
cached by razzfazz_common.rbac for the process; a FAILED load (missing mount,
malformed file, component or permission absent) is remembered for FAILURE_TTL_S,
so a broken mount costs one re-read and one CRITICAL line per window instead of
one per request. Inside the window everybody is denied; when it ends the mount is
re-read, so a repair takes effect without a restart.

The failure is remembered against the catalogue path it happened on: pointing
RZFZ_RBAC_CATALOGUE elsewhere is a new question, not a cached answer. A catalogue that
loads but lacks one permission is NOT a load failure: it denies only the checks that need
that permission (see _report_missing).
"""
import logging
import os
import time
from collections.abc import Iterable

from razzfazz_common import rbac
from razzfazz_common.rbac import CatalogueError

_log = logging.getLogger(__name__)
FAILURE_TTL_S = 30.0
_now = time.monotonic
_load_failure = None  # (deadline, path, CatalogueError): the catalogue cannot be loaded at all
_missing_logged = {}  # (path, "component/permission" | "role X") -> deadline of the next CRITICAL
_validated_path = None

#: what the Agent Manager and the MCP manager serve: (component, permission) and roles.
SERVED_PERMISSIONS = (("agent-manager", "admin"), ("agent-manager", "unlimited"), ("mcp-manager", "admin"))
SERVED_ROLES = ("platform-admin",)


def _path() -> str:
    return os.environ.get(rbac.CATALOGUE_ENV) or rbac.DEFAULT_PATH


def reset_cache() -> None:
    global _load_failure, _validated_path
    _load_failure = None
    _validated_path = None
    _missing_logged.clear()
    rbac.reset_cache()


def _report_missing(path: str, what: str, exc) -> None:
    """One CRITICAL per missing permission per FAILURE_TTL_S. Never cached as a failure of
    OTHER lookups: a catalogue that loads but lacks one permission denies only that check."""
    key = (path, what)
    if _now() >= _missing_logged.get(key, 0.0):
        _missing_logged[key] = _now() + FAILURE_TTL_S
        _log.critical("role catalogue lacks %s, denying it to everyone (fail closed): %s", what, exc)


def _load():
    """The loaded catalogue. A LOAD failure (missing, unreadable, malformed) is remembered per
    path for FAILURE_TTL_S: one re-read and one CRITICAL per window. On the first good load
    every served permission is validated and each missing one is reported by name."""
    global _load_failure, _validated_path
    path = _path()
    failure = _load_failure
    if failure is not None:
        if failure[1] == path and _now() < failure[0]:
            raise failure[2]
        _load_failure = None
    try:
        cat = rbac.catalogue()
    except CatalogueError as exc:
        _load_failure = (_now() + FAILURE_TTL_S, path, exc)
        _log.critical("role catalogue unusable, denying everyone (fail closed): %s", exc)
        raise
    if _validated_path != path:
        _validated_path = path
        for component, permission in SERVED_PERMISSIONS:
            try:
                cat.groups_for(component, permission)
            except CatalogueError as exc:
                _report_missing(path, f"{component}/{permission}", exc)
        for role in SERVED_ROLES:
            try:
                cat.role_groups(role)
            except CatalogueError as exc:
                _report_missing(path, f"role {role}", exc)
    return cat


def groups_for(component: str, permission: str) -> frozenset:
    """The groups the catalogue grants `component`/`permission`; CatalogueError if unusable
    or if this one permission is absent (other lookups are unaffected by the latter)."""
    cat = _load()
    try:
        return cat.groups_for(component, permission)
    except CatalogueError as exc:
        _report_missing(_path(), f"{component}/{permission}", exc)
        raise


def allows(component: str, permission: str, groups) -> bool:
    """True if any of `groups` holds the permission. Fails closed. `groups` may be a
    list or the raw pipe-separated X-Authentik-Groups header."""
    if isinstance(groups, bytes):
        groups = groups.decode("utf-8", "replace")
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split("|") if g.strip()]
    elif not isinstance(groups, Iterable):
        groups = []
    try:
        return bool(groups_for(component, permission) & set(groups or ()))
    except CatalogueError:
        return False


def role_groups(role: str) -> frozenset:
    """Groups of a role (CatalogueError if unusable or the role is absent)."""
    cat = _load()
    try:
        return cat.role_groups(role)
    except CatalogueError as exc:
        _report_missing(_path(), f"role {role}", exc)
        raise
