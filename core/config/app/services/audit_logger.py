# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Audit logger — now provided by the shared library (M033 B2).

Kept as a thin re-export so existing `from app.services.audit_logger import
AuditLogger` call sites in razzfazz-config keep working unchanged.
"""
from razzfazz_common.audit_log import AuditLogger  # noqa: F401
