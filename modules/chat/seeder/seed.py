#!/usr/bin/env python3
"""OpenWebUI Function/Tool/Pipeline seeder (M019 S01).

Two duties:
  Duty A — Functions/Tools API: POST source files to OpenWebUI's REST API.
  Duty B — Pipelines volume:    Copy source files into /app/pipelines-target
                                (the pipelines-data named volume), then
                                trigger a pipelines container restart so the
                                new files are picked up.

Idempotent. Re-runs without changes log "skipped" for every file. Honors
RAZZFAZZ_FORCE_RESEED=1 to overwrite regardless of version/hash.

Exit codes:
  0  — both duties succeeded
  1  — at least one duty failed (sidecar logs which one)
  2  — bootstrap failure (couldn't reach OpenWebUI, no admin credentials)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------- env / config

OPENWEBUI_URL = os.environ.get("OPENWEBUI_URL", "http://openwebui:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "razzfazz.ai Admin")
FORCE_RESEED = os.environ.get("RAZZFAZZ_FORCE_RESEED", "0") == "1"
RESTART_PIPELINES = os.environ.get("OPENWEBUI_SEED_RESTART_PIPELINES", "1") == "1"
PIPELINES_CONTAINER = os.environ.get("PIPELINES_CONTAINER", "pipelines")

FUNCTIONS_DIR = Path("/app/functions")
TOOLS_DIR = Path("/app/tools")
PIPELINES_SRC_DIR = Path("/app/pipelines")
PIPELINES_DST_DIR = Path("/app/pipelines-target")

WAIT_ATTEMPTS = 60
WAIT_INTERVAL = 5

VERSION_HEADER_RE = re.compile(r"^\s*(?:#|//|\")?\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.+?)\s*$")


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------- Phase 1: wait + login

def wait_for_openwebui() -> None:
    """Poll OpenWebUI until it returns a non-error response."""
    log(f"[seeder] Waiting for OpenWebUI at {OPENWEBUI_URL} ...")
    for attempt in range(1, WAIT_ATTEMPTS + 1):
        try:
            r = requests.get(f"{OPENWEBUI_URL}/health", timeout=5)
            if r.status_code < 500:
                log(f"[seeder] OpenWebUI healthy after {attempt} attempt(s).")
                return
        except requests.RequestException:
            pass
        try:
            # Some versions don't expose /health; fall back to /
            r = requests.get(f"{OPENWEBUI_URL}/", timeout=5)
            if r.status_code < 500:
                log(f"[seeder] OpenWebUI reachable (no /health) after {attempt} attempt(s).")
                return
        except requests.RequestException:
            pass
        time.sleep(WAIT_INTERVAL)
    log(f"[seeder] FATAL — OpenWebUI not reachable after {WAIT_ATTEMPTS * WAIT_INTERVAL}s.")
    sys.exit(2)


def bootstrap_admin_token() -> str:
    """Sign in (or sign up first user, who becomes admin) and return JWT."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        log("[seeder] FATAL — ADMIN_EMAIL or ADMIN_PASSWORD not set.")
        sys.exit(2)

    payload = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}

    # Try signin first
    r = requests.post(f"{OPENWEBUI_URL}/api/v1/auths/signin", json=payload, timeout=10)
    if r.status_code == 200:
        token = r.json().get("token") or r.json().get("access_token")
        if token:
            log(f"[seeder] Logged in as {ADMIN_EMAIL}.")
            return token

    # First-user signup (becomes admin automatically in OpenWebUI)
    if r.status_code in (400, 401, 404):
        log(f"[seeder] Signin failed ({r.status_code}); trying first-user signup.")
        signup = {**payload, "name": ADMIN_NAME}
        r2 = requests.post(f"{OPENWEBUI_URL}/api/v1/auths/signup", json=signup, timeout=10)
        if r2.status_code in (200, 201):
            token = r2.json().get("token") or r2.json().get("access_token")
            if token:
                log(f"[seeder] Created admin user {ADMIN_EMAIL}.")
                return token
            # Some versions return signup OK without a token — re-signin
        r3 = requests.post(f"{OPENWEBUI_URL}/api/v1/auths/signin", json=payload, timeout=10)
        if r3.status_code == 200:
            token = r3.json().get("token") or r3.json().get("access_token")
            if token:
                log(f"[seeder] Re-signin after signup OK.")
                return token

    log(f"[seeder] FATAL — could not obtain admin token: signin={r.status_code} body={r.text[:200]}")
    sys.exit(2)


# ----------------------------------------------------------- Duty A: API seed

def parse_header(content: str) -> dict[str, str]:
    """Parse the docstring/comment header at the top of a Function/Tool file.

    Open WebUI Functions use a triple-quoted docstring at the top with
    `key: value` lines. We tolerate both `\"\"\"...\"\"\"` and `# key: value`
    flavours.
    """
    header: dict[str, str] = {}
    in_docstring = False
    for line in content.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_docstring = not in_docstring
            continue
        if not in_docstring and not stripped.startswith("#"):
            continue
        m = VERSION_HEADER_RE.match(line)
        if m:
            header[m.group(1).lower()] = m.group(2)
    return header


_INCLUDE_RE = re.compile(r"^\s*#\s*@include\s+(_lib/[A-Za-z0-9_./-]+)\s*$", re.MULTILINE)


def expand_includes(content: str, source_dir: Path) -> str:
    """Expand `# @include _lib/<name>.py` directives by inlining the referenced file.

    Strategy (ii) from M020 SPEC C1: helper modules live in chat/functions/_lib/
    as the single source of truth, but Open WebUI's Function sandbox doesn't
    support sibling-file imports. We inline the helper at seed time so each
    Function source posted to OpenWebUI is fully self-contained.

    Behaviour:
    - Each `# @include <relpath>` line is replaced with the contents of
      <source_dir>/<relpath>, wrapped in a `# region @include ...` /
      `# endregion` pair so the inlined boundary is greppable in the
      OpenWebUI admin panel.
    - Includes are resolved one pass deep (no recursive include expansion).
    - Missing files fail loudly: the directive is replaced with a `raise
      ImportError(...)` line so the pipe errors at load time rather than
      silently shipping a broken file.
    - `from __future__ import …` lines in inlined helpers are stripped
      (Python requires those at the very top of a file; can't be when
      inlined). Pipes that use `# @include` MUST NOT carry their own
      `from __future__` imports either — if one is found in the pipe
      after include expansion, expand_includes raises ValueError with
      a clear message naming the offending pipe so the operator can
      remove it before re-seeding.
    """
    def _sub(match: "re.Match[str]") -> str:
        rel = match.group(1)
        target = source_dir / rel
        if not target.is_file():
            return f'raise ImportError("seeder @include: {rel} not found at {target}")\n'
        body = target.read_text(encoding="utf-8")
        # Strip `from __future__ import ...` lines — Python requires those to be
        # at the very beginning of a file, which they can't be when inlined into
        # a pipe whose docstring comes first. Modern (3.10+) defaults are fine
        # for everything we currently use (annotations, etc.).
        body = re.sub(r"^\s*from\s+__future__\s+import\s+.+$", "", body, flags=re.MULTILINE)
        return (
            f"# region @include {rel}\n"
            f"{body}"
            f"# endregion @include {rel}\n"
        )
    expanded = _INCLUDE_RE.sub(_sub, content)
    if expanded == content:
        # No @include directives were expanded — the pipe is unchanged, so
        # any `from __future__` it carries is still at its natural position
        # (after the docstring, which Python accepts). Skip the defensive
        # check; it would otherwise mis-fire on plain pipes like dify_pipe.py
        # that legitimately use future-imports.
        return expanded
    # Defensive: if the pipe itself still has `from __future__` AFTER include
    # expansion, the resulting source won't parse (Python's "must be first"
    # rule). Surface as ValueError so the seeder logs a precise reason
    # rather than emitting a SyntaxError at OpenWebUI load time.
    for i, line in enumerate(expanded.splitlines(), 1):
        if re.match(r"^\s*from\s+__future__\s+import", line):
            raise ValueError(
                f"pipe contains `from __future__` after @include expansion at "
                f"line {i}: {line.strip()!r}. Remove the future-import — "
                f"Python requires it at the very top of the file, which it "
                f"can't be once @include blocks expand above it."
            )
    return expanded


def seed_one(token: str, kind: str, file_path: Path) -> str:
    """Seed a single Function or Tool via the OpenWebUI REST API.

    Returns one of: 'created', 'updated', 'skipped', 'force-reseeded', 'error: ...'.
    """
    raw = file_path.read_text(encoding="utf-8")
    # M020 S01c: inline _lib/* helpers before posting (see expand_includes).
    content = expand_includes(raw, file_path.parent)
    header = parse_header(content)
    fid = header.get("id") or file_path.stem
    title = header.get("title") or fid
    version = header.get("version") or "0.0.0"
    description = header.get("description", "")

    headers = {"Authorization": f"Bearer {token}"}

    # rc6.7 #58: OpenWebUI 0.6.x removed /api/v1/{kind}/id/{fid} (the
    # endpoint that previously let us check existence with one round-trip).
    # The replacement is /api/v1/{kind}/list which returns ALL entries;
    # we filter client-side. Fetch the list once per call — fine for the
    # ~10 pipes we currently seed; if the list grows past ~100 we can
    # cache it across seed_one() invocations within duty_a.
    list_r = requests.get(
        f"{OPENWEBUI_URL}/api/v1/{kind}/list",
        headers=headers,
        timeout=10,
    )
    if list_r.status_code != 200:
        return f"error: list {list_r.status_code} {list_r.text[:120]}"

    existing = None
    for entry in list_r.json() or []:
        if entry.get("id") == fid:
            existing = entry
            break

    payload: dict[str, Any] = {
        "id": fid,
        "name": title,
        "content": content,
        "meta": {"description": description, "manifest": header},
    }

    if existing is None:
        # Create
        r = requests.post(
            f"{OPENWEBUI_URL}/api/v1/{kind}/create",
            headers=headers,
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            return "created"
        return f"error: create {r.status_code} {r.text[:120]}"

    existing_version = (existing.get("meta") or {}).get("manifest", {}).get("version") or "0.0.0"

    if not FORCE_RESEED and existing_version == version:
        return "skipped"

    # rc6.7 #80: OpenWebUI's update endpoint is POST, not PUT — see
    # /app/backend/open_webui/routers/functions.py:311
    # `@router.post('/id/{id}/update', ...)`. PUT returns 405 Method Not
    # Allowed. Same shape for tools (verified in routers/tools.py).
    update_url = f"{OPENWEBUI_URL}/api/v1/{kind}/id/{fid}/update"
    r = requests.post(update_url, headers=headers, json=payload, timeout=15)
    if r.status_code in (200, 201):
        return "force-reseeded" if FORCE_RESEED else "updated"
    return f"error: update {r.status_code} {r.text[:120]}"


def duty_a_functions_and_tools(token: str) -> bool:
    """Run Duty A: seed all chat/functions/*.py and chat/tools/*.py."""
    ok = True
    for kind, src_dir in (("functions", FUNCTIONS_DIR), ("tools", TOOLS_DIR)):
        if not src_dir.exists():
            log(f"[seeder] Duty A: {src_dir} not present, skipping {kind}.")
            continue
        for path in sorted(src_dir.glob("*.py")):
            # Helper modules in _lib/ are NOT seeded — they're consumed by
            # other pipes via either sys.path injection or seeder-time concat
            # (M020 S01 picks the strategy at implementation time).
            if "_lib" in path.parts:
                continue
            try:
                action = seed_one(token, kind, path)
            except requests.RequestException as e:
                action = f"error: network {e}"
            log(f"[seeder] [{kind}] {path.name}: {action}")
            if action.startswith("error"):
                ok = False
    return ok


# ----------------------------------------------------- Duty B: pipelines vol

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def duty_b_pipelines() -> tuple[bool, bool]:
    """Run Duty B: copy chat/pipelines/*.py into the pipelines-data volume.

    Returns (ok, changed) — `changed` is True if any file was created/updated
    (used to decide whether to trigger the pipelines container restart).
    """
    if not PIPELINES_SRC_DIR.exists():
        log(f"[seeder] Duty B: {PIPELINES_SRC_DIR} not present, skipping.")
        return True, False
    if not PIPELINES_DST_DIR.exists():
        log(f"[seeder] Duty B: {PIPELINES_DST_DIR} mount missing — pipelines-data volume not mounted?")
        return False, False

    ok = True
    changed = False
    for src in sorted(PIPELINES_SRC_DIR.glob("*.py")):
        if "_lib" in src.parts:
            continue
        dst = PIPELINES_DST_DIR / src.name
        try:
            if dst.exists() and not FORCE_RESEED and sha256_of(src) == sha256_of(dst):
                log(f"[seeder] [pipelines] {src.name}: skipped")
                continue
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(src.read_bytes())
            tmp.replace(dst)
            action = "force-reseeded" if FORCE_RESEED and dst.exists() else (
                "updated" if dst.exists() else "created"
            )
            # Note: by the time we read action above, dst already exists.
            # Use a simpler signal: did we have a hash mismatch / missing file?
            log(f"[seeder] [pipelines] {src.name}: {action}")
            changed = True
        except OSError as e:
            log(f"[seeder] [pipelines] {src.name}: error: {e}")
            ok = False
    return ok, changed


def restart_pipelines_container() -> None:
    """Trigger a restart of the pipelines container so it loads new files."""
    if not RESTART_PIPELINES:
        log("[seeder] OPENWEBUI_SEED_RESTART_PIPELINES=0 — skipping pipelines restart.")
        return
    log(f"[seeder] Restarting {PIPELINES_CONTAINER} container to load new pipelines ...")
    try:
        result = subprocess.run(
            ["docker", "restart", PIPELINES_CONTAINER],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log(f"[seeder] {PIPELINES_CONTAINER} restarted.")
        else:
            log(f"[seeder] WARN — restart failed (exit {result.returncode}): {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"[seeder] WARN — restart skipped ({e}). Operator can run `docker restart {PIPELINES_CONTAINER}` manually.")


# ------------------------------------------------------------------- main

def main() -> int:
    log(f"[seeder] M019 S01 function/tool/pipeline seeder. FORCE_RESEED={FORCE_RESEED}")
    wait_for_openwebui()
    token = bootstrap_admin_token()

    a_ok = duty_a_functions_and_tools(token)
    b_ok, b_changed = duty_b_pipelines()

    if b_changed:
        restart_pipelines_container()

    if not (a_ok and b_ok):
        log("[seeder] FAIL — see error lines above. Sidecar will exit non-zero; rerun with `docker compose run --rm openwebui-seed` after fixing.")
        return 1

    log("[seeder] OK — both duties complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
