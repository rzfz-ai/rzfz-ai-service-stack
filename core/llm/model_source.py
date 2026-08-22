# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Model-registration SOURCE fields — the one place that decides whether a
GPUStack model is registered from HuggingFace (online) or a local GGUF
(offline).  #184 P1 / WS7b.

Two registration sites build a GPUStack model payload:

  * ``cli/post-install.sh`` ``deploy_model()`` — the install/provision path
    (both the v0.7.x and the v2.x payload branches), and
  * ``core/llm/sync.py`` ``sync_gpustack()`` — the reconcile path.

Online / proxied boxes register with ``source: huggingface`` (+
``huggingface_repo_id`` / ``huggingface_filename``) so GPUStack pulls the GGUF
from huggingface.co.  An **offline** box (``RAZZFAZZ_NETWORK_MODE=offline``)
must NEVER reach huggingface.co, so it registers with ``source: local_path``
pointing at a GGUF already sitting in the ``gpustack-data`` volume under
:data:`LOCAL_MODELS_DIR` — sideloaded by the offline ``--package`` upgrade
(``cli/upgrade.sh``) or the Config-UI model sideload.

Both call sites go through :func:`model_source_fields` so the two never drift
on the online↔offline split.  Pure stdlib — import-safe as
``core.llm.model_source`` or as a sibling ``import model_source`` (the form the
``python3`` heredocs in ``deploy_model()`` use, and ``sync.py``'s
``sys.path``-inserted import).

The ``LOCAL_MODELS_DIR`` constant is mirrored in
``scripts/lib.sh::RAZZFAZZ_LOCAL_MODELS_DIR`` (the bash side) and read by
``core/llm/expected_models.py`` (``rzfz verify-models``) — keep the three in
lockstep.
"""
from __future__ import annotations

import os

# In-container path (the ``gpustack-data`` volume mounts at ``/var/lib/gpustack``
# in the gpustack container) where OFFLINE-sideloaded GGUFs live. Mirrors
# scripts/lib.sh::RAZZFAZZ_LOCAL_MODELS_DIR.
LOCAL_MODELS_DIR = "/var/lib/gpustack/local-models"


def local_model_path(filename: str) -> str:
    """In-container path to the offline-sideloaded GGUF for ``filename``.

    The offline package/load path stages GGUFs under :data:`LOCAL_MODELS_DIR`
    preserving the model's ``huggingface_filename`` verbatim — which may carry
    a sub-directory for multi-part shards (e.g.
    ``Qwen3-Coder-Next-Q4_K_M/Qwen3-Coder-Next-Q4_K_M-*.gguf``) or be a glob
    (``*f16*.gguf``).  It is passed through unchanged; GPUStack resolves the
    concrete file when it loads the model.  A leading ``/`` is stripped so the
    result is always UNDER :data:`LOCAL_MODELS_DIR`.
    """
    return f"{LOCAL_MODELS_DIR}/{str(filename).lstrip('/')}"


def model_source_fields(filename: str, repo: str, offline: bool = False) -> dict:
    """The ``source``-related keys of a GPUStack model-registration payload.

    ``offline=False`` (online / proxied)::

        {"source": "huggingface",
         "huggingface_repo_id": repo,
         "huggingface_filename": filename}

    ``offline=True``::

        {"source": "local_path",
         "local_path": "/var/lib/gpustack/local-models/<filename>"}

    The two are mutually exclusive: the offline spec carries NO
    ``huggingface_*`` keys (GPUStack would otherwise still try to fetch).
    """
    if offline:
        return {"source": "local_path", "local_path": local_model_path(filename)}
    return {
        "source": "huggingface",
        "huggingface_repo_id": repo,
        "huggingface_filename": filename,
    }


def env_is_offline(env_file: str) -> bool:
    """True when ``RAZZFAZZ_NETWORK_MODE=offline`` (or the legacy
    ``RAZZFAZZ_OFFLINE`` boolean) is set in ``env_file``.

    Mirrors ``scripts/lib.sh::razzfazz_network_mode`` derivation WITHOUT
    sourcing the file (operator-edited .env values routinely carry spaces /
    metachars — project memory ``feedback_dotenv_no_source.md``): grep the two
    targeted keys only.  Any read error → False (fail-open to online, the
    pre-#184 behaviour).
    """
    mode = _read_env_key(env_file, "RAZZFAZZ_NETWORK_MODE")
    if mode in ("online", "proxied", "offline"):
        return mode == "offline"
    # Derive from the legacy boolean (offline wins). Accept 1/true/yes/on.
    off = _read_env_key(env_file, "RAZZFAZZ_OFFLINE")
    return off.lower() in ("1", "true", "yes", "on")


def _read_env_key(env_file: str, key: str) -> str:
    """Grep a single ``KEY=value`` line from ``env_file`` (never source it).

    Strips surrounding single/double quotes and a trailing inline ``# comment``
    on unquoted values.  Returns '' when the file/key is absent or unreadable.
    """
    if not env_file or not os.path.isfile(env_file):
        return ""
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n").rstrip("\r")
                if not line.startswith(f"{key}="):
                    continue
                raw = line.split("=", 1)[1].strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
                    return raw[1:-1]
                # Unquoted: drop a trailing inline comment + surrounding space.
                if "#" in raw:
                    raw = raw.split("#", 1)[0]
                return raw.strip()
    except OSError:
        return ""
    return ""
