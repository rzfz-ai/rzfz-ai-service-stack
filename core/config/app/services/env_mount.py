# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1189 — stale single-file `.env` bind detection for the Portal.

#1224: the implementation moved to `razzfazz_common.env_mount` because the
backup-management container mounts the same two files the same way and needed
the same treatment. This module keeps the Portal's import path and the
Portal's defaults (`RECREATE_CMD` names razzfazz-config); see the shared
module for the mechanism.
"""
from razzfazz_common import env_mount as _shared
from razzfazz_common.env_mount import (  # noqa: F401
    DEFAULT_SERVICE, ENV_FILES, RECREATE_CMD, SERVICE_LABELS, _STALE_ERRNOS,
    StaleEnvMountError, _explain, env_write_guard, explain_stale,
    explain_write_failure, inspect_env_file, recreate_cmd,
)


def inspect_env_mounts(stack_root: str, service: str = DEFAULT_SERVICE) -> dict:
    """Both env files, probed through THIS module's `inspect_env_file` name —
    so a caller (or test) that swaps the probe here is honoured."""
    return _shared.inspect_env_mounts(stack_root, service, inspect_file=inspect_env_file)
