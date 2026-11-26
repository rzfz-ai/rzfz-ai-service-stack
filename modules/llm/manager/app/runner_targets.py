# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2429 — which declared llama.cpp runner targets the hub actually holds.

runners.yaml is the one source of the declared set (#1516); it is mounted
read-only at /srv/runners.yaml exactly like the model manifest (#1256)."""
from __future__ import annotations

import os

import yaml

DEFAULT_RUNNERS_PATH = "/srv/runners.yaml"
# The repository prefix post-install pushes runners under in the hub
# (cli/post-install.sh RAZZFAZZ_RUNNER_REPO_PREFIX). A guard test reads both.
RUNNER_REPO_PREFIX = "runners"

#: #3403: the box's opt-in for the experimental runner channel (.env, handed to
#: the manager by compose). Off — the default — means the box builds, loads and
#: publishes no experimental runner, so nothing may be deployed onto it.
EXPERIMENTAL_RUNNER_ENV = "RAZZFAZZ_EXPERIMENTAL_RUNNER"

#: What an operator reads when the channel is off (the API notice and the console).
EXPERIMENTAL_RUNNER_OFF_NOTICE = (
    "This model runs on the experimental runner (a newer llama.cpp build, no "
    "stability promise), which is off on this box. To turn it on, set "
    "RAZZFAZZ_EXPERIMENTAL_RUNNER=true in the stack's .env and run "
    "'rzfz post-install --refresh': it builds and publishes the experimental "
    "runner and restarts the LLM Manager with the setting. Then deploy again.")


def experimental_runner_enabled() -> bool:
    return (os.environ.get(EXPERIMENTAL_RUNNER_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def declared_targets(path: str | None = None) -> list[dict]:
    """One dict per declared target: repository (hub path), tag, hardware, arch,
    channel (#3403).
    A missing or unreadable manifest is an empty list, never an exception."""
    p = path or os.environ.get("RZFZ_RUNNERS_MANIFEST") or DEFAULT_RUNNERS_PATH
    try:
        with open(p, encoding="utf-8") as fh:
            m = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(m, dict):
        return []
    repo = f"{RUNNER_REPO_PREFIX}/{m.get('repository') or 'llama-runner'}"
    # #3403: `channel` — "experimental" for the experimental llama.cpp build of
    # a hardware line, "stable" for every other target (the pin and rollbacks).
    return [{"repository": repo, "tag": str(t["tag"]), "hardware": str(t.get("hardware") or ""),
             "arch": str(t.get("arch") or ""),
             "channel": "experimental" if str(t.get("channel") or "") == "experimental" else "stable"}
            for t in (m.get("targets") or []) if isinstance(t, dict) and t.get("tag")]


def target_rows(declared: list[dict], catalog: list[dict]) -> list[dict]:
    """``declared`` plus ``ref`` and ``present`` (the hub catalog holds that tag)."""
    have = {(c.get("repository"), t) for c in catalog for t in (c.get("tags") or [])}
    return [{**d, "ref": f"{d['repository']}:{d['tag']}", "present": (d["repository"], d["tag"]) in have}
            for d in declared]
