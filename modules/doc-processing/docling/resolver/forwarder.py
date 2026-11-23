# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""docling-resolver: name resolution for docling-mcp's URL guard (#2674).

docling-core refuses a URL source unless its host resolves to a global
address (docling_core/utils/file.py `_is_safe_url`, which calls
`socket.gethostbyname` inside docling-mcp). docling-mcp sits on the internal
docling-mcp-net, where Docker's embedded DNS does not forward external names,
so every remote URL was refused ("URL is not allowed").

This process gives that check an answer, and nothing more:
  * it listens on ONE address on docling-mcp-net (the address docling-mcp's
    `dns:` names) and relays each query to the embedded DNS of its own network
    namespace (127.0.0.11), which forwards to the host's resolvers because
    this container also sits on a non-internal network;
  * it relays the answer unchanged: the guard must see the real address, so
    a name that resolves to a private, loopback or link-local address is
    still refused by docling-core;
  * it forwards only what it validated: the header it rebuilds, the one
    question, and one EDNS OPT record when the query carries a well-formed
    one. Trailing bytes and every other additional record are dropped;
  * it answers itself, without asking upstream:
      - FORMERR for anything but a single standard query (QR clear, opcode
        QUERY, one question, no answer or authority records);
      - REFUSED for a class other than IN, a type other than A or AAAA, or a
        single-label name;
      - NXDOMAIN for `<name>.docling-mcp-net` and
        `<name>.docling-resolver-edge`.
    These two rules apply only to queries that reach the relay. Docker's
    embedded DNS inside docling-mcp answers the names on docling-mcp's own
    network itself (`docling-egress`, `docling-resolver`, and their
    `<name>.docling-mcp-net` forms) and never forwards them here. Those names
    still resolve inside docling-mcp, to private docling-mcp-net addresses,
    which the URL check and docling-egress both refuse. The rules keep the
    relay's own namespace from answering such names for anything that does
    reach it; they are not what keeps container addresses out of
    docling-mcp.

It is NOT the enforcing layer. docling-mcp fetches through docling-egress
(HTTP(S)_PROXY), which resolves the name again on its own, checks every
address against its deny list and dials only an address that passed
(forwardproxy.go dialContextCheckACL). A name that answers differently here
and there (DNS rebinding) is therefore still refused by the proxy.

Standard library only (it runs on the pinned docling image's own Python).
"""
from __future__ import annotations

import argparse
import socket
import socketserver
import struct
import sys
import threading
import time
from typing import NamedTuple

QTYPE_A = 1
QTYPE_AAAA = 28
QTYPE_OPT = 41
QCLASS_IN = 1
RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
RCODE_REFUSED = 5
#: This container's own networks: a query for `<name>.<network>` that reaches
#: the relay gets NXDOMAIN.
OWN_NETWORKS = (b"docling-mcp-net", b"docling-resolver-edge")
#: The longest query read from a client (a question is at most 260 bytes, so
#: this leaves room for one EDNS OPT record). A longer TCP query is dropped
#: unread; a longer UDP datagram is cut, which leaves its OPT record invalid,
#: so the record is dropped.
MAX_QUERY = 1024
#: The largest upstream answer read over UDP.
MAX_UDP = 4096
UPSTREAM_TIMEOUT = 4.0
#: The longest a TCP connection may take in total: reading the query, asking
#: upstream and writing the answer. A client that sends one byte just inside
#: every read timeout is cut off here.
TCP_DEADLINE = 8.0
#: Bounds the threads (and so the memory) a flood of queries can take. TCP has
#: its own small pool, so slow TCP clients cannot starve UDP, which docling-mcp
#: uses for every lookup.
MAX_INFLIGHT = 64
MAX_TCP_INFLIGHT = 8


class Query(NamedTuple):
    labels: list
    qtype: int
    qclass: int
    end: int  # offset just past the question
    opt: bytes  # the validated EDNS OPT record, or b""


def _opt_record(msg: bytes, pos: int, arcount: int) -> bytes:
    """The OPT record at `pos` when it is the first additional record and is
    well formed (root name, type 41, rdata made of whole options); else b""."""
    if not arcount or pos + 11 > len(msg) or msg[pos] != 0:
        return b""
    rtype, _size, _ttl, rdlen = struct.unpack("!HHIH", msg[pos + 1:pos + 11])
    if rtype != QTYPE_OPT or pos + 11 + rdlen > len(msg):
        return b""
    rdata, i = msg[pos + 11:pos + 11 + rdlen], 0
    while i < len(rdata):
        if i + 4 > len(rdata):
            return b""
        (olen,) = struct.unpack("!H", rdata[i + 2:i + 4])
        i += 4 + olen
    if i != len(rdata):
        return b""
    return msg[pos:pos + 11 + rdlen]


def parse_question(msg: bytes):
    """The only question of a plain query, as a Query, or None when the message
    is not a single standard query this relay accepts."""
    if len(msg) < 12:
        return None
    _id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", msg[:12])
    if flags & 0x8000 or (flags >> 11) & 0xF:  # a response, or not opcode QUERY
        return None
    if qd != 1 or an or ns:
        return None
    labels = []
    pos, total = 12, 0
    while True:
        if pos >= len(msg):
            return None
        n = msg[pos]
        pos += 1
        if n == 0:
            break
        if n > 63:  # a compression pointer or a reserved label type
            return None
        label = msg[pos:pos + n]
        if len(label) != n:
            return None
        labels.append(label)
        total += n + 1
        if total > 255:
            return None
        pos += n
    if pos + 4 > len(msg):
        return None
    qtype, qclass = struct.unpack("!HH", msg[pos:pos + 4])
    return Query(labels, qtype, qclass, pos + 4, _opt_record(msg, pos + 4, ar))


def forwarded(query: bytes, q: Query) -> bytes:
    """What goes upstream: a rebuilt header (the id; RD, AD and CD as asked),
    the question, and the validated OPT record. Nothing else of the client's."""
    qid, flags = struct.unpack("!HH", query[:4])
    header = struct.pack("!HHHHHH", qid, flags & 0x0130, 1, 0, 0, 1 if q.opt else 0)
    return header + query[12:q.end] + q.opt


def _reply(query: bytes, rcode: int, question_end: int | None) -> bytes:
    """An answer of our own: the query's id and RD bit, no records."""
    qid, flags = struct.unpack("!HH", query[:4])
    out_flags = 0x8000 | 0x0080 | (flags & 0x0100) | rcode  # QR, RA, RD as asked
    if question_end is None:
        return struct.pack("!HHHHHH", qid, out_flags, 0, 0, 0, 0)
    return struct.pack("!HHHHHH", qid, out_flags, 1, 0, 0, 0) + query[12:question_end]


def refusal(query: bytes) -> bytes | None:
    """The answer this relay gives itself, or None when the query is to be
    relayed. b"" means: drop it (too short to carry an id)."""
    if len(query) < 12:
        return b""
    q = parse_question(query)
    if q is None:
        return _reply(query, RCODE_FORMERR, None)
    if q.qclass != QCLASS_IN or q.qtype not in (QTYPE_A, QTYPE_AAAA) or len(q.labels) < 2:
        return _reply(query, RCODE_REFUSED, q.end)
    if q.labels[-1].lower() in OWN_NETWORKS:
        return _reply(query, RCODE_NXDOMAIN, q.end)
    return None


def _servfail(query: bytes) -> bytes:
    q = parse_question(query)
    return _reply(query, RCODE_SERVFAIL, q.end if q else None)


def _valid_answer(query: bytes, answer: bytes) -> bool:
    return len(answer) >= 12 and answer[:2] == query[:2] and bool(answer[2] & 0x80)


def relay_udp(query: bytes, upstream: tuple) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(UPSTREAM_TIMEOUT)
        try:
            s.connect(upstream)
            s.send(query)
            while True:
                answer = s.recv(MAX_UDP)
                if _valid_answer(query, answer):
                    return answer
        except OSError:
            return _servfail(query)


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return UPSTREAM_TIMEOUT
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("connection deadline passed")
    return min(left, UPSTREAM_TIMEOUT)


def _recv_exact(sock, n: int, deadline: float | None = None) -> bytes:
    buf = b""
    while len(buf) < n:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


def relay_tcp(query: bytes, upstream: tuple, deadline: float | None = None) -> bytes:
    try:
        with socket.create_connection(upstream, timeout=_remaining(deadline)) as s:
            s.settimeout(_remaining(deadline))
            s.sendall(struct.pack("!H", len(query)) + query)
            (n,) = struct.unpack("!H", _recv_exact(s, 2, deadline))
            answer = _recv_exact(s, n, deadline)
            return answer if _valid_answer(query, answer) else _servfail(query)
    except OSError:
        return _servfail(query)


def answer_for(query: bytes, upstream: tuple, tcp: bool = False, deadline: float | None = None) -> bytes:
    own = refusal(query)
    if own is not None:
        return own
    out = forwarded(query, parse_question(query))
    return relay_tcp(out, upstream, deadline) if tcp else relay_udp(out, upstream)


_slots = threading.BoundedSemaphore(MAX_INFLIGHT)
_tcp_slots = threading.BoundedSemaphore(MAX_TCP_INFLIGHT)


class _UDP(socketserver.BaseRequestHandler):
    def handle(self):
        slots = _slots
        if not slots.acquire(blocking=False):
            return  # overloaded: the client retries
        try:
            data, sock = self.request
            out = answer_for(data[:MAX_QUERY], self.server.upstream)
            if out:
                sock.sendto(out, self.client_address)
        finally:
            slots.release()


class _TCP(socketserver.BaseRequestHandler):
    def handle(self):
        slots = _tcp_slots
        if not slots.acquire(blocking=False):
            return
        try:
            deadline = time.monotonic() + TCP_DEADLINE
            (n,) = struct.unpack("!H", _recv_exact(self.request, 2, deadline))
            if n > MAX_QUERY:
                return
            out = answer_for(_recv_exact(self.request, n, deadline), self.server.upstream,
                             tcp=True, deadline=deadline)
            if out:
                self.request.settimeout(_remaining(deadline))
                self.request.sendall(struct.pack("!H", len(out)) + out)
        except (OSError, struct.error):
            pass
        finally:
            slots.release()


class _UDPServer(socketserver.ThreadingUDPServer):
    daemon_threads = True
    allow_reuse_address = True


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def serve(bind: str, port: int, upstream: tuple):
    """Start the UDP and TCP listeners; returns both servers (already serving)."""
    servers = [_UDPServer((bind, port), _UDP), _TCPServer((bind, port), _TCP)]
    for srv in servers:
        srv.upstream = upstream
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return servers


def check(addr: str, port: int = 53) -> int:
    """Healthcheck: a single-label name is refused by the relay itself, so a
    REFUSED answer proves the listener without touching upstream DNS."""
    query = struct.pack("!HHHHHH", 0xABCD, 0x0100, 1, 0, 0, 0) + b"\x01a\x00" + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        try:
            s.sendto(query, (addr, port))
            r = s.recv(512)
        except OSError as exc:
            print(f"[docling-resolver] no answer from {addr}:{port}: {exc}", file=sys.stderr)
            return 1
    return 0 if r[:2] == query[:2] and r[3] & 0xF == RCODE_REFUSED else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", metavar="ADDR", help="healthcheck the relay listening on ADDR, then exit")
    p.add_argument("--bind", help="the one address to listen on")
    p.add_argument("--port", type=int, default=53)
    p.add_argument("--upstream", default="127.0.0.11")
    p.add_argument("--upstream-port", type=int, default=53)
    a = p.parse_args(argv)
    if a.check:
        return check(a.check, a.port)
    if not a.bind:
        p.error("--bind is required")
    try:
        servers = serve(a.bind, a.port, (a.upstream, a.upstream_port))
    except OSError as exc:
        print(f"[docling-resolver] ERROR: cannot listen on {a.bind}:{a.port}: {exc}", file=sys.stderr)
        return 1
    print(f"[docling-resolver] listening on {a.bind}:{a.port} (udp+tcp), relaying to "
          f"{a.upstream}:{a.upstream_port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    for srv in servers:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
