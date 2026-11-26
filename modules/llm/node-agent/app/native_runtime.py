# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2559 — the macOS thin-worker agent: the node-agent protocol without Docker.

Registration + heartbeat, the command channel and the outbound relay are the
Linux agent's own modules (`registration`, `commands.run_command_cycle`,
`runtime.maybe_start_relay`); what differs is the engine: a native Ollama on
loopback that the agent manages through its HTTP API (drivers/ollama.py), on
the worker's own port (app/ollama_port.py: the bundle's ollama-port.conf, not
Ollama's default port, which the Mac's own Ollama usually holds). The one port the agent binds is its own loopback
liveness, 127.0.0.1:8090 (`serve_health`): what agent-upgrade.sh waits for
after a restart, with the running bundle's digest.

Two threads (finding 14): the heartbeat registers every interval no matter
what, and the command thread claims and runs commands — a 6-hour `ollama pull`
blocks only the command thread. `loaded` is shared between them (and the relay
thread) under one lock; nothing slow ever runs while the lock is held.

Known limit (D33 checklist): commands claimed in the same batch as a long pull
(an unload, an upgrade, a second load) wait behind it on the command thread —
head-of-line, acceptable for one Mac worker; the manager has no claim lease.

The relay forwards only OpenAI `/v1/` paths (`relay_path_allowed`): Ollama's
management API (`/api/*`) shares the loopback base and has no auth. Known and
accepted: `GET /v1/models` lists every model Ollama holds, hand-pulled ones too."""
from __future__ import annotations

import logging
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from app.drivers.ollama import OllamaDriver, ollama_ref
from app.ollama_port import managed_base
from app.registration import build_registration

log = logging.getLogger("node_agent.native")
ROOT = Path(os.environ.get("RZFZ_WORKER_DIR", "/opt/rzfz-worker"))

#: #2869: what the agent serves, kept under <root>/state/ (not in a release dir:
#: agent-upgrade.sh swaps releases, this survives it). A restarted agent reads it
#: back and registers the same instances again.
STATE_FILE = Path("state") / "loaded.json"

#: The only host scripts the agent ever runs, all from the RUNNING bundle at
#: <root>/current/mac-agent/bin/ (D32/D33, finding 16). A command payload names
#: a kind, never a script or a path.
SPAWNABLE_SCRIPTS = frozenset({"ollama-switch.sh", "agent-upgrade.sh"})

#: An Ollama release version as D31's route passes it (the bundle's pin):
#: X.Y.Z, optionally -rcN. ASCII digits only; anything else never reaches argv.
OLLAMA_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-rc[0-9]{1,3})?")

#: The ONLY environment the upgrade scripts inherit (an allowlist: they read
#: what they need from <root>/.env.node themselves, and a detached child
#: outlives us). Proxy variables pass so curl reaches the master on a proxied
#: site (#283) — but never one carrying credentials (userinfo).
_SPAWN_ENV = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "SSL_CERT_FILE"})
_SPAWN_PROXY_ENV = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"})

#: #1932's vocabulary for `engine_version_why` (drivers/base.ENGINE_VERSION_WHY).
WHY_PROBED, WHY_PROBE_FAILED = "probed", "probe-failed"

#: What the relay forwards to Ollama: an OpenAI path, optionally behind the
#: #2521 engine prefix. Plain characters only — no percent-encoding, no dot
#: segments, no empty segments — so nothing a proxy or Ollama normalises can
#: turn it into `/api/...`.
_RELAY_PATH = re.compile(r"(?:/e/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?/v1(?:/[A-Za-z0-9_.:-]+)+"
                         r"(?:\?[A-Za-z0-9_.~=&-]*)?")


def relay_path_allowed(path) -> bool:
    """#2559 review I1: only OpenAI inference paths reach Ollama."""
    if not isinstance(path, str) or not _RELAY_PATH.fullmatch(path):
        return False
    segments = path.split("?", 1)[0].split("/")[1:]
    return not any(seg in (".", "..") for seg in segments)


def _spawn_env(root) -> dict:
    env = {}
    for k, v in os.environ.items():
        if k in _SPAWN_ENV or k.startswith("LC_"):
            env[k] = v
        elif k in _SPAWN_PROXY_ENV and "@" not in v:
            env[k] = v
    env["RZFZ_WORKER_DIR"] = str(root)
    return env


class NativeOllamaNode:
    def __init__(self, env: dict, *, driver=None, metrics_fn=None, root=None):
        self.env = env
        # Ollama listens on loopback only (the plist's OLLAMA_HOST); the master
        # reaches it through the agent's outbound relay, never directly.
        self.driver = driver or OllamaDriver(managed_base(env))
        if urlparse(self.driver.base).hostname != "127.0.0.1":
            raise ValueError(f"the Mac agent manages Ollama on loopback only, not {self.driver.base!r} (#2559)")
        self.metrics_fn = metrics_fn or (lambda: {})
        self.root = Path(root) if root is not None else ROOT
        self.loaded: dict = {}
        self._lock = threading.Lock()   # heartbeat, command and relay threads

    # ── #2869: survive an agent restart ─────────────────────────────────────
    # `loaded` used to live in memory only, so every restart (self-upgrade,
    # reboot, launchd KeepAlive, ollama-port-migrate.sh) registered the worker
    # with ZERO instances; the manager pruned the instance row and, reconciling
    # in observe mode, never re-dispatched the load, while the model was still
    # in Ollama. The agent now writes the instances it serves to <root>/state
    # and, on start, registers them again; sync() then holds each one against
    # what Ollama really lists (missing -> failed, Ollama down -> loading).
    #
    # Why a record and not Ollama's own answer: Ollama cannot say which of its
    # tags are the agent's (the alias and the hf.co ref sit next to the
    # operator's own models) and has no idea of the manager's instance_id, which
    # keys the instance row and the later unload. `/api/ps` lists only models
    # held in memory, and Ollama drops those after its keep-alive, so after a
    # quiet hour it would report nothing for a model that is still deployed.
    def _state_path(self) -> Path:
        return self.root / STATE_FILE

    def _persist(self) -> None:
        """Write the ready instances. Never raises: serving matters more."""
        with self._lock:
            keep = {iid: {k: rec.get(k) for k in ("model", "ollama_ref", "task")}
                    for iid, rec in self.loaded.items()
                    if rec.get("status") == "ready" or rec.get("ollama_down")}
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(keep, fh, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot save the served instances to %s: %s (#2869)", path, str(exc)[:200])

    def readopt(self) -> int:
        """Re-adopt the instances the previous process was serving. Returns the
        count. They come back `ready`; the heartbeat's sync() runs before the
        first registration and corrects any that Ollama no longer holds."""
        try:
            saved = json.loads(self._state_path().read_text())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s (#2869)", self._state_path(), str(exc)[:200])
            return 0
        n = 0
        with self._lock:
            for iid, rec in (saved.items() if isinstance(saved, dict) else ()):
                if not (isinstance(iid, str) and iid and isinstance(rec, dict)
                        and all(isinstance(rec.get(k), str) and rec[k] for k in ("model", "ollama_ref"))
                        and iid not in self.loaded):
                    continue
                task = rec.get("task") if isinstance(rec.get("task"), str) else "chat"
                # the endpoint is rebuilt, never read back: the port may have moved (#2860)
                self.loaded[iid] = {"model": rec["model"], "endpoint": f"{self.driver.base}/v1",
                                    "status": "ready", "detail": None, "task": task,
                                    "ollama_ref": rec["ollama_ref"]}
                n += 1
        if n:
            log.info("re-adopted %d served instance(s) after restart (#2869)", n)
        return n

    def snapshot(self) -> dict:
        """A consistent copy of `loaded` for readers on other threads."""
        with self._lock:
            return {iid: dict(rec) for iid, rec in self.loaded.items()}

    # ── registration ────────────────────────────────────────────────────────
    def pinned_ollama(self):
        """Finding 16: the pin of the RUNNING bundle, read every time — a new
        bundle (agent-upgrade.sh flips `current`) brings its own pins.json."""
        try:
            pins = json.loads((self.root / "current" / "mac-agent" / "pins.json").read_text())
            return (pins.get("ollama") or {}).get("version")
        except (OSError, ValueError, AttributeError):
            return None

    def registration(self) -> dict:
        try:
            engine_version, why = self.driver.version() or None, WHY_PROBED
        except Exception as exc:  # noqa: BLE001 - a down Ollama must not stop the heartbeat
            log.warning("Ollama did not answer /api/version on %s: %s", self.driver.base, str(exc)[:200])
            engine_version, why = None, WHY_PROBE_FAILED
        worker = {"name": self.env["LLM_WORKER_NAME"],
                  "address": self.env.get("LLM_WORKER_ADDRESS", "mac-worker-agent"),
                  "hardware": "apple", "engine": "ollama",
                  "stack_version": self.env.get("RAZZFAZZ_VERSION"),
                  "engine_version": engine_version, "engine_version_why": why,
                  "agent_image_digest": self.env.get("LLM_WORKER_AGENT_DIGEST"),
                  "advertise_addr": self.env.get("LLM_WORKER_ADVERTISE_ADDR") or None}
        body = build_registration(worker, self.snapshot())
        body.update({k: v for k, v in self.metrics_fn().items() if v is not None})
        body["agent_managed"] = True
        body["ollama_pinned"] = self.pinned_ollama()
        # One Ollama endpoint serves every model here, so every instance has the
        # same host (127.0.0.1). A per-engine relay URL (`/e/<host>/`, #2521)
        # would name that host for all of them and the node would answer the
        # first record's model or refuse; the per-worker URL routes by the
        # model the request names (#1535), which is exactly right for Ollama.
        body["relay_engine_routing"] = False
        # #3403: Ollama runs no llama.cpp runner image, so it resolves no runner
        # channel; an experimental-channel deployment is never placed here.
        body["runner_channels"] = []
        return body

    def sync(self) -> None:
        """Reconcile `loaded` with what Ollama holds. Ollama not answering
        demotes every `ready` record to `loading` (the router serves only
        `ready`), remembered per record; the next answer restores them."""
        try:
            present = set(self.driver.list_models())
        except Exception:
            with self._lock:
                for rec in self.loaded.values():
                    if rec.get("status") == "ready":
                        rec.update(status="loading", ollama_down=True,
                                   detail=f"Ollama is not answering on {self.driver.base} (#2559)")
            raise
        with self._lock:
            for rec in self.loaded.values():
                if rec.pop("ollama_down", False):
                    rec.update(status="ready", detail=None)
                if rec.get("status") == "ready" and not ({rec["model"], f"{rec['model']}:latest"} & present):
                    rec["status"] = "failed"
                    rec["detail"] = f"{rec['model']} is no longer in Ollama — redeploy it (#2559)"

    # ── commands ────────────────────────────────────────────────────────────
    def execute(self, cmd: dict) -> dict:
        kind, args = cmd.get("kind"), cmd.get("args") or {}
        if kind == "load_engine":
            return self._load(args)
        if kind == "unload_engine":
            return self._unload(args)
        if kind == "upgrade_ollama":
            version = args.get("version")
            if not (isinstance(version, str) and OLLAMA_VERSION.fullmatch(version)):
                raise ValueError(f"refusing Ollama version {str(version)[:40]!r}: expected X.Y.Z (#2559)")
            return self._spawn("ollama-switch.sh", version)
        if kind == "upgrade_agent":
            # no argument: the agent always moves to the master's current bundle
            return self._spawn("agent-upgrade.sh")
        raise ValueError(f"command {kind!r} is not supported on a Mac worker (#2559)")

    def _load(self, args: dict) -> dict:
        task = args.get("task") or "chat"
        if not self.driver.supports(task):
            # refused BEFORE anything is recorded: no `loading`, never `ready`
            raise ValueError(f"Ollama does not serve the {task!r} task (no /v1/rerank) — "
                             f"place it on a llama.cpp worker (#2559)")
        weight = next((f for f in args.get("files") or []
                       if isinstance(f, str) and "mmproj" not in f.lower()), "")
        ref = ollama_ref(args.get("hf_repo") or "", weight, (args.get("params") or {}).get("ollama_tag"))
        iid, model = args["instance_id"], args["model"]
        with self._lock:
            self.loaded[iid] = {"model": model, "endpoint": f"{self.driver.base}/v1",
                                "status": "loading", "detail": None, "task": task, "ollama_ref": ref}
        try:
            # outside the lock: the pull blocks for hours
            self.driver.pull(ref)
            self.driver.alias(ref, model)
        except Exception as exc:
            with self._lock:
                if iid in self.loaded:
                    self.loaded[iid].update(status="failed", detail=f"ollama pull/alias failed: {str(exc)[:200]}")
            raise
        with self._lock:
            if iid in self.loaded:
                self.loaded[iid]["status"] = "ready"
        self._persist()
        return {"status": "ready", "instance_id": iid, "ollama_ref": ref}

    def _unload(self, args: dict) -> dict:
        """Idempotent and never orphans weights: the record stays (out of
        routing, `unloading`) until both deletes succeeded — the driver treats
        a 404 as done — so a retry after a partial failure finishes the job."""
        iid = args.get("instance_id")
        with self._lock:
            rec = self.loaded.get(iid)
            if rec is None:
                # #2953: answering {"unloaded": <unknown id>} told the manager
                # an instance was gone while Ollama still held it.
                raise ValueError(f"no instance {iid!r} on this worker; nothing unloaded "
                                 f"(it serves {sorted(self.loaded)})")
            others = [r for k, r in self.loaded.items() if k != iid]
            shared = any(r.get("ollama_ref") == rec["ollama_ref"] for r in others)
            alias_shared = any(r.get("model") == rec["model"] for r in others)
            rec.update(status="unloading", detail=None)
            rec.pop("ollama_down", None)
            model, ref = rec["model"], rec["ollama_ref"]
        errors = []
        if not alias_shared:
            try:   # #2953: out of memory, not only off disk; best effort
                self.driver.evict(model)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not evict %s from memory: %s", model, str(exc)[:200])
        for name, keep in ((model, alias_shared), (ref, shared)):
            if keep:
                continue
            try:
                self.driver.delete(name)
            except Exception as exc:  # noqa: BLE001 - try both, then report
                errors.append(f"{name}: {str(exc)[:200]}")
        with self._lock:
            if errors:
                if iid in self.loaded:
                    self.loaded[iid].update(status="failed",
                                            detail="unload incomplete, retry it — " + "; ".join(errors))
        if errors:
            self._persist()   # a half-deleted instance must not come back `ready`
            raise RuntimeError("ollama delete failed: " + "; ".join(errors))
        with self._lock:
            self.loaded.pop(iid, None)
        self._persist()
        return {"unloaded": iid}

    def _spawn(self, script: str, *args: str) -> dict:
        """Upgrades outlive the agent (they restart it): run detached, report
        later. Only an allowlisted script from the running bundle, argv as a
        list, no shell, and an environment without credentials."""
        if script not in SPAWNABLE_SCRIPTS:
            raise ValueError(f"refusing to run {script!r}: not one of the bundle's upgrade scripts (#2559)")
        path = self._script_path(script)
        env = _spawn_env(self.root)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        with open(self.root / "logs" / f"{script}.log", "ab") as out:
            subprocess.Popen([str(path), *args], shell=False, start_new_session=True, env=env,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        return {"scheduled": script, "args": list(args)}

    def _script_path(self, script: str) -> Path:
        """`current/mac-agent/bin/<script>` resolved: it must land on a regular
        file inside <root>/releases/ that only its owner can write."""
        link = self.root / "current" / "mac-agent" / "bin" / script
        try:
            real = link.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{script} is missing from the running bundle: {exc} (#2559)") from exc
        releases = (self.root / "releases").resolve()
        st = real.stat()
        if releases not in real.parents or not real.is_file() or st.st_mode & 0o022:
            raise ValueError(f"refusing {script}: {real} is not an owner-only file inside {releases} (#2559)")
        return real


def start_loops(node, *, register, run_commands, interval: float, stop: threading.Event):
    """#2559 finding 14: two daemon threads. The heartbeat syncs and registers
    every `interval` seconds no matter what; the command thread claims and runs
    commands, and a 6-hour `ollama pull` blocks only IT. Returns both threads."""
    def _beat():
        while not stop.is_set():
            try:
                node.sync()
            except Exception:  # noqa: BLE001 - a down Ollama still registers
                log.warning("Ollama model sync failed (non-fatal)", exc_info=True)
            try:
                register(node.registration())
            except Exception:  # noqa: BLE001 — the heartbeat must survive a bad cycle
                log.warning("registration cycle failed (non-fatal)", exc_info=True)
            stop.wait(interval)

    def _commands():
        while not stop.is_set():
            try:
                run_commands()
            except Exception:  # noqa: BLE001
                log.warning("command cycle failed (non-fatal)", exc_info=True)
            stop.wait(interval)

    beat = threading.Thread(target=_beat, name="mac-heartbeat", daemon=True)
    cmds = threading.Thread(target=_commands, name="mac-commands", daemon=True)
    beat.start(); cmds.start()
    return beat, cmds


class _RelayState:
    """Exactly what `runtime.maybe_start_relay`, `warn_on_registration_rejection`
    and the relay's engine lookup (`_engine_records`) touch on `app.state`:
    `worker_id`, `relay_thread`, `relay_stop`, `loaded` — the last one a locked
    snapshot, because the relay thread iterates it while commands change it —
    and `relay_path_allowed`, the Mac's relay path filter (review I1)."""

    relay_path_allowed = staticmethod(relay_path_allowed)

    def __init__(self, node):
        self._node = node
        self.worker_id = None
        self.relay_thread = None
        self.relay_stop = None

    @property
    def loaded(self):
        return self._node.snapshot()


class _RelayApp:
    def __init__(self, node):
        self.state = _RelayState(node)


def build_cycle(env: dict, node, *, http_post, registered=None):
    """The heartbeat's `register` and the command thread's `run_commands`,
    wired to the Linux agent's own registration, command and relay code.
    Returns (register, run_commands, app). Keys travel only in headers."""
    from app.commands import run_command_cycle
    from app.registration import register_with_manager
    from app.runtime import _worker_id_from, maybe_start_relay, warn_on_registration_rejection

    if not (env.get("LLM_WORKER_ADVERTISE_ADDR") and os.environ.get("LLM_WORKER_ADVERTISE_ADDR")):
        # A Mac worker is always remote: without the address its loopback
        # endpoint would be registered as a direct one (the master dials its
        # OWN 127.0.0.1) and the relay (which reads os.environ) never arms.
        msg = ("LLM_WORKER_ADVERTISE_ADDR is not set — a Mac worker is always remote and is "
               "reached only through the relay; set it in <root>/.env.node (#2559)")
        log.error(msg)
        raise ValueError(msg)
    url = env["LLM_MANAGER_URL"].rstrip("/")
    # #285 (as runtime.configure_runtime): the per-worker key when enrolled,
    # else the shared node key — for registration and the relay alike.
    key = env.get("LLM_WORKER_COMMAND_KEY") or env.get("LLM_MANAGER_NODE_KEY", "")
    ckey = env.get("LLM_WORKER_COMMAND_KEY") or None
    app = _RelayApp(node)

    def register(body):
        resp = register_with_manager(url, key, body, http_post=http_post)
        warn_on_registration_rejection(app, resp, now=time.monotonic(),
                                       node_name=body.get("name"), manager_url=url)
        status = getattr(resp, "status_code", 0) or 0
        if registered is not None and 200 <= status < 300:
            registered.set()   # serve_health's "ok" (D33 review I1)
        app.state.worker_id = app.state.worker_id or _worker_id_from(resp)
        if app.state.worker_id:
            maybe_start_relay(app, manager_url=url, node_key=key)

    def run_commands():
        if app.state.worker_id:
            # inline on THIS thread (dispatch_async=None): the command thread
            # is the async runner — the heartbeat never waits for it
            run_command_cycle(app.state.worker_id, manager_url=url, node_key=key, http_post=http_post,
                              execute=node.execute, command_key=ckey, dispatch_async=None)

    return register, run_commands, app


_ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")


def load_env_node(path, environ=None) -> list:
    """#2559 (D30 carry): `<root>/.env.node` into the PROCESS environment —
    `KEY=VALUE` lines, a key already set wins (the plist's own values). It must
    be `os.environ`, not a copy: the relay (`runtime.maybe_start_relay`) reads
    `LLM_WORKER_ADVERTISE_ADDR` from there. The trust anchor the installer
    wrote (`LLM_WORKER_CA_FILE`) becomes `SSL_CERT_FILE`, which both the
    master-facing httpx client and the relay's WebSocket (ssl default context)
    honour. Returns the keys it set — names only, never values."""
    environ = os.environ if environ is None else environ
    path = Path(path)
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.error("%s is missing — the worker is not enrolled (run the installer from Fleet → Add worker)", path)
        return []
    if st.st_mode & 0o077:
        log.warning("%s was readable by others (mode %o) — tightened to 0600 (#2559)", path, st.st_mode & 0o777)
        os.chmod(path, 0o600)
    set_keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key) or key in environ:
            continue
        environ[key] = value
        set_keys.append(key)
    if not environ.get("SSL_CERT_FILE") and environ.get("LLM_WORKER_CA_FILE"):
        environ["SSL_CERT_FILE"] = environ["LLM_WORKER_CA_FILE"]
        set_keys.append("SSL_CERT_FILE")
    return set_keys


def bundle_digest_of(release) -> str | None:
    """A release directory's digest (`<release>/.digest`, written by the
    installer and by agent-upgrade.sh), or None."""
    try:
        value = (Path(release) / ".digest").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if re.fullmatch(r"sha256:[0-9a-f]{64}", value) else None


def running_bundle_digest(root) -> str | None:
    """The running bundle's digest (`current/.digest`, written by the
    installer) — what Fleet shows as the agent version (D22's key)."""
    return bundle_digest_of(Path(root) / "current")


#: The release this very code was imported from (<release>/app/native_runtime.py).
OWN_RELEASE = Path(__file__).resolve().parents[1]


def adopt_running_digest(environ, release=None) -> str | None:
    """#2559: `LLM_WORKER_AGENT_DIGEST` (the registration's
    `agent_image_digest`) is the digest of the release this process RUNS —
    always overwritten, never `setdefault`: after agent-upgrade.sh flipped
    `current` a value inherited from anywhere else would report the old
    bundle. No digest (a checkout, not a release) removes the key."""
    digest = bundle_digest_of(OWN_RELEASE if release is None else release)
    if digest:
        environ["LLM_WORKER_AGENT_DIGEST"] = digest
    else:
        environ.pop("LLM_WORKER_AGENT_DIGEST", None)
    return digest


#: serve_health's address. Loopback only: the master never dials the Mac
#: (it reaches it through the relay), and agent-upgrade.sh asks from the Mac.
HEALTH_HOST, HEALTH_PORT = "127.0.0.1", 8090


def serve_health(digest: str, port: int = HEALTH_PORT, ready=None):
    """#2559: loopback-only liveness with the running bundle's digest.

    `GET /health` answers 503 `{"status": "starting", "digest": …}` until
    `ready()` is true — in `main()`: the first registration was accepted (2xx)
    and the heartbeat and command threads are alive — and 200
    `{"status": "ok", "digest": …}` from then on (503 again if a thread dies).
    agent-upgrade.sh accepts only "ok" with the NEW digest and rolls back
    otherwise: a bundle that imports and binds but never registers, or
    crash-loops, is not "up" (D33 review I1). `ready=None` means ready.
    Anything but /health is 404. Returns the server (a daemon thread runs it)."""
    import http.server

    def _body(status):
        return json.dumps({"status": status, "digest": digest}).encode()

    class _Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            if self.path != "/health":
                code, payload = 404, b'{"error": "not found"}'
            else:
                try:
                    up = True if ready is None else bool(ready())
                except Exception:  # noqa: BLE001 - a broken probe is "not up", never a crash
                    up = False
                code, payload = (200, _body("ok")) if up else (503, _body("starting"))
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer((HEALTH_HOST, port), _Health)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="mac-health", daemon=True).start()
    return srv


def agent_ready(registered: threading.Event, threads: list):
    """The `ready` of serve_health in `main()`: a registration was accepted AND
    both loops (heartbeat, commands) are running."""
    def ready() -> bool:
        return registered.is_set() and len(threads) == 2 and all(t.is_alive() for t in threads)
    return ready


def _write_private(path: str, text: str) -> None:
    """0600 from the first byte, replaced by rename — an existing file with a
    looser mode never holds the new keys, not even for a moment."""
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def enroll(manager: str, token: str, name: str, ca_file: str, env_path: str, *, http_post,
           advertise_addr: str = "") -> dict:
    """#2559: the Mac's enrolment — the `POST /api/workers/enroll` contract of
    cli/worker-join.sh (`{token, name}` → `{node_name, command_key,
    registry_user, registry_password}`), in Python: macOS ships bash 3.2 and
    worker-join is bash-4 code. Writes the same `.env.node` keys, plus the
    advertise address (a Mac worker is always remote, reached only through the
    relay). Nothing it handles is ever logged or printed."""
    manager = manager.rstrip("/")
    url = f"{manager}/api/workers/enroll"
    body = {"token": token}
    if name:
        body["name"] = name
    resp = http_post(url, json=body)
    status = getattr(resp, "status_code", 0)
    if status != 200:
        raise SystemExit(f"enrolment failed (HTTP {status or '?'}) — mint a fresh token in Fleet → Add worker")
    answer = resp.json()
    if not isinstance(answer, dict) or not answer.get("command_key"):
        raise SystemExit("the master answered without a command key — nothing written")
    node_name = answer.get("node_name") or name
    values = {"LLM_WORKER_COMMAND_KEY": answer["command_key"], "LLM_MANAGER_URL": manager,
              "LLM_WORKER_NAME": node_name, "LLM_WORKER_CA_FILE": ca_file,
              "LLM_WORKER_REGISTRY_USER": answer.get("registry_user") or "",
              "LLM_WORKER_REGISTRY_PASSWORD": answer.get("registry_password") or "",
              "LLM_WORKER_ADVERTISE_ADDR": advertise_addr or node_name}
    bad = [k for k, v in values.items() if "\n" in str(v) or "\r" in str(v)]
    if bad:
        raise SystemExit(f"refusing a value with a line break for {', '.join(bad)} — nothing written")
    _write_private(env_path, "".join(f"{k}={v}\n" for k, v in values.items()))
    return values


def enroll_cli(argv, *, stdin, http_post=None) -> int:
    """`python -m app.native_runtime enroll --manager … --name … --ca-file …
    --env-file … [--advertise …]`, the enrolment TOKEN on stdin: never on
    argv (world-readable through `ps`), never in a file."""
    import argparse

    ap = argparse.ArgumentParser(prog="app.native_runtime enroll")
    ap.add_argument("--manager", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--ca-file", required=True)
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--advertise", default="")
    args = ap.parse_args(argv)
    token = stdin.read().strip()
    if not token:
        raise SystemExit("no enrolment token on stdin")
    if http_post is None:  # pragma: no cover - the installer's path (D33)
        import httpx

        client = httpx.Client(timeout=30, verify=args.ca_file, trust_env=True)

        def http_post(u, *, json):
            return client.post(u, json=json)
    values = enroll(args.manager, token, args.name, args.ca_file, args.env_file,
                    http_post=http_post, advertise_addr=args.advertise)
    print(f"enrolled as {values['LLM_WORKER_NAME']}")
    return 0


def main() -> None:  # pragma: no cover - exercised on the Mac (D33)
    """Heartbeat and command threads (start_loops), the relay armed after the
    first registration answered with a worker id."""
    import sys

    if sys.argv[1:2] == ["enroll"]:
        raise SystemExit(enroll_cli(sys.argv[2:], stdin=sys.stdin))

    import httpx

    from app.mac_metrics import sample

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_node(ROOT / ".env.node")
    digest = adopt_running_digest(os.environ)
    registered, threads = threading.Event(), []
    try:
        # "starting" (503) until a registration is accepted and both loops run
        serve_health(digest or "", ready=agent_ready(registered, threads))
    except OSError as exc:
        # no liveness: an upgrade to this release then rolls back (exit 4),
        # but the heartbeat and the models keep running
        log.error("cannot serve %s:%s/health: %s (#2559)", HEALTH_HOST, HEALTH_PORT, exc)
    env = dict(os.environ)
    node = NativeOllamaNode(env, metrics_fn=sample)
    node.readopt()   # #2869: the instances a previous process was serving
    client = httpx.Client(timeout=30, verify=env.get("SSL_CERT_FILE") or True,
                          trust_env=True)  # #1064: master-facing, honours a proxy like the Linux report client

    def post(u, *, json, headers):
        return client.post(u, json=json, headers=headers)

    register, run_commands, _app = build_cycle(env, node, http_post=post, registered=registered)
    stop = threading.Event()
    threads.extend(start_loops(node, register=register, run_commands=run_commands,
                               interval=float(env.get("LLM_WORKER_REPORT_INTERVAL", "30")), stop=stop))
    while not stop.is_set():
        time.sleep(3600)


if __name__ == "__main__":  # pragma: no cover
    main()
