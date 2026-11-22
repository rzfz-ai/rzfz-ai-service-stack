#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Host-side ChecksumManager for `rzfz setup --checksum-take` & friends.

#1225: this file used to be a COPY of the Portal's checksum_manager — frozen
on 2026-08-16, 335 lines, no #1191 columns. Both wrote the same
`.checksums.db`, so a CLI snapshot (init baseline, pre/post-upgrade, release)
carried hash-only rows while a Portal snapshot carried the key list and the
per-key HMACs, and the governance diff could not compare the two on equal
terms. There is ONE implementation now: the Portal's module, loaded from
core/config/app/services/ on the host, plus a subclass that fills in the
host defaults (RAZZFAZZ_STACK_ROOT, the DB next to it, the secret provider
that derives the key-level MAC key from the box's .env).

The module is loaded under its own package name (not `app`): its
`default_secret_provider` does a relative `.env_utils` import, and that file
imports `razzfazz_common` — both resolve from the source tree here, without
touching the Portal's `app` package (which the test tiers evict per subtree).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SERVICES = os.path.join(_REPO, "core", "config", "app", "services")
_COMMON = os.path.join(_REPO, "core", "common")
_PKG = "razzfazz_portal_services"


def _load_portal_module():
    if os.path.isdir(_COMMON) and _COMMON not in sys.path:
        sys.path.append(_COMMON)  # an installed razzfazz_common still wins
    pkg = sys.modules.get(_PKG)
    if pkg is None:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [_SERVICES]
        sys.modules[_PKG] = pkg
    name = f"{_PKG}.checksum_manager"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_SERVICES, "checksum_manager.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


_portal = _load_portal_module()

# Back-compat names for host callers (resolved at import, like before).
STACK_ROOT = os.environ.get("RAZZFAZZ_STACK_ROOT", "/stack")
DB_PATH = os.path.join(STACK_ROOT, ".checksums.db")
GOVERNANCE_PATTERNS = _portal.GOVERNANCE_PATTERNS
default_secret_provider = _portal.default_secret_provider


def _stack_root_now():
    # Resolved at CALL time (cf. the Portal's _default_db_path): the CLI
    # wrappers export RAZZFAZZ_STACK_ROOT before invoking setup.py.
    return os.environ.get("RAZZFAZZ_STACK_ROOT", "/stack")


class ChecksumManager(_portal.ChecksumManager):
    """The Portal's manager with the host's defaults."""

    def __init__(self, db_path=None, *, stack_root=None, secret_provider=None, **kwargs):
        root = stack_root if stack_root is not None else _stack_root_now()
        if db_path is None:
            db_path = os.path.join(root, ".checksums.db")
        if secret_provider is None:
            secret_provider = default_secret_provider(root)
        super().__init__(db_path, stack_root=root, secret_provider=secret_provider, **kwargs)
