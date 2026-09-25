#!/usr/bin/env python3
"""
Coding-Agent Web UI  (#36 — coding-agent split)

A single Flask + xterm.js web terminal shared by every per-type coding-agent
image (opencode / gsd-pi / codex / user-defined). One image, one UI; the agent
identity + capabilities are driven entirely by environment variables so the
image is generic:

  AGENT_KIND          opencode | gsd-pi | codex | user-defined | coding-tools
  AGENT_LABEL         Human label shown in the top bar (e.g. "opencode")
  AGENT_LAUNCH_CMD    Shell command a "New session" launches by default in the
                      chosen repo (empty for user-defined → bare shell)
  AGENT_MODEL_CONFIG  "1" when this agent supports the per-session model/auth
                      picker (opencode/gsd-pi/codex); "0" for user-defined.

Key properties vs. the old single coding-tools UI:

  * TABS ARE tmux SESSIONS  — each browser tab attaches to a NAMED tmux session
    (`s<N>`) rather than spawning a throw-away bash PTY. tmux keeps running
    inside the container, so a `docker restart` (which re-execs the entrypoint
    and re-starts the tmux server from the resurrect/continuum save) restores
    every session; the browser simply re-attaches on reload. This is the Phase-1
    persistence mechanism together with the per-user named home/workspace
    volume (mounted by the provisioner) and tmux-resurrect+continuum.

  * NEW SESSION FLOW (Phase 3) — POST /session creates a tmux session bound to
    a chosen git repo working dir, optionally auto-launching the agent.

  * PER-SESSION MODEL / AUTH PICKER (Phase 4) — the New-session dialog lets the
    user pick Local model (default) / Own API key / Own subscription. The choice
    is written into the tmux session's environment BEFORE the agent launches
    (OPENAI_BASE_URL / OPENAI_API_KEY / codex `wire_api="responses"` via the
    local Responses→Chat shim etc.).

  * REPO CLONING (Phase 6) — POST /clone clones an arbitrary git URL (with the
    user's own token) into the workspace volume; GET /gitea/repos + POST
    /gitea/clone provide the one-click "clone from Gitea" convenience.

Endpoints:
  GET  /                 HTML dashboard (tabs + terminal)
  GET  /health           JSON healthcheck
  GET  /api/state        JSON: sessions, repos, model options, agent kind
  POST /session          create a tmux session {repo, auth_mode, api_key?, model?}
  POST /session/kill     kill a tmux session {name}
  POST /clone            clone a git URL {url, token?}  into the workspace
  GET  /gitea/repos      list the user's Gitea repos (needs stored gitea token)
  POST /gitea/token      store the user's Gitea token in the persistent volume
  POST /gitea/clone      clone one of the user's Gitea repos {full_name}
  WS   /terminal?session=<name>   attach xterm.js to a tmux session
"""

import functools
import hashlib
import json
import logging
import os
import re
import shlex
import ssl
import unicodedata
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import flask
from flask import Flask, Response, jsonify, request
from flask_sock import Sock
import ptyprocess

app = Flask(__name__, template_folder="templates", static_folder="static")
sock = Sock(app)

START_TIME = time.time()

# ── C1 layer-3: per-instance identity gate ───────────────────────────────────
# Defence-in-depth. Caddy forward_auth (layer 1) + agent-manager ownership check
# (layer 2) already fence this container, but a mis-configured Docker network
# (e.g. the agent subdomain route pointed straight at this container without the
# forward_auth handler) MUST NOT expose a bare shell. So every route — including
# the WS terminal — independently verifies the forwarded Authentik identity and
# requires it to be THIS instance's owner.
#
# The manager injects AGENT_OWNER_SLUG (== make_user_slug(owner_username)) at
# launch. On each request we read the X-Authentik-Username header that Caddy's
# forward_auth copies upstream, re-derive its slug with the SAME algorithm the
# manager uses (core/common/razzfazz_common/user_slug.make_user_slug, inlined
# here since this image doesn't vendor razzfazz_common), and require equality.
#
# AGENT_OWNER_SLUG unset (legacy/pre-fix instance) → we FAIL CLOSED only when an
# identity header is present but mismatched; when the owner slug is entirely
# absent we still require *some* authenticated identity so an unauth request is
# always refused. Set AGENT_AUTH_DISABLED=1 for local dev only.
AGENT_OWNER_SLUG = os.environ.get("AGENT_OWNER_SLUG", "").strip()
AGENT_AUTH_DISABLED = os.environ.get("AGENT_AUTH_DISABLED", "").strip().lower() in (
    "1", "true", "yes", "on")

# C1 bypass fix (PR #84 re-review) — source-IP trust anchor.
# The X-Authentik-Username header is forgeable by anyone who can reach this
# container's web port directly on the shared `coding-agents` network (a peer
# user's sandbox). We must therefore ONLY trust requests that actually came from
# agent-manager (which itself only proxies requests Caddy authenticated + proved
# via X-Razzfazz-Proxy-Proof). We resolve `agent-manager` via docker DNS at
# request time and require request.remote_addr to match. A peer sandbox CANNOT
# forge the manager's source IP because the sandbox has cap_drop ALL (no
# NET_RAW → no raw-socket source-address spoofing). We deliberately do NOT hold
# a shared secret here (the user reads their own container's env).
_MANAGER_HOST = os.environ.get("AGENT_MANAGER_HOST", "agent-manager")
import socket as _socket  # noqa: E402


def _manager_ips():
    """Resolve agent-manager → set of IPs, cached briefly. Fail-open to empty
    (caller then denies) rather than crash if DNS momentarily hiccups."""
    now = time.time()
    cached = getattr(_manager_ips, "_cache", None)
    if cached and now - cached[0] < 30:
        return cached[1]
    ips = set()
    try:
        for res in _socket.getaddrinfo(_MANAGER_HOST, None):
            ips.add(res[4][0])
    except Exception:  # noqa: BLE001
        pass
    _manager_ips._cache = (now, ips)
    return ips


def _from_manager() -> bool:
    """True iff the request's peer address is agent-manager."""
    remote = (request.remote_addr or "").strip()
    if not remote:
        return False
    return remote in _manager_ips()


def _make_user_slug(username: str) -> str:
    """Inline copy of razzfazz_common.user_slug.make_user_slug (#36).

    MUST stay byte-for-byte identical to the manager's helper so the ownership
    comparison agrees. See core/common/razzfazz_common/user_slug.py.
    """
    base = re.sub(r"[^a-z0-9]", "-", (username or "").lower())
    base = re.sub(r"-+", "-", base).strip("-")
    suffix = hashlib.sha256((username or "").encode("utf-8")).hexdigest()[:6]
    return f"{base[:14]}-{suffix}"


def _identity_ok() -> bool:
    """True iff the request came from agent-manager AND carries the owning
    Authentik identity.

    Two independent checks (C1 bypass fix):
      1. SOURCE-IP: the request must originate from agent-manager. This is the
         real trust anchor — X-Authentik-Username is a forgeable header, so a
         peer user's sandbox reaching this port directly on the coding-agents
         net could otherwise spoof the owner. The sandbox has no NET_RAW, so it
         cannot forge the manager's source IP. Requests NOT from the manager are
         refused regardless of headers.
      2. OWNER identity: among manager-proxied requests, the forwarded
         X-Authentik-Username must map to this instance's owner slug.
    """
    if AGENT_AUTH_DISABLED:
        return True
    # 1. Must come from agent-manager (source-IP anchor).
    if not _from_manager():
        return False
    # 2. Must carry the owning identity.
    username = (request.headers.get("X-Authentik-Username") or "").strip()
    if not username:
        return False
    if not AGENT_OWNER_SLUG:
        # Owner slug not injected (legacy instance): accept any authenticated
        # user that reached us THROUGH the manager (already source-IP-gated).
        return True
    return _make_user_slug(username) == AGENT_OWNER_SLUG


def require_owner(fn):
    """Route decorator: 403 unless the caller is this instance's owner."""
    @functools.wraps(fn)
    def _wrap(*args, **kwargs):
        if not _identity_ok():
            return Response("Forbidden", status=403)
        return fn(*args, **kwargs)
    return _wrap

# ── agent identity (env-driven) ──────────────────────────────────────────────
AGENT_KIND = os.environ.get("AGENT_KIND", "coding-tools")
AGENT_LABEL = os.environ.get("AGENT_LABEL", AGENT_KIND)
AGENT_LAUNCH_CMD = os.environ.get("AGENT_LAUNCH_CMD", "")
AGENT_MODEL_CONFIG = os.environ.get("AGENT_MODEL_CONFIG", "1") == "1"

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
HOME_DIR = Path(os.environ.get("HOME", "/home/agent"))
GITEA_EXTERNAL_URL = os.environ.get("GITEA_EXTERNAL_URL", "")
GITEA_INTERNAL_URL = os.environ.get("GITEA_INTERNAL_URL", "http://gitea:3000")
# PR #84 clone-allowlist: extra operator-configurable trusted hosts a clone
# TOKEN may be sent to (comma-separated), on top of the box's own Gitea +
# the fleet source-of-truth + the public forges below.
CODING_AGENT_CLONE_TOKEN_HOSTS = os.environ.get("CODING_AGENT_CLONE_TOKEN_HOSTS", "")


def _url_host(url: str) -> str:
    """Robust host component of an http(s) URL: lowercased, userinfo + port
    stripped. Returns "" for a non-http(s) URL or an unparseable one. Uses the
    stdlib urlsplit (.hostname handles userinfo/port/case + IPv6) — NOT a
    substring match — mirroring the mcp-manager ssrf-guard host-parse contract
    (PR #84). Fail-safe: any parse error → "" (which never matches the
    allowlist, so the token is refused)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    return (parts.hostname or "").lower()


def _clone_token_host_allowlist() -> set:
    """Hosts a clone TOKEN may be sent to. = the box's own Gitea (internal +
    external) + the fleet/origin source-of-truth git.razzfazz.ai + the public
    forges + any operator-configured extras. A token to a host NOT in this set
    is refused (anti-PAT-exfil); a public clone (no token) from ANY host is
    unaffected."""
    hosts = {"git.razzfazz.ai", "github.com", "gitlab.com"}
    for gitea_url in (GITEA_EXTERNAL_URL, GITEA_INTERNAL_URL):
        h = _url_host(gitea_url) if gitea_url else ""
        if h:
            hosts.add(h)
    for extra in CODING_AGENT_CLONE_TOKEN_HOSTS.split(","):
        extra = extra.strip().lower()
        if extra:
            hosts.add(extra)
    return hosts
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# Persistent per-user secrets (own API key / own gitea token) live in the
# workspace volume so they survive restart. NEVER world-readable.
SECRETS_DIR = HOME_DIR / ".config" / "razzfazz-agent"

# Model options offered in the per-session picker. Populated from
# $AGENT_MODEL_ALIASES (a JSON list written by the provisioner from
# standard-models.yaml); falls back to just the single default LLM_MODEL.
try:
    MODEL_ALIASES = json.loads(os.environ.get("AGENT_MODEL_ALIASES", "") or "[]")
except (ValueError, TypeError):
    MODEL_ALIASES = []
if not MODEL_ALIASES and LLM_MODEL:
    MODEL_ALIASES = [LLM_MODEL]


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(cmd, timeout=15, cwd=None, env=None):
    """Run a command (argv list), return (rc, stdout+stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def _tmux(*args, timeout=10):
    return _run(["tmux", *args], timeout=timeout)


_SESSION_RE = re.compile(r"^s[0-9]+$")


def list_sessions():
    """Return [{name, repo, agent}] for every live tmux session `sN`.

    The working dir (repo) is read from the session's start directory
    (`#{pane_start_path}` isn't stable, so we track it via a per-session
    tmux user option `@repo` set at creation time)."""
    # `@repo` is a tmux user-option we set at creation, but tmux-resurrect does
    # NOT restore user-options after a container restart — so fall back to the
    # pane's current path (which resurrect DOES restore) to recover the repo
    # label. `#{b:pane_current_path}` is the basename of the pane cwd.
    rc, out = _tmux(
        "list-sessions", "-F",
        "#{session_name}\t#{?@repo,#{@repo},}\t#{?@agent,#{@agent},}\t#{b:pane_current_path}")
    sessions = []
    if rc != 0:
        return sessions
    ws_base = WORKSPACE.name
    labels = read_prefs()["labels"]
    for line in out.splitlines():
        parts = line.split("\t")
        name = parts[0].strip()
        if not _SESSION_RE.match(name):
            continue
        repo = (parts[1].strip() if len(parts) > 1 else "")
        if not repo:
            cwd_base = (parts[3].strip() if len(parts) > 3 else "")
            repo = "(workspace root)" if (not cwd_base or cwd_base == ws_base) else cwd_base
        sessions.append({
            "name": name,
            "repo": repo,
            "agent": (parts[2].strip() if len(parts) > 2 else ""),
            # #235/#223 — the user's own tab name, from the durable store. Sent
            # with the session so the frontend never has to keep a second copy
            # of the mapping; that is how the browser and the volume drift.
            "label": labels.get(name, ""),
        })
    sessions.sort(key=lambda s: int(s["name"][1:]))
    return sessions


def _next_session_name():
    used = {int(s["name"][1:]) for s in list_sessions()}
    n = 1
    while n in used:
        n += 1
    return f"s{n}"


def list_repos():
    """Directories in the workspace that are git repos (plus the bare workspace)."""
    repos = ["(workspace root)"]
    try:
        for d in sorted(WORKSPACE.iterdir()):
            if d.is_dir() and (d / ".git").exists():
                repos.append(d.name)
    except Exception:  # noqa: BLE001
        pass
    return repos


def _repo_path(repo: str) -> Path:
    """Map a repo label from list_repos() to an absolute, jailed path."""
    if not repo or repo == "(workspace root)":
        return WORKSPACE
    # Jail: only a single path component under WORKSPACE, no traversal.
    name = Path(repo).name
    p = (WORKSPACE / name).resolve()
    if WORKSPACE.resolve() not in p.parents and p != WORKSPACE.resolve():
        return WORKSPACE
    return p if p.exists() else WORKSPACE


def _secret_path(name: str) -> Path:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    return SECRETS_DIR / name


def _read_secret(name: str) -> str:
    p = _secret_path(name)
    try:
        return p.read_text().strip() if p.exists() else ""
    except Exception:  # noqa: BLE001
        return ""


def _write_secret(name: str, value: str):
    p = _secret_path(name)
    p.write_text(value.strip())
    try:
        p.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass


# ── durable per-user UI preferences (#234/#222 themes, #235/#223 tab labels) ──
# One JSON file on the per-user HOME volume:
#
#   {"theme": "dark", "labels": {"s1": "API work"}}
#
# The location matters more than the format. #167 kept tab labels in the
# browser's localStorage, which is why #223 was filed anyway: a label stored
# there is gone the moment you open the agent from another machine, and it says
# nothing about surviving a re-provision. tmux state is no better — the design
# note is explicit that tmux-resurrect does NOT restore tmux user-options, so a
# label parked in `@label` evaporates on the next container restart. The home
# volume is the only thing that outlives all three of reconnect, restart and
# recreate.
#
# Theme and labels share the file because they are the same kind of thing (a
# per-user UI preference that must outlive the browser); two files would just be
# two chances to corrupt one.
PREFS_PATH = SECRETS_DIR / "ui-prefs.json"
# #236: durable per-tab {repo, cmd} records so an arbitrary start command
# is deterministically re-run after a container restart (tmux-resurrect
# only revives ALLOWLISTED processes; everything else came back as a bare
# shell and the create-time command was lost).
REPLAY_PATH = SECRETS_DIR / "tab-replay.json"
# review #685: one-shot marker on CONTAINER-lifetime tmpfs — a pure web-
# process restart (crash/update) while tmux lives must NOT replay again:
# a user who deliberately EXITED their custom command leaves an idle pane
# the shell-guard cannot distinguish from a fresh restore.
REPLAY_DONE_MARKER = Path(os.environ.get("RAZZFAZZ_REPLAY_MARKER",
                                         "/run/tab-replay.done"))
# pane commands that mean "resurrect did NOT revive the process" — replay
# is safe. Anything else running in the pane means we must NOT double-launch.
_IDLE_SHELLS = {"bash", "sh", "zsh", "dash", "fish"}
THEMES = ("dark", "light")
DEFAULT_THEME = "dark"
LABEL_MAX = 48


def _default_prefs() -> dict:
    # #230: default_repo/default_launch seed NEW sessions from the Settings
    # dialog; None/True = the pre-#230 behaviour exactly.
    return {"theme": DEFAULT_THEME, "labels": {}, "default_repo": "",
            "default_launch": True}


def read_prefs() -> dict:
    """Load the prefs file. NEVER raises.

    A half-written or hand-edited file reads as defaults rather than taking the
    terminal down — the UI it configures is how the user would fix it.
    """
    prefs = _default_prefs()
    try:
        raw = json.loads(PREFS_PATH.read_text())
    except Exception:  # noqa: BLE001 — missing, corrupt, unreadable: all default
        return prefs
    if not isinstance(raw, dict):
        return prefs
    theme = raw.get("theme")
    if theme in THEMES:
        prefs["theme"] = theme
    labels = raw.get("labels")
    if isinstance(labels, dict):
        prefs["labels"] = {str(k): str(v) for k, v in labels.items()
                           if isinstance(v, str) and v}
    if isinstance(raw.get("default_repo"), str):
        prefs["default_repo"] = raw["default_repo"]
    if isinstance(raw.get("default_launch"), bool):
        prefs["default_launch"] = raw["default_launch"]
    return prefs


def write_prefs(patch: dict) -> dict:
    """Merge `patch` into the stored prefs and persist atomically.

    A merge, not a replace: theme and labels live in the same file, so saving
    one must not drop the other. Written to a temp file and `os.replace`d,
    because a crash mid-write would otherwise cost the user every tab name at
    once.
    """
    prefs = read_prefs()
    if "theme" in patch and patch["theme"] in THEMES:
        prefs["theme"] = patch["theme"]
    if "default_repo" in patch and isinstance(patch["default_repo"], str):
        prefs["default_repo"] = patch["default_repo"][:256]
    if "default_launch" in patch and isinstance(patch["default_launch"], bool):
        prefs["default_launch"] = patch["default_launch"]
    if "labels" in patch and isinstance(patch["labels"], dict):
        merged = dict(prefs["labels"])
        for key, value in patch["labels"].items():
            if value:
                merged[str(key)] = str(value)
            else:
                merged.pop(str(key), None)
        prefs["labels"] = merged
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREFS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prefs, indent=2))
        os.replace(tmp, PREFS_PATH)
    except Exception:  # noqa: BLE001 — a failed save must not break the UI
        pass
    return prefs


def clean_theme(raw):
    """Validate a theme name. Returns (theme, error)."""
    if raw in THEMES:
        return raw, None
    return None, f"Unknown theme. Available: {', '.join(THEMES)}."


def clean_label(raw):
    """Validate a tab label. Returns (label, error); '' means "use the default".

    Rejects Unicode C* characters. The label is rendered into the page AND set
    as a tmux option, and a terminal reads escape sequences rather than showing
    them — so control bytes here are an injection vector, not a typo. The same
    rule guards the per-instance agent name in agent-manager (#233).
    """
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "Label must be text."
    label = raw.strip()
    if not label:
        return "", None
    if len(label) > LABEL_MAX:
        return None, f"Label is too long — maximum {LABEL_MAX} characters."
    for ch in label:
        if unicodedata.category(ch)[0] == "C":
            return None, ("Label contains a control or formatting character; "
                          "a terminal would interpret it rather than show it.")
    return label, None


def forget_session_label(name: str):
    """Drop a killed session's label.

    `_next_session_name` fills gaps, so `s1` WILL be handed out again — a stale
    label would silently attach itself to somebody's next tab.
    """
    write_prefs({"labels": {name: ""}})


def check_gitea():
    url = GITEA_INTERNAL_URL.rstrip("/") + "/api/v1/version"
    token = _read_secret("gitea_token") or os.environ.get("GITEA_API_TOKEN", "")
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
        with urllib.request.urlopen(req, context=ctx, timeout=4) as r:
            data = json.loads(r.read())
            return {"ok": True, "version": data.get("version", "?")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:80]}


def get_uptime():
    secs = int(time.time() - START_TIME)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── model / auth env for a new session (Phase 4) ─────────────────────────────
def _session_llm_env(auth_mode: str, api_key: str, model: str) -> dict:
    """Compute the env overrides a New session applies before launching the agent.

    auth_mode:
      local        — GPUStack local model (the default). Uses the container's
                     baked OPENAI_BASE_URL + GPUSTACK_API_KEY.
      own-key      — user's own OpenAI-compatible API key. base_url stays the
                     provider default the agent ships with; we only override the
                     key (and model, if given).
      subscription — for codex this is `codex login --device-auth` (handled in
                     the launch command, not env); for others we fall back to
                     own-key semantics. No key is injected here.
    """
    env = {}
    if auth_mode == "own-key" and api_key:
        env["OPENAI_API_KEY"] = api_key
        env["GPUSTACK_API_KEY"] = api_key
        # own key → talk to real OpenAI unless the user's key is for another
        # provider they've pre-set; blank the local base so the agent uses its
        # provider default.
        env["OPENAI_BASE_URL"] = os.environ.get(
            "OWN_KEY_BASE_URL", "https://api.openai.com/v1")
    # local (default) and subscription leave the baked env untouched.
    if model:
        env["LLM_MODEL"] = model
    return env


# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return Response(json.dumps({"status": "ok",
                                "uptime_seconds": int(time.time() - START_TIME)}),
                    content_type="application/json")


@app.route("/")
@require_owner
def index():
    gitea = check_gitea()
    context = {
        "agent_kind": AGENT_KIND,
        "agent_label": AGENT_LABEL,
        "agent_launch_cmd": AGENT_LAUNCH_CMD,
        "supports_model_config": AGENT_MODEL_CONFIG,
        "uptime": get_uptime(),
        "gitea_ok": gitea["ok"],
        "gitea_info": gitea.get("version", gitea.get("error", "")),
        "gitea_external_url": GITEA_EXTERNAL_URL,
        "gitea_token_set": bool(_read_secret("gitea_token")
                                or os.environ.get("GITEA_API_TOKEN")),
        "llm_base_url": LLM_BASE_URL,
        "llm_model": LLM_MODEL,
        "model_aliases": MODEL_ALIASES,
        "workspace": str(WORKSPACE),
        # Consumed by the page's JS bootstrap (repo list for the New-session
        # dialog); refreshed live via /api/state on load.
        "repos": list_repos(),
        # Phase 5: user-defined agents show a curated copy-paste installer list.
        "install_suggestions": INSTALL_SUGGESTIONS if AGENT_KIND == "user-defined" else [],
    }
    return flask.render_template("index.html", **context)


@app.route("/api/state")
@require_owner
def api_state():
    return jsonify({
        "agent_kind": AGENT_KIND,
        "agent_label": AGENT_LABEL,
        "supports_model_config": AGENT_MODEL_CONFIG,
        "sessions": list_sessions(),
        "repos": list_repos(),
        "model_aliases": MODEL_ALIASES,
        "default_model": LLM_MODEL,
        "prefs": read_prefs(),
        "gitea_ok": check_gitea()["ok"],
        "gitea_token_set": bool(_read_secret("gitea_token")
                                or os.environ.get("GITEA_API_TOKEN")),
    })


# ── Forwarded-port PREVIEW: list LISTENing TCP ports (#36 / PR #84 — FEATURE 1)
# The Ports panel polls this to discover dev servers the user starts inside the
# container (e.g. `npm run dev` on :3000). We read /proc/net/tcp[6] directly
# (no iproute2 in the image) and return every socket in the LISTEN state (0x0A)
# bound to a wildcard/loopback local address, excluding the terminal's own web
# port + the local-model shim + anything the preview couldn't reach anyway.
# Each entry is rendered by the UI as a click-to-open preview link
# (/__port/<port>/), which the agent-manager proxy reverse-proxies to
# http://<this-container>:<port> over docker DNS (owner-only, same auth path as
# the terminal).
_OWN_WEB_PORT = int(os.environ.get("CODING_TOOLS_WEB_PORT", "3004"))
# The local-model shim binds 127.0.0.1:<SHIM_PORT> (default 8123) — it's an
# internal helper, not a user dev server, so exclude it from the panel.
_SHIM_PORT = int(os.environ.get("CODING_SHIM_PORT",
                                os.environ.get("CODEX_SHIM_PORT", "8123")) or "8123")
# Ports we never surface as a preview (our own web UI + the shim).
_EXCLUDED_PREVIEW_PORTS = {_OWN_WEB_PORT, _SHIM_PORT}
_TCP_LISTEN_STATE = "0A"  # /proc/net/tcp `st` column value for LISTEN


def _listening_tcp_ports(proc_root: str = "/proc") -> list:
    """Parse /proc/net/tcp + /proc/net/tcp6 → sorted list of LISTENing local
    TCP ports (deduped), excluding our own web port + the shim. Stdlib only —
    works in the read-only sandbox with no iproute2. Fail-safe: any parse error
    yields an empty list rather than raising.

    `proc_root` exists so the FAIL-SAFE path is testable (#166); production
    never passes it. Same seam as shim.py::upstream_bind_scope (#94).
    """
    ports = set()
    for proc in (f"{proc_root}/net/tcp", f"{proc_root}/net/tcp6"):
        try:
            with open(proc, "r") as f:
                lines = f.readlines()[1:]  # skip header
        except Exception:  # noqa: BLE001
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local, st = fields[1], fields[3]
            if st.upper() != _TCP_LISTEN_STATE:
                continue
            # local = "HEXADDR:HEXPORT"; the port is the last colon-field.
            _, _, hexport = local.rpartition(":")
            try:
                port = int(hexport, 16)
            except ValueError:
                continue
            if port <= 0 or port > 65535:
                continue
            if port in _EXCLUDED_PREVIEW_PORTS:
                continue
            ports.add(port)
    return [{"port": p} for p in sorted(ports)]


@app.route("/api/ports")
@require_owner
def api_ports():
    """List LISTENing TCP ports inside this container for the Ports panel."""
    return jsonify({"ports": _listening_tcp_ports()})


def _read_replay() -> dict:
    try:
        raw = json.loads(REPLAY_PATH.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt = empty
        return {}


def _write_replay(records: dict) -> None:
    try:
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REPLAY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(records))
        os.replace(tmp, REPLAY_PATH)
    except Exception:  # noqa: BLE001 — replay is best-effort, never fatal
        pass


def record_tab(name: str, repo: str, cmd: str) -> None:
    """#236: remember what this tab should run after a restart."""
    records = _read_replay()
    records[name] = {"repo": repo, "cmd": cmd}
    _write_replay(records)


def replay_pending_tabs() -> dict:
    """Re-run recorded start commands for restored-but-idle tabs.

    Called once at web-UI boot (the entrypoint started tmux + continuum
    restore before us). The double-launch guard is the pane's CURRENT
    command: resurrect revives allowlisted processes — if the pane is
    running anything beyond a bare shell, we must not type into it.
    A record whose session no longer exists is dropped (tab was closed).
    Returns {replayed: [...], dropped: [...]} for logging/tests.
    """
    if REPLAY_DONE_MARKER.exists():
        return {"replayed": [], "dropped": [], "skipped": "already-ran"}
    try:
        REPLAY_DONE_MARKER.touch()
    except Exception:  # noqa: BLE001 — marker is belt, replay still runs once
        pass
    records = _read_replay()
    if not records:
        return {"replayed": [], "dropped": []}
    rc, out = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
    live = set(out.split()) if rc == 0 else set()
    replayed, dropped = [], []
    for name, rec in list(records.items()):
        if name not in live:
            records.pop(name, None)
            dropped.append(name)
            continue
        cmd = (rec or {}).get("cmd") or ""
        if not cmd:
            continue
        rc2, pane_cmd = _run(["tmux", "display-message", "-p", "-t", name,
                              "#{pane_current_command}"])
        if rc2 != 0 or pane_cmd.strip() not in _IDLE_SHELLS:
            continue  # resurrect brought the process back — no double-launch
        _tmux("send-keys", "-t", name, cmd, "Enter")
        replayed.append(name)
    if dropped:
        _write_replay(records)
    return {"replayed": replayed, "dropped": dropped}


@app.route("/session", methods=["POST"])
@require_owner
def create_session():
    """Create a tmux session in a chosen repo, optionally launching the agent.

    Body: {repo, launch: bool, auth_mode, api_key?, model?}
    """
    body = request.get_json(silent=True) or {}
    prefs = read_prefs()
    repo = body.get("repo", "")
    # #230: Settings-dialog defaults seed what the caller left unset
    if not repo and prefs.get("default_repo"):
        repo = prefs["default_repo"]
    launch = bool(body.get("launch", prefs.get("default_launch", True)))
    # #236: optional free-form start command — replaces the agent launch
    # for this tab and is durably replayed after a container restart.
    start_command = (body.get("start_command") or "").strip()
    auth_mode = body.get("auth_mode", "local")
    api_key = body.get("api_key", "")
    model = body.get("model", "")
    # M1: own-API-key persistence is OPT-IN. The prior behaviour silently wrote
    # the user's key to disk on every own-key session; now the caller must set
    # "persist_key": true to have it survive a restart. Default: use-once.
    persist_key = bool(body.get("persist_key", False))

    cwd = _repo_path(repo)
    name = _next_session_name()

    # Base env for the session = container env + per-session LLM/auth overrides.
    session_env = dict(os.environ)
    session_env["TERM"] = "xterm-256color"
    session_env["HOME"] = str(HOME_DIR)
    if AGENT_MODEL_CONFIG:
        session_env.update(_session_llm_env(auth_mode, api_key, model))
        # own-key: persist the key ONLY when the user explicitly opted in
        # (M1). Otherwise it lives only in this session's tmux env.
        if auth_mode == "own-key" and api_key and persist_key:
            _write_secret("own_api_key", api_key)

    # Create the detached tmux session in the target dir.
    rc, out = _run(["tmux", "new-session", "-d", "-s", name, "-c", str(cwd)],
                   env=session_env)
    if rc != 0:
        return jsonify({"error": f"tmux create failed: {out[:200]}"}), 500

    # Record repo + agent kind + model as tmux user options (for list_sessions).
    _tmux("set-option", "-t", name, "@repo", repo or "(workspace root)")
    _tmux("set-option", "-t", name, "@agent", AGENT_KIND if launch else "shell")
    if model:
        _tmux("set-option", "-t", name, "@model", model)

    # Launch inside the session (Phase 3 + 4). A free-form start command
    # (#236) takes the tab over instead of the agent CLI.
    sent_cmd = ""
    if start_command:
        _tmux("send-keys", "-t", name, start_command, "Enter")
        _tmux("set-option", "-t", name, "@agent", "custom")
        sent_cmd = start_command
    elif launch and AGENT_LAUNCH_CMD:
        launch_cmd = _build_launch_cmd(auth_mode, api_key, model)
        # send-keys types the command into the session's shell; the per-session
        # env we set above is already inherited by that shell.
        _tmux("send-keys", "-t", name, launch_cmd, "Enter")
        # NOT recorded for replay: the agent CLIs are on the resurrect
        # allowlist and come back by themselves — replaying would
        # double-launch exactly the case resurrect handles.

    # #236: durable record (repo always — the pane cwd survives via
    # resurrect, the record documents intent; cmd only for custom tabs).
    record_tab(name, repo, sent_cmd)

    return jsonify({"name": name, "repo": repo or "(workspace root)"})


def _build_launch_cmd(auth_mode: str, api_key: str, model: str) -> str:
    """Return the shell command string to launch the agent for this kind.

    Codex specifics (per #36 wiring facts):
      * Local model uses ~/.codex/config.toml [model_providers.gpustack] with
        base_url="http://127.0.0.1:<shim>/v1" AND wire_api="responses" pointing
        at the local Responses→Chat shim (codex dropped wire_api="chat";
        GPUStack is Chat-Completions-only — the entrypoint writes this file and
        starts the shim).
      * "Own subscription" runs `codex login --device-auth` first so the user
        can authenticate their ChatGPT/OpenAI subscription interactively.
    """
    cmd = AGENT_LAUNCH_CMD
    if AGENT_KIND == "codex":
        if auth_mode == "subscription":
            return "codex login --device-auth; codex"
        if model:
            return f'codex --model {shlex.quote(model)}'
        return cmd
    if AGENT_KIND in ("opencode",) and model:
        return f'opencode --model {shlex.quote("gpustack/" + model)}'
    if AGENT_KIND in ("gsd-pi",):
        return "gsd"
    return cmd


@app.route("/session/kill", methods=["POST"])
@require_owner
def kill_session():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    if not _SESSION_RE.match(name or ""):
        return jsonify({"error": "invalid session name"}), 400
    _tmux("kill-session", "-t", name)
    forget_session_label(name)
    return jsonify({"ok": True})


@app.route("/session/rename", methods=["POST"])
@require_owner
def rename_session():
    """#235/#223 — name a tab, durably.

    Body: {name: "s1", label: "API work"}; a blank label restores the default
    `sN · repo`. The label is persisted to the home volume, NOT to tmux: the
    design note is explicit that tmux-resurrect does not restore user-options,
    so a label kept there would vanish on the next container restart — which is
    the exact complaint #223 raises about the localStorage version.
    """
    body = request.get_json(silent=True) or {}
    name = body.get("name", "")
    if not _SESSION_RE.match(name or ""):
        return jsonify({"error": "invalid session name"}), 400
    label, error = clean_label(body.get("label"))
    if error:
        return jsonify({"error": error}), 400
    write_prefs({"labels": {name: label}})
    return jsonify({"ok": True, "name": name, "label": label})


@app.route("/api/prefs", methods=["GET", "POST"])
@require_owner
def api_prefs():
    """#234/#222 — read/write the durable UI preferences (currently: theme).

    Server-side rather than localStorage-only so the choice follows the user to
    another machine, which is half of what the issue asks for.
    """
    if request.method == "GET":
        return jsonify(read_prefs())
    body = request.get_json(silent=True) or {}
    patch = {}
    if "theme" in body:
        theme, error = clean_theme(body.get("theme"))
        if error:
            return jsonify({"error": error}), 400
        patch["theme"] = theme
    # #230: settings-dialog defaults for new sessions
    if "default_repo" in body and isinstance(body["default_repo"], str):
        patch["default_repo"] = body["default_repo"]
    if "default_launch" in body and isinstance(body["default_launch"], bool):
        patch["default_launch"] = body["default_launch"]
    return jsonify(write_prefs(patch))


@app.route("/clone", methods=["POST"])
@require_owner
def clone_repo():
    """Clone an arbitrary git URL into the workspace.

    Body: {url, username?, token?}. Credentials (if given) are embedded in the
    clone URL for HTTPS auth and NOT persisted (arbitrary-remote tokens are
    one-shot); the per-user Gitea token is persisted separately via /gitea/token.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    token = (body.get("token") or "").strip()
    username = (body.get("username") or "").strip()
    if not url or not re.match(r"^https?://", url):
        return jsonify({"error": "A https:// git URL is required."}), 400

    parts = urllib.parse.urlsplit(url)
    # M2 anti-PAT-exfil (trusted-host ALLOWLIST, PR #84): never let a hostile LLM
    # exfiltrate a PAT to an attacker host. A SECRET only leaves this box if the
    # user typed a token OR the URL already embeds a password — enforce the
    # allowlist (box Gitea + git.razzfazz.ai + public forges + operator extras)
    # in those cases. Host parsed via urlsplit (userinfo/port/case/IPv6-safe, not
    # substring), so `git.razzfazz.ai.evil.com` / `evil.com/git.razzfazz.ai` are
    # refused. A username is ALSO gated: this endpoint's own convention places a
    # PAT in the username position (token-only -> https://token@host), and HTTP
    # Basic transmits the username to the server, so a secret smuggled into the
    # username field (or the URL's username position) could otherwise exfil to any
    # host. Only fully credential-free public clones are allowed to any host.
    if token or username or parts.username or parts.password:
        target_host = _url_host(url)
        if not target_host or target_host not in _clone_token_host_allowlist():
            return jsonify({
                "error": "A clone token may only be used with a trusted host "
                         "(this box's Gitea, git.razzfazz.ai, github.com, "
                         "gitlab.com, or an operator-configured host). To clone "
                         "from another host, omit the token."
            }), 400

    # #213: build proper userinfo from the SEPARATE username + token fields.
    # Previously the token was jammed in AS the username (https://TOKEN@host),
    # which renders a Gitea PAT (a 40-char hex) as a bogus "hex username" and gave
    # no way to pass a real username or a URL you pre-authed yourself.
    #   URL already has user[:pass] -> clone AS-IS (you embedded creds yourself)
    #   username + token            -> https://username:token@host
    #   token only                  -> https://token@host  (PAT-as-username; GitHub/Gitea)
    #   username only               -> https://username@host
    # Values are URL-encoded so an '@' or ':' in them can't corrupt the netloc.
    if parts.username or parts.password:
        clone_url = url
    elif token or username:
        _q = lambda s: urllib.parse.quote(s, safe="")
        userinfo = (f"{_q(username)}:{_q(token)}" if (username and token)
                    else (_q(token) if token else _q(username)))
        clone_url = urllib.parse.urlunsplit(
            (parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, parts.query, parts.fragment))
    else:
        clone_url = url

    env = dict(os.environ)
    env["HOME"] = str(HOME_DIR)
    env["GIT_TERMINAL_PROMPT"] = "0"
    rc, out = _run(["git", "clone", clone_url], cwd=str(WORKSPACE), env=env, timeout=180)
    # Never echo a secret back in the error. Redact every credential value (typed
    # username/token OR embedded in the URL) in BOTH raw and URL-encoded form — git
    # echoes the percent-encoded userinfo on failure, which a raw .replace misses.
    for _sec in (token, username, parts.username, parts.password):
        if _sec:
            out = out.replace(_sec, "***").replace(urllib.parse.quote(_sec, safe=""), "***")
    if rc != 0:
        return jsonify({"error": f"clone failed: {out[-400:]}"}), 500
    return jsonify({"ok": True, "repos": list_repos()})


# ── Gitea integration (Phase 6) ──────────────────────────────────────────────
@app.route("/gitea/token", methods=["POST"])
@require_owner
def gitea_token():
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token required"}), 400
    _write_secret("gitea_token", token)
    return jsonify({"ok": True})


def _gitea_token():
    return _read_secret("gitea_token") or os.environ.get("GITEA_API_TOKEN", "")


def _gitea_api(path: str):
    token = _gitea_token()
    if not token:
        return None, "No Gitea token stored. Add one first."
    url = GITEA_INTERNAL_URL.rstrip("/") + path
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
        with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Gitea 1.27 enforces PAT scopes. Listing repos (/user/repos) needs BOTH
            # read:user AND read:repository; a clone-only PAT (read:repository) 403s
            # here even though it can clone. Tell the user exactly what to fix.
            return None, ("token is missing a required scope — a Gitea PAT for this "
                          "needs BOTH read:user AND read:repository (regenerate it in "
                          "Gitea → Settings → Applications with both boxes ticked).")
        return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:160]


@app.route("/gitea/repos")
@require_owner
def gitea_repos():
    """List the authenticated user's Gitea repos (one-click clone source)."""
    data, err = _gitea_api("/api/v1/user/repos?limit=50")
    if err:
        return jsonify({"error": err}), 400
    repos = [{
        "full_name": r.get("full_name"),
        "clone_url": r.get("clone_url"),
        "private": r.get("private"),
        "description": (r.get("description") or "")[:120],
    } for r in (data or [])]
    return jsonify({"repos": repos})


@app.route("/gitea/clone", methods=["POST"])
@require_owner
def gitea_clone():
    """One-click clone of one of the user's Gitea repos using the stored token."""
    body = request.get_json(silent=True) or {}
    full_name = (body.get("full_name") or "").strip()
    if not re.match(r"^[\w.-]+/[\w.-]+$", full_name):
        return jsonify({"error": "invalid repo name"}), 400
    token = _gitea_token()
    if not token:
        return jsonify({"error": "No Gitea token stored."}), 400
    host = re.sub(r"^https?://", "", GITEA_INTERNAL_URL).rstrip("/")
    clone_url = f"http://{token}@{host}/{full_name}.git"
    env = dict(os.environ)
    env["HOME"] = str(HOME_DIR)
    env["GIT_TERMINAL_PROMPT"] = "0"
    rc, out = _run(["git", "clone", clone_url], cwd=str(WORKSPACE), env=env, timeout=180)
    out = out.replace(token, "***")
    if rc != 0:
        return jsonify({"error": f"clone failed: {out[-400:]}"}), 500
    return jsonify({"ok": True, "repos": list_repos()})


# ── terminal (attach to a tmux session) ──────────────────────────────────────
@sock.route("/terminal")
def terminal(ws):
    """WebSocket PTY — xterm.js ↔ `tmux attach -t <session>`.

    The session name comes from ?session=<name>. If it doesn't exist yet
    (e.g. race on first load) we create it in the workspace root so the user
    always gets a shell. Attaching (rather than spawning bash) is what gives us
    restart-persistence: the tmux server + its sessions outlive this WS and the
    browser, and survive a container restart via resurrect/continuum.
    """
    # C1 layer-3: refuse the shell to anyone who isn't the owning user. The WS
    # handshake still carries the forwarded X-Authentik-Username header (Caddy
    # forward_auth runs on the Upgrade request too), so the same identity gate
    # as the HTTP routes applies here.
    if not _identity_ok():
        try:
            ws.send("\r\n\x1b[31mForbidden — not authorized for this agent.\x1b[0m\r\n")
        except Exception:  # noqa: BLE001
            pass
        return
    session = request.args.get("session", "")
    if not _SESSION_RE.match(session or ""):
        session = "s1"
    # Ensure the session exists (idempotent).
    rc, _ = _tmux("has-session", "-t", session)
    if rc != 0:
        env0 = dict(os.environ)
        env0["HOME"] = str(HOME_DIR)
        _run(["tmux", "new-session", "-d", "-s", session, "-c", str(WORKSPACE)], env=env0)
        _tmux("set-option", "-t", session, "@repo", "(workspace root)")

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["HOME"] = str(HOME_DIR)

    pty = ptyprocess.PtyProcess.spawn(
        ["tmux", "attach-session", "-t", session],
        env=env,
        dimensions=(24, 220),
        cwd=str(WORKSPACE),
    )

    def _reader():
        try:
            while pty.isalive():
                try:
                    data = pty.read(4096)
                    ws.send(data.decode("utf-8", errors="replace"))
                except EOFError:
                    break
                except Exception:  # noqa: BLE001
                    break
        finally:
            try:
                ws.send("\r\n[detached]\r\n")
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    try:
        while pty.isalive():
            # ws.receive(timeout=N): simple_websocket returns None on a plain
            # TIMEOUT (no keystroke arrived in N seconds) and raises ONLY on a
            # genuine disconnect (ConnectionClosed). We must NEVER break the loop
            # on the idle-input timeout — a session that is actively STREAMING
            # OUTPUT but receiving no keystrokes is perfectly alive, and breaking
            # here would terminate the pty and detach it (#36 / PR #84 — the
            # "detaches after ~2min of no typing while opencode streams" bug).
            # The timeout is now a short LIVENESS POLL (25s) whose only jobs are
            # to (a) re-check pty.isalive() and (b) send a keepalive ping so
            # idle-input sockets stay warm through Caddy / the manager bridge /
            # the browser. On timeout we CONTINUE; we break ONLY on a real close.
            try:
                msg = ws.receive(timeout=25)
            except Exception:  # noqa: BLE001 — ConnectionClosed etc → real disconnect
                break
            if msg is None:
                # Idle poll tick — NOT a disconnect. Send a keepalive so no proxy
                # in the path (Caddy reverse_proxy, manager WS bridge, browser)
                # closes a long idle-input connection, then loop to re-check the
                # pty. Failure to send the keepalive means the socket is really
                # gone → break.
                try:
                    ws.send(json.dumps({"type": "ping"}))
                except Exception:  # noqa: BLE001
                    break
                continue
            try:
                frame = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                pty.write(msg.encode() if isinstance(msg, str) else msg)
                continue
            ftype = frame.get("type")
            if ftype == "input":
                data = frame.get("data", "")
                if data:
                    pty.write(data.encode("utf-8", errors="replace"))
            elif ftype == "resize":
                cols = int(frame.get("cols", 220))
                rows = int(frame.get("rows", 24))
                pty.setwinsize(rows, cols)
                # Also resize the tmux client so the pane fills the browser.
                _tmux("resize-window", "-t", session, "-x", str(cols), "-y", str(rows))
            elif ftype == "ping":
                # Browser keepalive → reply so the browser's socket stays warm too.
                try:
                    ws.send(json.dumps({"type": "pong"}))
                except Exception:  # noqa: BLE001
                    break
    finally:
        # Detach (do NOT kill) so the session persists — a browser close / real
        # disconnect leaves the tmux session running; reload reattaches.
        try:
            pty.terminate()
        except Exception:  # noqa: BLE001
            pass


# ── Phase 5: curated, copy-paste install one-liners for User Defined Agent ───
# We ship NONE of these — they are suggestions the user runs themselves in the
# bare terminal. The persistent per-user volume means the self-installed tool +
# its config survive a container restart. The Claude Code command is the exact
# current official native installer (verified against
# https://code.claude.com/docs/en/setup, 2026-07).
INSTALL_SUGGESTIONS = [
    {
        "name": "Claude Code (Anthropic)",
        "cmd": "curl -fsSL https://claude.ai/install.sh | bash",
        "note": "Official native installer. Requires a Claude Pro/Max/Team/"
                "Enterprise or Console account to log in (run `claude`).",
    },
    # #36 FEATURE 2 (PR #84): gsd-pi + native pi moved here from being their own
    # dedicated agents. Both install under /home/agent (the writable+persistent
    # prefix — NPM_CONFIG_PREFIX=~/.npm-global; see entrypoint.sh) so `npm
    # install -g` works in the read-only-root sandbox and the tool persists.
    {
        # native "pi": the standalone Pi coding-agent CLI (Mario Zechner /
        # earendil-works, pi.dev). npm view verified @earendil-works/
        # pi-coding-agent@0.80.3 (CLI binary `pi`). PRE-WIRED (#36 / PR #84): the
        # user-defined entrypoint seeds ~/.pi/agent/models.json (gpustack
        # provider via the local-model shim, model=running chat default,
        # enable_thinking=false) BEFORE `pi` is installed, so the first run
        # already talks to the local model — no OpenAI 401, no cloud. The seed is
        # idempotent (a hand-edited config is never clobbered).
        "name": "Pi — coding agent (pi.dev)",
        "cmd": "npm install -g @earendil-works/pi-coding-agent",
        "note": "Standalone Pi coding-agent CLI (pi.dev). After install, run "
                "`pi` — it's already wired to the local model (qwen3.6 via "
                "the LLM Manager; ~/.pi/agent/models.json seeded for you). CLI command: "
                "`pi`.",
    },
    {
        # gsd-pi: upstream's task-focused fork of Pi — the MAINTAINED, re-scoped
        # @opengsd package (npm view verified @opengsd/gsd-pi@1.5.0; CLI binary
        # `gsd`, unchanged from the old deprecated `gsd-pi` package). Point it at
        # the local model like the dedicated agents did: gsd reads
        # ~/.gsd/agent/{models.json,settings.json} — set defaultProvider
        # "gpustack" + your GPUStack key/base URL, or run `gsd` and pick the
        # provider in its setup.
        "name": "GSD Pi — task-focused fork of Pi",
        "cmd": "npm install -g @opengsd/gsd-pi",
        "note": "GSD Pi — the local-first, task-focused fork of Pi. Run "
                "`gsd` to start; point it at the local model in `gsd`'s provider "
                "setup (the LLM Manager, http://llm:8080/v1). CLI command: "
                "`gsd`.",
    },
    {
        # DeepSeek Harness (#556): npm view verified @deepseek-ai/dsh@0.1.1-rc.2
        # on 2026-08-22 (CLI binary `dsh`, MIT, repo github.com/deepseek-ai/
        # deepseek-harness, maintainer tianyi@deepseek.com). DEVELOPER PREVIEW —
        # upstream warns of breaking changes. SUPPLY-CHAIN NOTE: the unscoped npm
        # name "deepseek-harness" is an unofficial third-party name RESERVATION
        # (2026-08-19, personal account) — only ever suggest the @deepseek-ai
        # scope. PRE-WIRED like Pi (#556): dsh embeds a fork of Pi's LLM layer,
        # so the user-defined entrypoint seeds $DSH_HOME/settings.yaml
        # (`llm-pi-ai:` root, gpustack provider via the local shim, apiKeyEnv)
        # and pins DSH_HOME=~/.dsh in the managed env file. Seed-if-absent.
        "name": "DeepSeek Harness (dsh)",
        "cmd": "npm install -g @deepseek-ai/dsh",
        "note": "DeepSeek's agent runtime (developer preview, MIT). Already "
                "wired to the local model ($DSH_HOME/settings.yaml seeded for "
                "you). Run `dsh --help` for modes; `dsh web` serves its browser "
                "UI on loopback inside this terminal. CLI command: `dsh`.",
    },
    {
        "name": "OpenAI Codex CLI",
        "cmd": "npm install -g @openai/codex",
        "note": "Apache-2.0. Also available pre-installed as the dedicated "
                "Codex agent. `codex login` to authenticate.",
    },
    {
        "name": "Google Gemini CLI",
        "cmd": "npm install -g @google/gemini-cli",
        "note": "Run `gemini`; authenticate with a Google account or API key.",
    },
    {
        "name": "Aider (pip)",
        "cmd": "python3 -m pip install --user aider-install && aider-install",
        "note": "AI pair-programming in your terminal. Point it at any "
                "OpenAI-compatible endpoint with --openai-api-base.",
    },
    {
        "name": "Cursor CLI (cursor-agent)",
        "cmd": "curl https://cursor.com/install -fsS | bash",
        "note": "Cursor's terminal agent. Requires a Cursor account.",
    },
]


if __name__ == "__main__":
    port = int(os.environ.get("CODING_TOOLS_WEB_PORT", "3004"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


# ── #236: one-shot replay at web-UI boot ─────────────────────────────────────
# The entrypoint started tmux + the continuum restore BEFORE this process;
# by now restored panes either run their (allowlisted) process again or sit
# on a bare shell. Guarded for tests/import-safety; failure never blocks the
# UI — the tabs still open, just without the auto-run.
if os.environ.get("RAZZFAZZ_SKIP_TAB_REPLAY") != "1":  # pragma: no cover
    try:
        _replayed = replay_pending_tabs()
        if _replayed["replayed"] or _replayed["dropped"]:
            logging.getLogger(__name__).info("tab replay (#236): %s", _replayed)
    except Exception:  # noqa: BLE001
        pass
