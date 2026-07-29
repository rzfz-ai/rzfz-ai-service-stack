# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Shared `.env` parsing helpers — thin shim over razzfazz_common.env_utils.

M026 S05 #5: this module used to carry its own implementation of the same
parser that razzfazz-upgrade.sh's `read_env_value` uses. The canonical
implementation now lives in `razzfazz_common.env_utils` (M026 S03), so
this file just re-exports those symbols.

The legacy local name `parse_env_file` is preserved as an alias for
`read_env_file` so the 5 in-repo callers don't have to change in this
slice (apply_manager, image_checker, config_manager, profile_manager,
api blueprint). A follow-up cleanup can rename the call sites and drop
the alias.
"""

from razzfazz_common.env_utils import (
    parse_env_value,
    read_env_file,
    read_env_key,
    write_env_value,
)

# Legacy local name — kept for backward compatibility with existing call
# sites in this container. razzfazz_common renamed it to `read_env_file`
# to match the read/write verb pairing of the public API.
parse_env_file = read_env_file

__all__ = [
    'parse_env_value',
    'parse_env_file',
    'read_env_file',
    'read_env_key',
    'write_env_value',
]
