"""
id: dify
title: Dify Manifold Pipe
author: razzfazz-stack
version: 1.0.0
required_open_webui_version: 0.5.0
license: MIT
description: Production-grade Dify.ai integration — Chatflow / Workflow / Completion / Agent as Manifold models; streaming, file upload, citations (retriever_resources), conversation persistence, status events.
requirements: requests, pydantic
"""
from __future__ import annotations
import json, logging, mimetypes, os, threading, asyncio, base64, tempfile, re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Generator, Iterator, List, Optional, Tuple, Union
import requests
from pydantic import BaseModel, Field

try:
    from open_webui.config import UPLOAD_DIR  # type: ignore
except Exception:
    UPLOAD_DIR = "/app/backend/data/uploads"

log = logging.getLogger("dify_pipe"); log.setLevel(logging.INFO)


class ConversationStore:
    """Thread-sicheres, datei-persistentes Mapping (pipe_id, chat_id) -> dify_conversation_id."""
    def __init__(self, path: str = "/app/backend/data/dify_conv_map.json") -> None:
        self.path = Path(path); self._lock = threading.Lock(); self._data: Dict[str, str] = {}
        self._load()
    def _load(self) -> None:
        try:
            if self.path.exists(): self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e: log.warning("ConversationStore load failed: %s", e); self._data = {}
    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8"); tmp.replace(self.path)
        except Exception as e: log.warning("ConversationStore flush failed: %s", e)
    @staticmethod
    def _key(pipe_id: str, chat_id: str) -> str: return f"{pipe_id}::{chat_id}"
    def get(self, pipe_id, chat_id):
        with self._lock: return self._data.get(self._key(pipe_id, chat_id), "")
    def set(self, pipe_id, chat_id, conversation_id):
        if not chat_id or not conversation_id: return
        with self._lock:
            self._data[self._key(pipe_id, chat_id)] = conversation_id; self._flush()
    def drop(self, pipe_id, chat_id):
        with self._lock: self._data.pop(self._key(pipe_id, chat_id), None); self._flush()

_store = ConversationStore()


class Pipe:
    class Valves(BaseModel):
        DIFY_BASE_URL: str = Field(default="https://api.dify.ai/v1")
        DIFY_APPS_JSON: str = Field(
            default="[]",
            description='JSON-Array: [{"id":"support","name":"Support","api_key":"app-xxx","type":"chat"}, ...]'
        )
        REQUEST_TIMEOUT: int = Field(default=300)
        EMIT_NODE_STATUS: bool = Field(default=True)
        EMIT_CITATIONS: bool = Field(default=True)
        DEBUG: bool = Field(default=False)

    def __init__(self) -> None:
        self.type = "manifold"; self.id = "dify"; self.name = "Dify: "
        self.valves = self.Valves()

    def _apps(self) -> List[dict]:
        try:
            apps = json.loads(self.valves.DIFY_APPS_JSON or "[]")
            if not isinstance(apps, list): return []
            out = []
            for a in apps:
                if not isinstance(a, dict): continue
                a.setdefault("type", "chat"); a.setdefault("inputs", {})
                if "id" in a and "name" in a and "api_key" in a: out.append(a)
            return out
        except Exception as e:
            log.error("DIFY_APPS_JSON parse error: %s", e); return []

    def _app_by_id(self, app_id: str) -> Optional[dict]:
        for a in self._apps():
            if a["id"] == app_id: return a
        return None

    def pipes(self) -> List[dict]:
        apps = self._apps()
        if not apps:
            return [{"id": "not_configured", "name": "⚠️ Keine Dify-Apps konfiguriert"}]
        return [{"id": a["id"], "name": a["name"]} for a in apps]

    def pipe(self, body: dict, __user__: Optional[dict] = None,
             __metadata__: Optional[dict] = None,
             __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
             __files__: Optional[List[dict]] = None,
             __task__: Optional[str] = None, **kwargs: Any):
        __metadata__ = __metadata__ or {}; __user__ = __user__ or {}; __files__ = __files__ or []
        raw_model = body.get("model", "")
        app_id = raw_model.split(".", 1)[-1] if "." in raw_model else raw_model
        app = self._app_by_id(app_id)
        if not app: return f"❌ Dify-App '{app_id}' nicht in Valves konfiguriert."

        is_util_task = __task__ in {"title_generation", "tags_generation"}
        stream = bool(body.get("stream", False)) and not is_util_task
        user_id = str(__user__.get("id") or __metadata__.get("user_id") or "owui")
        chat_id = str(__metadata__.get("chat_id") or body.get("chat_id") or "")
        messages = body.get("messages", []) or []
        query, vision_files = self._extract_query(messages)

        dify_files = []
        try:
            dify_files = self._upload_user_files(
                __files__, vision_files, api_key=app["api_key"], user=user_id)
        except Exception as e:
            log.warning("File-Upload partially failed: %s", e)

        app_type = (app.get("type") or "chat").lower()
        try:
            if app_type == "workflow":
                return self._run_workflow(app, query, app.get("inputs", {}),
                    dify_files, user_id, stream, __event_emitter__)
            elif app_type == "completion":
                return self._run_completion(app, query, app.get("inputs", {}),
                    dify_files, user_id, stream)
            else:
                return self._run_chat(app, query, app.get("inputs", {}),
                    dify_files, user_id, chat_id, stream, __event_emitter__)
        except requests.HTTPError as e:
            body_txt = ""
            try: body_txt = e.response.text[:500]
            except Exception: pass
            return f"❌ Dify HTTP {e.response.status_code}: {body_txt}"
        except requests.RequestException as e:
            return f"❌ Dify Netzwerkfehler: {e}"
        except Exception as e:
            log.exception("Dify-Pipe unerwarteter Fehler")
            return f"❌ Fehler: {e}"

    @staticmethod
    def _extract_query(messages: List[dict]) -> Tuple[str, List[str]]:
        query = ""; images: List[str] = []
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    for item in c:
                        t = item.get("type")
                        if t == "text": query += item.get("text", "")
                        elif t == "image_url":
                            url = (item.get("image_url") or {}).get("url", "")
                            if url: images.append(url)
                else: query = c or ""
                break
        return query, images

    def _upload_user_files(self, owui_files, vision_urls, api_key, user):
        out = []
        for f in owui_files:
            fid = f.get("id") or f.get("file_id") or ""
            name = f.get("name") or f.get("filename") or f"{fid}"
            path = self._resolve_local_path(fid, name)
            if not path or not os.path.exists(path): continue
            dify_type = self._classify_for_dify(path)
            try:
                upload_id = self._dify_upload(path, api_key=api_key, user=user)
                out.append({"type": dify_type, "transfer_method": "local_file",
                            "upload_file_id": upload_id})
            except Exception as e:
                log.warning("Upload '%s' fehlgeschlagen: %s", name, e)
        for url in vision_urls:
            if url.startswith("data:"):
                tmp = self._datauri_to_tempfile(url)
                if tmp:
                    try:
                        upload_id = self._dify_upload(tmp, api_key=api_key, user=user)
                        out.append({"type": "image", "transfer_method": "local_file",
                                    "upload_file_id": upload_id})
                    finally:
                        try: os.unlink(tmp)
                        except: pass
            else:
                out.append({"type": "image", "transfer_method": "remote_url", "url": url})
        return out

    @staticmethod
    def _resolve_local_path(file_id, name):
        if not file_id: return None
        for p in [os.path.join(UPLOAD_DIR, f"{file_id}_{name}"),
                  os.path.join(UPLOAD_DIR, file_id),
                  os.path.join(UPLOAD_DIR, name)]:
            if os.path.exists(p): return p
        return None

    @staticmethod
    def _classify_for_dify(path):
        mt, _ = mimetypes.guess_type(path); mt = (mt or "").lower()
        if mt.startswith("image/"): return "image"
        if mt.startswith("audio/"): return "audio"
        if mt.startswith("video/"): return "video"
        return "document"

    def _dify_upload(self, path, api_key, user):
        url = f"{self.valves.DIFY_BASE_URL.rstrip('/')}/files/upload"
        headers = {"Authorization": f"Bearer {api_key}"}
        mt, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            files = {"file": (os.path.basename(path), fh, mt or "application/octet-stream")}
            r = requests.post(url, headers=headers, files=files, data={"user": user},
                              timeout=self.valves.REQUEST_TIMEOUT)
        r.raise_for_status(); return r.json()["id"]

    @staticmethod
    def _datauri_to_tempfile(uri):
        m = re.match(r"^data:([^;]+);base64,(.+)$", uri, re.DOTALL)
        if not m: return None
        mt, b64 = m.group(1), m.group(2)
        ext = mimetypes.guess_extension(mt) or ".bin"
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f: f.write(base64.b64decode(b64))
        except Exception: return None
        return path

    def _run_chat(self, app, query, inputs, dify_files, user, chat_id, stream, event_emitter):
        url = f"{self.valves.DIFY_BASE_URL.rstrip('/')}/chat-messages"
        headers = {"Authorization": f"Bearer {app['api_key']}", "Content-Type": "application/json"}
        conv_id = _store.get(app["id"], chat_id) if chat_id else ""
        payload = {"inputs": inputs or {}, "query": query or "",
                   "response_mode": "streaming" if stream else "blocking",
                   "conversation_id": conv_id, "user": user, "files": dify_files,
                   "auto_generate_name": False}
        if stream:
            return self._stream_chat(url, headers, payload, app["id"], chat_id, event_emitter)
        r = requests.post(url, headers=headers, json=payload, timeout=self.valves.REQUEST_TIMEOUT)
        r.raise_for_status(); data = r.json()
        new_conv = data.get("conversation_id")
        if new_conv and chat_id: _store.set(app["id"], chat_id, new_conv)
        return data.get("answer", "") or ""

    def _stream_chat(self, url, headers, payload, pipe_app_id, chat_id, event_emitter):
        with requests.post(url, headers=headers, json=payload, stream=True,
                           timeout=self.valves.REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"): continue
                try: evt = json.loads(raw[5:].strip())
                except Exception: continue
                etype = evt.get("event")
                if etype in ("message", "agent_message"):
                    ans = evt.get("answer", "")
                    if ans: yield ans
                elif etype == "message_replace":
                    yield "\n\n[⚠️ Inhalt moderiert]\n" + (evt.get("answer") or "")
                elif etype == "agent_thought":
                    if self.valves.EMIT_NODE_STATUS and event_emitter:
                        tool = evt.get("tool") or ""; obs = (evt.get("observation") or "")[:200]
                        self._emit_safe(event_emitter, {"type": "status",
                            "data": {"description": f"🧠 Agent Tool: {tool} {obs}", "done": False}})
                elif etype == "message_file":
                    mf_url = evt.get("url") or ""
                    if mf_url:
                        if mf_url.startswith("/"):
                            base = self.valves.DIFY_BASE_URL.rstrip("/")
                            base = base[:-3] if base.endswith("/v1") else base
                            mf_url = f"{base}{mf_url}"
                        yield f"\n\n![generated]({mf_url})\n"
                elif etype in ("workflow_started", "node_started", "node_finished",
                               "workflow_finished", "parallel_branch_started",
                               "parallel_branch_finished"):
                    if self.valves.EMIT_NODE_STATUS and event_emitter:
                        d = evt.get("data", {}) or {}
                        title = d.get("title") or d.get("node_type") or etype
                        self._emit_safe(event_emitter, {"type": "status",
                            "data": {"description": f"⚙️ {etype}: {title}",
                                     "done": etype.endswith("_finished")}})
                elif etype == "message_end":
                    new_conv = evt.get("conversation_id")
                    if new_conv and chat_id: _store.set(pipe_app_id, chat_id, new_conv)
                    if self.valves.EMIT_CITATIONS and event_emitter:
                        meta = evt.get("metadata", {}) or {}
                        for rr in meta.get("retriever_resources", []) or []:
                            self._emit_citation(event_emitter, rr)
                elif etype == "error":
                    msg = evt.get("message") or evt.get("status") or "unknown"
                    yield f"\n\n❌ Dify-Error: {msg}\n"
                elif etype in ("tts_message", "tts_message_end", "ping"):
                    continue

    def _run_completion(self, app, query, inputs, dify_files, user, stream):
        url = f"{self.valves.DIFY_BASE_URL.rstrip('/')}/completion-messages"
        headers = {"Authorization": f"Bearer {app['api_key']}", "Content-Type": "application/json"}
        merged = dict(inputs or {}); merged.setdefault("query", query or "")
        payload = {"inputs": merged, "response_mode": "streaming" if stream else "blocking",
                   "user": user, "files": dify_files}
        if stream: return self._stream_text(url, headers, payload)
        r = requests.post(url, headers=headers, json=payload, timeout=self.valves.REQUEST_TIMEOUT)
        r.raise_for_status(); return r.json().get("answer", "") or ""

    def _run_workflow(self, app, query, inputs, dify_files, user, stream, event_emitter):
        url = f"{self.valves.DIFY_BASE_URL.rstrip('/')}/workflows/run"
        headers = {"Authorization": f"Bearer {app['api_key']}", "Content-Type": "application/json"}
        merged = dict(inputs or {}); merged.setdefault("query", query or "")
        payload = {"inputs": merged, "response_mode": "streaming" if stream else "blocking",
                   "user": user, "files": dify_files}
        if stream: return self._stream_workflow(url, headers, payload, event_emitter)
        r = requests.post(url, headers=headers, json=payload, timeout=self.valves.REQUEST_TIMEOUT)
        r.raise_for_status()
        outputs = ((r.json() or {}).get("data") or {}).get("outputs") or {}
        for k in ("text", "answer", "output", "result"):
            if isinstance(outputs.get(k), str): return outputs[k]
        return "```json\n" + json.dumps(outputs, ensure_ascii=False, indent=2) + "\n```"

    def _stream_workflow(self, url, headers, payload, event_emitter):
        with requests.post(url, headers=headers, json=payload, stream=True,
                           timeout=self.valves.REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"): continue
                try: evt = json.loads(raw[5:].strip())
                except Exception: continue
                etype = evt.get("event")
                if etype == "text_chunk":
                    t = (evt.get("data") or {}).get("text", "")
                    if t: yield t
                elif etype in ("workflow_started", "node_started", "node_finished",
                               "parallel_branch_started", "parallel_branch_finished"):
                    if self.valves.EMIT_NODE_STATUS and event_emitter:
                        d = evt.get("data", {}) or {}
                        title = d.get("title") or d.get("node_type") or etype
                        self._emit_safe(event_emitter, {"type": "status",
                            "data": {"description": f"⚙️ {etype}: {title}",
                                     "done": etype.endswith("_finished")}})
                elif etype == "workflow_finished":
                    outputs = ((evt.get("data") or {}).get("outputs") or {})
                    has_text = any(isinstance(v, str) for v in outputs.values())
                    if not has_text:
                        yield "\n```json\n" + json.dumps(outputs, ensure_ascii=False, indent=2) + "\n```"
                    elif "text" not in outputs and "answer" not in outputs:
                        for k, v in outputs.items():
                            if isinstance(v, str): yield f"\n\n**{k}:** {v}"
                elif etype == "error":
                    yield f"\n\n❌ Dify-Error: {evt.get('message', '')}"

    def _stream_text(self, url, headers, payload):
        with requests.post(url, headers=headers, json=payload, stream=True,
                           timeout=self.valves.REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"): continue
                try: evt = json.loads(raw[5:].strip())
                except Exception: continue
                if evt.get("event") == "message":
                    a = evt.get("answer", "")
                    if a: yield a

    @staticmethod
    def _emit_safe(emitter, event):
        try:
            coro = emitter(event)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running(): asyncio.ensure_future(coro)
                else: loop.run_until_complete(coro)
            except RuntimeError: asyncio.run(coro)
        except Exception as e: log.debug("emit failed: %s", e)

    def _emit_citation(self, emitter, rr):
        doc_name = rr.get("document_name") or rr.get("dataset_name") or "Source"
        content = rr.get("content") or ""
        meta = [{"source": doc_name, "dataset_id": rr.get("dataset_id"),
                 "document_id": rr.get("document_id"), "segment_id": rr.get("segment_id"),
                 "score": rr.get("score")}]
        self._emit_safe(emitter, {"type": "citation",
            "data": {"document": [content], "metadata": meta, "source": {"name": doc_name}}})
