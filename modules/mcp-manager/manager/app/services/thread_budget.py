# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The worker's thread budget (#2580, B9f ruling).

mcp-manager runs ONE gthread worker (gunicorn.conf.py). Every bounded thing
that can hold a worker thread while it waits on someone else has a cap, and
the worker gets enough threads for ALL of them at once plus a reserve:

    threads = gateway total          relay slots (gateway_limits.total, 48)
            + OWUI_CONCURRENT_CHECKS  Open WebUI session checks, taken BEFORE
                                      the relay slot (B8 fix round 5, 16)
            + AGENT_CHECKS_TOTAL      the agents' directory checks on the
                                      wildcard path, their own limiter (B9, 16)
            + RESERVE_THREADS         /healthz, the portal and /api/tls/ask (8)

Before this, 48 + 16 = 64 filled the whole worker: 48 held relays plus 16
hanging Open WebUI checks left /healthz waiting for the checks' timeout.

The caps live HERE, a stdlib-only module, so gunicorn.conf.py can import
them in the master process without Flask or razzfazz_common; the modules
that enforce them import them from here.
tests/unit/mcp-manager/test_2580_thread_budget.py fails when a cap is
raised (or re-declared elsewhere) without the thread count following."""
from __future__ import annotations

#: Open WebUI session checks running at once (gateway_identity).
OWUI_CONCURRENT_CHECKS = 16
#: The agents' directory checks (wildcard_proxy.check_limiter): per instance
#: owner, and in total.
AGENT_CHECKS_PER_OWNER = 4
AGENT_CHECKS_TOTAL = 16
#: Threads no cap can take: the budget of /healthz, the portal and
#: /api/tls/ask.
RESERVE_THREADS = 8


def worker_threads(gateway_total: int) -> int:
    """gunicorn's `threads` for a gateway relay cap of `gateway_total`."""
    return int(gateway_total) + OWUI_CONCURRENT_CHECKS + AGENT_CHECKS_TOTAL + RESERVE_THREADS
