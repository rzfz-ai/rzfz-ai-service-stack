# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1973 — prepare a box for a NON-ROOT gpustack when the profile is enabled
from the Configuration Portal.

`cli/upgrade.sh` Step 7c (`gpustack_nonroot_ownership_migration`, #536) does
this on every upgrade — but only when `llm-legacy` is already in
`COMPOSE_PROFILES`. A box that enables GPUStack LATER never runs it: measured
on 0.79 (2026-09-12), the worker died on `PermissionError` for the image's
root-owned `third_party/bin` and `/var/lib/gpustack/chat_templates` while the
container reported **healthy** — the healthcheck asks the server, not the
worker. That is the shape the operator's decision names: automating the
repair must measure the EFFECT, never trust the healthcheck.

This is a port of Step 7c's decision logic, kept deliberately close to it
(`test_1973` holds the constants together). The one difference is forced by
where it runs: the Portal mounts the repo read-only and cannot rebuild an
image, so an image built before the flip is a DEFER with the instruction to
run `rzfz upgrade`, not a rebuild.

Every outcome writes `.env` explicitly — `GPUSTACK_UID`/`GPUSTACK_GID` to the
flip values on success, to `0` with `GPUSTACK_NONROOT_DEFERRED=1` on any
doubt — because the compose default for the user is the flip value, and a box
that misses a prerequisite must run as root rather than hang at
`ready_replicas:0`.
"""
from __future__ import annotations

import os
import re
import subprocess

#: Constants shared with cli/upgrade.sh Step 7c. test_1973 asserts they agree.
LABEL_KEY = "ai.razzfazz.gpustack.nonroot-uid"
MARKER = "/d/.razzfazz-nonroot-owned"
HELPER_IMAGE = "alpine:3.21"
DEFAULT_UID = "1000"
DEFAULT_GID = "1000"
VOLUME_SUFFIX = "_gpustack-data"
SERVICE = "gpustack-legacy"


def _run(argv, run=subprocess.run, timeout=120):
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # docker missing, timeout — the caller decides
        class _Failed:
            returncode = 127
            stdout = ""
            stderr = str(exc)
        return _Failed()


def legacy_image(stack_root):
    """The `image:` of the gpustack-legacy service in modules/llm/compose.yml —
    the same awk Step 7c uses, so both look at the same image."""
    path = os.path.join(stack_root, "modules", "llm", "compose.yml")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(rf"^  {re.escape(SERVICE)}:\n(.*?)(?=^  [A-Za-z])", text, re.S | re.M)
    if not m:
        return None
    img = re.search(r"^\s+image:\s*\"?([^\"\n]+)\"?\s*$", m.group(1), re.M)
    return img.group(1).strip() if img else None


def prepare_nonroot(stack_root, env, run=subprocess.run):
    """Decide whether gpustack may run non-root on THIS box, and make it so.

    Returns ``(updates, lines)``: ``.env`` keys to write, and the action-log
    lines that say what was measured. Never raises for a docker failure —
    a failure is a DEFER with its reason.
    """
    lines = []
    uid = (env.get("GPUSTACK_UID") or DEFAULT_UID).strip()
    gid = (env.get("GPUSTACK_GID") or DEFAULT_GID).strip()
    deferred = (env.get("GPUSTACK_NONROOT_DEFERRED") or "").strip()

    if uid == "0" or gid == "0":
        if deferred == "1":
            lines.append("  #536: retrying the deferred gpustack non-root flip...")
            uid, gid = DEFAULT_UID, DEFAULT_GID
        else:
            lines.append(f"  #536: GPUSTACK_UID={uid} — gpustack stays ROOT by operator choice; no ownership preparation.")
            return {}, lines

    def defer(reason):
        lines.append(f"  #536: DEFERRING the gpustack non-root flip — {reason}.")
        lines.append("  Keeping gpustack as ROOT (GPUSTACK_UID=0). De-rooted on an unprepared box it does not crash — "
                     "the worker hangs at ready_replicas:0 while the container reports healthy (#1973).")
        return {"GPUSTACK_UID": "0", "GPUSTACK_GID": "0", "GPUSTACK_NONROOT_DEFERRED": "1"}, lines

    # (1) RENDER_GID — the container's only path to the GPU once de-rooted.
    if (env.get("HARDWARE") or "").strip() == "amd":
        host_gid = None
        try:
            host_gid = str(os.stat("/dev/dri/renderD128").st_gid)
        except OSError:
            pass
        if host_gid is None:
            lines.append(f"  #536: cannot see the host's render gid from here — RENDER_GID={env.get('RENDER_GID') or '<unset>'} "
                         "is UNVERIFIED; if deploys hang at ready_replicas:0, check 'amdgpu_query_info(ACCEL_WORKING) failed (-13)' first.")
        elif (env.get("RENDER_GID") or "") != host_gid:
            lines.append(f"  #536: RENDER_GID={env.get('RENDER_GID') or '<unset>'} but the host's render gid is {host_gid} — corrected.")
            env = dict(env, RENDER_GID=host_gid)
            render_update = {"RENDER_GID": host_gid}
        else:
            render_update = {}
    else:
        render_update = {}

    # (2) the image must have been built FOR the flip (the in-image tree is
    #     owned at build time; Step 7c refuses an unlabelled image the same way).
    image = legacy_image(stack_root)
    if not image:
        upd, l = defer(f"modules/llm/compose.yml names no image for {SERVICE} — nothing to inspect")
        upd.update(render_update)
        return upd, l
    r = _run(["docker", "image", "inspect", "--format", f'{{{{ index .Config.Labels "{LABEL_KEY}" }}}}', image], run)
    label = (r.stdout or "").strip() if r.returncode == 0 else ""
    if label in ("<no value>", "<nil>"):
        label = ""
    if r.returncode != 0:
        upd, l = defer(f"the gpustack image {image} is not present on this box (docker image inspect failed) — build it first")
        upd.update(render_update)
        return upd, l
    if label != uid:
        upd, l = defer(f"the gpustack image {image} was built before the flip (label {LABEL_KEY}='{label or '<none>'}', need '{uid}') — "
                       "the Portal cannot rebuild it (repo mounted read-only); run 'rzfz upgrade', which rebuilds and flips")
        upd.update(render_update)
        return upd, l

    # (3) the data volume must be owned by the flip uid. Fresh boxes inherit
    #     it from the image; an existing volume is re-owned ONCE, then measured.
    r = _run(["docker", "volume", "ls", "--format", "{{.Name}}"], run)
    vols = [v for v in (r.stdout or "").split() if v.endswith(VOLUME_SUFFIX)] if r.returncode == 0 else []
    if r.returncode != 0:
        upd, l = defer("docker volume ls failed — cannot tell whether gpustack-data is owned for the flip")
        upd.update(render_update)
        return upd, l
    if not vols:
        lines.append(f"  #536: no gpustack-data volume on this box yet — a fresh one inherits uid {uid} from the image.")
    else:
        vol = vols[0]
        helper = HELPER_IMAGE
        probe = ["docker", "run", "--rm", "-v", f"{vol}:/d", helper, "sh", "-c"]
        if _run(["docker", "image", "inspect", helper], run).returncode != 0:
            helper = image
            probe = ["docker", "run", "--rm", "-u", "0", "--entrypoint", "sh", "-v", f"{vol}:/d", image, "-c"]
            lines.append(f"  #536: helper image {HELPER_IMAGE} is not present — using the gpustack image for the ownership probe.")
        stat_cmd = f'printf "%s %s" "$(stat -c %u:%g /d)" "$([ -f {MARKER} ] && echo marker || echo nomarker)"'

        def measure():
            rr = _run(probe + [stat_cmd], run)
            out = (rr.stdout or "").strip() if rr.returncode == 0 else ""
            parts = out.split()
            return (parts[0], parts[1]) if len(parts) == 2 else (None, None)

        owner, mark = measure()
        if owner == f"{uid}:{gid}" and mark == "marker":
            lines.append(f"  #536: gpustack-data ({vol}) already owned by {uid}:{gid} (no change).")
        else:
            lines.append(f"  #536: re-owning gpustack-data ({vol}, currently {owner or 'unknown'}) to {uid}:{gid} — one-time, before gpustack starts non-root...")
            rr = _run(probe + [f"chown -R {uid}:{gid} /d && : > {MARKER} && chown {uid}:{gid} {MARKER}"], run, timeout=600)
            # The EFFECT, measured — not the chown's exit code, and not the healthcheck.
            owner, mark = measure()
            if rr.returncode != 0 or owner != f"{uid}:{gid}" or mark != "marker":
                upd, l = defer(f"gpustack-data ({vol}) could not be re-owned to {uid}:{gid} with helper {helper} "
                               f"(rc={rr.returncode}, measured owner {owner or 'unknown'}, marker {mark or 'unreadable'})")
                upd.update(render_update)
                return upd, l
            lines.append(f"  #536: gpustack-data re-owned; measured owner {owner}, marker present.")

    updates = dict(render_update)
    if deferred == "1" or (env.get("GPUSTACK_UID") or DEFAULT_UID) != uid or (env.get("GPUSTACK_GID") or DEFAULT_GID) != gid \
            or env.get("GPUSTACK_NONROOT_DEFERRED"):
        updates.update({"GPUSTACK_UID": uid, "GPUSTACK_GID": gid, "GPUSTACK_NONROOT_DEFERRED": ""})
    lines.append(f"  #536: prerequisites measured — gpustack runs as {uid}:{gid} (#1973).")
    return updates, lines
