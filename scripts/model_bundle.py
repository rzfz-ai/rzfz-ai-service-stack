#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2280 — read a per-class model bundle ONCE, verify everything, extract only
what this box needs.

A model bundle (`an offline package (Enterprise) <tag> --model-bundle <class>`) is a tar.gz whose
first members are its records (PACKAGE_INFO, expected-models.json,
MANIFEST.sha256, PAYLOAD_PROVENANCE) followed by models/. This tool streams the
archive a single time and, in that one pass:

* hashes the archive file itself (the out-of-band SHA-256, #781);
* accepts only the four records and regular files / directories under models/
  — no symlink, hardlink, FIFO, device, setuid/setgid bit, absolute or `..`
  path, no unpinned or non-GGUF file, no duplicate, at most MAX_MEMBERS members;
* checks every GGUF's size against the size the bundle records for it BEFORE
  reading it, and its bytes against the catalogue sha256 pin while reading;
* stops when the running total would exceed the recorded total x 1.1;
* checks free space for the files it will write (uncompressed sizes, x 1.1);
* writes only the files named by --need, as new files (mode 0644, this
  process's owner — nothing is taken from the archive's headers), under a
  `.part` name renamed into place once its hash is proven;
* checks MANIFEST.sha256 over the records and PACKAGE_INFO's kind and class;
* cross-checks the bundle's pins against the box's catalogue, and says so
  when it cannot (never silently).

Self-contained on purpose (stdlib + optional PyYAML): it lives in scripts/ so
the one-hop 2026.09 -> 2026.11 pre-step (`tar -xzf <base> ./cli ./scripts
./rzfz`) carries it, while the installed core/ is still the old release's.

Exit: 0 verified (and extracted, unless --verify-only); 1 could not run (I/O,
cannot write); 2 refused — the reason on stderr. A hostile or damaged bundle is
always 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import stat
import sys
import tarfile
import time
import zlib

RECORDS = ("PACKAGE_INFO", "expected-models.json", "MANIFEST.sha256", "PAYLOAD_PROVENANCE")
RECORD_MAX_BYTES = 4 * 1024 * 1024
MAX_MEMBERS = 4096
CHUNK = 4 * 1024 * 1024
SLACK = 1.1


class Refused(Exception):
    """The bundle must not be applied (exit 2)."""


class _HashingReader:
    """Hands the gzip stream to tarfile and hashes the archive bytes on the way."""

    def __init__(self, fh):
        self.fh, self.h, self.seconds, self.bytes = fh, hashlib.sha256(), 0.0, 0

    def read(self, n=-1):
        b = self.fh.read(n)
        t0 = time.monotonic()
        self.h.update(b)
        self.seconds += time.monotonic() - t0
        self.bytes += len(b)
        return b

    def drain(self):
        while self.read(CHUNK):
            pass


HEADER_MAX = 64 * 1024


class _BoundedTarInfo(tarfile.TarInfo):
    """tarfile reads a PAX or GNU long-name header's whole payload into memory
    BEFORE anything can look at it (a 1.2 GiB PAX header took the process to
    4.8 GiB). Refuse an oversized one before it is read, and GNU sparse
    members outright — a model bundle has neither reason to carry them."""

    def _proc_pax(self, tf):
        if self.size > HEADER_MAX:
            raise Refused(f"a PAX header of {self.size} bytes (limit {HEADER_MAX})")
        return super()._proc_pax(tf)

    def _proc_gnulong(self, tf):
        if self.size > HEADER_MAX:
            raise Refused(f"a GNU long-name header of {self.size} bytes (limit {HEADER_MAX})")
        return super()._proc_gnulong(tf)

    def _proc_sparse(self, tf):
        raise Refused("GNU sparse members are not allowed")

    # The PAX forms of GNU sparse: tarfile builds the sparse map (1.0 reads it
    # from the member's data, any length) inside the header read, before the
    # member reaches the loop — a 152 KB bundle declaring 40M entries took
    # 4 GiB. Refused here, before any of the map is read.
    def _proc_gnusparse_00(self, *_a, **_k):
        raise Refused("GNU sparse members are not allowed")

    def _proc_gnusparse_01(self, *_a, **_k):
        raise Refused("GNU sparse members are not allowed")

    def _proc_gnusparse_10(self, *_a, **_k):
        raise Refused("GNU sparse members are not allowed")


_HOOKS = ("_proc_pax", "_proc_gnulong", "_proc_sparse", "_proc_gnusparse_00", "_proc_gnusparse_01", "_proc_gnusparse_10")


def _bounded_tarfile_available() -> bool:
    """The hooks above are tarfile internals; without them the bound is gone,
    so the tool refuses to run rather than read unbounded headers."""
    return all(callable(getattr(tarfile.TarInfo, n, None)) for n in _HOOKS)


def _pax_member(name: str, pax: dict, data: bytes) -> bytes:
    ti = tarfile.TarInfo(name)
    ti.size, ti.pax_headers = len(data), pax
    return ti.tobuf(format=tarfile.PAX_FORMAT) + data + b"\0" * ((512 - len(data) % 512) % 512)


def _gnu_longname_member(size: int) -> bytes:
    ti = tarfile.TarInfo("././@LongLink")
    ti.type, ti.size = tarfile.GNUTYPE_LONGNAME, size
    return ti.tobuf(format=tarfile.GNU_FORMAT) + b"a" * size + b"\0" * ((512 - size % 512) % 512)


def _bound_self_test() -> str:
    """By effect, not by name: feed this python's tarfile an oversized PAX
    header and a sparse-1.0 member and require the refusal for each. A future
    tarfile that dispatches past these hooks (a rename, a new code path) fails
    here instead of reading unbounded data. "" = the bound holds."""
    import io  # noqa: PLC0415
    cases = {
        "a PAX header over the limit": _pax_member("big", {"comment": "a" * (HEADER_MAX + 1)}, b""),
        "a GNU long-name header over the limit": _gnu_longname_member(HEADER_MAX + 1),
        "a sparse-0.0 member": _pax_member("s0", {
            "GNU.sparse.size": "4", "GNU.sparse.numblocks": "1",
            "GNU.sparse.offset": "0", "GNU.sparse.numbytes": "4"}, b"GGUF"),
        "a sparse-0.1 member": _pax_member("s1", {
            "GNU.sparse.size": "4", "GNU.sparse.map": "0,4"}, b"GGUF"),
        "a sparse-1.0 member": _pax_member("GNUSparseFile.0/s", {
            "GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": "s",
            "GNU.sparse.realsize": "4"}, b"1\n0\n4\n" + b"\0" * 506 + b"GGUF"),
    }
    for what, member in cases.items():
        try:
            with tarfile.open(fileobj=io.BytesIO(member + b"\0" * 1024), mode="r|", tarinfo=_BoundedTarInfo) as tf:
                for _ti in tf:
                    pass
        except Refused:
            continue
        except Exception as exc:  # noqa: BLE001 — anything but our refusal means the hook was not called
            return f"{what}: {exc.__class__.__name__}"
        return f"{what}: read without a refusal"
    return ""


def _norm(name: str) -> str:
    """A member name as a relative path; Refused when it could leave the tree."""
    if name.startswith("/") or "\\" in name:
        raise Refused(f"member {name!r}: absolute or non-POSIX path")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise Refused(f"member {name!r}: '..' in the path")
    return "/".join(parts)


def _kind(ti: tarfile.TarInfo) -> str:
    return {tarfile.SYMTYPE: "symlink", tarfile.LNKTYPE: "hardlink", tarfile.FIFOTYPE: "FIFO",
            tarfile.CHRTYPE: "character device", tarfile.BLKTYPE: "block device"}.get(ti.type, f"type {ti.type!r}")


def _load_catalogue(path):
    """({file name: {sha256, ...}} across the WHOLE catalogue, None), or
    ({}, the reason it could not be read). Keyed by file name, not alias: the
    flat volume keys weights by name, so a bundle that renames an alias must
    not escape the check (review of 2fa3d0926, finding 9)."""
    if not path:
        return {}, "no catalogue given"
    if not os.path.isfile(path):
        return {}, f"{path} does not exist"
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return {}, "PyYAML is not installed for this python3"
    try:
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 — any unreadable catalogue is a reason, said out loud
        return {}, f"{path} is unreadable ({exc.__class__.__name__})"
    models = spec.get("models") if isinstance(spec, dict) else None
    if not isinstance(models, dict):
        return {}, f"{path} has no models map"
    by_file = {}
    for m in models.values():
        if isinstance(m, dict) and isinstance(m.get("sha256"), dict):
            for p, d in m["sha256"].items():
                by_file.setdefault(posixpath.basename(str(p)), set()).add(str(d).lower())
    if not by_file:
        return {}, f"{path} pins no file"
    return by_file, None


def plan_from_manifest(man) -> dict:
    """{basename: (alias, sha256, size)} for every file a carried model pins.

    Sizes come from `file_sizes` (written by the packager beside the pins); a
    bundle that predates it falls back to the entry's weight/projector size,
    which is exact for a single-file model. A carried file without a size or a
    pin refuses the bundle: neither presence nor completeness could be judged."""
    if not isinstance(man, dict) or not isinstance(man.get("models"), list):
        raise Refused("expected-models.json has no models list")
    plan = {}
    for e in man["models"]:
        alias = str(e.get("name") or "?")
        if int(e.get("size") or 0) <= 0:
            continue                                   # on-demand, not carried
        pins = e.get("sha256")
        if not isinstance(pins, dict) or not pins:
            raise Refused(f"{alias}: carried without a sha256 pin in the bundle manifest")
        sizes = e.get("file_sizes") if isinstance(e.get("file_sizes"), dict) else {}
        weight = posixpath.basename(str(e.get("filename") or ""))
        mmproj = e.get("mmproj") if isinstance(e.get("mmproj"), dict) else {}
        for path, digest in pins.items():
            base = posixpath.basename(str(path))
            size = sizes.get(path)
            if size is None and base == weight:
                size = e.get("size")
            if size is None and mmproj and base == posixpath.basename(str(mmproj.get("filename") or "")):
                size = mmproj.get("size")
            if size is None or int(size) <= 0:
                raise Refused(f"{base} ({alias}): no per-file size recorded — rebuild the bundle")
            if base in plan:
                raise Refused(f"{base}: pinned twice ({plan[base][0]}, {alias}) — the flat volume holds one")
            plan[base] = (alias, str(digest).lower(), int(size))
    if not plan:
        raise Refused("the bundle carries no model")
    return plan


def _cross_check(plan, catalogue_files):
    """Every carried GGUF must be a file this box's catalogue pins, with the
    same bytes — whatever alias the bundle files it under."""
    for base, (alias, want, _size) in sorted(plan.items()):
        if base not in catalogue_files:
            raise Refused(f"{base} ({alias}): this box's catalogue pins no file of that name — "
                          f"the bundle was built for another release")
        if want not in catalogue_files[base]:
            raise Refused(f"{base} ({alias}): the bundle pins {want[:12]}…, this box's catalogue pins "
                          f"{', '.join(sorted(d[:12] + '…' for d in catalogue_files[base]))} — "
                          f"the bundle was built for another release")


def _check_records(records, want_class):
    missing = [r for r in RECORDS if r not in records]
    if missing:
        raise Refused(f"records missing: {', '.join(missing)}")
    listed = {}
    for line in records["MANIFEST.sha256"].decode("utf-8", "replace").splitlines():
        if "  " in line:
            digest, name = line.split("  ", 1)
            listed[name.strip()] = digest.strip().lower()
    # the Enterprise package tooling writes MANIFEST.sha256 before PACKAGE_INFO, so the manifest
    # covers the other two records; PACKAGE_INFO is covered by the archive's
    # own hash or signature. Whatever the manifest lists must match.
    for r in ("expected-models.json", "PAYLOAD_PROVENANCE"):
        if r not in listed:
            raise Refused(f"record {r} is not listed in MANIFEST.sha256")
    for r, digest in listed.items():
        if r not in records or digest != hashlib.sha256(records[r]).hexdigest():
            raise Refused(f"record {r} does not match MANIFEST.sha256")
    info = dict(l.split("=", 1) for l in records["PACKAGE_INFO"].decode("utf-8", "replace").splitlines() if "=" in l)
    if info.get("package_kind") != "model-bundle":
        raise Refused(f"not a model bundle (package_kind={info.get('package_kind') or 'none'})")
    if want_class and info.get("hardware_class") != want_class:
        raise Refused(f"carries the {info.get('hardware_class') or 'unknown'} model set; this box is {want_class}")


def read_records(bundle):
    """The records only. Stops once all four are read or at the first models/
    member (records lead); refuses anything else in front of them, and more
    than MAX_MEMBERS members — the entry points run this first, so it gets the
    same limits as the full pass (2M empty members took ~1 GiB and 161 s)."""
    records, members = {}, 0
    try:
        with tarfile.open(bundle, "r|gz", tarinfo=_BoundedTarInfo) as tf:
            for ti in tf:
                tf.members.clear()        # stream mode keeps every TarInfo otherwise
                members += 1
                if members > MAX_MEMBERS:
                    raise Refused(f"more than {MAX_MEMBERS} members")
                name = _norm(ti.name)
                if name == "models" or name.startswith("models/"):
                    break
                if name == "" and ti.isdir():
                    continue
                if name not in RECORDS:
                    raise Refused(f"member {ti.name!r}: outside the bundle layout (records + models/)")
                if not ti.isreg() or ti.size > RECORD_MAX_BYTES or name in records:
                    raise Refused(f"record {name}: not a regular file, oversized or duplicate")
                records[name] = tf.extractfile(ti).read()
                if len(records) == len(RECORDS):
                    break
    except (tarfile.TarError, EOFError, zlib.error) as exc:
        raise Refused(f"the archive cannot be read ({exc.__class__.__name__}: {exc})") from exc
    return records


def process(bundle, dest=None, need=None, expect_sha256="", catalogue=None, want_class="", log=print):
    """One pass. Returns a dict of figures; raises Refused or OSError."""
    t_start = time.monotonic()
    catalogue_files, why = _load_catalogue(catalogue)
    if why:
        log(f"model-bundle: catalogue cross-check skipped: {why}")
    records, plan, man = {}, None, None
    seen, written, total, members = set(), [], 0, 0
    t_pin = t_write = 0.0
    need_set = None if need is None else set(need)
    with open(bundle, "rb") as raw:
        reader = _HashingReader(raw)
        try:
            with tarfile.open(fileobj=reader, mode="r|gz", tarinfo=_BoundedTarInfo) as tf:
                for ti in tf:
                    tf.members.clear()    # stream mode keeps every TarInfo otherwise
                    members += 1
                    if members > MAX_MEMBERS:
                        raise Refused(f"more than {MAX_MEMBERS} members")
                    name = _norm(ti.name)
                    if getattr(ti, "sparse", None):
                        raise Refused(f"member {ti.name!r}: sparse members are not allowed")
                    if not (ti.isreg() or ti.isdir()):
                        raise Refused(f"member {ti.name!r}: {_kind(ti)} members are not allowed")
                    # a file never carries setuid/setgid; a directory's setgid is
                    # inherited from the build host's tree and is never applied here
                    # (directories are created fresh, mode 0755)
                    if ti.isreg() and ti.mode & (stat.S_ISUID | stat.S_ISGID):
                        raise Refused(f"member {ti.name!r}: setuid/setgid bit")
                    if ti.isdir():
                        if name in ("", "models") or name.startswith("models/"):
                            continue
                        raise Refused(f"member {ti.name!r}: directory outside models/")
                    if name in RECORDS:
                        if name in records or ti.size > RECORD_MAX_BYTES:
                            raise Refused(f"record {name}: duplicate or oversized")
                        records[name] = tf.extractfile(ti).read()
                        if name == "expected-models.json":
                            try:
                                man = json.loads(records[name])
                            except ValueError as exc:
                                raise Refused(f"expected-models.json is not JSON ({exc})") from exc
                            plan = plan_from_manifest(man)
                            if catalogue_files:
                                _cross_check(plan, catalogue_files)
                            if dest is not None:
                                wanted = sum(s for b, (_a, _d, s) in plan.items()
                                             if need_set is None or b in need_set)
                                st = os.statvfs(dest)
                                free = st.f_bavail * st.f_frsize
                                if free < wanted * SLACK:
                                    raise Refused(
                                        f"not enough space to extract the model bundle under {dest}: "
                                        f"{free // 2**20} MiB free, {int(wanted * SLACK) // 2**20} MiB needed "
                                        f"(the files to stage x {SLACK})")
                        continue
                    if not name.startswith("models/"):
                        raise Refused(f"member {ti.name!r}: outside the bundle layout (records + models/)")
                    if plan is None:
                        raise Refused(f"member {ti.name!r} precedes expected-models.json — the records must lead")
                    base = posixpath.basename(name)
                    if not base.endswith(".gguf"):
                        raise Refused(f"member {ti.name!r}: not a GGUF")
                    if base not in plan:
                        raise Refused(f"{base}: in the bundle, not pinned by its manifest")
                    if base in seen:
                        raise Refused(f"{base}: twice in the bundle")
                    alias, want, size = plan[base]
                    if ti.size != size:
                        raise Refused(f"{base} ({alias}): {ti.size} bytes, the bundle records {size}")
                    total += ti.size
                    if total > sum(s for _a, _d, s in plan.values()) * SLACK:
                        raise Refused("the members exceed the recorded total")
                    seen.add(base)
                    keep = dest is not None and (need_set is None or base in need_set)
                    out = part = None
                    if keep:
                        target = os.path.join(dest, *name.split("/"))
                        os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                        part = os.path.join(os.path.dirname(target), f".{base}.part")
                        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        os.fchmod(fd, 0o644)      # whatever the umask or the member header says
                        out = os.fdopen(fd, "wb")
                    h = hashlib.sha256()
                    src = tf.extractfile(ti)
                    try:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            t0 = time.monotonic()
                            h.update(chunk)
                            t1 = time.monotonic()
                            t_pin += t1 - t0
                            if out is not None:
                                out.write(chunk)
                                t_write += time.monotonic() - t1
                    finally:
                        if out is not None:
                            out.close()
                    if h.hexdigest() != want:
                        if part:
                            os.unlink(part)
                        raise Refused(f"{base} ({alias}): sha256 {h.hexdigest()[:12]}… is not the pinned {want[:12]}…")
                    if part:
                        os.replace(part, target)
                        written.append(name)
            reader.drain()
        except (tarfile.TarError, EOFError, zlib.error) as exc:
            raise Refused(f"the archive cannot be read ({exc.__class__.__name__}: {exc})") from exc
    if plan is None:
        raise Refused("no expected-models.json in the bundle")
    _check_records(records, want_class)
    absent = sorted(set(plan) - seen)
    if absent:
        raise Refused(f"pinned as carried, not in the bundle: {', '.join(absent)}")
    archive = reader.h.hexdigest()
    if expect_sha256:
        want = expect_sha256.split()[0].lower()
        if len(want) != 64 or any(c not in "0123456789abcdef" for c in want):
            raise Refused(f"--model-bundle-sha256 is not a 64-character SHA-256: {expect_sha256!r}")
        if archive != want:
            raise Refused(f"the bundle does not match the out-of-band SHA-256 (expected {want}, actual {archive})")
    return {"archive_sha256": archive, "verified": len(seen), "written": written, "bytes": reader.bytes,
            "seconds": time.monotonic() - t_start, "hash_seconds": reader.seconds,
            "pin_seconds": t_pin, "write_seconds": t_write}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="model_bundle", description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dest", help="extract the verified files here (an existing, empty directory)")
    mode.add_argument("--verify-only", action="store_true", help="verify everything, write nothing")
    mode.add_argument("--list-carried", action="store_true",
                      help='"<file>\\t<bytes>" per carried GGUF, from the records alone')
    mode.add_argument("--print-info", action="store_true",
                      help="print PACKAGE_INFO (read from the leading records only)")
    ap.add_argument("--need", help="file: one GGUF basename per line to write (default: all)")
    ap.add_argument("--expect-sha256", default="")
    ap.add_argument("--catalogue", help="this box's standard-models.yaml, for the pin cross-check")
    ap.add_argument("--class", dest="want_class", default="", help="the box's hardware class")
    a = ap.parse_args(argv)
    if not _bounded_tarfile_available():
        print("model-bundle: this python3's tarfile lacks the header hooks the size bound relies on — "
              "not reading the bundle", file=sys.stderr)
        return 1
    broken = _bound_self_test()
    if broken:
        print(f"model-bundle: tarfile no longer calls the bounded hooks ({broken}) — not reading the bundle",
              file=sys.stderr)
        return 1
    try:
        if a.print_info:
            records = read_records(a.bundle)
            sys.stdout.write(records.get("PACKAGE_INFO", b"").decode("utf-8", "replace"))
            return 0
        if a.list_carried:
            records = read_records(a.bundle)
            if "expected-models.json" not in records:
                raise Refused("no expected-models.json among the leading records")
            for base, (_alias, _d, size) in sorted(plan_from_manifest(json.loads(records["expected-models.json"])).items()):
                print(f"{base}\t{size}")
            return 0
        need = None
        if a.need:
            with open(a.need, encoding="utf-8") as fh:
                need = [l.strip() for l in fh if l.strip()]
        r = process(a.bundle, None if a.verify_only else a.dest, need, a.expect_sha256, a.catalogue, a.want_class)
    except Refused as exc:
        print(f"model-bundle: REFUSED: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"model-bundle: could not process {a.bundle}: {exc}", file=sys.stderr)
        return 1
    gib = r["bytes"] / 2**30
    print(f"model-bundle: verified {r['verified']} GGUF(s) against their pins, wrote {len(r['written'])}; "
          f"archive sha256 {r['archive_sha256']}")
    print(f"model-bundle: one pass over {gib:.1f} GiB in {r['seconds']:.1f}s — archive hash {r['hash_seconds']:.1f}s, "
          f"pin hashing {r['pin_seconds']:.1f}s, extract (write) {r['write_seconds']:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
