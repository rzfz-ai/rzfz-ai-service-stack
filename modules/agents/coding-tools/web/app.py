#!/usr/bin/env python3
"""
Coding Tools Web UI
Lightweight Flask app on port 3004 providing a browser terminal for
gsd-pi and opencode. WebSocket PTY backed by xterm.js.

Endpoints:
  GET  /          — HTML dashboard with terminal
  GET  /health    — JSON healthcheck
  WS   /terminal  — WebSocket PTY (xterm.js ↔ /bin/bash in /workspace)
"""

import os
import json
import time
import threading
import subprocess
from pathlib import Path

import flask
from flask import Flask, Response
from flask_sock import Sock
import ptyprocess

app = Flask(__name__, template_folder="templates")
sock = Sock(app)

START_TIME = time.time()
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
HOME_DIR = Path(os.environ.get("HOME", "/home/agent"))
GITEA_EXTERNAL_URL = os.environ.get("GITEA_EXTERNAL_URL", "")
GITEA_INTERNAL_URL = os.environ.get("GITEA_INTERNAL_URL", "http://gitea:3000")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
VERSION_GSD = None
VERSION_OC  = None


def _tool_version(cmd):
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
        v = (r.stdout.strip() or r.stderr.strip()).split("\n")[0]
        return v if v else "unknown"
    except Exception:
        return "not found"


def get_uptime():
    secs = int(time.time() - START_TIME)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def check_gitea():
    import urllib.request, ssl
    url = GITEA_INTERNAL_URL.rstrip("/") + "/api/v1/version"
    token = os.environ.get("GITEA_API_TOKEN", "")
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
        with urllib.request.urlopen(req, context=ctx, timeout=4) as r:
            data = json.loads(r.read())
            return {"ok": True, "version": data.get("version", "?")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def workspace_stats():
    try:
        repos = [d for d in WORKSPACE.iterdir() if d.is_dir() and (d / ".git").exists()]
        files = sum(1 for _ in WORKSPACE.rglob("*") if _.is_file())
        return {"repos": len(repos), "files": files}
    except Exception:
        return {"repos": 0, "files": 0}


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return Response(
        json.dumps({"status": "ok", "uptime_seconds": int(time.time() - START_TIME)}),
        content_type="application/json"
    )


@app.route("/")
def index():
    global VERSION_GSD, VERSION_OC
    if VERSION_GSD is None:
        VERSION_GSD = _tool_version("gsd")
    if VERSION_OC is None:
        VERSION_OC = _tool_version("opencode")

    gitea = check_gitea()
    ws = workspace_stats()
    context = {
        "uptime": get_uptime(),
        "version_gsd": VERSION_GSD,
        "version_oc": VERSION_OC,
        "gitea_ok": gitea["ok"],
        "gitea_info": gitea.get("version", gitea.get("error", "")),
        "gitea_external_url": GITEA_EXTERNAL_URL,
        "llm_base_url": LLM_BASE_URL,
        "workspace": str(WORKSPACE),
        "repos": ws["repos"],
        "files": ws["files"],
        "token_set": bool(os.environ.get("GITEA_API_TOKEN")),
        "llm_key_set": bool(os.environ.get("LLM_API_KEY")),
    }
    return flask.render_template("index.html", **context)


# ── terminal ─────────────────────────────────────────────────────────────────

@sock.route("/terminal")
def terminal(ws):
    """WebSocket PTY — xterm.js ↔ bash in /workspace.

    Client frames (JSON):
      {"type": "input",  "data": "<chars>"}
      {"type": "resize", "cols": N, "rows": N}
      {"type": "ping"}
    Server sends raw terminal output as UTF-8 text frames.
    """
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["HOME"] = str(HOME_DIR)
    env["WORKSPACE"] = str(WORKSPACE)
    env["BASH_ENV"] = ""

    pty = ptyprocess.PtyProcess.spawn(
        ["/bin/bash", "--login"],
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
                except Exception:
                    break
        finally:
            try:
                ws.send("\r\n[terminal closed]\r\n")
            except Exception:
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    try:
        while pty.isalive():
            try:
                msg = ws.receive(timeout=120)
            except Exception:
                break
            if msg is None:
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
            # ping: no-op, just keeps the connection alive
    finally:
        try:
            pty.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("CODING_TOOLS_WEB_PORT", "3004"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
