# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Push model artifacts (GGUF files) INTO the in-stack Zot registry (#307).

The MASTER mirrors a model from HuggingFace into Zot **once**; every worker then
pulls it from Zot (``puller.py``) over the LAN instead of re-fetching from HF.
This module is the push half — the inverse of ``puller.py``:

    puller.py : Zot  /v2/<repo>/blobs/<digest>  → local file   (GET, by digest)
    pusher.py : local file → Zot /v2/<repo>/blobs/<digest>      (POST+PUT)  + manifest

Each file becomes a blob (monolithic upload); an OCI image manifest lists them as
layers (title annotation = filename) over a tiny empty config, so Zot lists the
repo in ``/v2/_catalog`` and garbage-collects the blobs as a unit. The resulting
``files=[{"name","digest","size"}]`` list is exactly what ``puller.pull_artifact``
consumes, so the two halves round-trip.

Content-addressed + idempotent: a blob already present (HEAD 200) is skipped, so
re-mirroring the same model is cheap and safe to resume.

Import-safe: stdlib only. All HTTP is injected via a ``request(method, url, *,
data, headers) -> Resp`` callable (``Resp`` has ``.status`` and ``.headers``) so
this unit-tests with no network and no running registry.
"""
from __future__ import annotations

import hashlib
import json
import os

# Custom layer media type for a GGUF weight file. Zot stores any media type; the
# puller keys purely off digests, so this is just a descriptive label + lets the
# manifest be a valid OCI image manifest.
GGUF_MEDIA_TYPE = "application/vnd.rzfz.model.gguf.v1"
_EMPTY_CONFIG = b"{}"
_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


class PushError(Exception):
    """A registry push step returned an unexpected status."""


def _sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return "sha256:" + h.hexdigest(), size


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ok(status: int, *allowed: int) -> bool:
    return status in allowed


def blob_exists(request, base: str, repo: str, digest: str) -> bool:
    r = request("HEAD", f"{base.rstrip('/')}/v2/{repo}/blobs/{digest}")
    return _ok(r.status, 200)


# OCI chunked-upload chunk size. Bounded well under the worker-agent's 512 MB
# mem_limit so one PATCH never buffers more than this from a multi-GB weight —
# the monolithic PUT this replaced streamed from disk but still issued ONE
# multi-GB request, which Zot reset on 4 GB+ blobs, leaving an empty manifest
# (#1001). Chunking makes each request small, so any single failure is a chunk,
# not the whole 34 GB transfer.
_UPLOAD_CHUNK = 64 * 1024 * 1024  # 64 MiB


def _abs_upload_url(base: str, location: str) -> str:
    """Resolve a registry upload Location (absolute or path-relative) to a URL."""
    if location.startswith("http"):
        return location
    return f"{base.rstrip('/')}{location if location.startswith('/') else '/' + location}"


def _with_digest(url: str, digest: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}digest={digest}"


def push_blob(request, base: str, repo: str, digest: str, *,
              data: bytes = None, filepath: str = None, size: int = None) -> str:
    """Upload one blob by digest. Returns "cached" if already present (HEAD 200),
    else "pushed".

    Small in-memory blobs (the manifest config) pass ``data`` and go up in ONE
    monolithic PUT. Big weight files pass ``filepath`` (+ ``size``) and go up via
    the OCI CHUNKED protocol (POST → PATCH byte-ranges → PUT close), reading at
    most ``_UPLOAD_CHUNK`` from disk at a time. Chunking is what makes multi-GB
    weights push reliably: the former single multi-GB PUT was reset by the
    registry on 4 GB+ blobs, leaving an empty manifest (#1001)."""
    if blob_exists(request, base, repo, digest):
        return "cached"
    start = request("POST", f"{base.rstrip('/')}/v2/{repo}/blobs/uploads/")
    if not _ok(start.status, 202):
        raise PushError(f"open upload for {repo} failed: HTTP {start.status}")
    location = start.headers.get("Location") or start.headers.get("location")
    if not location:
        raise PushError(f"upload session for {repo} returned no Location header")

    if filepath is None:
        # Small in-memory blob (config): one monolithic PUT.
        body = data or b""
        put_url = _with_digest(_abs_upload_url(base, location), digest)
        r = request("PUT", put_url, data=body, size=len(body),
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(len(body))})
        if not _ok(r.status, 201, 202):
            raise PushError(f"PUT blob {digest} to {repo} failed: HTTP {r.status}")
        return "pushed"

    # Big weight: streamed chunked upload. Never buffer more than one chunk; each
    # PATCH sends [offset, end] and the registry hands back the next upload URL.
    upload_url = _abs_upload_url(base, location)
    offset = 0
    with open(filepath, "rb") as fh:
        while True:
            chunk = fh.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            r = request("PATCH", upload_url, data=chunk, size=len(chunk),
                        headers={"Content-Type": "application/octet-stream",
                                 "Content-Range": f"{offset}-{end}",
                                 "Content-Length": str(len(chunk))})
            if not _ok(r.status, 202):
                raise PushError(
                    f"PATCH chunk {offset}-{end} of {digest} to {repo} "
                    f"failed: HTTP {r.status}")
            nxt = r.headers.get("Location") or r.headers.get("location")
            if nxt:
                upload_url = _abs_upload_url(base, nxt)
            offset = end + 1
    # Close: zero-body PUT that names the full-blob digest, finalizing the upload.
    close_url = _with_digest(upload_url, digest)
    r = request("PUT", close_url, data=b"", size=0,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Length": "0"})
    if not _ok(r.status, 201, 202):
        raise PushError(f"close upload {digest} to {repo} failed: HTTP {r.status}")
    return "pushed"


def build_manifest(config_digest: str, layers: list[dict]) -> dict:
    """OCI image manifest: an empty config + one layer per file, each carrying
    its filename in the standard title annotation."""
    return {
        "schemaVersion": 2,
        "mediaType": _MANIFEST_MEDIA_TYPE,
        "config": {"mediaType": _CONFIG_MEDIA_TYPE, "digest": config_digest,
                   "size": len(_EMPTY_CONFIG)},
        "layers": [
            {"mediaType": GGUF_MEDIA_TYPE, "digest": ly["digest"], "size": ly["size"],
             "annotations": {"org.opencontainers.image.title": ly["name"]}}
            for ly in layers
        ],
    }


def push_artifact(request, base: str, repo: str, tag: str, files: list[dict]) -> dict:
    """Mirror a local artifact into Zot. ``files`` = [{"name","path"}] (local
    paths). Pushes each file as a blob + a manifest tagged ``tag``. Returns
    ``{"repo","tag","files":[{"name","digest","size"}],"pushed":N,"cached":M}`` —
    the ``files`` list is directly consumable by ``puller.pull_artifact``.

    Idempotent: unchanged blobs (same digest) are skipped; re-pushing the
    manifest is harmless (same content → same digest).

    NODE-8: an EMPTY ``files`` list is refused here rather than published. It
    would otherwise PUT a valid manifest with ``"layers": []`` — the registry
    then advertises a cached model whose pull yields nothing, which is the #1001
    empty-manifest failure state arrived at deliberately. Callers that legitimately
    have nothing to push (a repo-dir deploy) skip the call; anything else reaching
    here with no files is a bug, and a loud one is cheaper than a hollow cache
    entry."""
    if not files:
        raise PushError(
            f"refusing to push an EMPTY artifact to {repo}:{tag} — a manifest "
            f"with no layers advertises a cached model that contains nothing "
            f"(NODE-8/#1001)")
    layers: list[dict] = []
    pushed = cached = 0
    for f in files:
        digest, size = _sha256_file(f["path"])   # streaming hash — no full read
        state = push_blob(request, base, repo, digest, filepath=f["path"], size=size)
        pushed += state == "pushed"
        cached += state == "cached"
        layers.append({"name": f["name"], "digest": digest, "size": size})

    # config blob (empty JSON) — required for a valid OCI image manifest
    cfg_digest = _sha256_bytes(_EMPTY_CONFIG)
    push_blob(request, base, repo, cfg_digest, data=_EMPTY_CONFIG)

    manifest = build_manifest(cfg_digest, layers)
    body = json.dumps(manifest, separators=(",", ":")).encode()
    r = request("PUT", f"{base.rstrip('/')}/v2/{repo}/manifests/{tag}",
                data=body, headers={"Content-Type": _MANIFEST_MEDIA_TYPE})
    if not _ok(r.status, 201):
        raise PushError(f"PUT manifest {repo}:{tag} failed: HTTP {r.status}")
    return {"repo": repo, "tag": tag,
            "files": [{"name": ly["name"], "digest": ly["digest"], "size": ly["size"]}
                      for ly in layers],
            "pushed": pushed, "cached": cached}
