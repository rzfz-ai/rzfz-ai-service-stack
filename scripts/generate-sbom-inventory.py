#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Offline SBOM inventory (CycloneDX 1.5) written next to a release.

#2762: the SBOM per release answers "are we affected by <advisory>?" within
minutes. scripts/generate-sbom.sh produces per-image Syft/Grype evidence but
needs docker, syft and grype (or the network for their images). This generator
needs none of them: it reads the source tree only and lists

  * container images from the compose files and core/config/profiles.yaml
    (reference, and the digest when pinned by one),
  * Python requirements (requirements*.txt, name==version, with --hash values),
  * npm packages: package-lock.json entries (version + integrity), and
    `npm install -g name@version` pins in Dockerfiles,
  * vendored binaries / bundled components from core/licenses/vendored/provenance.json.

Usage: generate-sbom-inventory.py <tag> [--root DIR] [--output FILE] [--check]
Default output: <root>/releases/<tag>/sbom.json. --check builds the document
and validates it, writes nothing. Exit 0 ok, 1 nothing found / unusable root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "releases", "security-run", ".gsd"}
IMAGE_RE = re.compile(r"""^\s*-?\s*image:\s*["']?([^"'\s#]+)""")
VAR_DEFAULT_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")
VAR_RE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*")
DIGEST_RE = re.compile(r"@(sha256:[0-9a-f]{64})$")
REQ_RE = re.compile(r"^([A-Za-z0-9_.\-\[\]]+)==([^\s;\\]+)")
HASH_RE = re.compile(r"--hash=(sha256:[0-9a-f]{64})")
NPM_CMD_RE = re.compile(r"npm\s+(?:install|i)\b([^\n&;|]*(?:\\\n[^\n&;|]*)*)")
NPM_PIN_RE = re.compile(r"""["']?((?:@[\w.\-]+/)?[\w.\-]+)@(\d[\w.\-+]*)["']?""")


def _walk(root: Path):
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        for f in sorted(fn):
            yield Path(dp) / f


def resolve_image(raw: str) -> str | None:
    """Substitute ${VAR:-default}; a reference that still has a variable is unresolved."""
    ref = raw
    for _ in range(4):
        new = VAR_DEFAULT_RE.sub(lambda m: m.group(1), ref)
        if new == ref:
            break
        ref = new
    if VAR_RE.search(ref):
        return None
    return ref or None


def image_name(ref: str) -> str:
    """Repository part of a reference: no digest, no tag (a registry port is kept)."""
    ref = DIGEST_RE.sub("", ref)
    ref = re.sub(r"@sha256:.*$", "", ref)
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref = ref[: len(ref) - len(last)] + last.split(":", 1)[0]
    return ref


def collect_images(root: Path) -> list[dict]:
    seen: dict[str, dict] = {}
    for p in _walk(root):
        rel = p.relative_to(root).as_posix()
        is_compose = re.match(r"^(docker-)?compose[\w.\-]*\.ya?ml$", p.name)
        if not (is_compose or rel == "core/config/profiles.yaml"):
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = IMAGE_RE.match(line)
            if not m:
                continue
            ref = resolve_image(m.group(1))
            if not ref:
                continue
            d = DIGEST_RE.search(ref)
            e = seen.setdefault(ref, {"ref": ref, "digest": d.group(1) if d else None, "files": []})
            if rel not in e["files"]:
                e["files"].append(rel)
    return [seen[k] for k in sorted(seen)]


def collect_python(root: Path) -> list[dict]:
    out: dict[tuple, dict] = {}
    for p in _walk(root):
        if not re.match(r"^requirements[\w.\-]*\.txt$", p.name):
            continue
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        # fold `\` continuations so a pin and its --hash lines are one record
        for rec in re.sub(r"\\\s*\n", " ", text).splitlines():
            m = REQ_RE.match(rec.strip())
            if not m:
                continue
            key = (m.group(1).lower(), m.group(2), rel)
            out[key] = {"name": m.group(1), "version": m.group(2), "file": rel,
                        "hashes": HASH_RE.findall(rec)}
    return [out[k] for k in sorted(out)]


def collect_npm(root: Path) -> list[dict]:
    out: dict[tuple, dict] = {}
    for p in _walk(root):
        rel = p.relative_to(root).as_posix()
        if p.name == "package-lock.json":
            try:
                lock = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for path, meta in sorted((lock.get("packages") or {}).items()):
                name = meta.get("name") or path.split("node_modules/")[-1]
                if not path or not name or not meta.get("version"):
                    continue
                out[(name, meta["version"], rel)] = {"name": name, "version": meta["version"], "file": rel,
                                                     "integrity": meta.get("integrity")}
        elif p.name.startswith("Dockerfile"):
            for cmd in NPM_CMD_RE.finditer(p.read_text(encoding="utf-8", errors="replace")):
                for m in NPM_PIN_RE.finditer(cmd.group(1)):
                    out[(m.group(1), m.group(2), rel)] = {"name": m.group(1), "version": m.group(2),
                                                          "file": rel, "integrity": None}
    return [out[k] for k in sorted(out)]


def collect_vendored(root: Path) -> list[dict]:
    p = root / "core" / "licenses" / "vendored" / "provenance.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return [{"name": k, "version": v.get("ref"), "sha256": v.get("sha256"), "source": v.get("source")}
            for k, v in sorted(data.items()) if isinstance(v, dict)]


def _purl_oci(ref: str) -> str:
    name = image_name(ref)
    ver = DIGEST_RE.search(ref)
    tag = ref[len(name):].split("@")[0].lstrip(":")
    return f"pkg:oci/{quote(name, safe='/')}" + (f"@{quote(ver.group(1))}" if ver else (f"@{quote(tag)}" if tag else ""))


def build(tag: str, root: Path) -> dict:
    comps: list[dict] = []
    for i in collect_images(root):
        c = {"type": "container", "name": image_name(i["ref"]), "version": i["digest"] or (i["ref"][len(image_name(i["ref"])):].lstrip(":") or "unpinned"),
             "purl": _purl_oci(i["ref"]),
             "properties": [{"name": "rzfz:ref", "value": i["ref"]}, {"name": "rzfz:pinned-by-digest", "value": str(bool(i["digest"])).lower()}]
             + [{"name": "rzfz:file", "value": f} for f in i["files"]]}
        if i["digest"]:
            c["hashes"] = [{"alg": "SHA-256", "content": i["digest"].split(":", 1)[1]}]
        comps.append(c)
    for d in collect_python(root):
        c = {"type": "library", "name": d["name"], "version": d["version"],
             "purl": f"pkg:pypi/{d['name'].lower()}@{d['version']}",
             "properties": [{"name": "rzfz:file", "value": d["file"]}]}
        if d["hashes"]:
            c["hashes"] = [{"alg": "SHA-256", "content": h.split(":", 1)[1]} for h in d["hashes"]]
        comps.append(c)
    for d in collect_npm(root):
        c = {"type": "library", "name": d["name"], "version": d["version"],
             "purl": f"pkg:npm/{quote(d['name'], safe='@/')}@{d['version']}",
             "properties": [{"name": "rzfz:file", "value": d["file"]},
                            {"name": "rzfz:locked", "value": str(bool(d["integrity"])).lower()}]}
        if d["integrity"]:
            c["properties"].append({"name": "rzfz:integrity", "value": d["integrity"]})
        comps.append(c)
    for d in collect_vendored(root):
        c = {"type": "file", "name": d["name"], "version": d["version"] or "unversioned",
             "properties": [{"name": "rzfz:source", "value": d["source"] or ""}]}
        if d["sha256"]:
            c["hashes"] = [{"alg": "SHA-256", "content": d["sha256"]}]
        comps.append(c)
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "serialNumber": "urn:rzfz:sbom:" + tag,
            "metadata": {"component": {"type": "application", "name": "razzfazz-ai-service-stack", "version": tag},
                         "tools": [{"name": "generate-sbom-inventory.py"}]},
            "components": comps}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tag")
    ap.add_argument("--root", default=".")
    ap.add_argument("--output")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if not root.is_dir():
        print(f"error: --root {root} is not a directory", file=sys.stderr)
        return 1
    doc = build(a.tag, root)
    kinds = {}
    for c in doc["components"]:
        kinds[c["type"]] = kinds.get(c["type"], 0) + 1
    if not doc["components"]:
        print("error: no components found (wrong --root?)", file=sys.stderr)
        return 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    if a.check:
        print(f"SBOM inventory would list {len(doc['components'])} components ({summary})")
        return 0
    out = Path(a.output) if a.output else root / "releases" / a.tag / "sbom.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(doc['components'])} components: {summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
