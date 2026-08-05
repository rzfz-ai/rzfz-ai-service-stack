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

from app.services.provisioner import make_user_slug

logger = logging.getLogger(__name__)

terminal_bp = Blueprint('terminal', __name__)
sock = Sock()


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
    username = request.headers.get('X-Authentik-Username')
    if not username:
        ws.send('\r\n\x1b[31mUnauthorized\x1b[0m\r\n')
        return

    user_slug = make_user_slug(username)

    try:
        instance = current_app.db.get_instance(uuid.UUID(instance_id))
    except (ValueError, TypeError):
        ws.send('\r\n\x1b[31mInvalid instance ID\x1b[0m\r\n')
        return

    if not instance or instance['user_slug'] != user_slug:
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

        # Create exec instance with PTY
        exec_id = docker_client.api.exec_create(
            container.id,
            ['/bin/sh', '-c', 'if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi'],
            stdin=True, tty=True, stderr=True,
            environment={'TERM': 'xterm-256color'},
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
