# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Microsoft 365 write, send and delete calls in the stack's audit trail (#3433).

The gateway's own rows go to mcp_audit_log (the database). Wazuh reads the
stack's audit trails from JSON Lines files written by AuditLogger
(razzfazz_common/audit_log.py; scripts/install-wazuh-agent.sh AUDIT_SOURCES,
rules in modules/apps/wazuh/.../rzfz_audit_rules.xml), so every Microsoft 365
call that is not a read is ALSO written here, to MCP_AUDIT_LOG_PATH (default
/var/log/razzfazz/mcp-audit.log on the mcp-manager-audit volume).

One entry: event `mcp.tool_call`, category `mcp`, target `m365`, user = the
person's sub (or the technical user), action = the tool, risk = caution for a
write and danger for a send or delete, outcome = success when it was relayed,
confirmed or is waiting for the person's confirmation, failure when it was
refused (so an agent's refused sends count toward Wazuh's burst rule), and
`context` = caller class, tool class, decision and m365_guard.audit_fields
(object ids, recipient count and domains). Never a subject, a message body,
a file's content or an address.

A failed write here is logged and never blocks the call: the database row,
written first, is the gate (a call that cannot be audited there is not
relayed)."""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/var/log/razzfazz/mcp-audit.log"
_LOCK = threading.Lock()
_LOGGER = None
_RISK = {"write": "caution", "send": "danger", "delete": "danger"}
_SUCCESS = {"relayed", "allowed", "confirmed", "confirmation_given", "confirmation_required"}


def _audit_logger():
    global _LOGGER
    with _LOCK:
        if _LOGGER is None:
            from razzfazz_common.audit_log import AuditLogger
            _LOGGER = AuditLogger(os.environ.get("MCP_AUDIT_LOG_PATH") or DEFAULT_PATH)
        return _LOGGER


def open_at_start() -> bool:
    """Create the audit file at start (never raises)."""
    try:
        _audit_logger()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("m365 audit: the audit file %s cannot be opened; Microsoft 365 writes are "
                       "audited in the database only", os.environ.get("MCP_AUDIT_LOG_PATH") or DEFAULT_PATH)
        return False


def entry(principal: str, tool: str, tool_class: str, who: str, decision: str, fields: dict) -> dict:
    """The AuditLogger keyword arguments of one entry (pure, for tests)."""
    ctx = {"caller_class": who, "tool_class": tool_class, "decision": decision}
    for k, v in (fields or {}).items():
        if k in ("target_ids", "recipient_count", "recipient_domains", "batch_methods",
                 "consumer", "owui_user_id"):
            ctx[k] = v
    return dict(user=principal or "unknown", category="mcp", action=str(tool)[:128], target="m365",
                risk=_RISK.get(tool_class, "caution"),
                outcome="success" if decision in _SUCCESS else "failure",
                detail=decision, context=ctx)


def write(principal, tool, tool_class, who, decision, fields) -> bool:
    try:
        _audit_logger().log("mcp.tool_call", **entry(principal, tool, tool_class, who, decision, fields))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("m365 audit: the audit file could not be written")
        return False
