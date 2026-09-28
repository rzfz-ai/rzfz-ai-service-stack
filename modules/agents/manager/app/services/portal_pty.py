# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#611 — portal-PTY shell/reap command builders (flask-free, unit-testable)."""
from __future__ import annotations

# ── #611: the portal PTY is tmux-backed ──────────────────────────────────────
# The original handler exec'd a bare bash per WS connect. Three defects, one
# root: (A) every reconnect (layout switch, reload) got a FRESH shell — typed
# state gone; (B) closing the WS only closes the attach socket, the docker
# daemon keeps the exec'd process running → one leaked bash per discarded
# session, marching toward pids_limit (#221); (E) any close-path (mobile
# crossing forces layout-1) read as "session ended".
#
# Now: session state lives in a tmux session `portal` INSIDE the container
# (created detached, unmarked), and each WS exec is only an ATTACH CLIENT
# carrying a unique environment marker. Re-attach (`new -A` semantics via
# ensure+attach) brings the buffer back; on WS close the marked CLIENT — and
# only it — is reaped by marker, so neither the tmux server nor the user's
# session dies with the socket. Images without tmux fall back to a marked
# bash: no persistence (as before), but the reap closes the leak.

PORTAL_TMUX_SESSION = 'portal'

_ENSURE_CMD = (
    'command -v tmux >/dev/null 2>&1 && '
    'tmux has-session -t {s} 2>/dev/null || '
    'tmux new-session -d -s {s} 2>/dev/null || true'
)


def ensure_session_cmd() -> list:
    """Pre-create the tmux session in a SEPARATE, unmarked exec.

    Separate on purpose: `tmux new -A` inside the marked exec would fork the
    tmux SERVER as a child of the marked client — the server inherits the
    marker environ and the reap would kill the user's whole session tree."""
    return ['/bin/sh', '-c', _ENSURE_CMD.format(s=PORTAL_TMUX_SESSION)]


def shell_cmd() -> list:
    """The marked exec: attach the portal session; bash/sh fallback."""
    return ['/bin/sh', '-c',
            'if command -v tmux >/dev/null 2>&1; then '
            f'exec tmux attach-session -t {PORTAL_TMUX_SESSION}; '
            'elif command -v bash >/dev/null 2>&1; then exec bash; '
            'else exec sh; fi']


def reap_cmd(marker: str) -> list:
    """Kill exactly the processes carrying this WS's environ marker.

    POSIX sh over /proc — deliberately NO pkill/killall (pattern kills are
    the #257 accident class). Matches the NUL-delimited environ, so a marker
    can never match on a substring of another value."""
    return ['/bin/sh', '-c',
            'for p in /proc/[0-9]*; do '
            # -zxF: NUL-delimited records, WHOLE-record fixed-string match —
            # a PREFIX_RZFZ_PORTAL_WS=<marker> sibling must never match.
            f'grep -qzxF "RZFZ_PORTAL_WS={marker}" "$p/environ" 2>/dev/null '
            '&& kill "${p#/proc/}" 2>/dev/null; done; true']
