# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#287 pull-on-deploy from HuggingFace.

When a deploy references weights the node doesn't have yet, the node fetches
them from HuggingFace (repo_id + filename from the catalog) INTO the models
volume BEFORE launching the engine — so deploy-from-catalog actually works
instead of crash-looping on a missing GGUF.

NETWORK-MODE GUARDED: only in ``online``/``proxied`` mode. In ``offline`` mode
HF is never contacted (the in-stack Zot registry puller — puller.py — is the
offline path). In ``proxied`` mode the HTTP client inherits HTTPS_PROXY, so the
fetch rides the corporate proxy.

Import-safe: stdlib only; the byte stream is injected so it unit-tests with no
network.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.request

_GLOB_CHARS = set("*?[]")


def _logger():
    import logging
    return logging.getLogger("llm-worker-agent.hf_pull")


def hf_file_url(repo_id: str, filename: str, revision: str = "main") -> str:
    return f"https://huggingface.co/{repo_id}/resolve/{revision}/{filename}"


def network_allows_hf(mode: str | None) -> bool:
    """HF fetch is allowed in online/proxied, refused in offline."""
    return (mode or "online").strip().lower() != "offline"


def is_glob(name: str) -> bool:
    return any(c in _GLOB_CHARS for c in name or "")


def hf_tree_url(repo_id: str, revision: str = "main") -> str:
    """The HF API listing for a repo's files (recursive, so subfolders show)."""
    return f"https://huggingface.co/api/models/{repo_id}/tree/{revision}?recursive=1"


def list_repo_entries(repo_id: str, revision: str = "main", *, timeout: int = 30) -> list[dict]:
    """The repo's file ENTRIES from the HF API — path plus the LFS metadata.

    #356: the tree listing this module already fetched for glob resolution also
    carries each LFS file's sha256 (``lfs.oid``) — the digest the pull path was
    discarding. One fetch now serves both name resolution and verification.
    """
    with urllib.request.urlopen(hf_tree_url(repo_id, revision), timeout=timeout) as resp:
        data = json.loads(resp.read())
    return [e for e in data if isinstance(e, dict) and e.get("type") == "file"
            and e.get("path")]


def list_repo_files(repo_id: str, revision: str = "main", *, timeout: int = 30) -> list[str]:
    """Default lister: the repo's file paths from the HF API.

    Injected in tests and by any caller that already holds a listing, so the
    resolution logic is exercisable with no network — the same seam
    ``http_stream`` uses for the byte path.
    """
    return [e["path"] for e in list_repo_entries(repo_id, revision, timeout=timeout)]


_HEX64 = None  # compiled lazily below


def digest_for(entries: list[dict] | None, filename: str) -> str | None:
    """The sha256 HF publishes for ``filename``, or None (non-LFS / unknown).

    Exact path match first; else a UNIQUE basename match (callers flatten repo
    subfolders to the basename, #303 — but an ambiguous basename must not pick
    a digest at random, that would guarantee a false mismatch). ``lfs.oid`` is
    bare hex on HF; a ``sha256:`` prefix is tolerated. Anything that is not 64
    hex chars is treated as absent rather than fed to the comparator.
    """
    import re as _re
    global _HEX64
    if _HEX64 is None:
        _HEX64 = _re.compile(r"^[0-9a-f]{64}$")

    def _oid(e: dict) -> str | None:
        oid = str(((e.get("lfs") or {}).get("oid")) or "").strip().lower()
        if oid.startswith("sha256:"):
            oid = oid[7:]
        return oid if _HEX64.match(oid) else None

    entries = entries or []
    for e in entries:
        if e.get("path") == filename:
            return _oid(e)
    base = os.path.basename(filename)
    hits = [e for e in entries if os.path.basename(e.get("path") or "") == base]
    return _oid(hits[0]) if len(hits) == 1 else None


def resolve_glob(repo_id: str, pattern: str, *, list_files, revision: str = "main") -> list[str]:
    """Expand a glob weight name against the repo's real file listing (#360).

    The bundled catalog mirrors ``core/llm/standard-models.yaml``, which is
    written for GPUStack — and GPUStack resolves globs itself via this same API.
    The node's puller did not, so two mainline entries (``qwen3-coder-next``,
    behind `post-install --preset developer`, and ``nomic-embed-text``) could be
    selected in the console and could never deploy.

    Matches against the FULL repo path, because a catalog pattern may carry a
    directory (``Qwen3-Coder-Next-Q4_K_M/…-*.gguf``). Returns every match sorted,
    not just the first: a sharded GGUF is several files and an engine launched on
    a subset is worse than one that never launched.
    """
    names = [n for n in (list_files(repo_id, revision) or []) if n]
    matched = sorted(n for n in names if fnmatch.fnmatch(n, pattern))
    if not matched:
        raise ValueError(
            f"glob '{pattern}' matched no file in {repo_id}@{revision} "
            f"({len(names)} files listed)"
        )
    return matched


class InsufficientDiskSpace(Exception):
    """The models volume cannot hold this artifact (#356)."""


#: Headroom kept free beyond the artifact itself, as a fraction of its size.
#: A weight that exactly fills the volume leaves no room for the NEXT shard, the
#: engine's own scratch, or the atomic rename's transient double-occupancy.
DISK_HEADROOM_FRACTION = 0.05


def free_bytes(path: str) -> int | None:
    """Bytes available on the filesystem holding ``path``, or None if unknown."""
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):  # pragma: no cover - non-POSIX / missing
        return None
    return st.f_bavail * st.f_frsize


def check_space_for(dest_dir: str, needed_bytes: int, *, free_fn=None) -> None:
    """Raise InsufficientDiskSpace if ``needed_bytes`` (+headroom) will not fit.

    #356: weights are tens of GB and nothing checked. A pull that fills the
    models volume does not just fail — it takes the whole node down with it: the
    engine cannot write, the agent cannot log, and the operator gets a disk-full
    cascade instead of one clear "this model does not fit". Failing BEFORE the
    first byte is written turns that into a legible error.
    """
    if not needed_bytes or needed_bytes <= 0:
        return
    avail = (free_fn or free_bytes)(dest_dir)
    if avail is None:
        return  # cannot tell — do not block the pull on an unknowable
    required = int(needed_bytes * (1 + DISK_HEADROOM_FRACTION))
    if avail < required:
        raise InsufficientDiskSpace(
            f"need ~{required / 1e9:.1f} GB (artifact {needed_bytes / 1e9:.1f} GB "
            f"+ {DISK_HEADROOM_FRACTION:.0%} headroom) but only "
            f"{avail / 1e9:.1f} GB free on {dest_dir}"
        )


class PullCancelled(Exception):
    """The caller withdrew interest in this download (#302).

    Raised from the chunk loop so the existing cleanup path removes the temp
    file — a cancelled pull must leave NOTHING half-written behind.
    """


class RangeIgnored(Exception):
    """The server answered a ``Range`` request with a full body (200, no
    ``Content-Range``). Raised by the stream BEFORE it yields, so the caller can
    drop its partial and start from byte 0 instead of appending a whole file to
    a half one (#2367)."""


class TransientHTTPError(Exception):
    """A 429 or 5xx from the weight host: the request was fine, the far side was
    not, and asking again is the right thing to do (#2367)."""


#: Exceptions a pull may retry: the transport died (socket, proxy, CDN), the
#: read timed out (#1633 turns a silent stall into one of these), or the host
#: said "not now". Everything else — digest mismatch, disk full, an unverified
#: weight refused, a cancel — is a decision, not weather, and is never retried.
def _retryable_exceptions() -> tuple:
    excs: list = [OSError, TimeoutError, ConnectionError, EOFError, TransientHTTPError]
    try:  # httpx is the agent's client; the pure module stays importable without it
        import httpx  # noqa: WPS433
        excs.append(httpx.TransportError)
    except Exception:  # noqa: BLE001
        pass
    return tuple(excs)


#: #2367: a 35 GB stream over a consumer link stalls, the read timeout ends it
#: (#1633) — and then nothing tried again. Five attempts with 5·3^n seconds
#: between them (5, 15, 45, 135, 300) tolerate roughly ten minutes of a flaky
#: link without giving up, and each attempt RESUMES from the kept partial.
DEFAULT_FETCH_ATTEMPTS = 5
_BACKOFF_BASE_S = 5.0
_BACKOFF_CAP_S = 300.0


def fetch_attempts() -> int:
    raw = os.environ.get("WEIGHT_FETCH_ATTEMPTS", "")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FETCH_ATTEMPTS
    return max(1, n)


def backoff_seconds(attempt: int) -> float:
    """Delay BEFORE retry number ``attempt`` (1-based): 5, 15, 45, 135, 300, 300…"""
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (3 ** max(0, attempt - 1)))


#: One writer per destination. Two pulls of the same weight — a deploy and a
#: mirror command, or two deployments of one model — used to write to two
#: mkstemp names; with a shared resumable partial they would append into one
#: file while each hashed only its own stream, and the first to finish would
#: place an interleaved file as "verified" (ga.1 security review, Mode B).
#: The lock is per process, which is the agent's whole write side of the mount.
_DEST_LOCKS: dict = {}
_DEST_LOCKS_GUARD = threading.Lock()


def _dest_lock(dest_path: str) -> threading.Lock:
    with _DEST_LOCKS_GUARD:
        lock = _DEST_LOCKS.get(dest_path)
        if lock is None:
            lock = _DEST_LOCKS[dest_path] = threading.Lock()
        return lock


def partial_path(dest_path: str) -> str:
    """Where a resumable partial lives: beside its destination, named after it,
    so a later attempt (this process or the next one) finds it. Deliberately NOT
    a mkstemp name: the #1633 start-up sweep removes those, and a resumable
    partial is the one thing that must survive a restart."""
    return dest_path + ".part"


def download_stream(url: str, dest_path: str, *, http_stream, progress_cb=None,
                    free_fn=None, expected_sha256: str | None = None,
                    cancel_cb=None, resume: bool = True,
                    identity_out: dict | None = None) -> int:
    """Stream ``url`` → ``dest_path`` atomically (partial file + os.replace).
    Returns the byte count. ``http_stream(url)`` yields ``(chunk_bytes,
    total_or_None)``; when a partial exists it is called as
    ``http_stream(url, start=<bytes already held>)`` and must serve the
    remainder (HTTP Range) or raise ``RangeIgnored`` before yielding.
    ``progress_cb(got, total)`` is called as bytes arrive, ``got`` counting from
    the first byte of the FILE, not of this attempt.

    #2367: the partial is ``partial_path(dest_path)`` and is KEPT on a transport
    failure — that is what makes a retry a resume rather than a restart of 35 GB.
    It is removed on a cancel (#302: nothing half-written behind), on a digest
    mismatch (the bytes are wrong, resuming them would be too) and on a
    disk-space refusal. A server that ignores ``Range`` restarts from byte 0.

    #356: the first chunk carries the total, so the space check happens as soon
    as the size is known and before the bulk is written. Raises
    InsufficientDiskSpace rather than filling the volume.

    #356 integrity: with ``expected_sha256`` set, the stream is hashed as it is
    written — on a resume the hash is rebuilt over the bytes already held — and a
    mismatch raises DigestMismatch BEFORE the file reaches its destination.
    Same contract as puller.pull_blob, whose exception this reuses.

    NODE-5: this is a CORRUPTION/TRUNCATION check, not a tamper check (#1037).
    """
    existed_before = os.path.exists(dest_path)
    with _dest_lock(dest_path):
        # A second pull of the same weight waited here for the first; if that
        # one placed the file meanwhile, there is nothing left to do. A file
        # that was ALREADY there when the caller asked is re-fetched as asked
        # (a caller re-verifying a sizeless entry, #1037).
        if not existed_before and os.path.exists(dest_path):
            return os.path.getsize(dest_path)
        return _download_stream_locked(url, dest_path, http_stream=http_stream,
                                       progress_cb=progress_cb, free_fn=free_fn,
                                       expected_sha256=expected_sha256,
                                       cancel_cb=cancel_cb, resume=resume,
                                       identity_out=identity_out)


def _download_stream_locked(url: str, dest_path: str, *, http_stream, progress_cb,
                            free_fn, expected_sha256, cancel_cb, resume,
                            identity_out=None) -> int:
    from app.puller import DigestMismatch

    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    part = partial_path(dest_path)
    hasher = hashlib.sha256() if expected_sha256 else None

    start = 0
    if resume and os.path.isfile(part):
        start = os.path.getsize(part)
    if start and hasher is not None:
        with open(part, "rb") as fh:  # rebuild the digest over what we already hold
            for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                hasher.update(block)

    def _open_stream(offset: int):
        return http_stream(url, start=offset) if offset else http_stream(url)

    got = start
    checked = False
    keep_partial = True
    try:
        stream = _open_stream(start)
        try:
            first = next(stream)
        except RangeIgnored:
            # The host served the whole file: our partial is worthless. Start over.
            _logger().warning("host ignored Range for %s — restarting from byte 0 "
                              "(%d bytes of partial dropped, #2367)", url, start)
            start, got = 0, 0
            hasher = hashlib.sha256() if expected_sha256 else None
            stream = _open_stream(0)
            first = next(stream, None)
        except StopIteration:
            first = None

        def _chunks():
            if first is not None:
                yield first
            yield from stream

        mode = "ab" if start else "wb"
        with open(part, mode) as fh:
            for chunk, remaining in _chunks():
                # #302: checked per chunk — an undeployed instance must stop
                # streaming NOW, not when a multi-part quant finishes.
                if cancel_cb is not None and cancel_cb():
                    keep_partial = False
                    raise PullCancelled(f"pull cancelled after {got} bytes: {url}")
                total = (start + int(remaining)) if remaining else None
                if not checked and remaining:
                    try:
                        check_space_for(dest_dir, int(remaining), free_fn=free_fn)
                    except InsufficientDiskSpace:
                        keep_partial = False
                        raise
                    checked = True
                if chunk:
                    fh.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    got += len(chunk)
                    if progress_cb:
                        progress_cb(got, total)
            fh.flush()
            os.fsync(fh.fileno())
        if hasher is not None and hasher.hexdigest() != expected_sha256:
            keep_partial = False
            raise DigestMismatch(
                f"sha256 mismatch for {url}: expected {expected_sha256}, "
                f"got {hasher.hexdigest()} ({got} bytes) — refusing to keep it")
        os.replace(part, dest_path)  # atomic within the same dir
        if identity_out is not None:
            # #2392: the inode THIS download placed, read under the name's lock
            identity_out[dest_path] = file_identity(os.stat(dest_path))
    except BaseException as exc:
        if keep_partial and isinstance(exc, _retryable_exceptions()):
            _logger().warning("transfer of %s ended after %d bytes; partial kept for "
                              "resume (#2367)", url, got)
        else:
            try:
                os.unlink(part)
            except OSError:
                pass
        raise
    return got


def download_with_retries(url: str, dest_path: str, *, http_stream, attempts: int | None = None,
                          sleep=time.sleep, **kw) -> int:
    """``download_stream`` with the #2367 retry policy: transport errors, read
    timeouts and 429/5xx are retried up to ``attempts`` times (default
    ``WEIGHT_FETCH_ATTEMPTS`` or 5) with 5·3^n seconds between attempts, each
    attempt resuming from the kept partial. Anything else propagates at once.
    The last failure is re-raised with the attempt count in its message."""
    n = attempts if attempts is not None else fetch_attempts()
    retryable = _retryable_exceptions()
    last: BaseException | None = None
    for attempt in range(1, n + 1):
        try:
            return download_stream(url, dest_path, http_stream=http_stream, **kw)
        except retryable as exc:  # noqa: PERF203 - the loop IS the policy
            last = exc
            if attempt >= n:
                break
            delay = backoff_seconds(attempt)
            _logger().warning("pull attempt %d/%d for %s failed (%s: %s) — retrying in "
                              "%.0f s, resuming from the kept partial (#2367)",
                              attempt, n, url, type(exc).__name__, str(exc)[:160], delay)
            sleep(delay)
    assert last is not None
    raise type(last)(f"{last} (after {n} attempt(s), #2367)") from last


class UnverifiedPullRefused(Exception):
    """HF published no sha256 for a file this node insists on verifying (NODE-5).

    Raised INSTEAD of pulling — the pre-NODE-5 code logged
    ``pulling UNVERIFIED (#356)`` and carried on, which made the integrity check
    advisory for exactly the files it matters most for.
    """


#: Extensions whose HF listing carries an ``lfs.oid`` in practice — every weight
#: format is an LFS object on the hub, so a MISSING digest for one of these means
#: the listing failed, the basename was ambiguous, or the file is not what it
#: claims. Refusing costs a legitimate pull nothing and closes the advisory hole.
DIGEST_REQUIRED_SUFFIXES = (
    ".safetensors", ".gguf", ".bin", ".pt", ".pth", ".ckpt", ".h5",
    ".msgpack", ".onnx", ".model",
)


def require_digest_for(filename: str) -> bool:
    """Policy (NODE-5): must a pull of ``filename`` be REFUSED when HF publishes
    no sha256 for it?

    Default: yes for the weight formats above, no for everything else. The
    non-LFS remainder of a #574 full-repo pull (``config.json``,
    ``tokenizer*.json``, and any ``*.py`` the repo ships) is usually a plain git
    object with no ``lfs.oid``, so requiring a digest there by default would
    refuse every repo-dir deploy of a ``trust_remote_code`` model — a functional
    regression, not a hardening. Those still pull, and still say so out loud.

    ``RZFZ_HF_REQUIRE_DIGEST`` overrides both ways:
      * ``1``/``true``/``all`` — EVERY file must carry a digest (the strict
        posture for a box that mirrors only LFS-complete repos; this is what
        closes the ``.py``-executes-unverified gap).
      * ``0``/``false`` — nothing is required; the pre-NODE-5 advisory
        behaviour, as an escape hatch for an oddly-published repo.
    """
    env = (os.environ.get("RZFZ_HF_REQUIRE_DIGEST") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on", "all", "strict"):
        return True
    return str(filename or "").lower().endswith(DIGEST_REQUIRED_SUFFIXES)


def _refuse_or_warn_unverified(filename: str, repo_id: str, revision: str) -> None:
    """One place for the NODE-5 decision, so both pull paths behave identically."""
    if require_digest_for(filename):
        raise UnverifiedPullRefused(
            f"refusing to pull {filename!r} from {repo_id}@{revision}: HuggingFace "
            f"published no sha256 for it and this node requires one for weight "
            f"files (set RZFZ_HF_REQUIRE_DIGEST=0 to accept unverified pulls)")
    _logger().warning("no HF digest for %s in %s@%s — pulling UNVERIFIED "
                      "(#356/NODE-5)", filename, repo_id, revision)


def store_name(filename: str, repo_id: str | None = None) -> str:
    """#2431: the name a file gets in the node's FLAT models volume. Weights keep
    their basename — every adopted, staged and registry-pulled volume relies on
    it. A projector is not identified by its basename (one F32 projector name
    ships in dozens of repos), so it is namespaced by its repository:
    ``mmproj--<org>--<repo>--<basename>``. Without a repository there is
    nothing to namespace by, and the basename stays.

    `core/llm/adopt_models.py::projector_store_name` is the same rule (the host
    tool cannot import this package); tests/unit/llm-node-agent/
    test_2431_projectors_are_keyed_by_repository.py pins the two equal."""
    base = os.path.basename(filename or "")
    if repo_id and "mmproj" in base.lower():
        return "mmproj--" + repo_id.strip("/").replace("/", "--") + "--" + base
    return base


def file_sha256(path: str, *, chunk: int = 1024 * 1024) -> str:
    """Streaming sha256 of an on-disk file (never reads a weight into RAM)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


class DigestPinMismatch(RuntimeError):
    """#2392: the bytes on offer are not the ones the catalogue approved.

    A ``RuntimeError`` on purpose: ``download_with_retries`` retries only
    transport errors, and a digest disagreement is never fixed by asking again.
    """


#: #2392: a successful hash is remembered next to the weight, so a 37 GB file
#: is hashed once, not at every launch. One line:
#:     <sha256> <st_dev> <st_ino> <st_size> <st_mtime_ns> <st_ctime_ns>
#: The five numbers are the identity of the INODE that was hashed, and the
#: sidecar vouches for nothing else: a file that is not exactly that inode, in
#: exactly that state, is hashed again. Timestamps alone vouched for a name and
#: a time window (D15 review I1: a replacement in the same tick, an in-place
#: rewrite put back with `touch -d`, a swap during the hash).
#: `core/llm/adopt_models.py` writes the same format after a hard link; the
#: two are held together by tests/unit/llm/test_2392_adoption_checks_the_pin.py.
SIDECAR_SUFFIX = ".rzfz-sha256"


class FileChangedWhileHashing(RuntimeError):
    """#2392: the file under the name changed while it was being hashed — the
    digest describes nothing that is still there, so it is void."""


def _sidecar(path: str) -> str:
    return path + SIDECAR_SUFFIX


def file_identity(st) -> tuple:
    """The five ``stat`` fields a sidecar binds to (exact equality)."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def write_sidecar(path: str, sha256: str, identity: tuple) -> None:
    """Record that the inode ``identity`` under ``path`` hashed to ``sha256``.
    Atomic: a temp file in the same directory, then a rename — a reader sees
    the old sidecar or the new one, never half of one."""
    side = _sidecar(path)
    tmp = f"{side}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "w", encoding="ascii") as fh:
        fh.write(" ".join([sha256, *(str(int(v)) for v in identity)]) + "\n")
    os.replace(tmp, side)


def bind_sidecar(path: str, sha256: str, identity: tuple | None) -> bool:
    """Write the sidecar for bytes verified on the way IN (a download or a
    registry blob, hashed as it was written) — bound to ``identity``, the
    inode recorded by the writer right after ITS rename (under the name's
    download lock), never to whatever a later ``stat`` finds (D15 review
    round 2). Written only while the name still holds that inode; a sidecar
    for another identity would never confirm anyway. Returns whether it wrote."""
    if identity is None:
        return False
    try:
        if file_identity(os.stat(path)) != tuple(identity):
            return False
    except OSError:
        return False
    write_sidecar(path, sha256, tuple(identity))
    return True


def sidecar_confirms(path: str, sha256: str) -> bool:
    """#2392: does a sidecar vouch for the file at ``path`` having ``sha256``?

    Cheap — one ``stat`` and a ~120-byte read, never a hash — so the report
    loop may ask it. True only when the digest AND all five identity fields
    match the file as it is now."""
    try:
        with open(_sidecar(path), encoding="ascii") as fh:
            parts = fh.read().split()
        if len(parts) != 6 or parts[0] != sha256:
            return False
        return tuple(int(v) for v in parts[1:]) == file_identity(os.stat(path))
    except (OSError, UnicodeDecodeError, ValueError):
        return False


def grant_engine_read(models_dir: str) -> dict:
    """#2393: make every weight at the mount root readable by the engine user.

    Engines run as nobody since #2393, and a registry pull wrote its weights
    0600 (``tempfile.mkstemp``) until the same change — so an upgraded box holds
    root-only GGUFs its next engine could not open. Adds ``g+r,o+r`` to a
    ``*.gguf`` that lacks ``o+r`` and ``o+x`` to the mount itself; widens
    nothing else, and never a set-aside (``.rejected-*``), a partial or a temp.

    chmod moves ``st_ctime``, which a pin sidecar binds to (#2392). A sidecar
    that vouched EXACTLY for the inode the scan saw is re-bound to the inode
    after the chmod — for every name linked to it — provided the chmod ran on
    that very inode (one ``O_NOFOLLOW`` fd, ``fstat`` = the scan's identity)
    and moved nothing but ctime (dev, ino, size, mtime_ns unchanged). A name
    swapped mid-sweep, a symlink or a rewrite gets no trusted sidecar; any
    other sidecar is left as it was.

    Runs at agent start, before the runtime arms; it does not rely on being
    the only writer (`adopt-models` is a separate container). Never
    raises. Returns ``{"files": n, "sidecars": n, "mount": bool}``."""
    import stat as _stat

    out = {"files": 0, "sidecars": 0, "mount": False}
    try:
        st = os.stat(models_dir)
        if not st.st_mode & _stat.S_IXOTH:
            os.chmod(models_dir, _stat.S_IMODE(st.st_mode) | 0o011)
            out["mount"] = True
        entries = list(os.scandir(models_dir))
    except OSError:
        return out            # no mount yet: never a reason not to come up
    by_inode: dict = {}
    for e in entries:
        if not e.name.lower().endswith(".gguf"):
            continue
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            est = e.stat(follow_symlinks=False)
        except OSError:
            continue
        by_inode.setdefault((est.st_dev, est.st_ino), (est, []))[1].append(e.path)
    for est, paths in by_inode.values():
        if est.st_mode & _stat.S_IROTH:
            continue
        pre = file_identity(est)
        vouched = []
        for path in paths:
            try:
                with open(_sidecar(path), encoding="ascii") as fh:
                    parts = fh.read().split()
                if len(parts) == 6 and tuple(int(v) for v in parts[1:]) == pre:
                    vouched.append((path, parts[0]))
            except (OSError, UnicodeDecodeError, ValueError):
                pass
        # D18 review I1: the name is resolved ONCE, without following a
        # symlink, and everything after works on that fd. The fd must hold the
        # inode the scan saw (a name swapped since is left alone), and a sidecar
        # is re-bound only if the chmod moved nothing but ctime.
        try:
            fd = os.open(paths[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            _logger().warning("could not make %s readable for the engine: %s", paths[0], exc)
            continue
        try:
            st_fd = os.fstat(fd)
            if not _stat.S_ISREG(st_fd.st_mode) or file_identity(st_fd) != pre:
                _logger().warning("%s changed during the #2393 sweep — left as it is", paths[0])
                continue
            os.fchmod(fd, _stat.S_IMODE(st_fd.st_mode) | 0o044)
            post = file_identity(os.fstat(fd))
        except OSError as exc:
            _logger().warning("could not make %s readable for the engine: %s", paths[0], exc)
            continue
        finally:
            os.close(fd)
        if post[:4] != pre[:4]:          # dev, ino, size, mtime_ns: the bytes moved
            vouched = []
        out["files"] += 1
        for path, sha in vouched:
            try:
                write_sidecar(path, sha, post)
                out["sidecars"] += 1
            except OSError:
                pass
        _logger().warning("made %s readable for the non-root engine (was %o) (#2393)",
                          ", ".join(paths), _stat.S_IMODE(est.st_mode))
    return out


def _hash_fd(fh) -> str:
    h = hashlib.sha256()
    while True:
        buf = fh.read(1024 * 1024)
        if not buf:
            break
        h.update(buf)
    return h.hexdigest()


def hash_stable(path: str) -> tuple:
    """``(sha256, identity)`` of the file at ``path``, read through ONE fd.

    The identity is taken from that fd before and after the hash, and the name
    must still point at it afterwards; any difference voids the hash
    (``FileChangedWhileHashing``) — nothing may be written about bytes that are
    no longer the ones under the name."""
    with open(path, "rb") as fh:
        before = file_identity(os.fstat(fh.fileno()))
        digest = _hash_fd(fh)
        after = file_identity(os.fstat(fh.fileno()))
    if before != after or file_identity(os.stat(path)) != before:
        raise FileChangedWhileHashing(
            f"{path} changed while it was being hashed — the digest is void (#2392)")
    return digest, before


def _hash_retrying(path: str, attempts: int = 3) -> tuple:
    for i in range(attempts):
        try:
            return hash_stable(path)
        except FileChangedWhileHashing:
            if i == attempts - 1:
                raise
    raise AssertionError("unreachable")


_VERIFY_LOCKS: dict = {}
_VERIFY_LOCKS_GUARD = threading.Lock()


def _verify_lock(path: str):
    """One verification per file name at a time: two loads sharing a weight
    hash it once, and one never quarantines what the other just approved."""
    with _VERIFY_LOCKS_GUARD:
        lock = _VERIFY_LOCKS.get(path)
        if lock is None:
            lock = _VERIFY_LOCKS[path] = threading.RLock()
        return lock


def check_against(path: str, sha256: str) -> tuple:
    """``(ok, digest, identity)``: the sidecar's word when it confirms (digest
    = the pin, identity None), else a stable hash — a match is remembered."""
    if sidecar_confirms(path, sha256):
        return True, sha256, None
    digest, ident = _hash_retrying(path)
    if digest == sha256:
        write_sidecar(path, sha256, ident)
        return True, digest, ident
    return False, digest, ident


def verified_against(path: str, sha256: str) -> bool:
    """#2392: does the file at ``path`` have the pinned digest? HASHES a file
    with no confirming sidecar — call it from a background thread, never the
    report loop."""
    with _verify_lock(path):
        return check_against(path, sha256)[0]


def set_aside(path: str, sha256: str | None = None, *, got: str | None = None,
              identity: tuple | None = None) -> str | None:
    """#2392: move a weight whose bytes are not the approved ones out of the
    way — ``<name>.rejected-<its sha256[:12]>`` next to it — so the engine can
    never load it and the evidence survives. Never deletes.

    ``got``/``identity`` are a digest the caller already computed and the inode
    it describes; they are reused only while the name still points at exactly
    that inode, otherwise the file is hashed again. If the bytes under the
    name ARE the pin (another load fetched them meanwhile — D15 review I2),
    nothing moves: the sidecar is written and None returned. Else returns the
    new path."""
    # Lock order: _verify_lock, then _dest_lock — never the reverse (a
    # download holds only _dest_lock; ensure_file releases _verify_lock before
    # it downloads). The hash runs OUTSIDE _dest_lock; the identity check and
    # the move run INSIDE it, so a download's rename can never land between
    # "these are the bad bytes" and "move them" (D15 review round 2).
    with _verify_lock(path):
        for _ in range(3):
            if got is None or identity is None:
                got, identity = _hash_retrying(path)
            with _dest_lock(path):
                if file_identity(os.stat(path)) != identity:
                    got = identity = None          # changed under us: judge again
                    continue
                if sha256 and got == sha256:
                    write_sidecar(path, sha256, identity)
                    _logger().info("%s already holds the catalogue's %s… — left in "
                                   "place (#2392)", path, sha256[:12])
                    return None
                aside = f"{path}.rejected-{got[:12]}"
                os.replace(path, aside)
                try:
                    os.unlink(_sidecar(path))
                except OSError:
                    pass
            _logger().warning("%s is sha256 %s…, not the catalogue's %s… — moved to %s (#2392)",
                              path, got[:12], (sha256 or "?")[:12], aside)
            return aside
    raise FileChangedWhileHashing(
        f"{path} kept changing while it was being set aside — left in place (#2392)")


def verify_or_set_aside(path: str, sha256: str) -> tuple:
    """``(ok, aside)``: verify the file at ``path`` against its pin, and set a
    mismatch aside reusing the digest just computed (one read, not two).
    ``aside`` is the quarantine path, or None when nothing moved."""
    with _verify_lock(path):
        ok, got, ident = check_against(path, sha256)
        if ok:
            return True, None
        aside = set_aside(path, sha256, got=got, identity=ident)
        return aside is None, aside


def ensure_file(
    models_dir: str, filename: str, repo_id: str, *,
    http_stream, progress_cb=None, revision: str = "main", list_files=None,
    list_entries=None, cancel_cb=None, catalogue_sha256: str | None = None,
    catalogue_pins: dict | None = None,
) -> str:
    """Ensure ``filename`` is present in ``models_dir``; fetch it from HF if not.

    #2392: ``catalogue_sha256`` is the digest the catalogue approved for this
    file (``standard-models.yaml``'s per-file pin). With it, a present file
    counts as cached only when its bytes match (a truncated blob is moved aside
    to ``<name>.rejected-<sha12>`` — never deleted — and fetched again), a hub
    listing that names a different digest is refused (``DigestPinMismatch``),
    and the download is verified against the pin. ``catalogue_pins``
    ({basename: hex}) does the same for every file a GLOB resolves to.

    Returns "cached" (everything already present) or "pulled". Propagates any
    download error — the caller must NOT launch an engine on a missing weight.

    #360: a GLOB filename is resolved against the repo listing rather than
    refused. ``list_files`` defaults to the HF API and is injectable for tests.
    Every match is fetched: a sharded GGUF is several files, and an engine
    launched on a subset fails in a far more confusing way than one that never
    launched. A glob that matches nothing still raises — an empty match is not a
    successful pull.

    Destinations are the BASENAME of each match, because a catalog pattern may
    carry a repo subfolder while the engine expects the weights at the mount
    root (#303) — the same flattening ``missing_files`` already assumes.
    """
    if is_glob(filename):
        # #356: one listing serves BOTH glob resolution and digest lookup.
        entries = list_entries(repo_id, revision) if list_entries else None
        lf = (list_files or
              ((lambda r, rev: [e.get("path") for e in entries]) if entries is not None
               else list_repo_files))
        resolved = resolve_glob(repo_id, filename, list_files=lf, revision=revision)
        le = (lambda r, rev: entries) if entries is not None else None
        if catalogue_pins:
            # #2392: a shard the catalogue does not pin is not approved bytes —
            # refused before anything is fetched.
            unpinned = [n for n in resolved if os.path.basename(n) not in catalogue_pins]
            if unpinned:
                raise DigestPinMismatch(
                    f"refusing {filename!r} from {repo_id}@{revision}: "
                    f"{', '.join(os.path.basename(n) for n in unpinned)} is not in the "
                    "catalogue pins (#2392)")
        results = [
            ensure_file(models_dir, name, repo_id, http_stream=http_stream,
                        progress_cb=progress_cb, revision=revision,
                        list_entries=le, cancel_cb=cancel_cb,
                        catalogue_sha256=(catalogue_pins or {}).get(os.path.basename(name)))
            for name in resolved
        ]
        return "cached" if all(r == "cached" for r in results) else "pulled"

    # #2431: a projector lives under its repository's store name
    dest = os.path.join(models_dir, store_name(filename, repo_id))
    if os.path.exists(dest):
        if not catalogue_sha256:
            return "cached"
        # #2392: the volume holds a file under this name whose bytes may not be
        # the approved ones (a test box: two truncated gemma4 blobs). A mismatch
        # is set aside — never delete evidence — and the approved one fetched.
        ok, _aside = verify_or_set_aside(dest, catalogue_sha256)
        if ok:
            return "cached"
    # #356: verify against the sha256 HF publishes in its tree listing (lfs.oid)
    # whenever the caller supplies a lister. None → unverified pull, said out
    # loud — silence here is how corrupt weights become engine crash-loops.
    expected = None
    if list_entries is not None:
        try:
            expected = digest_for(list_entries(repo_id, revision), filename)
        except Exception as exc:  # noqa: BLE001 - listing failure ≠ pull failure
            _logger().warning("HF listing unavailable for %s@%s (%s) — no digest "
                              "for %s (#356)", repo_id, revision, exc, filename)
    if catalogue_sha256 and expected and expected != catalogue_sha256:
        raise DigestPinMismatch(
            f"refusing {filename!r} from {repo_id}@{revision}: the hub lists sha256 "
            f"{expected[:12]}… but the catalogue approved {catalogue_sha256[:12]}… (#2392)")
    if catalogue_sha256:
        # #2392: the catalogue pin IS a digest — NODE-5's "no published digest"
        # refusal does not apply, and the download is checked against the pin.
        expected = catalogue_sha256
    elif list_entries is not None and expected is None:
        # NODE-5: policy decision, not a log line. A weight format with no
        # published digest is refused; the rest pulls with the warning.
        _refuse_or_warn_unverified(filename, repo_id, revision)
    placed: dict = {}
    download_with_retries(
        hf_file_url(repo_id, filename, revision), dest,
        http_stream=http_stream, progress_cb=progress_cb,
        expected_sha256=expected, cancel_cb=cancel_cb,
        **({"identity_out": placed} if catalogue_sha256 else {}),
    )
    if catalogue_sha256:
        # the stream was hashed against the pin as it was written; bind the
        # sidecar to the inode the download itself placed (not a later stat)
        bind_sidecar(dest, catalogue_sha256, placed.get(dest))
    return "pulled"


def missing_files(models_dir: str, files: list[str], repo_id: str | None = None) -> list[str]:
    """The subset of ``files`` NOT already in the volume under their store
    name (#2431: another repository's flat projector is not this one's)."""
    return [f for f in (files or [])
            if f and not os.path.exists(os.path.join(models_dir, store_name(f, repo_id)))]


def pending_verification(models_dir: str, files, pins_sha256: dict,
                         repo_id: str | None = None) -> list[str]:
    """#2392/#2431: the declared files that are present under their STORE
    name, pinned, and not yet proven — no sidecar next to the store name that
    ``sidecar_confirms`` (exact digest and inode identity). They are hashed in
    the pull thread before launch; they are not "missing" for the offline
    refusal. Cheap (a stat and a tiny read per file): safe on the report loop."""
    pins_sha256 = pins_sha256 or {}
    out = []
    for f in files or []:
        pin = pins_sha256.get(os.path.basename(f or ""))
        if not f or not pin:
            continue
        dest = os.path.join(models_dir, store_name(f, repo_id))
        if os.path.exists(dest) and not sidecar_confirms(dest, pin):
            out.append(f)
    return out


def flat_projector_waiting(models_dir: str, filename: str, repo_id: str | None) -> bool:
    """#2431: is there a legacy flat projector that ``adopt_flat_projector``
    could link to this repository's store name (the store name is free, the
    flat name holds a file)? Says nothing about its bytes."""
    target = os.path.join(models_dir, store_name(filename, repo_id))
    flat = os.path.join(models_dir, os.path.basename(filename or ""))
    return target != flat and not os.path.lexists(target) and os.path.isfile(flat)


def adopt_flat_projector(models_dir: str, filename: str, repo_id: str | None,
                         sha256: str | None, *, may_hash: bool = True) -> bool:
    """#2431 migration: a volume from before the namespacing holds projectors
    under their bare name. Hard-link one to its store name only when its bytes
    are the catalogue-pinned ones — never on the name alone (that is the bug).

    The flat file is judged by its own sidecar when that confirms the pin
    (cheap), else hashed — unless ``may_hash`` is False (the report loop,
    D15: never hash there), in which case nothing happens. The identity that
    was judged is re-checked under the flat name's download lock right before
    the link, and the new name gets a sidecar bound to that inode as it is
    after the link (the link moves ctime). Never moves, rewrites or deletes
    the flat file.

    Returns True ONLY when the store name holds exactly the inode that was
    judged and a sidecar bound to it is in place (D16 review I2). Every other
    outcome is False — including a link that landed other bytes, because a
    writer renamed over the flat name inside the window: the caller then
    finds the store name present and judges it on its bytes.

    With ``may_hash`` False the call never waits (D16 review I3): a lock held
    by another thread — a pull thread hashing this projector, or a download
    into the flat name — answers False at once ("pending")."""
    if not sha256:
        return False
    target = os.path.join(models_dir, store_name(filename, repo_id))
    flat = os.path.join(models_dir, os.path.basename(filename or ""))
    if target == flat:
        return False
    vlock = _verify_lock(target)
    if not vlock.acquire(blocking=may_hash):
        return False
    try:
        if not flat_projector_waiting(models_dir, filename, repo_id):
            return False
        try:
            before = file_identity(os.stat(flat))
            if sidecar_confirms(flat, sha256) and file_identity(os.stat(flat)) == before:
                got, ident = sha256, before
            elif not may_hash:
                return False
            else:
                got, ident = _hash_retrying(flat)
        except (OSError, FileChangedWhileHashing) as exc:
            _logger().warning("flat projector %s could not be judged (%s) — not "
                              "adopted (#2431)", flat, exc)
            return False
        if got != sha256:
            _logger().warning("flat projector %s is sha256 %s…, not %s's pinned %s… — "
                              "not adopted (#2431)", flat, got[:12], repo_id, sha256[:12])
            return False
        # Lock order: _verify_lock(target) → _dest_lock(flat). Writers into the
        # flat name (download_stream, puller.pull_blob) hold only _dest_lock.
        dlock = _dest_lock(flat)
        if not dlock.acquire(blocking=may_hash):
            return False
        try:
            if file_identity(os.stat(flat)) != tuple(ident):
                return False            # changed since it was judged
            os.link(flat, target)       # atomic; never replaces an existing name
        except OSError:
            return False
        finally:
            dlock.release()
        st = os.stat(target)
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != tuple(ident)[:4]:
            _logger().warning("%s does not hold the inode that matched %s's pin — "
                              "left for verification (#2431)", target, repo_id)
            return False
        write_sidecar(target, sha256, file_identity(st))
        if not sidecar_confirms(target, sha256):
            return False
        _logger().info("flat projector %s matches %s's pin — linked as %s (#2431)",
                       flat, repo_id, os.path.basename(target))
        return True
    finally:
        vlock.release()


# ── #574: full-repo pulls for directory-serving engines (vLLM) ───────────────

#: Housekeeping files a serving directory does not need. Everything else is
#: mirrored — config.json / tokenizer / *.safetensors are all load-bearing and
#: an allow-list of "known needed" names would break on the next repo layout.
_REPO_SKIP_SUFFIXES = (".md", ".gitattributes")
_REPO_SKIP_PREFIXES = ("LICENSE", ".git")


def _repo_dir_wanted(path: str) -> bool:
    base = os.path.basename(path)
    if base.startswith(_REPO_SKIP_PREFIXES):
        return False
    return not base.lower().endswith(_REPO_SKIP_SUFFIXES)


def _safe_subpath(root: str, rel: str) -> str:
    """``root/rel`` with traversal refused. Entry paths come from the HF API —
    third-party data — so absolute paths, drive-ish prefixes and ``..``
    components must die here, not land under /models."""
    rel = (rel or "").replace("\\", "/")
    if rel.startswith("/") or ":" in rel.split("/", 1)[0]:
        raise ValueError(f"unsafe repo path {rel!r}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"unsafe repo path {rel!r}")
    return os.path.join(root, *parts)


def safe_repo_root(models_dir: str, model_name: str) -> str:
    """``models_dir/model_name`` with containment enforced (#355 on the repo-dir
    path).

    ``model_name`` is MANAGER-supplied (``payload.model`` off the command
    channel), and ``_safe_subpath`` below only sanitises the HF entry paths
    joined UNDER this root — the root itself was taken on trust, so
    ``model="../app"`` wrote a whole HF repo over the worker-agent's own source
    (the container runs as root). Same containment posture as
    ``runtime.delete_cached_weight``: normalise, then require the result to sit
    strictly INSIDE the mount. Strictly: a repo dir is a subdirectory, so
    ``"."``/``""`` (the mount itself) is refused too.
    """
    base = os.path.normpath(models_dir)
    root = os.path.normpath(os.path.join(base, str(model_name or "")))
    if root == base or not root.startswith(base + os.sep):
        raise ValueError(
            f"refusing repo-dir pull for model {model_name!r}: destination "
            f"escapes the models mount {models_dir!r}")
    return root


def _repo_file_complete(dest: str, sz: int, entries: list[dict], path: str) -> bool:
    """Is the file already at ``dest`` the complete listed file? (NODE-17)

    A LISTED size that matches is completeness: ``download_stream`` writes
    atomically (temp file + ``os.replace``), so a partial file never exists at
    the destination.

    A listing entry with NEITHER ``lfs.size`` NOR ``size`` used to make mere
    PRESENCE count as complete — which trusts whatever a previous layout, a
    different revision, or an out-of-band volume seeding (the air-gap story
    explicitly supports that) happened to leave at that path. Now: verify
    against the published digest when there is one, and otherwise re-fetch. The
    re-fetch only ever touches small non-LFS files — every real weight publishes
    an ``lfs.size``.
    """
    if sz:
        try:
            return os.path.getsize(dest) == sz
        except OSError:  # pragma: no cover - raced deletion
            return False
    expected = digest_for(entries, path)
    if not expected:
        return False
    try:
        return file_sha256(dest) == expected
    except OSError:  # pragma: no cover - unreadable → re-fetch
        return False


def ensure_repo_dir(
    models_dir: str, model_name: str, repo_id: str, *,
    http_stream, progress_cb=None, revision: str = "main", list_entries=None,
    cancel_cb=None,
) -> str:
    """Materialise the WHOLE HF repo as ``models_dir/model_name/…`` (#574).

    vLLM serves ``--model <dir>`` — an HF-format directory (safetensors +
    config + tokenizer), not a flat weight file, so the per-file ensure_file
    path cannot feed it. One tree listing (the #356 one, digests included)
    drives everything: the file set, the summed-size space preflight, and
    per-file sha256 verification. Per-file resume: a file already present at
    its listed size is kept (digest was verified when it was written; hashing
    tens of GB on every load would make restarts minutes-long). A file at its
    LISTED size is complete because download_stream writes atomically (temp file
    + os.replace) - a partial file never exists at the destination (#580
    review). A file whose entry publishes NO size is checked against its digest
    or re-fetched, never trusted on presence alone (NODE-17) - see
    ``_repo_file_complete``.

    Returns "cached" (nothing fetched) or "pulled". Raises on a missing
    listing — unlike single-file pulls there is no filename to fall back on;
    a directory serve needs the manifest.
    """
    # Containment FIRST: refuse a traversing destination before a single byte is
    # fetched (and before the listing call), not somewhere down in the write loop.
    root = safe_repo_root(models_dir, model_name)
    entries = (list_entries or list_repo_entries)(repo_id, revision)
    wanted = [e for e in entries if _repo_dir_wanted(e.get("path") or "")]
    if not wanted:
        raise ValueError(f"{repo_id}@{revision}: listing has no serveable files")

    def _size(e):
        return int(((e.get("lfs") or {}).get("size")) or e.get("size") or 0)

    todo = []
    for e in wanted:
        dest = _safe_subpath(root, e["path"])
        sz = _size(e)
        if os.path.exists(dest) and _repo_file_complete(dest, sz, entries, e["path"]):
            continue
        todo.append((e, dest, sz))
    if not todo:
        return "cached"

    check_space_for(models_dir, sum(sz for _, _, sz in todo))
    total_all = sum(sz for _, _, sz in todo) or None
    done_bytes = 0
    for e, dest, sz in todo:
        expected = digest_for(entries, e["path"])
        if expected is None:
            # NODE-5: same policy as the single-file path — a weight format with
            # no published digest refuses the whole repo pull rather than
            # mirroring unverified bytes into the serving directory.
            _refuse_or_warn_unverified(e["path"], repo_id, revision)

        def _cb(got, _t, _base=done_bytes):
            if progress_cb and total_all:
                progress_cb(_base + got, total_all)

        download_with_retries(
            hf_file_url(repo_id, e["path"], revision), dest,
            http_stream=http_stream, progress_cb=_cb, expected_sha256=expected,
            cancel_cb=cancel_cb,
        )
        done_bytes += sz
    return "pulled"


def repo_dir_present(models_dir: str, model_name: str) -> bool:
    """A cheap 'is anything there' probe for the load path — the authoritative
    completeness check is ensure_repo_dir's per-file pass."""
    try:
        root = safe_repo_root(models_dir, model_name)
    except ValueError:
        # A traversing name has nothing PRESENT under the mount by definition —
        # answering False sends the caller down the pull path, where
        # ensure_repo_dir refuses loudly. A probe never raises.
        return False
    try:
        return any(os.scandir(root))
    except OSError:
        return False
