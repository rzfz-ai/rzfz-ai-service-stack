#!/usr/bin/env python3
"""
core/llm/sync.py — propagate standard-models.yaml to every consumer in the stack.

Reads `core/llm/standard-models.yaml` and reconciles each consumer's per-model
config so the stack has a single source of truth for LLM aliases, context
sizes, and role mappings. Idempotent: re-running with no YAML change is a
no-op.

Usage:
    python3 core/llm/sync.py [--dry-run | --plan] [--target TARGET[,TARGET,...]] [--config PATH] [-v]
    python3 core/llm/sync.py --check                  # read-only drift report (B-7)

Targets:
    gpustack      — model registrations + backend_parameters via gpustack API
    openwebui     — model.params (num_ctx, max_tokens) in postgres openwebui_db
    dify          — no-op (the gpustack provider path is retired; #2901)
    cognee        — LLM_MODEL / EMBEDDING_MODEL in .env + LLM_ARGS for context budget
    moltis        — per-running-instance [models.<id>] context_window blocks in moltis.toml
    hermes        — per-running-instance hermes config set model.context_length
    all (default) — every target above

Verification (after running):
    python3 core/llm/sync.py --dry-run                # should report no pending changes
    python3 core/llm/sync.py --check                  # COMPARE YAML to live gpustack

--check mode (B-7):
    Read-only. Compares standard-models.yaml (declared) against live
    gpustack /v1/models (actual). Surfaces three drift classes:

      MISSING  — declared in YAML but absent from gpustack
      EXTRA    — registered in gpustack but absent from YAML
      MISMATCH — alias in both but huggingface_filename or
                 backend_parameters differ

    Exit code 0 iff zero drift; non-zero on any drift. Used by
    `kassasturz` for periodic drift detection and by the operator on
    demand. Never mutates live state.

Exit codes:
    0 — synced (or dry-run produced no errors, or --check found no drift)
    1 — invalid YAML / missing credentials / --check found drift
    2 — partial sync (one or more consumers failed; details printed)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("error: pyyaml not installed. Install with: pip3 install pyyaml")


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
DEFAULT_YAML = Path(__file__).parent / "standard-models.yaml"

# #184 P1 / WS7b: the OFFLINE model-source split (source=huggingface vs
# source=local_path) lives in a sibling module so this reconcile path and the
# post-install deploy path never drift. Import it via the module's own directory
# — sys.path[0] is core/llm when sync.py runs as a script, but a test that loads
# sync.py via importlib doesn't add that dir, so insert it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_source  # noqa: E402
import hardware_catalog  # noqa: E402  — #2158: per-hardware auto_start + defaults
import expected_models  # noqa: E402  — shared mmproj-sidecar derivation (#138)
# The most output tokens one cognee LLM call may generate (LLM_ARGS max_tokens);
# shared with the upgrade step that lowers the old default on an existing .env.
from cognee_output_budget import COGNEE_MAX_OUTPUT_TOKENS  # noqa: E402

ALL_TARGETS = ["gpustack", "openwebui", "dify", "cognee", "lightrag",
               "moltis", "hermes", "paperclip"]


# ── Helpers ──────────────────────────────────────────────────────────────────



def _defaults(spec):
    """#2158: `defaults` with the box's hardware-family overrides applied
    (`defaults_by_hardware`), the box's class read from .env HARDWARE — or,
    #2460, the cluster family post-install resolved for a CPU master that
    serves through GPU workers."""
    return hardware_catalog.defaults_for(spec, hardware_catalog.catalog_hardware(ENV_FILE), ENV_FILE)

def load_env_value(key: str) -> str | None:
    """Read one key from .env without `source` (operator-edited values often contain spaces)."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def is_offline() -> bool:
    """True when the box is in offline network mode (RAZZFAZZ_NETWORK_MODE=offline,
    or the legacy RAZZFAZZ_OFFLINE boolean). Reads .env without sourcing it —
    delegates to model_source.env_is_offline so the derivation matches
    scripts/lib.sh::razzfazz_network_mode."""
    return model_source.env_is_offline(str(ENV_FILE))


def run(cmd: list[str] | str, check: bool = True, *, env: dict[str, str] | None = None,
        input: str | None = None) -> tuple[int, str, str]:
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                       env=env, input=input)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed (rc={r.returncode}): {cmd}\n{r.stderr.strip()}")
    return r.returncode, r.stdout, r.stderr


def gpustack_api(method: str, path: str, data: dict | None = None) -> Any:
    api_key = load_env_value("GPUSTACK_API_KEY")
    if not api_key:
        raise SystemExit("GPUSTACK_API_KEY not set in .env")
    # #2577: the key reaches neither this process's nor docker's argv. It rides
    # in the exec environment by NAME (`-e RZ_CURL_HEADERS`), and curl inside
    # the container reads the header from a 0600 file (`-K`). The body goes on
    # stdin.
    script = (
        'umask 077; cfg=$(mktemp) || exit 1; trap \'rm -f "$cfg"\' EXIT; '
        'printf \'header = "%s"\\n\' "$RZ_CURL_HEADERS" > "$cfg"; curl -K "$cfg" "$@"'
    )
    args = [
        "docker", "exec", "-i", "-e", "RZ_CURL_HEADERS", "openwebui", "sh", "-c", script, "_",
        "-sS", "-X", method,
        "-H", "Content-Type: application/json",
        f"http://gpustack:9090{path}",
    ]
    if data is not None:
        args.extend(["-d", "@-"])
    _, out, err = run(args, check=False,
                      env={**os.environ, "RZ_CURL_HEADERS": f"Authorization: Bearer {api_key}"},
                      input=json.dumps(data) if data is not None else "")
    if not out.strip():
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise SystemExit(f"gpustack API returned non-JSON: {out!r} (err: {err!r})")


_PSQL_VAR_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


class PostgresError(RuntimeError):
    """A psql statement failed. Never swallowed: a failed statement is an error."""


def _psql_argv(database: str) -> list[str]:
    """argv prefix that reaches `database`. The one seam tests replace (a real
    ephemeral postgres instead of `docker exec`)."""
    return ["docker", "exec", "-i", "postgres", "psql", "-U", "docker", "-d", database]


def postgres_exec(database: str, sql: str,
                  params: dict[str, str] | None = None) -> tuple[str, str]:
    r"""Run `sql` in `database` through psql, reading the script on STDIN.

    #2901: the previous form (`psql -v name=value -tAc "... :'name' ..."`) never
    worked. psql does NOT interpolate variables inside a `-c` string, so every
    parameterised statement was a syntax error — swallowed by `check=False`,
    which turned every write into a silent no-op AND blanked the guard reads.
    psql interpolates `:'name'` in a script read from stdin, so the script goes
    there:

        \set name__hex '<hex of the value>'
        <sql with :'name' replaced by convert_from(decode(:'name__hex','hex'),'UTF8')>

    The value reaches psql only as a hex string ([0-9a-f]), so no quote,
    backslash, newline or `;` in a model id can leave its literal (#378), and
    it is on neither argv (#2746) nor the shell. `ON_ERROR_STOP=1` plus the
    return-code check make a failed statement raise PostgresError.
    """
    sql = sql.strip()
    if any(line.lstrip().startswith("\\") for line in sql.splitlines()):
        raise ValueError("psql meta-commands are not allowed in sync SQL")
    lines: list[str] = []
    for name, value in (params or {}).items():
        if not _PSQL_VAR_RE.match(name):
            raise ValueError(f"invalid psql variable name: {name!r}")
        if f":'{name}'" not in sql:
            raise ValueError(f"param {name!r} is not referenced in the SQL")
        raw = str(value).encode("utf-8")
        if b"\x00" in raw:
            raise ValueError(f"param {name!r} contains a NUL byte")
        lines.append(f"\\set {name}__hex '{raw.hex()}'")
        sql = sql.replace(f":'{name}'", f"convert_from(decode(:'{name}__hex', 'hex'), 'UTF8')")
    if re.search(r":'(?![A-Za-z0-9_]*__hex')[A-Za-z_]", sql):
        raise ValueError("SQL references a variable that was not bound")
    script = "\n".join(lines + [sql]) + "\n"
    args = _psql_argv(database) + ["-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    rc, out, err = run(args, check=False, input=script)
    if rc != 0:
        raise PostgresError(f"psql failed on {database} (rc={rc}): {err.strip()}")
    return out.strip(), err.strip()


def docker_containers_by_label(label_kv: str, exclude_label: str | None = None) -> list[str]:
    _, out, _ = run(["docker", "ps", "--filter", f"label={label_kv}", "--format", "{{.Names}}"])
    names = [n for n in out.splitlines() if n]
    if exclude_label:
        # docker doesn't support negative label filters; check each container's labels manually
        result = []
        for n in names:
            _, labels_out, _ = run(["docker", "inspect", n, "--format", "{{json .Config.Labels}}"])
            try:
                labels = json.loads(labels_out)
                k, _, v = exclude_label.partition("=")
                if labels.get(k) != (v if v else "true"):
                    result.append(n)
            except Exception:
                result.append(n)
        return result
    return names


# ── Per-target sync functions ────────────────────────────────────────────────


def sync_gpustack(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """Reconcile gpustack model registrations with desired backend_parameters."""
    changes: list[str] = []
    current = gpustack_api("GET", "/v1/models")
    by_name = {m["name"]: m for m in current.get("items", [])}

    for name, model in spec["models"].items():
        desired_params = list(model["backend_parameters"])
        # #138: offline vision — point --mmproj at the sideloaded local-models
        # copy, exactly like post-install's first registration. Without this
        # the reconcile would flag the rewritten param as drift and revert it
        # (back to the nonexistent HF-cache path) on every sync run.
        sidecar = expected_models.mmproj_filename(model)
        if sidecar:
            desired_params = model_source.apply_mmproj(
                desired_params, model["huggingface_repo_id"], sidecar,
                mode="offline" if is_offline() else "online")
        desired_file = model["huggingface_filename"]
        desired_repo = model["huggingface_repo_id"]
        cur = by_name.get(name)
        if not cur:
            changes.append(f"gpustack: ADD {name} (not implemented — register via gpustack UI)")
            continue
        cur_params = cur.get("backend_parameters") or []
        cur_file = cur.get("huggingface_filename")
        if cur_params == desired_params and cur_file == desired_file:
            if verbose:
                print(f"  gpustack: {name} already aligned")
            continue
        diff = []
        if cur_file != desired_file:
            diff.append(f"file {cur_file} → {desired_file}")
        if cur_params != desired_params:
            diff.append(f"params {cur_params} → {desired_params}")
        changes.append(f"gpustack: UPDATE {name}: " + "; ".join(diff))
        if dry_run:
            continue

        # Determine category for the API payload
        roles = model.get("roles", [])
        if "embedding" in roles:
            categories = ["embedder"]
        elif "reranker" in roles:
            categories = ["reranker"]
        else:
            categories = ["llm"]

        payload = {
            "name": name,
            "categories": categories,
            # The inference backend is a MANDATORY, edit-disabled field in gpustack:
            # it can only be set at create time, and a model registered without it
            # (backend = NULL) can never be edited/saved in the UI afterwards — the
            # form fails its required-field validation on the greyed-out Backend box.
            # All standard models are GGUF → llama-box; allow a per-model override.
            "backend": model.get("backend", "llama-box"),
            "placement_strategy": "spread",
            "distributed_inference_across_workers": True,
            "restart_on_error": False,
            "backend_parameters": desired_params,
        }
        # #184 P1 / WS7b: online → source=huggingface (+ repo/filename); offline →
        # source=local_path pointing at the sideloaded GGUF (never huggingface.co).
        # One shared builder keeps this in lockstep with post-install deploy_model.
        payload.update(model_source.model_source_fields(
            desired_file, desired_repo, offline=is_offline()))

        # scale=0 first to free VRAM and force shutdown
        payload["replicas"] = 0
        gpustack_api("PUT", f"/v1/models/{cur['id']}", payload)
        # wait for the running llama-server to exit
        for _ in range(30):
            cur_state = gpustack_api("GET", f"/v1/models/{cur['id']}")
            if cur_state.get("ready_replicas", 0) == 0:
                break
            time.sleep(2)
        # scale back to 1 with new params
        payload["replicas"] = 1
        gpustack_api("PUT", f"/v1/models/{cur['id']}", payload)
        if verbose:
            print(f"  gpustack: {name} respawn issued — gpustack will download/load with new args")

    return changes


#: #2901 — what this module may delete or rewrite in Open WebUI's `model` table.
#:
#: A row is sync's own ONLY if sync stamped it: `meta.synced_by` equals
#: SYNC_OWNER (written together with the params it updates). No stamp means
#: nobody here can prove the row is ours, and it is never deleted — rows made
#: by the LLM Manager row writer (`meta.managed_by`, #1305), by Open WebUI
#: auto-discovery, by the model-sync container, or by a user stay untouched.
SYNC_OWNER = "standard-models-sync"
SYNC_OWNER_KEY = "synced_by"
#: Shipped presets (scripts/owui_config_reconcile.py PRESET_ID and kin): never
#: deleted and never rewritten here, whatever else a row claims.
PROTECTED_OWUI_IDS = frozenset({"rzfz-dokumentanalyse"})


def _owui_rows() -> dict[str, dict]:
    """Every `model` row with the columns the ownership decision reads. A read
    that fails raises (PostgresError): an empty answer must mean an empty table."""
    out, _ = postgres_exec(
        "openwebui_db",
        "SELECT json_build_object('id', id, 'base', base_model_id, "
        "'managed_by', (meta::json)->>'managed_by', "
        f"'owner', (meta::json)->>'{SYNC_OWNER_KEY}') FROM model;")
    rows: dict[str, dict] = {}
    for line in out.splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["id"]] = r
    return rows


def owui_delete_verdict(row: dict, desired_ids: set[str]) -> tuple[bool, str]:
    """(may_delete, reason). Deleting needs POSITIVE ownership; every other
    state keeps the row."""
    rid = row["id"]
    if rid in desired_ids:
        return False, "is a standard-models.yaml key"
    if rid in PROTECTED_OWUI_IDS:
        return False, "shipped preset (protected)"
    if row.get("base"):
        return False, "custom model (base_model_id set)"
    if row.get("managed_by"):
        return False, f"owned by {row['managed_by']} (meta.managed_by, #1305)"
    if row.get("owner") != SYNC_OWNER:
        return False, "not stamped by this sync (ownership unproven)"
    return True, "stale row stamped by this sync"


def sync_openwebui(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """Set model.params on the YAML models' rows; delete only rows this sync
    stamped itself that are no longer in the YAML (#2901)."""
    changes: list[str] = []
    desired_ids = set(spec["models"].keys())
    rows = _owui_rows()

    for rid in sorted(set(rows) - desired_ids):
        ok, why = owui_delete_verdict(rows[rid], desired_ids)
        if not ok:
            if verbose:
                print(f"  openwebui: keeping {rid} ({why})")
            continue
        changes.append(f"openwebui: DELETE {rid}")
        if not dry_run:
            out, _ = postgres_exec(
                "openwebui_db",
                "DELETE FROM model WHERE id = :'ident' "
                f"AND (meta::json)->>'{SYNC_OWNER_KEY}' = :'owner' "
                "AND coalesce((meta::json)->>'managed_by', '') = '' "
                "AND coalesce(base_model_id, '') = '' RETURNING id;",
                {"ident": rid, "owner": SYNC_OWNER})
            if out.splitlines() != [rid]:
                raise PostgresError(f"DELETE {rid} matched {out!r}, expected exactly that row")

    # Upsert params for each model in YAML
    for name, model in spec["models"].items():
        roles = model.get("roles", [])
        # Skip embeddings/rerankers — OWUI handles those via separate config
        if "embedding" in roles or "reranker" in roles or "vision-document-conversion" in roles:
            continue
        if name in PROTECTED_OWUI_IDS:
            continue
        desired_params = {}
        if model.get("per_slot_context"):
            desired_params["num_ctx"] = model["per_slot_context"]
        if model.get("max_completion_tokens"):
            desired_params["max_tokens"] = model["max_completion_tokens"]
        if name not in rows:
            # #1266: the row is WRITTEN by the `model-sync` container (GPUStack
            # box) or post-install's OWUI step (scripts/owui_model_rows.py,
            # Manager box), which puts these params on it at INSERT time. A
            # missing row is expected on a first run, a defect on a second.
            changes.append(
                f"openwebui: SKIP {name} (no `model` row yet — the Open WebUI "
                f"provisioning step writes the rows and their context params; "
                f"if it is still missing after that, re-run "
                f"'rzfz post-install --refresh')")
            continue
        out, _ = postgres_exec(
            "openwebui_db", "SELECT params FROM model WHERE id = :'ident';",
            {"ident": name})
        try:
            cur_params = json.loads(out.strip()) if out.strip() else {}
        except json.JSONDecodeError:
            cur_params = {}
        if not isinstance(cur_params, dict):
            cur_params = {}
        # Compare only the keys we care about
        if all(cur_params.get(k) == v for k, v in desired_params.items()):
            if verbose:
                print(f"  openwebui: {name} params aligned ({desired_params})")
            continue
        changes.append(f"openwebui: SET {name}.params {cur_params} → {desired_params}")
        if dry_run:
            continue
        new_params = {**cur_params, **desired_params}
        row = rows[name]
        # Stamp ownership only on rows nobody else claims: never on a Manager
        # row (its writer owns it) or a user's custom model.
        stamp = not row.get("managed_by") and not row.get("base")
        sql = ("UPDATE model SET params = :'params'::json"
               + (", meta = (coalesce(meta::jsonb, '{}'::jsonb) || "
                  f"jsonb_build_object('{SYNC_OWNER_KEY}', :'owner'::text))::json" if stamp else "")
               + " WHERE id = :'ident' RETURNING id;")
        p = {"params": json.dumps(new_params), "ident": name}
        if stamp:
            p["owner"] = SYNC_OWNER
        out, _ = postgres_exec("openwebui_db", sql, p)
        if out.splitlines() != [name]:
            raise PostgresError(f"UPDATE {name} matched {out!r}, expected exactly that row")

    return changes


def sync_dify(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """No-op (#2901). This target only knew the retired gpustack provider
    (`langgenius/gpustack/gpustack`); manager boxes keep Dify's rows under
    `langgenius/openai_api_compatible/...`, and there is no verified current
    provider path to reconcile. Deleting 'stale' gpustack-provider rows would
    remove a user's own aliases, so nothing is read or written."""
    print("  dify: skipped — the gpustack provider path is retired and no current "
          "provider path is implemented; nothing is read or changed (#2901)")
    return []


def sync_cognee(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """Write COGNEE_LLM_MODEL + EMBEDDING_MODEL + EMBEDDING_DIM + LLM_ARGS to .env.

    M031 S3 / cognee context budget: LiteLLM doesn't recognize our custom
    aliases and falls back to a ~8K context default, silently truncating
    long prompts. We push `max_tokens` (the per-call output budget cap)
    via the LLM_ARGS JSON env var so cognee's litellm calls don't get
    capped at LiteLLM's conservative default. The value comes from the
    chat model's per_slot_context — divided by 4 to leave room for the
    prompt itself. (Refining this further requires registering the model
    with LiteLLM's model_cost dict at runtime — deferred to M032.)
    """
    import json as _json

    changes: list[str] = []
    chat_default = _defaults(spec).get("chat")
    embed_default = _defaults(spec).get("embedding")
    if not (chat_default and embed_default):
        return [f"cognee: SKIP — defaults.chat or defaults.embedding missing"]

    chat_model = spec["models"].get(chat_default, {})
    embed_model = spec["models"][embed_default]
    embed_dim = embed_model.get("embedding_dimensions", 768)

    # max_tokens budget: per_slot_context / 4 leaves headroom for the
    # prompt itself. If per_slot_context is missing, fall back to 32K
    # (4× LiteLLM's default of 8K — a conservative floor).
    # The budget is capped at COGNEE_MAX_OUTPUT_TOKENS: an extraction or summary
    # needs far less (8,915 output tokens at most, measured over a 500-document
    # ingest), and on a large-context model per_slot / 4 is 65,536, so one looping
    # generation held an engine slot for 55 minutes before it hit that ceiling.
    per_slot = int(chat_model.get("per_slot_context") or 0)
    max_tokens_budget = min(per_slot // 4 if per_slot else COGNEE_MAX_OUTPUT_TOKENS,
                            COGNEE_MAX_OUTPUT_TOKENS)

    # LLM_ARGS is a JSON blob cognee passes to LiteLLM as kwargs. Preserve
    # any operator-set keys (extra_body for enable_thinking etc.) by merging
    # into the existing value when we can parse it.
    existing_args: dict = {}
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("LLM_ARGS="):
            try:
                existing_args = _json.loads(line.split("=", 1)[1].strip())
                if not isinstance(existing_args, dict):
                    existing_args = {}
            except Exception:
                existing_args = {}
            break
    merged_args = {**existing_args, "max_tokens": max_tokens_budget}
    new_llm_args = _json.dumps(merged_args, separators=(",", ":"))

    desired = {
        "COGNEE_LLM_MODEL": f"openai/{chat_default}",
        "COGNEE_EMBEDDING_MODEL": embed_default,
        "COGNEE_EMBEDDING_DIM": str(embed_dim),
        "LLM_ARGS": new_llm_args,
    }
    env_text = ENV_FILE.read_text()
    env_lines = env_text.splitlines()
    new_lines = []
    seen = set()
    for line in env_lines:
        replaced = False
        for k, v in desired.items():
            if line.startswith(f"{k}="):
                cur_v = line.split("=", 1)[1].strip()
                if cur_v != v:
                    changes.append(f"cognee/.env: {k} {cur_v} → {v}")
                    new_lines.append(f"{k}={v}")
                else:
                    new_lines.append(line)
                seen.add(k)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)
    # Append any missing keys
    for k, v in desired.items():
        if k not in seen:
            changes.append(f"cognee/.env: ADD {k}={v}")
            new_lines.append(f"{k}={v}")
    if changes and not dry_run:
        ENV_FILE.write_text("\n".join(new_lines) + ("\n" if env_text.endswith("\n") else ""))
        if verbose:
            print("  cognee: .env updated; run `docker compose up -d --force-recreate cognee` to apply")
    return changes


def sync_moltis(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """For every running moltis container, append/update [models.<id>] context_window blocks."""
    changes: list[str] = []
    instances = docker_containers_by_label("razzfazz.agent.type=moltis")
    if not instances:
        return ["moltis: no running instances"]
    chat_models = {n: m for n, m in spec["models"].items() if any(r in ["chat","coding","general"] for r in m.get("roles", []))}
    block = "\n\n# ── Stack-wide model context windows (managed by core/llm/sync.py) ──\n"
    for name, model in chat_models.items():
        # quote name if it contains a dot
        key = f'"{name}"' if "." in name else name
        block += f"\n[models.{key}]\ncontext_window = {model['per_slot_context']}\n"

    # M031-FOLLOWUPS A4: in-container config writes must run as the agent user,
    # not root. moltis runs as `moltis` (uid 1000 in our image). Without -u
    # the file lands as root-owned and breaks moltis's next startup.
    DOCKER_EXEC = ["docker", "exec", "-u", "moltis"]
    for c in instances:
        # Read current managed block (if any) and compare against the desired block
        _, out, _ = run(DOCKER_EXEC + [c, "sh", "-c",
                         "sed -n '/# ── Stack-wide model context windows/,$p' /home/moltis/.config/moltis/moltis.toml"],
                        check=False)
        existing_block = out
        # Normalize for comparison (strip trailing whitespace)
        if existing_block.strip() == block.strip():
            if verbose:
                print(f"  moltis: {c} already aligned")
            continue
        already_managed = bool(existing_block.strip())
        verb = "UPDATE" if already_managed else "ADD"
        changes.append(f"moltis: {verb} {c} ({len(chat_models)} model blocks)")
        if not dry_run:
            if already_managed:
                run(DOCKER_EXEC + [c, "sh", "-c",
                     "sed -i '/# ── Stack-wide model context windows/,$d' /home/moltis/.config/moltis/moltis.toml"])
            run(DOCKER_EXEC + [c, "sh", "-c",
                 f"cat >> /home/moltis/.config/moltis/moltis.toml << 'MOLTIS_TOML_EOF'\n{block}\nMOLTIS_TOML_EOF"])
    return changes


def sync_hermes(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """For every running hermes container, set model.context_length to match the agent's assigned role."""
    changes: list[str] = []
    instances = docker_containers_by_label("razzfazz.agent.type=hermes",
                                            exclude_label="razzfazz.agent.companion=true")
    if not instances:
        return ["hermes: no running instances"]
    role = spec["agents"]["hermes"]["role"]
    model_name = _defaults(spec)[role]
    model = spec["models"][model_name]
    desired_ctx = model["per_slot_context"]
    desired_model = model_name

    # M031-FOLLOWUPS A4: hermes runs as `hermes` (uid 10000 in our image).
    # `docker exec hermes config set ...` as root writes /opt/data/config.yaml
    # owned by root, then on next startup the hermes user can't read it and
    # falls back to defaults (provider=openrouter), 401-ing every chat call.
    # This caused a 1-hour debugging session on dev; the -u guards against repeat.
    DOCKER_EXEC = ["docker", "exec", "-u", "hermes"]
    for c in instances:
        # Read current config
        _, out, _ = run(
            DOCKER_EXEC + [c, "/opt/hermes/.venv/bin/hermes", "config", "show"],
            check=False,
        )
        cur_ctx_line = [ln for ln in out.splitlines() if "context_length" in ln]
        needs_ctx = not any(f"'context_length': {desired_ctx}" in ln for ln in cur_ctx_line)
        needs_model = not any(f"'default': '{desired_model}'" in ln for ln in out.splitlines() if "Model:" in ln)

        if needs_ctx:
            changes.append(f"hermes: SET {c} model.context_length={desired_ctx}")
            if not dry_run:
                run(DOCKER_EXEC + [c, "/opt/hermes/.venv/bin/hermes", "config", "set",
                     "model.context_length", str(desired_ctx)], check=False)
        if needs_model:
            changes.append(f"hermes: SET {c} model.default={desired_model}")
            if not dry_run:
                run(DOCKER_EXEC + [c, "/opt/hermes/.venv/bin/hermes", "config", "set",
                     "model.default", desired_model], check=False)
        if not needs_ctx and not needs_model and verbose:
            print(f"  hermes: {c} already aligned (model={desired_model}, ctx={desired_ctx})")
    return changes


def sync_lightrag(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """Write LIGHTRAG_LLM_MODEL + LIGHTRAG_EMBEDDING_MODEL + LIGHTRAG_EMBEDDING_DIM to .env.

    LightRAG uses the OpenAI SDK directly (no LiteLLM in the middle), so
    the model name goes in BARE — no `openai/` prefix. Embedding dim is
    pulled from the YAML if the embedding model declares one; otherwise
    defaults to 768 (nomic-embed-text's value).
    """
    changes: list[str] = []
    chat_default = _defaults(spec).get("chat")
    embed_default = _defaults(spec).get("embedding")
    if not (chat_default and embed_default):
        return [f"lightrag: SKIP — defaults.chat or defaults.embedding missing"]

    embed_model = spec["models"].get(embed_default, {})
    embed_dim = embed_model.get("embedding_dimensions", 768)

    desired = {
        "LIGHTRAG_LLM_MODEL": chat_default,
        "LIGHTRAG_EMBEDDING_MODEL": embed_default,
        "LIGHTRAG_EMBEDDING_DIM": str(embed_dim),
    }
    env_text = ENV_FILE.read_text()
    env_lines = env_text.splitlines()
    new_lines = []
    seen = set()
    for line in env_lines:
        replaced = False
        for k, v in desired.items():
            if line.startswith(f"{k}="):
                cur_v = line.split("=", 1)[1].strip()
                if cur_v != v:
                    changes.append(f"lightrag/.env: {k} {cur_v} → {v}")
                    new_lines.append(f"{k}={v}")
                else:
                    new_lines.append(line)
                seen.add(k)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)
    for k, v in desired.items():
        if k not in seen:
            changes.append(f"lightrag/.env: ADD {k}={v}")
            new_lines.append(f"{k}={v}")
    if changes and not dry_run:
        ENV_FILE.write_text("\n".join(new_lines) + ("\n" if env_text.endswith("\n") else ""))
        if verbose:
            print("  lightrag: .env updated; run `docker compose up -d --force-recreate lightrag` to apply")
    return changes


def sync_paperclip(spec: dict, dry_run: bool, verbose: bool) -> list[str]:
    """For every running paperclip container, regenerate OPENCODE_GPUSTACK_CONFIG.

    Paperclip is per-user; new instances pick up the YAML via agent-manager
    catalog at provision time (M031 S2). This sync target covers the
    already-running case: a YAML edit + sync.py invocation rewrites the
    bundled opencode config in-place without a recreate.

    Note: paperclip caches the parsed config in-memory on boot; the new
    value lands at file level but only takes effect after the next
    `docker restart` of the affected instance. Reported as part of the
    change list so the operator knows.
    """
    changes: list[str] = []
    instances = docker_containers_by_label("razzfazz.agent.type=paperclip")
    if not instances:
        return ["paperclip: no running instances"]

    # Build the expected provider block from YAML in opencode's partial shape.
    import json as _json
    chat_models = {n: m for n, m in spec["models"].items()
                   if any(r in {"chat", "coding", "general"} for r in m.get("roles", []))}
    models_obj = {n: {"name": m.get("display_name", n)} for n, m in chat_models.items()}
    new_value = _json.dumps({
        "provider": {
            "gpustack": {
                "name": "GPUStack",
                "npm": "@ai-sdk/openai-compatible",
                "env": ["GPUSTACK_API_KEY"],
                "options": {
                    "baseURL": "http://llm:8080/v1",
                    "apiKey": "${GPUSTACK_API_KEY}",
                },
                "models": models_obj,
            }
        }
    }, separators=(",", ":"))

    for c in instances:
        rc, cur, _ = run(["docker", "exec", c, "printenv", "OPENCODE_GPUSTACK_CONFIG"], check=False)
        if rc != 0:
            changes.append(f"paperclip: SKIP {c} (no OPENCODE_GPUSTACK_CONFIG env)")
            continue
        if cur.strip() == new_value:
            if verbose:
                print(f"  paperclip: {c} already aligned")
            continue
        # We can't change env vars on a running container without recreate.
        # Report the diff; the operator (or next agent-manager upgrade pass)
        # picks it up at the next container recreate via catalog.py S2.
        changes.append(
            f"paperclip: {c} OPENCODE_GPUSTACK_CONFIG diff — recreate via agent-manager Update to apply"
        )
    return changes


# ── Drift detection (--check, read-only) ─────────────────────────────────────
#
# B-7: standard-models.yaml ↔ live GPUStack drift.
#
# Background: the propagate-llm-config skill exists because gpustack's
# /v1/models response carries `meta: null` for every entry — no consumer can
# auto-discover the per-model context size. The YAML is the declarative
# source of truth, sync.py pushes that forward to consumers, but until B-7
# nothing audited the *backwards* direction: an operator could edit gpustack
# directly through the UI (or sync.py could fail mid-flight) and the YAML
# would silently disagree with live state.
#
# `--check` is the audit: read /v1/models, diff against the YAML, report
# every drifting alias with one of three tags:
#
#   MISSING   — alias declared in YAML but absent from gpustack
#   EXTRA     — alias registered in gpustack but absent from YAML
#   MISMATCH  — alias in both, but huggingface_filename or backend_parameters
#               differ
#
# Read-only by design. Never issues PUT/POST/DELETE. Exit code 0 iff zero
# drift, non-zero otherwise — safe to wire into kassasturz.


def check_gpustack(spec: dict, verbose: bool) -> list[str]:
    """Compare YAML spec to live gpustack /v1/models. Return drift list.

    Each entry is a string of the form `gpustack: <CLASS> <name>: <detail>`
    where CLASS ∈ {MISSING, EXTRA, MISMATCH}. Empty list means fully
    aligned.
    """
    drift: list[str] = []
    current = gpustack_api("GET", "/v1/models")
    by_name = {m["name"]: m for m in current.get("items", [])}
    declared = set(spec.get("models", {}).keys())
    live = set(by_name.keys())

    # MISSING: declared but not in live.
    for name in sorted(declared - live):
        drift.append(f"gpustack: MISSING {name} (declared in YAML, not registered in gpustack)")

    # EXTRA: in live but not declared.
    for name in sorted(live - declared):
        drift.append(f"gpustack: EXTRA {name} (registered in gpustack, not in YAML)")

    # MISMATCH: alias on both sides; compare huggingface_filename + backend_parameters.
    for name in sorted(declared & live):
        model = spec["models"][name]
        cur = by_name[name]
        desired_file = model.get("huggingface_filename")
        desired_params = list(model.get("backend_parameters", []))
        cur_file = cur.get("huggingface_filename")
        cur_params = cur.get("backend_parameters") or []
        if cur_file == desired_file and cur_params == desired_params:
            if verbose:
                print(f"  gpustack: {name} aligned")
            continue
        diff_parts = []
        if cur_file != desired_file:
            diff_parts.append(f"file {cur_file!r} ↔ YAML {desired_file!r}")
        if cur_params != desired_params:
            diff_parts.append(f"params {cur_params!r} ↔ YAML {desired_params!r}")
        drift.append(
            f"gpustack: MISMATCH {name}: " + "; ".join(diff_parts)
        )

    return drift


CHECK_FUNCS = {
    "gpustack": check_gpustack,
}


# ── Main ─────────────────────────────────────────────────────────────────────


TARGET_FUNCS = {
    "gpustack": sync_gpustack,
    "openwebui": sync_openwebui,
    "dify": sync_dify,
    "cognee": sync_cognee,
    "lightrag": sync_lightrag,
    "moltis": sync_moltis,
    "hermes": sync_hermes,
    "paperclip": sync_paperclip,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_YAML), help="path to standard-models.yaml")
    ap.add_argument("--target", default="all",
                    help=f"comma-separated subset of: {','.join(ALL_TARGETS)}, or 'all'")
    ap.add_argument("--dry-run", action="store_true", help="report changes without applying")
    ap.add_argument("--plan", action="store_true",
                    help="dry-run that also prints every row kept and why (#2901); applies nothing")
    ap.add_argument("--check", action="store_true",
                    help="read-only drift report: compare YAML to live gpustack; "
                         "exit non-zero on any drift (B-7)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print per-item aligned status")
    args = ap.parse_args()
    if args.plan:
        args.dry_run = True
        args.verbose = True

    config_path = Path(args.config)
    if not config_path.is_file():
        sys.exit(f"config file not found: {args.config}")
    spec = yaml.safe_load(config_path.read_text())
    if spec.get("version") != 1:
        sys.exit(f"unsupported standard-models.yaml version: {spec.get('version')}")

    # Validate per_slot_context ≤ max_context_length for each model
    for name, m in spec.get("models", {}).items():
        psc = m.get("per_slot_context")
        mcl = m.get("max_context_length")
        if psc and mcl and psc > mcl:
            sys.exit(f"validation: {name}.per_slot_context ({psc}) > max_context_length ({mcl})")

    # --check is the read-only drift detector (B-7). It only audits gpustack
    # today; future check_* functions (consumer-side drift) plug into
    # CHECK_FUNCS the same way TARGET_FUNCS does for the apply side.
    if args.check:
        print(f"==> drift check (read-only) from {args.config}")
        print(f"    targets: {', '.join(CHECK_FUNCS.keys())}")
        print()
        all_drift: list[str] = []
        errors: list[str] = []
        for t, fn in CHECK_FUNCS.items():
            print(f"── {t} (check) ──")
            try:
                drift = fn(spec, args.verbose)
            except SystemExit as e:
                errors.append(f"{t}: {e}")
                print(f"  ERROR: {e}")
                continue
            except Exception as e:
                errors.append(f"{t}: {e}")
                print(f"  ERROR: {e}")
                continue
            if not drift:
                print("  (aligned)")
            else:
                for d in drift:
                    print(f"  {d}")
                all_drift.extend(drift)
            print()
        print(f"==> drift summary: {len(all_drift)} drift entry/entries")
        if errors:
            print(f"    ERRORS: {len(errors)}")
            for e in errors:
                print(f"      {e}")
            return 1
        return 1 if all_drift else 0

    targets = ALL_TARGETS if args.target == "all" else args.target.split(",")
    bad = [t for t in targets if t not in TARGET_FUNCS]
    if bad:
        sys.exit(f"unknown target(s): {bad}; valid: {ALL_TARGETS} or 'all'")

    print(f"==> sync {('(dry-run)' if args.dry_run else '')} from {args.config}")
    print(f"    targets: {', '.join(targets)}")
    print()

    all_changes: dict[str, list[str]] = {}
    errors: list[str] = []
    for t in targets:
        print(f"── {t} ──")
        try:
            changes = TARGET_FUNCS[t](spec, args.dry_run, args.verbose)
            all_changes[t] = changes
            if not changes:
                print(f"  (no changes)")
            else:
                for c in changes:
                    print(f"  {c}")
        except SystemExit as e:
            errors.append(f"{t}: {e}")
            print(f"  ERROR: {e}")
        except Exception as e:
            errors.append(f"{t}: {e}")
            print(f"  ERROR: {e}")
        print()

    total_changes = sum(len(v) for v in all_changes.values())
    print(f"==> summary: {total_changes} change(s) across {len(targets)} target(s)")
    if args.dry_run:
        print("    (dry-run: nothing applied; rerun without --dry-run to apply)")
    if errors:
        print(f"    ERRORS: {len(errors)}")
        for e in errors:
            print(f"      {e}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
