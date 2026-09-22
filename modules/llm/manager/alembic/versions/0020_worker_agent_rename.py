# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1059 P1.3 — migrate the STORED worker identity across the HARD rename.

Revision ID: 0020_worker_agent_rename
Revises: 0019_deployment_display_name

The agent's service/container/DNS name moved from ``llm-node-agent`` to
``llm-worker-agent`` in one step, with no back-compat alias (spec 2026-08-31,
binding decision 1). The manager does not merely *display* that name — it
STORES it:

* ``workers.address`` is what the manager dials. On a co-located worker it is
  the agent's container name (``LLM_WORKER_ADDRESS`` defaults to it), so a row
  left saying ``llm-node-agent`` points at a container that no longer exists.
* ``workers.labels->>'advertise_addr'`` (#262) is the host-routable address the
  master uses to reach that worker's engines; on a same-box setup it carries
  the same spelling.

Worker *names* are operator-chosen (``LLM_WORKER_NAME`` / the enrolment token,
#285) and are NOT touched — renaming those would break the per-worker command
key, which is an HMAC over the name.

THIS MIGRATION IS THE ENTIRE "UPGRADE WINDOW" in which the old identity is
accepted anywhere in the manager. There is deliberately NO alias table, no
fallback lookup and no dual-name acceptance in the request path: after this
runs, the string ``llm-node-agent`` appears nowhere in the manager but here.
`tests/unit/llm-manager/test_1059_worker_identity_migration.py` asserts exactly
that, so a "temporary" alias cannot quietly become permanent.

The helpers below are pure so that behaviour can be tested without a database;
`upgrade()` performs the same rewrite in SQL so it is a single statement per
column rather than a row-by-row read-modify-write.
"""
from __future__ import annotations

from typing import Optional

from alembic import op

revision = "0020_worker_agent_rename"
down_revision = "0019_deployment_display_name"
branch_labels = None
depends_on = None

OLD_AGENT_IDENTITY = "llm-node-agent"
NEW_AGENT_IDENTITY = "llm-worker-agent"


def migrated_address(address: Optional[str]) -> Optional[str]:
    """Rewrite a stored address across the rename.

    Matches the bare name and the ``name:port`` form, and ONLY at the start —
    a worker deliberately named e.g. ``lab-llm-node-agent-2`` by an operator is
    not this agent's identity and is left alone.
    """
    if not address:
        return address
    if address == OLD_AGENT_IDENTITY:
        return NEW_AGENT_IDENTITY
    if address.startswith(OLD_AGENT_IDENTITY + ":"):
        return NEW_AGENT_IDENTITY + address[len(OLD_AGENT_IDENTITY):]
    return address


def migrated_labels(labels: Optional[dict]) -> Optional[dict]:
    """Same rewrite for the one label that can carry the agent's own name."""
    if not labels or "advertise_addr" not in labels:
        return labels
    current = labels.get("advertise_addr")
    if not isinstance(current, str):
        return labels
    migrated = migrated_address(current)
    if migrated == current:
        return labels
    out = dict(labels)
    out["advertise_addr"] = migrated
    return out


def _rewrite(old: str, new: str) -> None:
    op.execute(
        f"""
        UPDATE workers
           SET address = '{new}' || substring(address from {len(old) + 1})
         WHERE address = '{old}' OR address LIKE '{old}:%'
        """
    )
    op.execute(
        f"""
        UPDATE workers
           SET labels = jsonb_set(
                   labels, '{{advertise_addr}}',
                   to_jsonb('{new}' || substring(labels->>'advertise_addr'
                                                 from {len(old) + 1})))
         WHERE labels->>'advertise_addr' = '{old}'
            OR labels->>'advertise_addr' LIKE '{old}:%'
        """
    )


def upgrade() -> None:
    _rewrite(OLD_AGENT_IDENTITY, NEW_AGENT_IDENTITY)


def downgrade() -> None:
    _rewrite(NEW_AGENT_IDENTITY, OLD_AGENT_IDENTITY)
