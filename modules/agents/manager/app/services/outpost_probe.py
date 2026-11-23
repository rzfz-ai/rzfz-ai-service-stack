# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2482 C6 ("warm before running"):
does an instance HOST get past the Authentik outpost yet?

Why this exists. `register_instance` attaches the per-instance forward_single
provider to the embedded outpost, but the outpost only learns about it on its
next refresh. Measured on a test box, 3/3 identical: the manager
wrote `running` after ~7 s, the instance host answered with the OUTPOST's 404
for the next ~6 s, and the Moltis page appeared at ~13 s. The portal opens a
subdomain pane on `running`, so a pane opened in that window stayed on the
outpost 404 (which also carries `X-Frame-Options: DENY`) and never retried.

The probe asks the same question the browser's first request asks, by the same
path: an HTTPS request to Caddy (the stack's only ingress) for the instance
host, with the host as SNI and as `Host`, WITHOUT any session. The answer is
read on the forward-auth status alone, so no user session is needed:

* **404**: the outpost does not know the host. It is NOT ready.
* **3xx or 401**: the outpost knows the host and starts its sign-in flow
  (Authentik answers Caddy's forward_auth with a redirect to
  `/outpost.goauthentik.io/start`). It IS ready: a signed-in user's pane gets
  through.
* any other 2xx/4xx: the request got past forward_auth to the manager. Also
  ready. (Without a session that does not happen today; it is not the 404.)
* **5xx**, or no HTTP answer at all (connect refused, TLS failure, timeout):
  not ready, but for a different reason (`unreachable`), so it is logged as
  such.

The request carries no credentials and its body is never read, so the TLS
certificate is not verified: the hop is the stack-internal one to Caddy, whose
certificate for an instance host is the box's wildcard (often from the box's
own CA), and nothing on that hop is a secret.
"""
from __future__ import annotations

import logging
import time

import httpx

from razzfazz_common.proxy_anchor import DEFAULT_INGRESS_HOST

logger = logging.getLogger(__name__)

#: Where the probe enters the stack: Caddy, by its container name (the same
#: `CADDY_HOST` the manager's source-IP anchor resolves). Tests point it at a
#: fake outpost.
INGRESS_URL = f"https://{DEFAULT_INGRESS_HOST}"
#: One probe request.
PROBE_TIMEOUT_S = 3.0
#: How long a launch/start waits for the outpost before it reports an error.
#: The measured gap is ~6 s; the bound leaves an order of magnitude for a busy
#: box and stays well under gunicorn's 120 s worker timeout.
WAIT_DEADLINE_S = 60.0
#: Pause between two probes while waiting.
WAIT_INTERVAL_S = 1.0

PASSED = 'passed'
OUTPOST_404 = 'outpost-404'
UNREACHABLE = 'unreachable'
UI_5XX = 'ui-5xx'


def classify(status: int | None) -> str:
    """The probe's verdict for one forward-auth answer (None = no HTTP answer)."""
    if status is None or status >= 500:
        return UNREACHABLE
    if status == 404:
        return OUTPOST_404
    return PASSED


def probe_host(host: str, *, timeout: float | None = None) -> str:
    """One unauthenticated request for `https://<host>/` through Caddy.
    Returns PASSED, OUTPOST_404 or UNREACHABLE; never raises."""
    try:
        with httpx.Client(verify=False,  # noqa: S501 - see the module docstring
                          follow_redirects=False,
                          timeout=PROBE_TIMEOUT_S if timeout is None else timeout) as c:
            r = c.get(f"{INGRESS_URL}/", headers={'Host': host, 'Accept': 'text/html'},
                      # The host must be the SNI too: Caddy picks the site (and
                      # its certificate) from the TLS server name before it
                      # reads `Host`.
                      extensions={'sni_hostname': host})
    except (httpx.HTTPError, OSError) as e:
        logger.debug("outpost probe for %s: no HTTP answer (%s)", host, e)
        return UNREACHABLE
    return classify(r.status_code)


def probe_ui(url: str, headers: dict | None = None, *, timeout: float | None = None) -> str:
    """#2482 C6 round 2: one request to the agent's own web UI, exactly as the
    manager's proxy sends a pane's first request (same container, port and
    `X-Forwarded-Prefix`). PASSED for any answer below 500, UI_5XX for a 5xx
    (Hermes answers 502 while its UI boots), UNREACHABLE without an answer.

    Not through Caddy, unlike probe_host: on the portal host every path is
    behind forward_auth, so an unauthenticated `/i/<token>/` only ever gets the
    outpost's sign-in redirect and never shows the upstream's status. What the
    pane receives behind that gate is this answer, passed through verbatim by
    `proxy._forward_to_instance` (a refused connection becomes its own 502)."""
    try:
        r = httpx.get(url, headers=headers or {}, follow_redirects=False,
                      timeout=PROBE_TIMEOUT_S if timeout is None else timeout)
    except (httpx.HTTPError, OSError) as e:
        logger.debug("ui probe %s: no HTTP answer (%s)", url, e)
        return UNREACHABLE
    return UI_5XX if r.status_code >= 500 else PASSED


def wait_until_passed(host: str | None = None, *, probe=None, deadline_s: float | None = None,
                      interval_s: float | None = None) -> tuple[bool, str, float]:
    """Run a probe (`probe_host(host)` unless `probe` is given) until it
    passes or the deadline elapses.

    Returns (passed, last verdict, seconds waited). Bounded: at most
    `deadline_s` plus one probe timeout."""
    deadline_s = WAIT_DEADLINE_S if deadline_s is None else deadline_s
    interval_s = WAIT_INTERVAL_S if interval_s is None else interval_s
    probe = probe or (lambda: probe_host(host))
    start = time.monotonic()
    verdict = UNREACHABLE
    while True:
        verdict = probe()
        waited = time.monotonic() - start
        if verdict == PASSED:
            return True, verdict, waited
        if waited + interval_s > deadline_s:
            return False, verdict, waited
        time.sleep(interval_s)
