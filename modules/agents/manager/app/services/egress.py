# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#959 W4 / #802 — default-deny egress policy for the agent fence networks.

Where this sits
---------------
#256 fenced every socket-less agent type off the flat stack bridge, closing the
LATERAL half of the boundary-test finding. The EXFIL half was split out into
#802 and deliberately left open: both fence nets (`coding-agents`,
`agent-assistants`) are `internal: false`, because a coding sandbox whose whole
purpose is "install the tool you need" must be able to reach git/npm/pip.

This module is the policy half of closing that. It is **opt-in**: the default
policy is `open`, which resolves to an empty env dict, so a box that does not
opt in is byte-identical to today. The operator flips
`AGENT_EGRESS_POLICY=allowlist` in `.env` and adds the
`modules/agents/compose.egress-allowlist.yml` overlay, which (a) flips both
fence nets to `internal: true` and (b) starts the `agent-egress-proxy`.

Why the env var is not the enforcement
--------------------------------------
`HTTP_PROXY` inside a hostile container is a hint, not a control — the agent can
unset it. The enforcement is the `internal: true` fence: with no route off the
bridge, unsetting the proxy env buys the agent nothing, because there is nothing
to bypass the proxy *with*. The env var only tells well-behaved clients where
the single door is. That is also why a client that ignores `NO_PROXY` (headless
Chromium being the known one, see the crawl4ai compose note) still works: the
in-fence service names are on the ACL as well as in `NO_PROXY`.

Corporate proxy
---------------
When `RAZZFAZZ_CORPORATE_PROXY=1`, #281 already injects the box's mandated proxy
+ CA into every agent container. Chaining our forward proxy in front of that
upstream is not something this sandbox can verify, so the policy is REFUSED
loudly (see `policy_conflict`) rather than emitting a config that half-works.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

POLICY_OPEN = 'open'
POLICY_ALLOWLIST = 'allowlist'

#: The forward proxy the fenced containers are pointed at. A SEPARATE Caddy
#: instance from the stack edge (same image, own config) — the stack Caddy must
#: never be dual-homed onto a sandbox bridge, or an agent could use it as a
#: confused deputy into Authentik-gated stack routes.
PROXY_SERVICE = 'agent-egress-proxy'
PROXY_PORT = 8197

#: Caddy's `allow {$VAR:default}` only applies the default when the var is
#: UNSET. A set-but-empty value expands to a bare `allow` and crashes Caddy at
#: startup — the same trap `.env.example` records for DIFY_DOC_TOOLS_SSRF_ALLOW.
#: A non-resolving sentinel is how you spell "fully closed".
CLOSED_SENTINEL = 'disabled.invalid'

#: Public destinations the bring-your-own-tool premise needs. #802 named the
#: floor: "npm/PyPI, die konfigurierten LLM-Endpunkte, die Gitea-Instanz".
_DEFAULT_PUBLIC = (
    'registry.npmjs.org',
    'pypi.org',
    'files.pythonhosted.org',
    'github.com',
    'codeload.github.com',
    'objects.githubusercontent.com',
    'raw.githubusercontent.com',
)

#: In-fence services, reached by container NAME. They are on the allowlist as
#: well as in NO_PROXY so a client that honours neither still reaches them —
#: the proxy can only resolve names on the nets it is actually attached to, and
#: Docker's inter-bridge isolation keeps the stack default net out of reach.
_DEFAULT_INTERNAL = (
    'agent-manager',
    'gitea',
    'gpustack',
    'ollama-proxy',
    'llm-manager',
    # #785 (#94 rest): `cognee`, not `cognee-mcp`.
    #
    # cognee itself is dual-homed on this fence (#36) precisely so sandbox tools
    # can reach cognee:8000 — but it was missing here, and this tuple feeds BOTH
    # the Caddy ACL and NO_PROXY. On an `allowlist`-policy box a proxy-honouring
    # sandbox therefore sent http://cognee:8000/... to the egress proxy, which
    # does not allow the name: the docker dual-homing was undone one layer up,
    # silently, and only under egress control. Its data paths stay auth-gated
    # (401 without credentials) — reachability is not authorisation.
    #
    # `cognee-mcp` came OFF for the opposite reason. That sidecar authenticates
    # as the cognee ADMIN superuser and exposes remember/recall/forget +
    # cognify_file with no per-request auth, so #36 BLOCKER-2 moved it to
    # cognee-backend only, off this fence, and turned off the registry entry
    # that wired it into agents. The entry here was therefore already dead —
    # and dead in the worst direction: an allow-list whose job is to say what a
    # sandbox MAY reach was naming the one container it must never reach. If
    # that sidecar is ever re-homed by accident, nothing here should be holding
    # the door open for it.
    'cognee',
    'searxng',
)

#: Never send these through the proxy. Bare names + loopback; the RFC1918 CIDRs
#: are best-effort (curl honours them, `requests` does not) and harmless where
#: unsupported because the same names are on the ACL.
_NO_PROXY = (
    ('localhost', '127.0.0.1', '::1')
    + _DEFAULT_INTERNAL
    + ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
)


def _get(env, key, default=''):
    return (env or {}).get(key, default) or ''


def resolve_policy(env=None) -> str:
    """Return the effective policy — `open` unless the operator asked for
    `allowlist` AND nothing conflicts with it.

    Unknown values resolve to `open` and are logged: a typo in `.env` must not
    silently fence a box, and must not silently un-fence one either (the only
    value that fences is the exact word)."""
    raw = _get(env, 'AGENT_EGRESS_POLICY').strip().lower()
    if raw and raw != POLICY_ALLOWLIST and raw != POLICY_OPEN:
        logger.warning(
            "AGENT_EGRESS_POLICY=%r is not one of %s/%s — falling back to %s",
            raw, POLICY_OPEN, POLICY_ALLOWLIST, POLICY_OPEN)
        return POLICY_OPEN
    if raw != POLICY_ALLOWLIST:
        return POLICY_OPEN
    if policy_conflict(env):
        return POLICY_OPEN
    return POLICY_ALLOWLIST


def policy_conflict(env=None) -> str | None:
    """Why the requested `allowlist` policy cannot be honoured, or None.

    Returns None when the policy was not requested at all — a conflict is only
    interesting for an operator who asked for something they did not get."""
    raw = _get(env, 'AGENT_EGRESS_POLICY').strip().lower()
    if raw != POLICY_ALLOWLIST:
        return None
    if _get(env, 'RAZZFAZZ_CORPORATE_PROXY').strip() == '1':
        return ("AGENT_EGRESS_POLICY=allowlist is refused while "
                "RAZZFAZZ_CORPORATE_PROXY=1: the corporate proxy (#281) is "
                "already the box's mandated egress path and chaining the agent "
                "egress proxy in front of it is unverified. Egress policy stays "
                "'open' for agent containers.")
    return None


def _normalise_host(token: str) -> str:
    """`https://PyPI.org:443/simple/` → `pypi.org`. The ACL matches hosts, so a
    pasted URL must not silently become a never-matching entry."""
    host = token.strip()
    if not host:
        return ''
    if '://' in host:
        host = host.split('://', 1)[1]
    host = host.split('/', 1)[0]
    if host.startswith('['):                       # bracketed IPv6 literal
        host = host.split(']', 1)[0].lstrip('[')
    elif host.count(':') == 1:
        host = host.split(':', 1)[0]
    return host.strip().lower()


def allowlist_hosts(env=None) -> list[str]:
    """The allowed destinations, normalised, de-duplicated and sorted.

    Sorted because the value ends up in a generated Caddy ACL: an unstable
    order would churn config and defeat any diff-based review of it.

    UNSET and SET-BUT-EMPTY are deliberately different: unset means "we have
    not been told, use the documented default set"; set-but-empty means the
    operator emptied it on purpose, and the fail-safe reading of that is FULLY
    CLOSED, not "silently reinstate the defaults they just deleted"."""
    raw = (env or {}).get('AGENT_EGRESS_ALLOW')
    if raw is None:
        tokens = list(_DEFAULT_PUBLIC) + list(_DEFAULT_INTERNAL)
    else:
        tokens = raw.replace(',', ' ').split()
    return sorted({h for h in (_normalise_host(t) for t in tokens) if h})


def acl_value(env=None) -> str:
    """The space-separated value for the Caddyfile's `allow {$AGENT_EGRESS_ALLOW}`
    — never empty, so Caddy can never see a bare `allow`."""
    hosts = allowlist_hosts(env)
    return ' '.join(hosts) if hosts else CLOSED_SENTINEL


def proxy_env(env=None) -> dict:
    """Proxy env for a FENCED agent container — `{}` unless the allowlist
    policy is actually in force.

    Both cases of every var are set: curl reads `http_proxy`, `requests` reads
    either, and a half-set pair is exactly how the #281 CA injection bit us."""
    if resolve_policy(env) != POLICY_ALLOWLIST:
        return {}
    url = 'http://%s:%d' % (PROXY_SERVICE, PROXY_PORT)
    no_proxy = ','.join(_NO_PROXY)
    return {
        'HTTP_PROXY': url, 'HTTPS_PROXY': url,
        'http_proxy': url, 'https_proxy': url,
        'NO_PROXY': no_proxy, 'no_proxy': no_proxy,
    }
