# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2393 — a cheap structural check of a GGUF header before an engine opens it.

Reads only the header, metadata and tensor-info section (a few MB even for a
37 GB weight) and never hashes. It costs seconds of CPU for a big weight, so
callers run it OFF the report loop (``_launch_off_loop`` / the pull thread);
checks are serialised by a lock and budgeted in CPU time. Bounds are
generous for every real model and tight enough that a crafted length cannot
make the parser (ours or llama.cpp's) walk off the file or allocate much:
every length is compared with the bytes left in the file BEFORE anything is
read, scalar arrays are skipped with a seek instead of a read, and nested
arrays are depth-limited.
"""
from __future__ import annotations

import os
import struct
import threading
import time

# GGUF metadata value types -> byte width of the scalars.
_U64 = struct.Struct("<Q")
_SCALAR = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_STRING, _ARRAY = 8, 9
MAX_TENSORS, MAX_KV, MAX_STR, MAX_ARRAY, MAX_DIMS = 65_536, 100_000, 1 << 20, 10_000_000, 4
MAX_ALIGNMENT, MAX_DEPTH = 1 << 20, 4
MAX_HEADER_BYTES = 64 << 20     # the real catalogue headers are at most ~11 MB
TIME_BUDGET_S = 10.0            # CPU seconds (thread_time) for the parse
CHUNK = 1 << 20                 # buffered read window
_LOCK = threading.Lock()        # header checks run one at a time
LOCK_TIMEOUT_S = 30.0           # waiting longer means a read is stalled: busy, not bad

# ggml type id -> (elements per block, bytes per block). A tensor of a type not
# listed is still offset-checked, just not size-checked (newer formats must not
# be refused for being newer).
_GGML = {
    0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24),
    8: (32, 34), 9: (32, 36), 10: (256, 84), 11: (256, 110), 12: (256, 144),
    13: (256, 176), 14: (256, 210), 15: (256, 292), 16: (256, 66), 17: (256, 74),
    18: (256, 98), 19: (256, 50), 20: (32, 18), 21: (256, 110), 22: (256, 82),
    23: (256, 136), 24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8),
    29: (256, 56), 30: (1, 2), 34: (256, 54), 35: (256, 66), 39: (32, 17),
}


class GGUFRejected(ValueError):
    """The file is not a GGUF this node will hand to an engine."""


class GGUFBusy(RuntimeError):
    """The check could not start: another header check holds the lock past
    ``LOCK_TIMEOUT_S`` (a stalled read). Says nothing about the file — callers
    keep the load pending, never refuse it."""


class _R:
    """Bounded reader over a sliding buffer: one ``read`` per MiB, values
    decoded with ``unpack_from``; strings that are not needed are skipped."""

    def __init__(self, fh, size):
        self.fh, self.size = fh, size
        self.pos, self.base, self.buf = 0, 0, b""
        # CPU time of THIS thread: waiting for the lock or the disk is free.
        self.deadline = time.thread_time() + TIME_BUDGET_S
        self._ticks = 0

    def tick(self):
        """Called once per loop iteration; the clock is read every 256th."""
        self._ticks += 1
        if not self._ticks & 255 and time.thread_time() > self.deadline:
            raise GGUFRejected(f"header check exceeded its {TIME_BUDGET_S:g} s CPU budget")

    def _need(self, n):
        end = self.pos + n
        if n < 0 or end > self.size:
            raise GGUFRejected(f"truncated at byte {self.pos} (needs {n} more)")
        if end > MAX_HEADER_BYTES:
            raise GGUFRejected(f"header larger than {MAX_HEADER_BYTES >> 20} MiB")

    def _window(self, n):
        """Make [pos, pos+n) available in ``buf``; return its offset."""
        off = self.pos - self.base
        if off < 0 or off + n > len(self.buf):
            self.fh.seek(self.pos)
            self.buf = self.fh.read(max(n, CHUNK))
            self.base, off = self.pos, 0
            if len(self.buf) < n:   # the file shrank under us
                raise GGUFRejected(f"truncated at byte {self.pos} (file changed while reading)")
        return off

    def skip(self, n):
        self._need(n)
        self.pos += n

    def take(self, n):
        self._need(n)
        off = self._window(n)
        self.pos += n
        return self.buf[off:off + n]

    def _unpack(self, fmt, n):
        self._need(n)
        off = self._window(n)
        self.pos += n
        return struct.unpack_from(fmt, self.buf, off)[0]

    def u32(self):
        return self._unpack("<I", 4)

    def u64(self):
        return self._unpack("<Q", 8)

    def string(self, keep=False):
        n = self.u64()
        if n > MAX_STR:
            raise GGUFRejected(f"string length {n} exceeds {MAX_STR}")
        return self.take(n) if keep else self.skip(n)

    def _strings(self, n):
        """Skip ``n`` length-prefixed strings (a tokenizer vocabulary): the
        hot loop of a real header, so the bookkeeping is inlined."""
        unpack, cap = _U64.unpack_from, min(self.size, MAX_HEADER_BYTES)
        pos, buf, base = self.pos, self.buf, self.base
        for i in range(n):
            if not i & 255 and time.thread_time() > self.deadline:
                raise GGUFRejected(f"header check exceeded its {TIME_BUDGET_S:g} s CPU budget")
            off = pos - base
            if off < 0 or off + 8 > len(buf):
                self.pos = pos
                self._need(8)
                off = self._window(8)
                buf, base = self.buf, self.base
            ln = unpack(buf, off)[0]
            if ln > MAX_STR:
                raise GGUFRejected(f"string length {ln} exceeds {MAX_STR}")
            pos += 8 + ln
            if pos > cap:
                self.pos = pos - ln
                self._need(ln)
        self.pos = pos

    def value(self, t, depth=0):
        if t in _SCALAR:
            self.skip(_SCALAR[t])
        elif t == _STRING:
            self.string()
        elif t == _ARRAY:
            if depth >= MAX_DEPTH:
                raise GGUFRejected("metadata arrays nested too deeply")
            et, n = self.u32(), self.u64()
            if n > MAX_ARRAY:
                raise GGUFRejected(f"array of {n} elements exceeds {MAX_ARRAY}")
            if et in _SCALAR:
                self.skip(_SCALAR[et] * n)
            elif et == _STRING:
                self._strings(n)
            else:
                for _ in range(n):
                    self.tick()
                    self.value(et, depth + 1)
        else:
            raise GGUFRejected(f"unknown metadata type {t}")


def _nbytes(ggml_type, dims):
    """Bytes a tensor occupies, or None when its type is not in the table."""
    bs_ts = _GGML.get(ggml_type)
    if bs_ts is None or not dims:
        return None
    bs, ts = bs_ts
    if dims[0] % bs:
        return None
    n = dims[0] // bs * ts
    for d in dims[1:]:
        n *= d
    return n


def check_gguf(path: str) -> None:
    """Raise GGUFRejected when ``path`` is a malformed GGUF; else return.

    Covers the header AND the tail: the tensor data section must be at least as
    long as the tensors' own types and shapes say."""
    if not _LOCK.acquire(timeout=LOCK_TIMEOUT_S):
        raise GGUFBusy("header check queued behind a stalled read")
    try:
        _check(path)
    except struct.error as exc:   # belt and braces: never an escape
        raise GGUFRejected(f"unreadable header ({exc})") from exc
    finally:
        _LOCK.release()


def _check(path):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        r = _R(fh, size)
        if r.take(4) != b"GGUF":
            raise GGUFRejected("bad magic (not a GGUF file)")
        version = r.u32()
        if version not in (2, 3):
            raise GGUFRejected(f"unsupported GGUF version {version}")
        n_tensors, n_kv = r.u64(), r.u64()
        if n_tensors > MAX_TENSORS or n_kv > MAX_KV:
            raise GGUFRejected(f"implausible counts: {n_tensors} tensors, {n_kv} metadata keys")
        alignment = 32
        for _ in range(n_kv):
            r.tick()
            key = r.string(keep=True)
            t = r.u32()
            if key == b"general.alignment" and t == 4:
                alignment = r.u32() or 32
                if alignment > MAX_ALIGNMENT:
                    raise GGUFRejected(f"implausible alignment {alignment}")
            else:
                r.value(t)
        tensors = []
        for _ in range(n_tensors):
            r.tick()
            r.string()
            n_dims = r.u32()
            if n_dims > MAX_DIMS:
                raise GGUFRejected(f"tensor with {n_dims} dimensions")
            dims = [r.u64() for _ in range(n_dims)]
            ggml_type = r.u32()
            tensors.append((r.u64(), ggml_type, dims))
        data_start = r.pos + (-r.pos) % alignment
        for off, ggml_type, dims in tensors:
            if data_start + off > size:
                raise GGUFRejected(
                    f"tensor offset {off} points past the end of the file ({size} bytes)")
            nb = _nbytes(ggml_type, dims)
            if nb is not None and data_start + off + nb > size:
                raise GGUFRejected(
                    f"truncated tensor data: the tensors need {data_start + off + nb} bytes, "
                    f"the file has {size}")
