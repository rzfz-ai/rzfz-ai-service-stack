# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Where the Mac worker's own Ollama listens (loopback only).

The port is defined once, in the bundle's ``mac-agent/ollama-port.conf``. The
installer writes it into ``<root>/.env.node`` as ``RZFZ_OLLAMA_PORT`` and
renders it into the launchd plist; the agent reads that key (``load_env_node``
puts it into the process environment). A worker installed before the port
moved has no such key: its Ollama still runs on the conf's legacy port until
``bin/ollama-port-migrate.sh`` (or a re-install) moves it.

Not Ollama's default port on purpose: the Mac usually runs an Ollama of its own
on it already, and the worker never touches that one.

Standard library only; imported by the Mac agent (native_runtime, the Ollama
driver), never by the Linux agent."""
from __future__ import annotations

import os
from pathlib import Path

CONF_NAME = "ollama-port.conf"
ENV_KEY = "RZFZ_OLLAMA_PORT"
LEGACY_KEY = "RZFZ_OLLAMA_LEGACY_PORT"


def conf_path() -> Path:
    """The conf next to the agent: ``<release>/mac-agent/`` in a bundle (app/ is
    its sibling), ``modules/llm/mac-agent/`` in a checkout."""
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parents[1]):
        candidate = base / "mac-agent" / CONF_NAME
        if candidate.is_file():
            return candidate
    return here.parent / "mac-agent" / CONF_NAME


def valid_port(value) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit() or text.startswith("0") or len(text) > 5:
        return None
    port = int(text)
    return port if 1024 <= port <= 65535 else None


def read_conf(path: Path | None = None) -> dict:
    path = path or conf_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"the Mac agent bundle has no {CONF_NAME} ({path}): {exc}") from None
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        port = valid_port(value)
        if port is not None:
            out[key.strip()] = port
    if ENV_KEY not in out or LEGACY_KEY not in out:
        raise RuntimeError(f"{path} must define {ENV_KEY} and {LEGACY_KEY}")
    return out


def managed_port(environ=None, conf: Path | None = None) -> int:
    """This worker's Ollama port: ``RZFZ_OLLAMA_PORT`` from .env.node (via the
    environment), else the legacy port of a worker installed before the move."""
    environ = os.environ if environ is None else environ
    port = valid_port(environ.get(ENV_KEY))
    return port if port is not None else read_conf(conf)[LEGACY_KEY]


def managed_base(environ=None, conf: Path | None = None) -> str:
    return f"http://127.0.0.1:{managed_port(environ, conf)}"
