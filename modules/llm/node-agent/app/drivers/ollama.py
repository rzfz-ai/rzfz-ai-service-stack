# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2559 — manage models in a native Ollama (macOS thin worker).

Ollama runs as its own launchd service on loopback, on the worker's own port
(app/ollama_port.py); this driver only speaks its HTTP API. A catalogue GGUF entry is pulled as `hf.co/<repo>:<QUANT>`
and aliased to the deployment's served name, so the relay can forward an
OpenAI request naming that model unchanged.

Standard library only, and deliberately NOT imported by ``drivers/__init__.py``:
the Linux agent never loads this module."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

_QUANT = re.compile(r"(?i)(?:^|[-_.])((?:I?Q\d(?:_[0-9A-Z]+)*)|BF16|F16|F32)$")


def ollama_ref(hf_repo: str, filename: str, override: str | None = None) -> str:
    if override:
        return override
    stem = os.path.basename(filename or "")
    stem = stem[:-5] if stem.lower().endswith(".gguf") else stem
    m = _QUANT.search(stem)
    if not (hf_repo and m):
        raise ValueError(f"cannot derive an Ollama quant from {filename!r} — set ollama_tag "
                         f"on the catalogue entry (#2559)")
    return f"hf.co/{hf_repo}:{m.group(1).upper()}"


class OllamaHTTPError(RuntimeError):
    """An HTTP error answer from Ollama, with Ollama's own reason and the code."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


#: Ollama is on loopback: never send it through HTTP(S)_PROXY. The default
#: urllib opener honours the proxy environment (and `proxy_bypass` does not
#: treat 127.0.0.1 as local without NO_PROXY), so a proxied Mac would ship
#: every model-management call to the corporate proxy (#1064's rule for
#: engine traffic: trust_env=False).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Http:
    def _req(self, method, url, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Ollama puts the real reason ("pull model manifest: file does not
            # exist") in the JSON body; urllib's message is only "HTTP Error 404".
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001 - the status code alone still helps
                detail = ""
            raise OllamaHTTPError(f"{method} {url}: HTTP {exc.code} {detail[:500]}".rstrip(), exc.code) from exc
        return json.loads(raw) if raw else {}

    def get_json(self, url):
        return self._req("GET", url)

    def post_json(self, url, body, timeout=None):
        return self._req("POST", url, body, timeout or 30)

    def delete_json(self, url, body):
        return self._req("DELETE", url, body)


class OllamaDriver:
    engine = "ollama"

    def __init__(self, base: str | None = None, http=None):
        if base is None:
            from app.ollama_port import managed_base
            base = managed_base()
        self.base = base.rstrip("/")
        self.http = http or _Http()

    def version(self) -> str:
        body = self.http.get_json(f"{self.base}/api/version")
        if not isinstance(body, dict):
            raise RuntimeError(f"ollama /api/version answered {type(body).__name__}, not an object: "
                               f"{str(body)[:200]}")
        return str(body.get("version") or "")

    def list_models(self) -> list[str]:
        models = self.http.get_json(f"{self.base}/api/tags").get("models") or []
        return [m["name"] for m in models if isinstance(m, dict) and m.get("name")]

    def pull(self, ref: str) -> None:
        # a 40 GB pull takes its time; the agent runs it on the background runner
        out = self.http.post_json(f"{self.base}/api/pull", {"model": ref, "stream": False}, timeout=6 * 3600)
        if (out or {}).get("status") != "success":
            raise RuntimeError(f"ollama pull {ref} failed: {out}")

    def alias(self, ref: str, served: str) -> None:
        self.http.post_json(f"{self.base}/api/copy", {"source": ref, "destination": served})

    def delete(self, name: str) -> None:
        """Idempotent: a model Ollama no longer has (404) is already deleted."""
        try:
            self.http.delete_json(f"{self.base}/api/delete", {"model": name})
        except OllamaHTTPError as exc:
            if exc.code != 404:
                raise

    def evict(self, name: str) -> None:
        """Drop `name` from memory now (keep_alive 0) so its RAM is free before
        the weights are deleted; a model that is not loaded is a no-op."""
        try:
            self.http.post_json(f"{self.base}/api/generate", {"model": name, "keep_alive": 0}, timeout=60)
        except OllamaHTTPError as exc:
            if exc.code != 404:
                raise

    @staticmethod
    def supports(task: str | None) -> bool:
        return (task or "chat") in ("chat", "embed")
