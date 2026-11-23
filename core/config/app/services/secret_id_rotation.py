# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 D7 (rulings 4, 10, 11) — renew the 24 h secret_id of every Dify app
bound to a technical user, without ever publishing someone's unfinished draft.

Per bound Dify app, once its secret is 12 h old: mint a new secret_id (older
ones expire by their own 24 h TTL), patch the app's OPENBAO_SECRET_ID secret
variable on the draft and republish, ONLY when the draft equals the published
version. Otherwise skip the app with WARN "rotation skipped: unpublished
draft"; the manual rotate stays on Settings → Technical users. Also WARN below
20 % of a secret_id's uses and when an app's secret is 20 h old (it expires at
24 h). State and warnings go to <stack>/.openbao-state/; no secret ever does.

The CIDR bound is Caddy's FIXED stack-internal address (CADDY_STACK_IPV4):
nothing here resolves `caddy` and no AppRole is ever re-bound. A Caddy address
that could move would silently lock every bound Dify app out (or, worse,
leave the bound on an address another container later gets)."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import threading
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROTATE_AFTER = timedelta(hours=12)
EXPIRY_WARN_AFTER = timedelta(hours=20)
#: APPROLE_LIMITS["secret_id_ttl"] (technical_users.py): a secret_id is dead this long after it was set.
SECRET_ID_TTL = timedelta(hours=24)
USES_WARN_FRACTION = 0.20
SECRET_VAR = "OPENBAO_SECRET_ID"
CADDY_ADDRESS_KEY = "CADDY_STACK_IPV4"
#: Only RFC1918: the SSRF proxy's deny list and Caddy's trusted_proxies
#: private_ranges cover exactly these; is_private also admits 198.18/15,
#: 192.0.2/24, 240/4 … which neither covers.
_RFC1918 = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


_log = logging.getLogger(__name__)


class AgentNodesUnsupported(RuntimeError):
    """The Dify app has Agent (v2) nodes: its draft GET projects agent
    bindings into the graph that the published GET does not carry, and its
    publish re-resolves a roster agent to that agent's CURRENT snapshot —
    neither is in the digest, so it is never rotated automatically."""


class DraftChanged(RuntimeError):
    """The Dify app's draft moved while its secret was being rotated: nothing
    was published, the published version keeps its old secret."""


def caddy_cidr(env: dict) -> str:
    """Caddy's fixed stack-internal address as a /32 (`env` is the stack .env
    as config_manager.read_env() returns it). A Dify HTTP node's call leaves
    through Caddy's SSRF proxy, so this is the source OpenBao sees (D7). It is
    shared with other stack traffic behind the proxy: the stated residual.
    Raises ValueError when no usable fixed address is configured, so no
    AppRole is ever minted unbound."""
    raw = ((env or {}).get(CADDY_ADDRESS_KEY) or "").strip()
    if not raw:
        raise ValueError(f"{CADDY_ADDRESS_KEY} is not set: Caddy has no fixed stack address")
    try:
        ip = ipaddress.IPv4Address(raw)
    except ValueError:
        raise ValueError(f"{CADDY_ADDRESS_KEY} is not an IPv4 address") from None
    if not any(ip in n for n in _RFC1918) or ip.is_unspecified:
        raise ValueError(f"{CADDY_ADDRESS_KEY} is not an RFC1918 stack address")
    return f"{ip}/32"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class SecretIdRotator:
    def __init__(self, registry, bao, dify, state_dir, *, uses=1000, now=None):
        self._reg, self._bao, self._dify = registry, bao, dify
        self._dir = Path(state_dir)
        self._uses = uses
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.last_failures = []

    def _load(self) -> dict:
        try:
            state = json.loads((self._dir / "secret-id-rotation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        if not isinstance(state.get("apps"), dict):
            state["apps"] = {}
        return state

    def _write(self, name: str, text: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._dir / name)

    def _save(self, state: dict, warnings=None) -> None:
        self._write("secret-id-rotation.json", json.dumps(state, sort_keys=True))
        if warnings is not None:
            self._write("secret-id-rotation.status", "LAST_RUN\t%d\n" % int(self._now().timestamp())
                        + "".join("WARN\t%s\n" % " ".join(w.split()) for w in warnings))

    def record_set(self, name: str, app_ids, accessor: str) -> None:
        """Create or manual rotate: the operator pasted a fresh secret_id into these apps."""
        with self._lock:
            state = self._load()
            for app_id in app_ids:
                state["apps"][app_id] = {"user": name, "set_at": _iso(self._now()), "accessor": accessor}
            self._save(state)

    def run_once(self) -> list:
        with self._lock:
            return self._run_once()

    def _run_once(self) -> list:
        """Every user and every app is its own unit: whatever one of them
        raises (a deleted Dify app answering 404, a null body, OpenBao
        refusing a secret_id, a malformed binding) becomes a WARN naming it,
        and the run goes on, so the others are rotated and the state and
        status files are always written. A record the registry could not
        read comes as a row with `error`, which is reported the same way.
        Only the registry listing itself (Authentik unreachable) can stop a
        run (then the status row reports a stale LAST_RUN)."""
        now, state, warnings = self._now(), self._load(), []
        self.last_failures = []

        def failed(name, what, e):
            self.last_failures.append((name, what, type(e).__name__))
            warnings.append(f"{name}: rotation FAILED for {what} ({type(e).__name__})")
            _log.warning("secret_id rotation: %s: %s failed (%s)", name, what, type(e).__name__)

        for u in self._reg.list():
            name = u.get("username") if isinstance(u, dict) else None
            try:
                if u.get("error"):
                    self.last_failures.append((name, "its record", "unreadable"))
                    warnings.append(f"{name}: rotation FAILED: {u['error']} - its Dify apps are not rotated")
                    _log.warning("secret_id rotation: %s: its record cannot be read", name)
                    continue
                if not u.get("is_active"):
                    continue
                app_ids = sorted(b.split(":", 1)[1] for b in u.get("bindings") or []
                                 if isinstance(b, str) and b.startswith("dify-app:"))
            except Exception as e:  # noqa: BLE001 — one user never stops the run
                failed(name or "(unreadable user)", "its record", e)
                continue
            due = []
            for app_id in app_ids:
                try:
                    if self._due(name, app_id, state, now, warnings):
                        due.append(app_id)
                except Exception as e:  # noqa: BLE001 — one app never stops the run
                    failed(name, f"Dify app {app_id}", e)
            if not due:
                continue
            try:
                new = self._bao.rotate_secret_id(name)
                secret, accessor = new["secret_id"], new["secret_id_accessor"]
            except Exception as e:  # noqa: BLE001 — one user never stops the run
                failed(name, "a new secret_id from OpenBao", e)
                continue
            for app_id in due:
                try:
                    self._dify.set_secret_and_publish(app_id, SECRET_VAR, secret)
                except DraftChanged:
                    warnings.append(f"{name}: rotation skipped: unpublished draft in Dify app {app_id} "
                                    "(edited during the rotation; nothing published, the old secret stays)")
                    continue
                except Exception as e:  # noqa: BLE001 — one app never stops the others
                    failed(name, f"Dify app {app_id}", e)
                    continue
                state["apps"][app_id] = {"user": name, "set_at": _iso(now), "accessor": accessor}
        state["last_run"] = _iso(now)
        self._save(state, warnings)
        return warnings

    def _due(self, name, app_id, state, now, warnings) -> bool:
        rec = state["apps"].get(app_id) or {}
        if not isinstance(rec, dict) or rec.get("user") not in (None, name):
            rec = {}     # the app was re-bound to another technical user (or the record is unreadable)
        try:
            set_at = datetime.fromisoformat(rec["set_at"]) if rec.get("set_at") else None
        except (TypeError, ValueError):
            set_at = None
        if set_at is not None and set_at.tzinfo is None:
            set_at = set_at.replace(tzinfo=timezone.utc)     # a hand-edited state file: read as UTC
        elif set_at is not None:
            set_at = set_at.astimezone(timezone.utc)
        if rec.get("accessor"):
            try:    # advisory: a failed lookup never decides whether the app is rotated
                left = self._bao.secret_id_uses_left(name, rec["accessor"])
            except Exception as e:  # noqa: BLE001
                left = None
                warnings.append(f"{name}: Dify app {app_id}'s secret_id uses left could not be read "
                                f"({type(e).__name__})")
                _log.warning("secret_id rotation: %s: uses left of Dify app %s unreadable (%s)",
                             name, app_id, type(e).__name__)
            if left is not None and left < USES_WARN_FRACTION * self._uses:
                warnings.append(f"{name}: Dify app {app_id}'s secret_id has {left} of {self._uses} uses left")
        if set_at is not None and now - set_at < ROTATE_AFTER:
            return False
        try:
            if self._dify.draft_equals_published(app_id):
                return True
            warnings.append(f"{name}: rotation skipped: unpublished draft in Dify app {app_id} - "
                            "publish it, or rotate manually on Settings > Technical users")
        except AgentNodesUnsupported:
            when = set_at.strftime("%Y-%m-%d %H:%M UTC") if set_at is not None else "set time unknown"
            warnings.append(f"{name}: rotation skipped: Dify app {app_id} has Agent (v2) nodes and is never "
                            f"rotated automatically; its secret_id expires 24 h after it was set ({when}) - "
                            "rotate it manually on Settings > Technical users before then")
        # The expiry check runs for every skipped app, whatever the reason.
        if set_at is not None and now - set_at >= SECRET_ID_TTL:
            warnings.append(f"{name}: Dify app {app_id}'s secret_id expired at "
                            f"{(set_at + SECRET_ID_TTL).strftime('%Y-%m-%d %H:%M UTC')} - its OpenBao login "
                            "fails until it is rotated on Settings > Technical users")
        elif set_at is not None and now - set_at >= EXPIRY_WARN_AFTER:
            warnings.append(f"{name}: Dify app {app_id}'s secret_id expires within 4 h - rotate it now")
        return False

    def start(self, interval: float = 3600.0) -> None:
        threading.Thread(target=self._loop, args=(interval,), daemon=True, name="secret-id-rotation").start()

    def _loop(self, interval: float) -> None:
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — logged; the status row reports a stale LAST_RUN
                _log.exception("secret_id rotation: the run failed")
            time.sleep(interval)


def _curl_quote(value: str) -> str:
    """A curl config (`-K -`) double-quoted string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


class DifyError(RuntimeError):
    """A Dify console call did not answer as it must (a non-2xx status, a
    sync that did not say success, a missing variable, a read-back that does
    not hold the new secret). Nothing is recorded as rotated. The message
    never carries a response body."""


class DifyUnauthorized(DifyError):
    """The console session was refused (HTTP 401): it expired."""


class DifyConsole:
    """Dify 1.17.1 console API through `docker exec -i dify-api curl`, the
    transport of cli/post-install.sh (dify_login / dify_api). Everything that
    is a secret — the login body, the session cookie, the CSRF token, the new
    secret_id and the DSL export that reads it back — travels on STDIN/STDOUT
    (`curl -K -`), never on an argv (a host `ps` shows every docker exec
    argument). Logs in with the stack's Dify admin account
    (DIFY_ADMIN_PASSWORD, else AUTHENTIK_BOOTSTRAP_PASSWORD; the email is
    post-install's S20 rule, razzfazz-ai-admin@<MAIN_DOMAIN>).

    Pinned against langgenius/dify tag 1.17.1:
      * api/controllers/console/app/workflow.py L125-156:
        SyncDraftWorkflowPayload is `extra="forbid"` (graph, features, hash,
        _is_collaborative, environment_variable_patch, conversation_variables)
        (`_is_collaborative` is its alias for graph_only, L152, which the
        service honours by leaving features and conversation variables
        untouched, services/workflow_service.py L442-486)
        and environment_variable_patch is an OBJECT
        {"environment_variables": [{"id", …}], "deleted_environment_variable_ids": []}
        where every upsert needs the variable's id (validate_patch). The POST
        answers {"result": "success", "hash", "updated_at"} (L689-695).
      * api/models/workflow.py L555-563: unique_hash covers the GRAPH ONLY —
        features, environment and conversation variables are outside it, so
        "draft equals published" is this module's own digest of all four.
      * the console masks every secret as full_mask_token() ("*" * 20,
        workflow.py L538-545, core/helper/encrypter.py), so the new secret
        is read back from the published version through the DSL export
        (api/controllers/console/app/app.py L1024-1053, include_secret and
        workflow_id; services/app_dsl_service.py L807-829)."""

    BASE = "http://localhost:5001"

    def __init__(self, env: dict, run=subprocess.run):
        self._env, self._run, self._cookie = env, run, None
        self._checked = {}

    def _curl(self, method, path, body=None, headers=()):
        cfg = [f"request = {_curl_quote(method)}", f"url = {_curl_quote(self.BASE + path)}",
               f"header = {_curl_quote('Content-Type: application/json')}",
               *(f"header = {_curl_quote(h)}" for h in headers)]
        if method != "GET":
            cfg.append(f"data = {_curl_quote(json.dumps(body or {}))}")
        p = self._run(["docker", "exec", "-i", "dify-api", "curl", "-s", "-D-", "-K", "-"],
                      input=("\n".join(cfg) + "\n").encode("utf-8"), capture_output=True, timeout=30)
        if p.returncode != 0:
            raise DifyError(f"dify {method} {path.split('?')[0]} failed (rc={p.returncode})")
        # Bytes mode: text mode would translate curl's CRLF header terminators
        # to LF and the header/body split below would never match.
        out = p.stdout.decode("utf-8", errors="replace") if isinstance(p.stdout, bytes) else p.stdout
        # curl -D- prints every header block (100 Continue, redirects): the
        # status is the last block's, the body what follows it.
        head, payload = "", out
        while payload.startswith("HTTP/"):
            head, _, payload = payload.partition("\r\n\r\n")
        m = re.match(r"HTTP/\S+ (\d{3})", head)
        status = int(m.group(1)) if m else 0
        if status == 401:
            raise DifyUnauthorized(f"dify {method} {path.split('?')[0]}: HTTP 401")
        if not 200 <= status < 300:
            raise DifyError(f"dify {method} {path.split('?')[0]}: HTTP {status or 'unknown'}")
        try:
            data = json.loads(payload) if payload.strip() else {}
        except ValueError:
            raise DifyError(f"dify {method} {path.split('?')[0]}: not JSON") from None
        return head, data

    def _login(self):
        import base64
        pw = self._env.get("DIFY_ADMIN_PASSWORD") or self._env.get("AUTHENTIK_BOOTSTRAP_PASSWORD") or ""
        email = f"razzfazz-ai-admin@{self._env.get('MAIN_DOMAIN', '')}"
        head, _ = self._curl("POST", "/console/api/login",
                             {"email": email, "password": base64.b64encode(pw.encode()).decode()})
        jar = dict(l.split("=", 1) for l in
                   (h.split(":", 1)[1].strip().split(";", 1)[0] for h in head.splitlines()
                    if h.lower().startswith("set-cookie:")) if "=" in l)
        if not jar.get("access_token") or not jar.get("csrf_token"):
            raise DifyError("dify console login refused")
        self._cookie = jar

    def _api(self, method, path, body=None):
        """One console call. The rotator keeps one DifyConsole for its
        lifetime, so an expired session (401) logs in again, once; a 401 was
        refused before anything ran, so repeating the call is safe."""
        for attempt in (0, 1):
            if self._cookie is None:
                self._login()
            c = self._cookie
            try:
                return self._curl(method, path, body, headers=(
                    f"Cookie: access_token={c['access_token']}; csrf_token={c['csrf_token']}",
                    f"X-CSRF-Token: {c['csrf_token']}"))[1]
            except DifyUnauthorized:
                self._cookie = None
                if attempt:
                    raise

    @staticmethod
    def _env_sig(wf, skip=None):
        return sorted((v.get("name"), v.get("value_type"), "" if v.get("value_type") == "secret" else v.get("value"))
                      for v in wf.get("environment_variables") or [] if v.get("name") != skip)

    @classmethod
    def _digest(cls, wf, skip=None):
        """Everything a publish ships: graph, features, the environment
        variables (a secret's value is masked by Dify, so only its presence
        counts) and the conversation variables."""
        import hashlib
        doc = {"graph": wf.get("graph"), "features": wf.get("features"),
               "env": cls._env_sig(wf, skip), "conv": wf.get("conversation_variables") or [],
               "rag": wf.get("rag_pipeline_variables") or []}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _has_agent_v2(wf) -> bool:
        """Dify 1.17.1 core/workflow/nodes/agent_v2/discriminator.py:
        type "agent", version "2", agent_node_kind "dify_agent"."""
        for node in (wf.get("graph") or {}).get("nodes") or []:
            data = node.get("data") if isinstance(node, dict) else None
            if isinstance(data, dict) and data.get("type") == "agent" and str(data.get("version")) == "2" \
                    and data.get("agent_node_kind") == "dify_agent":
                return True
        return False

    def draft_equals_published(self, app_id) -> bool:
        """True when the draft equals the published version in everything a
        publish ships; remembers that version, which set_secret_and_publish
        then insists on (compare, set, compare again, publish, read back)."""
        draft = self._api("GET", f"/console/api/apps/{app_id}/workflows/draft")
        if not isinstance(draft, dict) or not isinstance(draft.get("graph"), dict):
            raise DifyError(f"Dify app {app_id}: no usable draft")
        if self._has_agent_v2(draft):
            raise AgentNodesUnsupported(app_id)
        published = self._api("GET", f"/console/api/apps/{app_id}/workflows/publish")
        if published is not None and not isinstance(published, dict):
            raise DifyError(f"Dify app {app_id}: no usable published version")
        same = bool(published) and bool(draft.get("graph")) and self._digest(draft) == self._digest(published)
        self._checked[app_id] = published if same else None
        return same

    def set_secret_and_publish(self, app_id, var, value) -> None:
        """Only after draft_equals_published(app_id) said True, and only while
        the draft STILL is that published version:
          1. the draft is re-read and must digest like the checked version
             (else DraftChanged, nothing written);
          2. the secret variable is patched BY ITS ID (Dify's patch object),
             with the draft's hash; the answer must be result=success;
          3. the draft is re-read and must still digest like the checked
             version apart from the secret (else DraftChanged: NOT published;
             the draft carries the new secret_id, which is valid, and the
             published version keeps the old one);
          4. publish (result=success);
          5. the new published version must digest like the checked one
             apart from the secret, and its DSL export must hold the new
             secret_id in `var` — only then is the rotation a success
             (else DifyError: nothing is recorded, the old secret_id's record
             stays and its 24 h expiry warning follows).
        Residual: an edit landing between 3 and 4 is published with it; step
        5 then reports it (DifyError) instead of recording success."""
        checked = self._checked.pop(app_id, None)
        if not checked:
            raise DraftChanged(app_id)
        want = self._digest(checked, skip=var)
        draft = self._api("GET", f"/console/api/apps/{app_id}/workflows/draft")
        if self._digest(draft) != self._digest(checked):
            raise DraftChanged(app_id)
        hits = [v for v in draft.get("environment_variables") or []
                if v.get("name") == var and v.get("value_type") == "secret" and v.get("id")]
        if len(hits) != 1:
            raise DifyError(f"Dify app {app_id} has no secret variable {var}")
        current = hits[0]
        answer = self._api("POST", f"/console/api/apps/{app_id}/workflows/draft", {
            # graph_only (workflow_service.py L442-486 at 1.17.1): features and
            # conversation variables are NOT written back from this read, so a
            # collaborator's edit through /draft/features or
            # /draft/conversation-variables in between survives (and step 3
            # then sees it); the environment patch is still merged.
            "_is_collaborative": True,
            "graph": draft["graph"], "features": draft.get("features") or {}, "hash": draft.get("hash"),
            "conversation_variables": draft.get("conversation_variables") or [],
            "environment_variable_patch": {
                "environment_variables": [{"id": current["id"], "name": var, "value": value,
                                           "value_type": "secret",
                                           "description": current.get("description") or ""}],
                "deleted_environment_variable_ids": []}})
        if answer.get("result") != "success":
            raise DifyError(f"Dify app {app_id}: the draft sync did not succeed")
        again = self._api("GET", f"/console/api/apps/{app_id}/workflows/draft")
        if self._digest(again, skip=var) != want:
            raise DraftChanged(app_id)
        if self._api("POST", f"/console/api/apps/{app_id}/workflows/publish", {}).get("result") != "success":
            raise DifyError(f"Dify app {app_id}: the publish did not succeed")
        self._verify_published(app_id, var, value, want)

    def _verify_published(self, app_id, var, value, want) -> None:
        import hmac

        import yaml
        published = self._api("GET", f"/console/api/apps/{app_id}/workflows/publish")
        if not published or self._digest(published, skip=var) != want:
            raise DifyError(f"Dify app {app_id}: the published version is not the checked one")
        pub_id = published.get("id")
        if not isinstance(pub_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", pub_id):
            raise DifyError(f"Dify app {app_id}: the published version has no id")
        dsl = self._api("GET", f"/console/api/apps/{app_id}/export?include_secret=true&workflow_id={pub_id}")
        try:
            doc = yaml.safe_load(dsl.get("data") or "") or {}
            env = (doc.get("workflow") or {}).get("environment_variables") or []
        except (yaml.YAMLError, AttributeError):
            raise DifyError(f"Dify app {app_id}: the export is unreadable") from None
        got = [v.get("value") for v in env if isinstance(v, dict) and v.get("name") == var]
        if len(got) != 1 or not isinstance(got[0], str) or not hmac.compare_digest(got[0], value):
            raise DifyError(f"Dify app {app_id}: the published version does not hold the new secret_id")
