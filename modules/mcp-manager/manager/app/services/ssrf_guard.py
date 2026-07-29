# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""SSRF hardening for the MCP manager's outbound URLs (#67).

The OAuth authorization-code flow fetches provider endpoints
(``auth_url`` / ``token_url`` from core/mcp/oauth-providers.yaml) and the proxy
routing points Caddy at per-integration upstream targets. A crafted or
misconfigured provider/upstream URL could otherwise steer the server at an
INTERNAL host (``postgres``, ``authentik-server``, the cloud metadata endpoint
``169.254.169.254``, any RFC1918 address, ``localhost`` …) — a classic SSRF.

This module enforces two invariants on every such URL, fail-CLOSED:

  1. **https-only** — plain ``http://`` (and every non-https scheme) is rejected.
     The OAuth token endpoint carries the client secret + the authorization
     code; it must never be sent in clear.
  2. **host allowlist** — the host must be one the operator legitimately
     configured. The allowlist is DERIVED from the configured providers (their
     ``auth_url`` / ``token_url`` hosts), never hardcoded, so operator-added
     providers work without a code change while an internal/attacker host does
     not.

Additionally, even if an allowlisted name were made to resolve to an internal
address, any URL whose host is an IP LITERAL in a private / loopback /
link-local / reserved range is rejected outright (defense in depth against an
allowlisted-but-poisoned entry and against IP-literal upstreams). The internal-
IP check is ENCODING-AWARE: decimal (2130706433), octal (0177.0.0.1), hex
(0x7f.0.0.1), and shortened (127.1) IPv4 forms — which ``ipaddress.ip_address``
does not parse but the OS resolver WOULD dial — are normalized and caught. On
the allowlist-less proxy-upstream path, a host in ANY numeric IP encoding is
refused outright (a legit upstream is a container/service name).

Errors are raised as :class:`SSRFError` with a clear, non-leaky message.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class SSRFError(ValueError):
    """Raised when a URL fails the https-only / host-allowlist SSRF checks."""


# A host that is entirely digits/dots or carries a hex prefix / leading-zero
# octet is an IP in a NON-STANDARD encoding (decimal 2130706433, octal
# 0177.0.0.1 / 010.0.0.5, hex 0x7f.0.0.1, shortened 127.1). ipaddress.ip_address
# does NOT parse these, so they'd slip past the plain internal-IP check. A
# legitimate public host is a dotted alphabetic FQDN, never one of these shapes.
_ALL_NUMERIC_HOST = re.compile(r"^[0-9.]+$")
_HEX_OR_LEADING_ZERO = re.compile(r"(?:^|\.)(?:0x[0-9a-f]+|0[0-9]+)(?:\.|$)", re.I)


def _host_of(url: str) -> str:
    """Return the lowercased hostname of `url` (no port), or '' if unparseable."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def derive_allowed_hosts(providers: dict) -> set[str]:
    """Build the outbound host allowlist from the configured OAuth providers.

    For every provider we take the host of its ``auth_url`` and ``token_url``.
    Result is a set of lowercased hostnames (e.g. ``github.com``,
    ``oauth2.googleapis.com``). Empty/malformed URLs contribute nothing.
    """
    hosts: set[str] = set()
    for spec in (providers or {}).values():
        if not isinstance(spec, dict):
            continue
        for key in ("auth_url", "token_url"):
            h = _host_of(str(spec.get(key, "")))
            if h:
                hosts.add(h)
    return hosts


def _internal_ip(ip: ipaddress._BaseAddress) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _is_internal_ip_literal(host: str) -> bool:
    """True if `host` is an IP literal in a private/loopback/link-local/reserved
    range (the addresses an SSRF wants to reach) — INCLUDING non-standard
    encodings that ``ipaddress.ip_address`` won't parse: decimal (2130706433),
    octal (0177.0.0.1 / 010.0.0.5), hex (0x7f.0.0.1), and shortened (127.1)
    forms, all of which the OS resolver / a downstream client WOULD dial as the
    internal address. A name-based host (dotted alphabetic FQDN) → False; those
    are gated by the allowlist instead.
    """
    # 1. Standard textual IPv4/IPv6 literal.
    try:
        return _internal_ip(ipaddress.ip_address(host))
    except ValueError:
        pass

    # 2. Non-standard numeric encodings. Only consider hosts that CANNOT be a
    #    legitimate public FQDN: all-numeric/dot, or carrying a hex-prefixed /
    #    leading-zero octet. socket.inet_aton accepts decimal/octal/hex/shortened
    #    dotted forms and yields the packed address the resolver would use.
    if _is_numeric_shaped_host(host):
        try:
            packed = socket.inet_aton(host)
        except OSError:
            # Numeric-shaped but not a valid IPv4 encoding → still never a
            # legitimate public host; treat as internal/refused (fail closed).
            return True
        return _internal_ip(ipaddress.ip_address(socket.inet_ntoa(packed)))

    return False


def _is_numeric_shaped_host(host: str) -> bool:
    """True if `host` is an IP in ANY numeric encoding (standard textual, decimal
    2130706433, octal 0177.0.0.1 / 010.0.0.5, hex 0x7f.0.0.1, shortened 127.1) —
    i.e. NEVER a legitimate name-based host. Used to hard-reject numeric-shaped
    upstreams outright (the upstream path has no allowlist backstop, so a numeric
    encoding that decodes to a *public* address must still be refused rather than
    silently dialed)."""
    if _ALL_NUMERIC_HOST.match(host) or _HEX_OR_LEADING_ZERO.search(host):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# Internal service names / metadata endpoints an outbound URL or a proxy
# upstream must never resolve to: the shared stack infra + the cloud metadata
# endpoints. Rejected UNCONDITIONALLY (regardless of allowlist) — a legitimate
# public OAuth endpoint is always a dotted public FQDN, never one of these.
_FORBIDDEN_UPSTREAM_HOSTS = {
    "postgres", "authentik-server", "authentik-worker", "valkey", "caddy",
    "docker-socket-proxy", "localhost",
    "169.254.169.254", "metadata.google.internal",
}


def assert_safe_url(url: str, allowed_hosts: set[str], *, what: str = "URL") -> str:
    """Validate `url` for the outbound-fetch SSRF invariants; return it if safe.

    Raises :class:`SSRFError` when `url`:
      * is empty / unparseable,
      * is not ``https``,
      * has an IP-literal host in an internal range, or
      * has a host not in `allowed_hosts`.

    `allowed_hosts` is the operator-derived allowlist (see
    :func:`derive_allowed_hosts`). `what` names the field for the error message.
    """
    if not url or not isinstance(url, str):
        raise SSRFError(f"{what} is empty")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SSRFError(
            f"{what} must be https (got {parts.scheme or 'no'}-scheme URL); "
            f"non-https OAuth/upstream targets are refused")
    host = (parts.hostname or "").lower()
    if not host:
        raise SSRFError(f"{what} has no host")
    if _is_internal_ip_literal(host):
        raise SSRFError(
            f"{what} points at an internal/reserved IP ({host}) — refused (SSRF)")
    # Hard-reject internal infra service names + bare single-label hosts
    # UNCONDITIONALLY (even if the allowlist somehow carried them, e.g. a
    # self-referential/misconfigured provider defining its OWN url as the
    # internal target): a legitimate public OAuth endpoint is always a dotted
    # FQDN, never `postgres` / `authentik-server` / `localhost`.
    if host in _FORBIDDEN_UPSTREAM_HOSTS:
        raise SSRFError(
            f"{what} host {host!r} is a protected internal service — refused (SSRF)")
    if "." not in host:
        raise SSRFError(
            f"{what} host {host!r} is a bare single-label name (internal Docker "
            f"service shape) — refused (SSRF)")
    if host not in allowed_hosts:
        raise SSRFError(
            f"{what} host {host!r} is not in the configured provider allowlist "
            f"— refused (SSRF). Allowed: {sorted(allowed_hosts)}")
    return url


def assert_safe_upstream(host: str) -> str:
    """Validate a proxy UPSTREAM host (the container/service Caddy dials) (#67).

    The manager itself doesn't fetch the upstream, but the route it installs
    steers Caddy at ``host:port``. Reject an upstream that is an internal
    IP-literal (private/loopback/link-local/metadata) or one of the shared-infra
    service names — so a crafted/misconfigured integration can't turn a per-user
    proxy route into a path to ``postgres`` / ``authentik-server`` / the metadata
    endpoint. Returns `host` when safe; raises :class:`SSRFError` otherwise."""
    h = (host or "").strip().lower()
    if not h:
        raise SSRFError("upstream host is empty")
    if "://" in h or "/" in h:
        raise SSRFError(f"upstream host {host!r} must be a bare host, not a URL")
    if h in _FORBIDDEN_UPSTREAM_HOSTS:
        raise SSRFError(f"upstream host {h!r} is a protected internal service — refused (SSRF)")
    # A legit upstream is a Docker container/service name (alphabetic), never an
    # IP in ANY encoding. Reject numeric-shaped hosts outright — including
    # decimal/octal/hex/shortened forms that decode to a public address — since
    # the upstream path has no allowlist to catch a "public-looking" numeric.
    if _is_numeric_shaped_host(h):
        raise SSRFError(
            f"upstream host {h!r} is an IP literal / numeric encoding — refused "
            f"(SSRF); upstreams must be container/service names")
    return host
