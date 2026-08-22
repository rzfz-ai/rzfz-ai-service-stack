"""Tiny Host-rewriting HTTP+WS reverse proxy for the hermes v0.18 dashboard.

v0.18 bind logic: a LOOPBACK-bound dashboard runs its auth gate as a NO-OP
(fully open) — correct here because Authentik forward-auth + the agent-manager
owner-check already fence every request. But a loopback bind also enforces a
DNS-rebind Host-header check that rejects any non-loopback Host (400). The
agent-manager proxy dials this container over docker DNS (agent-hermes-<slug>),
so its Host header is non-loopback → 400.

This stdlib proxy binds 0.0.0.0:9119 (reachable over docker DNS), rewrites the
Host header to 127.0.0.1:<DASH_PORT>, and forwards to the loopback dashboard —
so the dashboard sees a loopback Host (accepts, auth off) while the manager
proxy reaches it normally. Handles HTTP + WebSocket (the dashboard chat TUI
uses a WS upgrade). Stdlib only.
"""
import os
import select
import socket
import threading

LISTEN_PORT = int(os.environ.get("HERMES_DASH_PROXY_PORT", "9119"))
DASH_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9118"))
DASH_HOST = "127.0.0.1"
REWRITE_HOST = f"127.0.0.1:{DASH_PORT}".encode()


def _pump(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _rewrite_host_in_headers(head: bytes) -> bytes:
    lines = head.split(b"\r\n")
    out = []
    for ln in lines:
        if ln[:5].lower() == b"host:":
            out.append(b"Host: " + REWRITE_HOST)
        else:
            out.append(ln)
    return b"\r\n".join(out)


def handle(client):
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Read the request head (until CRLFCRLF) so we can rewrite Host.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
            if len(buf) > 65536:
                break
        head, _, rest = buf.partition(b"\r\n\r\n")
        new_head = _rewrite_host_in_headers(head)
        upstream = socket.create_connection((DASH_HOST, DASH_PORT), timeout=10)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        upstream.sendall(new_head + b"\r\n\r\n" + rest)
        # Bi-directional pump (covers keep-alive bodies + WS upgrade frames).
        t1 = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(128)
    print(f"[hermes-dash-proxy] 0.0.0.0:{LISTEN_PORT} -> {DASH_HOST}:{DASH_PORT} (Host->{REWRITE_HOST.decode()})", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
