# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""WebSocket terminal — docker exec into agent containers.

Provides a browser-based terminal for any running agent instance.
Uses Docker's exec API to attach a shell inside the container,
streaming I/O over WebSocket via flask-sock.
"""

import json
import logging
import threading
import uuid

from flask import Blueprint, current_app, request
from flask_sock import Sock

from app.services.ingress import ingress_ok, log_refusal
from app.services.provisioner import slug_candidates

logger = logging.getLogger(__name__)

terminal_bp = Blueprint('terminal', __name__)
sock = Sock()


from app.services.portal_pty import (PORTAL_TMUX_SESSION,  # noqa: F401
                                       ensure_session_cmd, reap_cmd, shell_cmd)


def init_terminal(app):
    """Initialize flask-sock on the app."""
    sock.init_app(app)



@sock.route('/ws/terminal/<instance_id>')
def terminal(ws, instance_id):
    """WebSocket terminal — docker exec /bin/sh into an agent container.

    Client sends JSON frames:
      {"type": "input", "data": "<chars>"}
      {"type": "resize", "cols": N, "rows": N}

    Server sends raw terminal output as text frames.
    """
    # --- source-IP anchor (#397) --------------------------------------------
    # FIRST, before any DB or Docker work. `@sock.route` registers on the APP,
    # not on `terminal_bp`, so a blueprint `before_request` would never fire
    # here — the guard has to live in the handler.
    #
    # Everything below authorises on `X-Authentik-Username`, and the ownership
    # test compares the instance's stored `user_slug` against a slug DERIVED
    # FROM that same header. A direct dial therefore satisfies it by simply
    # claiming the victim's username (which #397 step 1 hands over via the
    # unanchored admin listing) — and the handler then `docker exec`s an
    # interactive shell inside that user's container. Anchor first, exec never.
    if not ingress_ok():
        log_refusal('terminal-ws', request.method, request.path,
                    request.remote_addr)
        ws.send('\r\n\x1b[31mUnauthorized\x1b[0m\r\n')
        return

    username = request.headers.get('X-Authentik-Username')
    if not username:
        ws.send('\r\n\x1b[31mUnauthorized\x1b[0m\r\n')
        return

    try:
        instance = current_app.db.get_instance(uuid.UUID(instance_id))
    except (ValueError, TypeError):
        ws.send('\r\n\x1b[31mInvalid instance ID\x1b[0m\r\n')
        return

    # AGM-11 (#1039): current OR legacy pre-hash slug (#192), matching
    # api._owns_instance / proxy._owns. Exact-slug matching locked the owner
    # of a legacy-slug instance out of their own terminal. Candidates are
    # derived from the CALLER's username only — cross-user isolation holds.
    if not instance or instance['user_slug'] not in slug_candidates(username):
        ws.send('\r\n\x1b[31mInstance not found\x1b[0m\r\n')
        return

    if instance['state'] != 'running':
        ws.send(f'\r\n\x1b[33mAgent is {instance["state"]}. Start it first.\x1b[0m\r\n')
        return

    container_name = instance['container_name']

    # Use Docker API to exec into the container
    try:
        docker_client = current_app.docker_client._client
        container = docker_client.containers.get(container_name)

        # #611: ensure the tmux session exists (unmarked exec, see helpers),
        # then attach with a per-WS marker for the close-time reap.
        ws_marker = uuid.uuid4().hex
        try:
            _ens = docker_client.api.exec_create(container.id, ensure_session_cmd())
            docker_client.api.exec_start(_ens['Id'])
        except Exception:  # noqa: BLE001 — no tmux / race: attach falls back
            pass
        exec_id = docker_client.api.exec_create(
            container.id,
            shell_cmd(),
            stdin=True, tty=True, stderr=True,
            environment={'TERM': 'xterm-256color',
                         'RZFZ_PORTAL_WS': ws_marker},
        )

        # Start exec with socket
        sock_stream = docker_client.api.exec_start(
            exec_id['Id'], socket=True, tty=True,
        )
        # Get the raw socket
        raw_sock = sock_stream._sock

    except Exception as e:
        ws.send(f'\r\n\x1b[31mFailed to exec into container: {e}\x1b[0m\r\n')
        return

    # Reader thread: container → WebSocket
    stop_event = threading.Event()

    def _reader():
        try:
            while not stop_event.is_set():
                data = raw_sock.recv(4096)
                if not data:
                    break
                ws.send(data.decode('utf-8', errors='replace'))
        except Exception:
            pass
        finally:
            try:
                ws.send('\r\n\x1b[33m[session ended]\x1b[0m\r\n')
            except Exception:
                pass

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # Main loop: WebSocket → container
    try:
        while True:
            try:
                msg = ws.receive(timeout=300)
            except Exception:
                break
            if msg is None:
                continue
            try:
                frame = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                raw_sock.send(msg.encode() if isinstance(msg, str) else msg)
                continue

            ftype = frame.get('type')
            if ftype == 'input':
                data = frame.get('data', '')
                if data:
                    raw_sock.send(data.encode('utf-8', errors='replace'))
            elif ftype == 'resize':
                cols = int(frame.get('cols', 80))
                rows = int(frame.get('rows', 24))
                try:
                    docker_client.api.exec_resize(exec_id['Id'], height=rows, width=cols)
                except Exception:
                    pass
            elif ftype == 'ping':
                pass  # keepalive
    finally:
        stop_event.set()
        try:
            raw_sock.close()
        except Exception:
            pass
        # #611 leak fix: the docker daemon keeps the exec'd process alive
        # after the attach socket closes — reap by marker. tmux path: kills
        # ONLY the attach client (server + session survive → re-attach gets
        # the buffer back). bash fallback: kills the shell that used to leak.
        try:
            _reap = docker_client.api.exec_create(container.id, reap_cmd(ws_marker))
            docker_client.api.exec_start(_reap['Id'])
        except Exception:  # noqa: BLE001 — container already gone etc.
            logger.debug("portal PTY reap failed for %s", container_name,
                         exc_info=True)
