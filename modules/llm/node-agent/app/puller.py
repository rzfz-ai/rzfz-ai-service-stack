# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Pull model artifacts by digest from the in-stack Zot registry (#254 P2-D2).

The manager records a model's artifact files by OCI digest; a node pulls those
blobs BY DIGEST from ``llm-registry:5000`` into its models volume, with sha256
verification + atomic write. Idempotent: a blob already present with the right
bytes is skipped (content-addressed → safe resume across restarts).

OFFLINE-SAFE: this puller only ever talks to the given in-stack registry base —
never HuggingFace or any WAN host. (The engine containers additionally run with
HF_HUB_OFFLINE so their own libraries don't reach out; that's engine-side.)

Import-safe: stdlib only; the HTTP fetch is injected so it unit-tests with no
network.
"""
from __future__ import annotations

import hashlib
import os
import tempfile


class DigestMismatch(Exception):
    """The fetched bytes don't hash to the requested digest — refuse to keep them."""


def _digest_hex(digest: str) -> str:
    """'sha256:abcd…' → 'abcd…' (accepts a bare hex too)."""
    return digest.split(":", 1)[1] if ":" in digest else digest


def blob_url(registry_base: str, repo: str, digest: str) -> str:
    return f"{registry_base.rstrip('/')}/v2/{repo}/blobs/{digest}"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pull_blob(registry_base: str, repo: str, digest: str, dest_path: str, *,
              fetch=None, fetch_stream=None, identity_out: dict | None = None) -> str:
    """Pull one blob by digest → ``dest_path`` (sha256-verified, atomic).

    Returns "cached" if the destination already holds the correct bytes (no
    fetch), else "pulled". Raises DigestMismatch on a hash mismatch, leaving no
    partial file behind.

    Pass EXACTLY ONE of:
      ``fetch(url) -> bytes``                  small blobs (manifests, configs)
      ``fetch_stream(url) -> Iterable[bytes]`` weights

    #354: a model artifact is a SINGLE file of tens of GB — qwen3.6 Q8_0 is
    36.9 GB — against a node host-RAM budget of ~16 GB (#419 resource model).
    Reading it into a `bytes` first is not a slow path, it is an OOM, and no
    plausible node size fixes it. The streaming path hashes and writes as bytes
    arrive and never holds the artifact whole. ``pusher.push_blob`` already
    streams the same data in the opposite direction for the same reason; this
    mirrors its shape rather than inventing a second convention.
    """
    if (fetch is None) == (fetch_stream is None):
        raise ValueError("pull_blob: pass exactly one of fetch= or fetch_stream=")

    from app.hf_pull import FileChangedWhileHashing, _dest_lock, file_identity, hash_stable

    want = _digest_hex(digest)
    if os.path.exists(dest_path):
        # #2392: hashed through one fd with its identity, so a caller binding a
        # pin sidecar binds it to the inode that was actually hashed.
        try:
            got_cached, ident = hash_stable(dest_path)
        except FileChangedWhileHashing:
            got_cached, ident = None, None
        if got_cached == want:
            if identity_out is not None:
                identity_out[dest_path] = ident
            return "cached"  # content-addressed resume: already have it

    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    url = blob_url(registry_base, repo, digest)
    hasher = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=dest_dir)
    try:
        # #2393: mkstemp creates 0600 whatever the umask, and the engine that
        # reads this weight runs as nobody. 0644 like every other writer (HF
        # pulls, adoption) — set before the bytes and before the identity a pin
        # sidecar binds to, so the sidecar describes the final inode state.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as fh:
            if fetch_stream is not None:
                for chunk in fetch_stream(url):
                    if not chunk:
                        continue
                    hasher.update(chunk)
                    fh.write(chunk)
            else:
                data = fetch(url)
                hasher.update(data)
                fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        got = hasher.hexdigest()
        if got != want:
            # Verify BEFORE publishing. A rename-then-check would expose corrupt
            # weights to an engine that is already loading them.
            raise DigestMismatch(f"{digest}: fetched bytes are sha256:{got}")
        # D16 review I2: the rename takes the name's download lock, like every
        # other in-process writer, so a reader judging this name under that
        # lock (hf_pull.set_aside, hf_pull.adopt_flat_projector) never has
        # bytes renamed in between its check and its act. Only _dest_lock is
        # held here — never _verify_lock — so the lock order has no inversion.
        with _dest_lock(dest_path):
            os.replace(tmp, dest_path)  # atomic within the same dir
            tmp = None
            if identity_out is not None:
                # #2392: the inode this pull placed, read right after its rename
                identity_out[dest_path] = file_identity(os.stat(dest_path))
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return "pulled"


class UnsafeArtifactName(Exception):
    """An artifact filename tried to escape the models directory (#355)."""


def safe_artifact_name(raw: str) -> str:
    """Reduce a manager-supplied artifact filename to a safe leaf name (#355).

    ``pull_artifact`` joins this straight onto the models mount, and the value
    arrives from the MANAGER over the command channel — so an entry of
    ``../../etc/cron.d/x`` would write outside the mount as whatever uid the
    worker-agent runs as.

    Two different inputs, two different answers, deliberately:

    * a plain nested name (``sub/model-00001.gguf``) is FLATTENED to its
      basename. HuggingFace repos legitimately carry subfolders and the engine
      expects the file at the mount root (#303), so this is a layout
      difference, not an attack — flattening is the compatible behaviour.
    * a ``..`` segment is an escape ATTEMPT, not a layout. It is refused loudly
      rather than silently flattened, because ``os.path.basename('../../x')``
      is ``'x'`` — quietly "fixing" it would hide the fact that a node was sent
      a hostile command, which is the part worth alerting on.

    This became reachable with #353: before the Zot pull path was wired,
    ``puller.py`` had no production caller and the join was unreachable.
    """
    norm = (raw or "").replace("\\", "/")
    if not norm.strip():
        raise UnsafeArtifactName("artifact file name is empty")
    if any(part == ".." for part in norm.split("/")):
        raise UnsafeArtifactName(
            f"refusing artifact file name with a '..' segment: {raw!r}")
    leaf = os.path.basename(norm)
    if not leaf or leaf in (".", ".."):
        raise UnsafeArtifactName(f"artifact file name has no usable leaf: {raw!r}")
    if os.path.isabs(norm):
        # an absolute path is the same escape by another spelling
        raise UnsafeArtifactName(f"refusing absolute artifact file name: {raw!r}")
    return leaf


def pull_artifact(registry_base: str, repo: str, files: list[dict], dest_dir: str, *,
                  fetch=None, fetch_stream=None, identities: dict | None = None) -> dict:
    """Pull every file of an artifact. ``files`` = [{"name", "digest"}]. Returns
    {name: "pulled"|"cached"}. An entry may carry ``dest_name`` (#2431). A
    DigestMismatch on any file propagates (the caller must not launch an
    engine on a partial artifact).

    #355: names are manager-supplied, so each is reduced to a safe leaf before
    being joined onto ``dest_dir`` — see ``safe_artifact_name``.
    """
    result: dict[str, str] = {}
    for f in files:
        # #2431: ``dest_name`` is the file's store name on the flat volume (a
        # projector is keyed by its repository); the blob is still named by
        # ``name`` in the registry. Both are reduced to a safe leaf (#355).
        leaf = safe_artifact_name(f.get("dest_name") or f["name"])
        dest = os.path.join(dest_dir, leaf)
        result[f["name"]] = pull_blob(registry_base, repo, f["digest"], dest,
                                      fetch=fetch, fetch_stream=fetch_stream,
                                      identity_out=identities)
    return result
