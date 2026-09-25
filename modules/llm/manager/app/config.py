# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Runtime configuration for the LLM Manager (manager).

All settings are read from the environment LAZILY (``get_settings()`` reads
``os.environ`` on each call). Nothing here touches the network, the DB, the
filesystem, or a logger at import time — the module must import cleanly
off-box (import-side-effect guard: tests/unit/llm-manager).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Sensible in-stack defaults (Docker DNS names). Overridden by .env in prod.
DEFAULT_DATABASE_URL = "postgresql+psycopg2://llm_manager_user:@postgres:5432/llm_manager_db"
DEFAULT_VALKEY_URL = "redis://valkey:6379/9"
DEFAULT_LITELLM_BASE_URL = "http://llm-manager-router:4000"
# #262 Task 4: the manager API app's own reachable base, used to compose a
# REMOTE worker's relay-routed instance endpoints
# (f"{relay_internal_base}/relay/{worker_id}/v1" — see router_config.py). This
# is an INTERNAL, in-stack address (the router dials it container-to-container
# on the default network), unrelated to `advertise_url` (#262 enrollment,
# handed to a JOINING worker so it knows the master's external address).
DEFAULT_RELAY_INTERNAL_BASE = "http://llm-manager:8080"
# #928: how long the WS inference relay waits for the NEXT frame of a relayed
# response (the head, or the next chunk) before giving up on that ONE request —
# an IDLE-between-frames deadline, deliberately NOT a total request deadline. A
# legitimate long completion streams tokens continuously, so it never idles;
# what this bounds is a WEDGED upstream on a LIVE socket (engine accepted the
# request and emitted nothing, transport pings keep succeeding), the one failure
# mode neither the WS-drop path nor the client-disconnect path recovers from.
# Without it such a request pins a relay slot — and one of #19's box-wide
# in-flight completion slots — until the process restarts.
#
# 300s is deliberately generous: it must comfortably cover a cold engine's
# time-to-first-token (weights paging in, a long-context prefill) on the slowest
# box we ship, since a false positive there 502s a request that would have
# succeeded. Non-positive disables the bound entirely (pre-#928 behaviour) for an
# operator who would rather hang than drop.
DEFAULT_RELAY_IDLE_TIMEOUT_S = 300.0

#: #1955 — seconds the proxy may hold a request whose TCP connect to the router
#: was refused. Sized from the measured outage on 0.91 (11.1 s, 2026-09-12) plus
#: room for a slower cold start; 0.0 = disabled.
DEFAULT_ROUTER_RECONNECT_SECONDS = 20.0
# Client-facing API-key prefix. A key looks like ``rzfz-sk-<random>``.
DEFAULT_KEY_PREFIX = "rzfz-sk-"

# Billing-meter behaviour on the proxy hot path when the usage store is
# unreachable — an operator CAP choice (LLM_MANAGER_METERING_MODE):
#   "available" (default): never block a completion on the meter — serve and
#       record best-effort (usage flagged ``estimated`` on write failure).
#       Availability-first; may under-count during a meter outage.
#   "strict": refuse (503) to serve an unmetered request when the usage store
#       is unreachable. Consistency-first; sacrifices availability during a
#       meter outage. Guarantees no un-billed tokens.
DEFAULT_METERING_MODE = "available"
_METERING_MODES = ("strict", "available")


def _normalize_metering_mode(raw: str | None) -> str:
    """Map the env value onto a known mode; unknown/empty → the safe default
    (``available``: don't take LLM serving down over a billing-store blip)."""
    v = (raw or "").strip().lower()
    return v if v in _METERING_MODES else DEFAULT_METERING_MODE


_ENTITLEMENT_MODES = ("report", "enforce")


def _normalize_entitlement_mode(raw: str | None) -> str:
    """Map the env value onto a known entitlement mode; unknown/empty → the safe
    default (``report``: never block — community / dev boxes must keep serving)."""
    v = (raw or "").strip().lower()
    return v if v in _ENTITLEMENT_MODES else "report"


def _normalize_seconds_or_off(raw: str | None, default: float) -> float:
    """A seconds knob where non-positive means OFF, in one place.

    `_normalize_relay_idle_timeout` had exactly these semantics hard-wired to
    one default. #1955 needed the same rule for a second knob, and a second copy
    of "unset/empty/unparseable -> default, non-positive -> a single 0.0" is how
    two knobs start disagreeing about what "off" means. Never raises: a typo in
    a knob must not stop the manager from starting.
    """
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else 0.0


def _normalize_relay_idle_timeout(raw: str | None) -> float:
    """Seconds for the #928 relay idle timeout; ``0.0`` means DISABLED.

    Unset, empty (the ``VAR: "${VAR:-}"`` compose shape — see ``_env_or_default``
    and the ``LLM_MANAGER_VRAM_HEADROOM`` note in the manager's compose file) or
    unparseable → the default. A non-positive value is normalised to a single
    ``0.0`` so consumers test one thing (``> 0``) rather than each inventing
    their own idea of "off". Never raises: a typo in this knob must not stop the
    manager from starting.
    """
    return _normalize_seconds_or_off(raw, DEFAULT_RELAY_IDLE_TIMEOUT_S)


# #1518 (E5): `_parse_bool_flag` stood here. Its one caller was the #1182
# `vllm_enabled` knob, which is gone with the engine it hid — a boolean parser
# with no knob to parse is the kind of helper that quietly acquires a second,
# less careful caller. The next opt-in knob brings it back with its own test.


def _env_or_default(key: str, default: str) -> str:
    """Like ``os.environ.get(key, default)`` but ALSO falls back to
    ``default`` when the variable is SET to the EMPTY STRING (#851) — the
    shape a plain compose ``"${VAR:-}"`` forward produces whenever the
    operator has not overridden it in ``.env``. Plain
    ``os.environ.get(key, default)`` only falls back to ``default`` when the
    variable is UNSET, so forwarding one of the RBAC group-list vars below
    that way would silently collapse a fail-closed default to an empty tuple
    the moment compose started setting it at all — exactly the #842/#843
    three-tier RBAC regression this function exists to prevent.
    """
    return os.environ.get(key) or default


@dataclass(frozen=True)
class Settings:
    database_url: str
    valkey_url: str
    litellm_base_url: str
    litellm_internal_key: str
    relay_internal_base: str
    # #928: idle-between-frames deadline for ONE relayed request, in seconds.
    # 0.0 = disabled (unbounded). See DEFAULT_RELAY_IDLE_TIMEOUT_S above.
    relay_idle_timeout_s: float
    key_prefix: str
    log_file: str | None
    cache_ttl_seconds: int
    usage_flush_interval_ms: int
    router_config_path: str
    valkey_host: str
    valkey_port: int
    # Admin authz for the management API (/api/*, /ui/*): trust the Authentik
    # forward-auth identity Caddy injects post-OIDC, gated on an admin group.
    admin_user_header: str
    admin_groups_header: str
    admin_groups: tuple[str, ...]
    # #314 three-tier RBAC. `admin_groups` above is DELIBERATELY reused as the
    # SUPER-ADMIN tier (the issue text explicitly allows this: "or reuse the
    # existing Super Admins group for tier 1") — every existing consumer of
    # `require_admin`/`is_admin` keeps its exact current behaviour untouched.
    # These two are NEW, narrower tiers layered underneath it via
    # `app.authz.require_role`; empty by default until the Authentik
    # blueprint (issue work-item 3, out of scope here) provisions the groups.
    llm_admin_groups: tuple[str, ...]
    llm_user_groups: tuple[str, ...]
    # Billing-meter CAP behaviour on the hot path: "available" | "strict".
    metering_mode: str
    # Shared secret a fleet node presents (Bearer) to register itself via
    # POST /api/workers. Empty → node registration is DISABLED (fail-closed:
    # 503), never open. Distinct from LITELLM_INTERNAL_KEY (manager↔router) so
    # a node can't also act as the router master.
    node_key: str
    # Optional HMAC key for signing the monthly entitlement rollup (P2-E2).
    # Empty → the rollup is returned unsigned (signed=false). Local
    # usage_events remain the billing source-of-truth either way.
    rollup_key: str
    # Manager's own reachable URL, handed to a joining worker so it knows where
    # to POST /api/workers (#262 enrollment). Empty → the enroll response omits
    # it and the operator fills <manager-url> into the join command by hand.
    advertise_url: str
    # Local subscription entitlement (#265). "report" (default) computes +
    # exposes the entitlement state but NEVER blocks — the posture for community
    # / dev / unlicensed boxes. "enforce" gates the hot path: an expired /
    # inactive / absent subscription → 402. Entirely LOCAL — read from the
    # subscriptions table (provisioned out-of-band), no request-time broker call.
    entitlement_mode: str
    # Which subscription is THIS box's (its subscription_number). Empty → the
    # single local Subscription row is used (the common one-box case).
    subscription_number: str
    # Command-channel credential policy (#207). "allow" (default): a node may
    # present the shared node key OR its per-worker key for claim/result.
    # "enforce": per-worker keys ONLY — the bare shared key is refused, so no
    # node can claim/complete another worker's commands.
    command_key_mode: str
    # Worker admission policy (#419 P0 Task 4). "auto" (default): a node that
    # completes enrollment is immediately schedulable — the behaviour every
    # existing box already has. "manual": a newly seen worker lands in `pending`
    # and an admin must approve it before it can take traffic.
    #
    # Deliberately DEFAULT-OFF and modelled on command_key_mode (#207/#285),
    # for the same reason: flipping admission semantics under a running fleet is
    # a flag day. Enrolling proves possession of a short-lived token, not that an
    # operator meant to give that box production traffic, so security-sensitive
    # installs set "manual"; nobody is forced through a queue on upgrade.
    worker_approval_mode: str
    # #19 "option C": a single box-wide in-flight completion cap so ALL
    # consumers (OWUI, Dify, the agents, Cognee, …) are throttled together,
    # not just Dify's own max_active_requests (#30). 0/empty disables the gate
    # (pre-#19 behaviour: unbounded). Default of 24 is a conservative starting
    # point for a single-GPU box serving one model at a time — high enough
    # that normal multi-user chat traffic never trips it, low enough that a
    # pile-up gets a fast 429 instead of queueing behind a wedged GPU.
    # Operators with multiple GPUs / workers behind this manager raise it via
    # the env var.
    max_concurrent_requests: int
    # OTLP endpoint for the observability module (OpenLIT + ClickHouse via the
    # otel-collector). Empty (default) → observability OFF: the router config is
    # generated WITHOUT the OTel callback, so nothing is emitted and there are no
    # export errors. Set (e.g. http://otel-collector:4318) → the router gets a
    # `callbacks:["otel"]` and LiteLLM exports a span per request to the
    # collector, which writes the OpenLIT ClickHouse tables. Wired only when the
    # `observability` profile is enabled.
    otel_endpoint: str
    # #1955 — how long the proxy may HOLD a request whose TCP connect to the
    # router was refused, instead of answering 502 straight away.
    #
    # Measured on 0.91 (2026-09-12): a router reload leaves the port shut for
    # about eleven seconds — LiteLLM drains gracefully first (81 s there, the
    # old process still serving), and the hole is between the old process
    # letting go of :4000 and the new one taking it. Three of thirty-seven
    # in-flight requests died in that hole, all with the same shape:
    #
    #     502 {"error":{"message":"router unreachable: All connection
    #                              attempts failed","type":"orchestrator"}}
    #
    # `All connection attempts failed` is httpx's ConnectError: the handshake
    # never completed, so the request was never delivered. That is what makes
    # holding safe — there is nothing to duplicate.
    #
    # 0 disables the hold and restores the immediate 502.
    router_reconnect_seconds: float


def get_settings() -> Settings:
    """Build a Settings snapshot from the current environment.

    Deliberately un-cached: tests flip env vars between cases, and the read
    is cheap. Callers that want a stable snapshot should hold the returned
    object.
    """
    return Settings(
        database_url=os.environ.get("LLM_MANAGER_DATABASE_URL", DEFAULT_DATABASE_URL),
        valkey_url=os.environ.get("LLM_MANAGER_VALKEY_URL", DEFAULT_VALKEY_URL),
        litellm_base_url=os.environ.get(
            "LLM_MANAGER_LITELLM_BASE_URL", DEFAULT_LITELLM_BASE_URL
        ),
        litellm_internal_key=os.environ.get("LITELLM_INTERNAL_KEY", ""),
        relay_internal_base=os.environ.get(
            "RELAY_INTERNAL_BASE", DEFAULT_RELAY_INTERNAL_BASE
        ),
        relay_idle_timeout_s=_normalize_relay_idle_timeout(
            os.environ.get("LLM_MANAGER_RELAY_IDLE_TIMEOUT_S")
        ),
        key_prefix=os.environ.get("LLM_MANAGER_KEY_PREFIX", DEFAULT_KEY_PREFIX),
        log_file=os.environ.get("LLM_MANAGER_LOG_FILE") or None,
        cache_ttl_seconds=int(os.environ.get("LLM_MANAGER_CACHE_TTL", "300")),
        usage_flush_interval_ms=int(os.environ.get("LLM_MANAGER_USAGE_FLUSH_MS", "500")),
        router_config_path=os.environ.get(
            "LLM_MANAGER_ROUTER_CONFIG_PATH", "/config/router-config.yaml"
        ),
        valkey_host=os.environ.get("LLM_MANAGER_VALKEY_HOST", "valkey"),
        valkey_port=int(os.environ.get("LLM_MANAGER_VALKEY_PORT", "6379")),
        admin_user_header=os.environ.get("LLM_MANAGER_ADMIN_USER_HEADER", "X-Authentik-Username"),
        admin_groups_header=os.environ.get("LLM_MANAGER_ADMIN_GROUPS_HEADER", "X-Authentik-Groups"),
        # #851: see `_env_or_default` above — empty-string-safe so a compose
        # forward of these RBAC group-list vars cannot silently zero out a
        # tier's group list and collapse the #842/#843 RBAC.
        admin_groups=tuple(
            g.strip()
            for g in _env_or_default(
                "LLM_MANAGER_ADMIN_GROUPS", "razzfazz.ai Super Admins,authentik Admins"
            ).split(",")
            if g.strip()
        ),
        llm_admin_groups=tuple(
            g.strip()
            for g in _env_or_default(
                "LLM_MANAGER_ADMIN_TIER_GROUPS", "razzfazz.ai LLM Admins"
            ).split(",")
            if g.strip()
        ),
        llm_user_groups=tuple(
            g.strip()
            for g in _env_or_default(
                "LLM_MANAGER_USER_TIER_GROUPS", "razzfazz.ai LLM Users"
            ).split(",")
            if g.strip()
        ),
        metering_mode=_normalize_metering_mode(
            os.environ.get("LLM_MANAGER_METERING_MODE", DEFAULT_METERING_MODE)
        ),
        node_key=os.environ.get("LLM_MANAGER_NODE_KEY", ""),
        rollup_key=os.environ.get("LLM_MANAGER_ROLLUP_KEY", ""),
        advertise_url=os.environ.get("LLM_MANAGER_ADVERTISE_URL", "").rstrip("/"),
        entitlement_mode=_normalize_entitlement_mode(
            os.environ.get("LLM_MANAGER_ENTITLEMENT_MODE", "report")
        ),
        subscription_number=os.environ.get("LLM_MANAGER_SUBSCRIPTION_NUMBER", "").strip(),
        command_key_mode=(
            "enforce"
            if os.environ.get("LLM_MANAGER_COMMAND_KEY_MODE", "allow").strip().lower() == "enforce"
            else "allow"
        ),
        worker_approval_mode=(
            "manual"
            if os.environ.get("LLM_MANAGER_WORKER_APPROVAL_MODE", "auto").strip().lower() == "manual"
            else "auto"
        ),
        max_concurrent_requests=int(
            os.environ.get("LLM_MANAGER_MAX_CONCURRENT_REQUESTS", "24")
        ),
        otel_endpoint=os.environ.get("LLM_MANAGER_OTEL_ENDPOINT", "").strip().rstrip("/"),
        router_reconnect_seconds=_normalize_seconds_or_off(
            os.environ.get("LLM_MANAGER_ROUTER_RECONNECT_SECONDS"),
            DEFAULT_ROUTER_RECONNECT_SECONDS,
        ),
    )
