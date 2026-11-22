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

# #813: the model's TASK, declared rather than guessed.
#
# Registering the gateway as Dify's 2nd endpoint means registering CONCRETE
# chat models, and review #666 already established what happens without a task
# filter: `/v1/models` returns the whole router list, and an embed/rerank model
# registered as `mode=chat` is a broken Dify provider entry. Until now this
# config had no notion of a task at all — every entry was implicitly chat, which
# holds only because the Config Portal's add-model flow offers Ollama chat tags.
# `ollama pull nomic-embed-text` onto one of those same Macs is an entirely
# ordinary thing to do, and the moment it happens "every alias in model_list"
# becomes exactly the #666 defect on a new surface.
#
# So the task is DECLARED per model and persisted as LiteLLM's own
# `model_info.mode`, which means it round-trips through the very YAML the
# gateway already reads — no sidecar file, nothing for the two to drift apart
# over.
DEFAULT_TASK = "chat"
KNOWN_TASKS = ("chat", "embedding", "rerank")

# #242: use-case sampling profiles. A Mac Ollama tag can carry chat-creative
# defaults baked into its Modelfile (e.g. temperature 1.0, presence_penalty
# 1.5) — harmful for structured extraction (presence_penalty penalizes the
# repeated digits in IBAN/PAN/amount + repeated JSON keys extraction NEEDS) and
# it skews Mac-vs-box comparisons (the box runs qwen3.6 at temp 0.7 /
# presence_penalty 0.0). So the gateway pins an EXPLICIT sampling profile on
# every model_list entry instead of inheriting the tag default. The DEFAULT
# profile matches the box, so a Mac model is apples-to-apples out of the box;
# tag a model with ``"profile": "extraction"`` for deterministic runs.
DEFAULT_SAMPLING_PROFILE = "chat"
_SAMPLING_PROFILES = {
    # Box-matched qwen3.6 defaults — the safe default for chat / RAG.
    "chat": {"temperature": 0.7, "presence_penalty": 0.0},
    # Deterministic: structured document extraction (PSA CMT). Low temp +
    # presence_penalty 0.0 so recurring digits and repeated JSON structure are
    # never penalized or dropped.
    "extraction": {"temperature": 0.1, "top_p": 0.9, "presence_penalty": 0.0},
}


def _sampling_params(profile):
    """litellm_params sampling defaults for a use-case profile (#242).

    Unknown / missing profile falls back to the box-matched DEFAULT rather than
    inheriting the Ollama tag's Modelfile defaults.
    """
    key = profile if profile in _SAMPLING_PROFILES else DEFAULT_SAMPLING_PROFILE
    return dict(_SAMPLING_PROFILES[key])


def sampling_profiles():
    """#1149: the declared profile names, DEFAULT first.

    A public accessor exists so the surfaces OUTSIDE this module — the Config
    Portal's Mac-backends panel offers the profiles in a selector — do not keep
    their own copy of the table. A stale copy fails SILENTLY rather than
    loudly: ``_sampling_params`` falls back to the DEFAULT for anything it does
    not recognise, so an option this table no longer has would provision
    chat sampling onto a model the operator believes is pinned to extraction.
    """
    return [DEFAULT_SAMPLING_PROFILE] + [
        name for name in _SAMPLING_PROFILES if name != DEFAULT_SAMPLING_PROFILE
    ]


def sampling_profile_params(profile):
    """#1149: public ``_sampling_params`` — the params a profile pins.

    Same fall-back-to-DEFAULT contract, so a caller comparing what a config
    entry CARRIES against what a profile PINS compares against exactly what
    ``build_config`` would have written.
    """
    return _sampling_params(profile)


def profile_from_params(params):
    """#865: inverse of ``_sampling_params`` — litellm_params -> profile name.

    The profile NAME is not persisted; ``build_config`` writes the profile's
    sampling values into ``litellm_params``. So reading a config.yaml back
    (``_macs_from_config`` in the Config Portal) has to recognise the values, or
    the profile is lost and the next regeneration silently reverts the model to
    the default profile's sampling — the #865 failure.

    Returns None for the DEFAULT profile as well as for anything unrecognised,
    mirroring ``build_config``'s contract exactly: absent, unknown and default
    all produce the same output, so the round-tripped macs stay equal to what
    the Config Portal's own ``add()`` produces.
    """
    params = params or {}
    for name, pinned in _SAMPLING_PROFILES.items():
        if name == DEFAULT_SAMPLING_PROFILE:
            continue
        if all(params.get(k) == v for k, v in pinned.items()):
            return name
    return None


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
            params = {
                # Ollama tag, openai/ prefix so LiteLLM uses the
                # OpenAI-compatible path against the Mac.
                "model": f"openai/{m['ollama_tag']}",
                "api_base": base,
                # Ollama ignores it; LiteLLM requires a non-empty value.
                "api_key": "ollama",
            }
            task = m.get("task") or DEFAULT_TASK
            # #242: pin an explicit sampling profile (default = box-matched) so
            # the Mac model never silently inherits chat-creative Modelfile
            # defaults (temp 1.0 / presence_penalty 1.5). These are LiteLLM
            # request defaults — an explicit per-request value still wins.
            #
            # UNCONDITIONAL, including on a non-chat entry, and #1948 is the
            # measurement that settles why. It looked wrong — a `mode: embedding`
            # entry carrying `temperature` describes generation a model does not
            # do — so the pin was made conditional on the task. Measured
            # afterwards against the PINNED LiteLLM (v1.83.7-stable, an echo
            # provider, 2026-09-11):
            #
            #   /v1/embeddings through a mode=embedding entry -> HTTP 200,
            #   and LiteLLM FORWARDS the pair verbatim:
            #       {"input": "hello", "model": "nomic-embed-text",
            #        "temperature": 0.7, "presence_penalty": 0.0}
            #   identical with drop_params true AND false.
            #
            # So nothing raises and nothing is stripped — the hazard the
            # conditional was built for does not exist. The hazard #242 named
            # does: an UNPINNED entry inherits the tag's Modelfile defaults
            # (presence_penalty 1.5), and "write no sampling" therefore means
            # "take whatever the tag brings", not "stay neutral". Ollama's own
            # answer was identical with and without the pair (measured on the
            # Mac: same 500 either way, from a missing projector, not from the
            # parameters), so the provider does not object either.
            params.update(_sampling_params(m.get("profile")))
            # #813: declare the task. Written VERBATIM — an unrecognised value
            # is deliberately not coerced to the default. Coercion would
            # silently register a typo'd embedding model with Dify as a chat
            # model (invisible until a workflow answers nonsense); leaving it
            # unrecognised costs that model its registration, which the
            # operator sees. Fail toward the visible failure.
            model_list.append({
                "model_name": m["alias"],
                "litellm_params": params,
                "model_info": {"mode": task},
            })
    return {
        "model_list": model_list,
        "router_settings": {"routing_strategy": "least-busy"},
        # #1948: `drop_params` is what our OWN router config already carries —
        # `modules/llm/manager/router-config.example.yaml:23` — with the
        # measured reason in `app/proxy.py`: LiteLLM "raises
        # UnsupportedParamsError for `dimensions` BEFORE `drop_params: true`
        # can" take effect. Same LiteLLM image, and the gateway was the half
        # without the guard.
        #
        # What it does NOT do, measured rather than assumed (2026-09-11,
        # v1.83.7-stable against an echo provider): it does not strip
        # `temperature`/`presence_penalty` from an embeddings call — those are
        # forwarded verbatim with drop_params both true and false. So this line
        # is not what makes the sampling pin safe; the pin is safe because
        # nothing rejects it. This is here for the parameter class the router
        # already met (`dimensions`), so the two LiteLLM deployments do not
        # differ on a setting one of them needed and the other never got.
        "litellm_settings": {"num_retries": 2, "request_timeout": 600,
                             "drop_params": True},
        "general_settings": {
            "master_key": MASTER_KEY_REF,
            "background_health_checks": True,
            "health_check_interval": 60,
        },
    }


def chat_models(cfg):
    """#813: the CHAT model aliases a gateway config serves, in order.

    THE source of truth for what gets registered with Dify as the 2nd endpoint.
    Deterministic — it reads the task each model DECLARES (see ``DEFAULT_TASK``)
    rather than filtering ``/v1/models`` by name-shape, which is the fragile
    alternative the issue rejects.

    De-duplicated: the same alias on two Macs is ONE LiteLLM load-balance group
    and therefore ONE model, not two Dify provider entries. Only chat entries
    claim an alias, so a stray non-chat entry cannot shadow a chat one that
    shares its name.
    """
    out, seen = [], set()
    for entry in (cfg or {}).get("model_list") or []:
        name = (entry or {}).get("model_name")
        if not name or name in seen:
            continue
        mode = ((entry.get("model_info") or {}).get("mode")) or DEFAULT_TASK
        if mode != DEFAULT_TASK:
            continue
        seen.add(name)
        out.append(name)
    return out


def dump_yaml(cfg):
    """Serialize a config dict to YAML (stable key order, block style)."""
    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)


def main(argv=None):
    """CLI: read a JSON list of Macs on stdin, write LiteLLM YAML to stdout.

    Pure convenience for hand-generation / scripting — no network. Example:
        echo '[{"name":"studio","ip":"192.0.2.194","port":11434,
               "models":[{"alias":"mac-qwen3","ollama_tag":"qwen3"}]}]' \\
          | python3 gen_config.py

    --chat-models: read a gateway config.yaml on stdin instead and write the
    CHAT model aliases, one per line, to stdout (#813). This is the ONE call
    `cli/post-install.sh::wire_mac_llm_dify_consumer` makes, so the shell never
    re-derives the chat-vs-embed rule in yq/grep and this file stays the single
    source of truth. No chat models is an EMPTY stdout with exit 0 — the caller
    must be able to tell that apart from a broken call, which exits non-zero.
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(main.__doc__)
        return 0
    if argv and argv[0] == "--chat-models":
        cfg = yaml.safe_load(sys.stdin) if not sys.stdin.isatty() else {}
        for name in chat_models(cfg):
            sys.stdout.write(f"{name}\n")
        return 0
    macs = json.load(sys.stdin) if not sys.stdin.isatty() else []
    sys.stdout.write(dump_yaml(build_config(macs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
