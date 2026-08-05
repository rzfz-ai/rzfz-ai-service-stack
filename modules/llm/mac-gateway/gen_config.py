#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Generate the LiteLLM config for the Mac LLM gateway from a list of Macs.

Pure — no network, no filesystem side effects. This is the single source of
truth for the gateway's model_list: the Config Portal panel (P2) and any
hand-generation both go through here so config.yaml always has one canonical
shape (kept in sync with config.example.yaml).

macs shape (also what _load_macs() in the P2 blueprint parses back out):
    [
      {"name": "studio", "ip": "192.0.2.194", "port": 11434,
       "models": [{"alias": "mac-qwen3-coder", "ollama_tag": "qwen3-coder"}]},
      ...
    ]

Rules:
- One model_list entry per (Mac × model).
- The SAME `alias` on the same model across two Macs = ONE LiteLLM load-balance
  group (LiteLLM groups deployments by model_name and routes across healthy
  members).
- `model:` is the Ollama tag with an `openai/` prefix so LiteLLM speaks Ollama's
  OpenAI-compatible /v1 endpoint; `api_base` is that Mac's Ollama /v1 URL.
- master_key stays a `os.environ/...` reference — never inline the secret.

Design: .gsd/reports/2026.08-mac-ollama-gateway-design.md
"""
from __future__ import annotations

import json
import sys

import yaml

DEFAULT_OLLAMA_PORT = 11434
MASTER_KEY_REF = "os.environ/MAC_GATEWAY_MASTER_KEY"


def build_config(macs):
    """Turn a list of Macs into the full LiteLLM config dict.

    Returns a dict with the four canonical top-level keys:
    model_list, router_settings, litellm_settings, general_settings.
    """
    model_list = []
    for mac in macs or []:
        port = mac.get("port") or DEFAULT_OLLAMA_PORT
        base = f"http://{mac['ip']}:{port}/v1"
        for m in mac.get("models", []) or []:
            model_list.append(
                {
                    "model_name": m["alias"],
                    "litellm_params": {
                        # Ollama tag, openai/ prefix so LiteLLM uses the
                        # OpenAI-compatible path against the Mac.
                        "model": f"openai/{m['ollama_tag']}",
                        "api_base": base,
                        # Ollama ignores it; LiteLLM requires a non-empty value.
                        "api_key": "ollama",
                    },
                }
            )
    return {
        "model_list": model_list,
        "router_settings": {"routing_strategy": "least-busy"},
        "litellm_settings": {"num_retries": 2, "request_timeout": 600},
        "general_settings": {
            "master_key": MASTER_KEY_REF,
            "background_health_checks": True,
            "health_check_interval": 60,
        },
    }


def dump_yaml(cfg):
    """Serialize a config dict to YAML (stable key order, block style)."""
    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)


def main(argv=None):
    """CLI: read a JSON list of Macs on stdin, write LiteLLM YAML to stdout.

    Pure convenience for hand-generation / scripting — no network. Example:
        echo '[{"name":"studio","ip":"192.0.2.194","port":11434,
               "models":[{"alias":"mac-qwen3","ollama_tag":"qwen3"}]}]' \\
          | python3 gen_config.py
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(main.__doc__)
        return 0
    macs = json.load(sys.stdin) if not sys.stdin.isatty() else []
    sys.stdout.write(dump_yaml(build_config(macs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
