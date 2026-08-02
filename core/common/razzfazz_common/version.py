# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Stack-version + brand context processor for templates.

Reads `RAZZFAZZ_VERSION` on every request so the nav reflects the live
state after an upgrade (razzfazz-upgrade.sh writes the new version into
.env inside verify_upgrade, AFTER restart_stack force-recreates the UI
containers). Without per-request reads, the nav stays on the
pre-upgrade version until a manual `docker restart`.

Lookup chain (matches `_current_stack_version` in the existing Config UI):
    1. $RAZZFAZZ_VERSION env var (cheapest, set by container env)
    2. {STACK_ROOT}/.env -> RAZZFAZZ_VERSION= line
    3. {STACK_ROOT}/VERSION  (tree-level fallback for dev mode)
    4. literal 'unknown'
"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .env_utils import read_env_key

BRAND_COLOR_DEFAULT = '#CD1719'  # razzfazz red


def stack_version(stack_root: str | os.PathLike | None = None) -> str:
    """Return the current stack version string.

    `stack_root` defaults to `$STACK_ROOT` or `/stack` (the bind-mount used
    by every UI container). Caller can override for tests / non-container
    invocations.
    """
    env_version = os.environ.get('RAZZFAZZ_VERSION')
    if env_version:
        return env_version.strip()

    root = Path(stack_root or os.environ.get('STACK_ROOT') or '/stack')

    env_file = root / '.env'
    if env_file.exists():
        v = read_env_key(env_file, 'RAZZFAZZ_VERSION')
        if v:
            return v

    version_file = root / 'VERSION'
    if version_file.exists():
        try:
            return version_file.read_text().strip() or 'unknown'
        except OSError:
            pass

    return 'unknown'


def register_context_processor(app: Flask, extras=None) -> None:
    """Inject `{razzfazz_version, stack_version, main_domain, brand_color}`
    into all templates, plus optional caller-supplied extras.

    `main_domain` is read from the env (set by docker compose from
    `.env`'s `MAIN_DOMAIN`). `brand_color` defaults to the razzfazz red
    but can be overridden via `$BRAND_COLOR`.

    `stack_version` is exposed alongside `razzfazz_version` (#148) — both
    map to the same value. Older config UI templates reference
    `stack_version`; new licenses/help/setup/backup templates reference
    `razzfazz_version`. Shipping both avoids forcing a stack-wide template
    rename in S07.

    `extras` (#148): optional callable returning a dict of extra template
    vars to merge in. Lets callers add nav-data / module counts / enabled
    profiles without registering a second context_processor (Flask merges
    multiple processors fine, but a single hook is simpler to reason
    about). Called once per request; raise to bail out (caller's exception
    propagates to Flask's error handler — consistent with bare
    `@app.context_processor`).
    """
    @app.context_processor
    def inject_stack_meta():
        v = stack_version(app.config.get('STACK_ROOT'))
        ctx = {
            'razzfazz_version': v,
            'stack_version': v,  # legacy template-key alias (#148)
            'main_domain': os.environ.get('MAIN_DOMAIN', ''),
            'brand_color': os.environ.get('BRAND_COLOR', BRAND_COLOR_DEFAULT),
        }
        if extras is not None:
            ctx.update(extras())
        return ctx
