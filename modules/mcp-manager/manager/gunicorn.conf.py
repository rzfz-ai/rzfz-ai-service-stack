# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""gunicorn settings for mcp-manager (#2472; B4 decision record §3(g)).

The MCP gateway relays streamable-http: one tool call or SSE stream can hold
a response open for minutes. The default sync worker serves ONE request at a
time, so a single held call blocked every other gateway caller and the
portal. The gthread worker serves each request on its own thread.

ONE worker process, many threads — deliberately not `-w 2`:
  * `create_app()` runs the schema migration at start; two worker processes
    would run the same DDL concurrently.
  * The in-process state is per process: the gateway registry, the Authentik
    directory, the identity re-key throttle (`identity_migration`) and the
    OAuth refresh locks (`gateway_credentials`).
  * The OAuth flow itself holds no server-side state: its `state` is
    HMAC-signed and bound to the user (`services/oauth.py`), so a flow started
    on one request completes on any other
    (tests/unit/mcp-manager/test_2472_oauth_flow_survives_another_request.py).
  The thread count is DERIVED (#2580): enough for every bounded waiter at
  once — the gateway's relay slots, the Open WebUI session checks, the
  agents' directory checks — plus RESERVE_THREADS for /healthz, the portal
  and /api/tls/ask (app/services/thread_budget.py). 88 with the shipped
  caps; it was a fixed 64 (the decision record's `-w 2 --threads 32`), which
  48 relays + 16 Open WebUI checks alone filled.

Timeout: with gthread the worker's heartbeat does not wait for a request, so
`timeout` does not cut a running call; it is still set from the longest
per-server timeout in the gateway registry, plus a margin, so a legitimate
long tool call is never the reason a worker is recycled."""

bind = "0.0.0.0:5000"
worker_class = "gthread"
workers = 1
graceful_timeout = 30


def _timeout() -> int:
    try:
        from app.services.gateway_registry import load_gateway_servers, longest_timeout
        return max(120, longest_timeout(load_gateway_servers()) + 30)
    except Exception:  # noqa: BLE001 — never keep the manager from starting
        return 330  # DEFAULT_TIMEOUT + the margin


timeout = _timeout()


def _threads() -> int:
    try:
        from app.services.gateway_registry import load_gateway_limits
        from app.services.thread_budget import worker_threads
        return worker_threads(load_gateway_limits().total)
    except Exception:  # noqa: BLE001 — never keep the manager from starting
        return 88  # worker_threads(GatewayLimits().total); pinned by test_2580_thread_budget.py


threads = _threads()
