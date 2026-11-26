# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""A wedged cognee API ends its container, so Docker's restart policy brings it back.

cognee serves its whole API from one gunicorn worker. That worker has wedged (every thread
parked in a lock wait): /health stopped answering, Docker marked the container unhealthy,
and nothing restarted it for an hour. Docker's healthcheck only reports; it never restarts.

This watchdog runs inside the container next to the server (started by the entrypoint
wrapper, stdlib only, no Docker socket). It probes /health every INTERVAL seconds with a
TIMEOUT. Only after the API has answered once (so a slow start or a graph migration is
never cut short) does it count misses, and only FAILURES misses in a row end the server:
SIGTERM to the server process (the container's main process), then SIGQUIT. The
container then exits and ``restart: unless-stopped`` starts a fresh one. One answer resets
the count, so a busy but working API is never touched.

Defaults: INTERVAL 30 s, TIMEOUT 20 s, FAILURES 6 (about 3 minutes without one answer).
COGNEE_LIVENESS=0 turns it off.
"""
from __future__ import annotations

import os
import signal
import sys
import time
import urllib.request


def _number(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def probe(url: str, timeout: float) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def end(pid: int, grace: float, log) -> None:
    """SIGTERM (gunicorn's graceful stop: it ends its worker, then itself), then SIGQUIT
    (its immediate stop), then SIGKILL. The server is the container's PID 1, which the
    kernel never lets SIGKILL reach from inside the container, so the first two do it."""
    log("cognee liveness: the API has not answered; ending the server (pid %d) so the "
        "container restarts" % pid)
    for sig in (signal.SIGTERM, signal.SIGQUIT, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.2)
        log("cognee liveness: the server is still there %.0f s after %s" % (grace, sig.name))


def watch(pid: int, url: str, interval: float, timeout: float, failures: int, grace: float,
          log=lambda m: print(m, file=sys.stderr, flush=True)) -> str:
    """Watch until the server is gone ('gone') or has been ended ('ended')."""
    answered, misses = False, 0
    while _alive(pid):
        started = time.monotonic()
        if probe(url, timeout):
            if not answered:
                log("cognee liveness: the API answers; watching it")
            answered, misses = True, 0
        elif answered:
            misses += 1
            log("cognee liveness: /health did not answer (%d of %d)" % (misses, failures))
            if misses >= failures:
                end(pid, grace, log)
                return "ended"
        time.sleep(max(0.0, interval - (time.monotonic() - started)))
    return "gone"


def main(argv) -> int:
    if (os.environ.get("COGNEE_LIVENESS", "1") or "1").strip() == "0":
        return 0
    pid = int(argv[1])
    port = os.environ.get("HTTP_PORT", "8000")
    url = argv[2] if len(argv) > 2 else "http://127.0.0.1:%s/health" % port
    watch(pid, url,
          interval=_number("COGNEE_LIVENESS_INTERVAL_S", 30),
          timeout=_number("COGNEE_LIVENESS_TIMEOUT_S", 20),
          failures=int(_number("COGNEE_LIVENESS_FAILURES", 6)),
          grace=_number("COGNEE_LIVENESS_GRACE_S", 60))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
