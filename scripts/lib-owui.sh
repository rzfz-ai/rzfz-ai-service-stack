#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/lib-owui.sh — Open WebUI wiring shared by post-install and upgrade
# =============================================================================
# ONE implementation of "which LLM backend + key does Open WebUI get, and how
# is it asserted in OWUI's persisted config", sourced by cli/post-install.sh
# (--preset / --refresh) and cli/upgrade.sh (post-upgrade reconcile). Before
# this file the resolvers lived only in post-install.sh, so an upgrade could
# not re-assert anything without spawning the whole provisioning script — and
# when that died early, nothing reached OWUI (#908 follow-up).
#
# #1185 — the persisted OpenAI connection is reconciled as an UPSERT per base
# URL (scripts/owui_config_reconcile.py, JSON-aware), never appended, and the
# .env key is VALIDATED against the manager before it is allowed to overwrite
# anything: a dead .env key never clobbers a working in-app key.
#
# Sourced library: NO top-level `set` line (options would leak into every
# sourcing shell, #382). Requires scripts/lib.sh (print_*, read_env_value,
# update_env_value) to be sourced first. Callers running with `set -u` are
# supported; every external read is `|| true`-guarded for `set -e`.
#
# Env contract (post-install exports .env; upgrade calls owui_lib_env_from_file):
#   ENV_FILE, COMPOSE_PROFILES, MAIN_DOMAIN, AUTHENTIK_BOOTSTRAP_PASSWORD,
#   OPENWEBUI_PORT, LLM_MANAGER_PORT, POSTGRES_USER, OPENWEBUI_DB,
#   GPUSTACK_API_KEY, LLM_MANAGER_GPUSTACK_FEDERATED (#1442: the ownership
#   predicate reads it, so upgrade.sh has to see it too)
# =============================================================================

# Repo root for the python helper — SCRIPT_DIR when the caller set it (both
# CLIs do), else derived from this file's location.
_OWUI_REPO_ROOT="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Fill the env contract from $ENV_FILE for callers that do NOT export .env
# (cli/upgrade.sh). Never sources .env (metachars, #949): one read_env_value
# per key, and an already-set variable always wins.
owui_lib_env_from_file() {
    local _f="${ENV_FILE:-.env}" _v _val
    for _v in COMPOSE_PROFILES MAIN_DOMAIN AUTHENTIK_BOOTSTRAP_PASSWORD \
              OPENWEBUI_PORT LLM_MANAGER_PORT POSTGRES_USER OPENWEBUI_DB \
              GPUSTACK_API_KEY LLM_MANAGER_GPUSTACK_FEDERATED \
              RAZZFAZZ_ADMIN_USERNAME; do
        if [ -z "${!_v:-}" ]; then
            _val=$(read_env_value "$_f" "$_v" 2>/dev/null) || _val=""
            [ -n "$_val" ] && printf -v "$_v" '%s' "$_val"
        fi
    done
    return 0
}

# ── LLM backend source-of-truth (#976) ───────────────────────────────────────
# On an llm-manager box the manager fronts every worker and METERS usage, so
# stack consumers (OWUI chat + RAG embedding / completion / reranker, …) must
# talk to IT — not the bundled GPUStack, which on a manager-only box may not be
# enabled at all. These resolvers are the SINGLE place that decides which
# backend URL + key a consumer gets, so the gpustack default lives in exactly
# one spot and a gpustack-disabled box wires cleanly to the manager instead of
# to a dead endpoint.

_gpustack_profile_active() {
    # True when a profile that actually RUNS GPUStack is enabled. Exact
    # comma-delimited token match — `grep -w llm` would false-match
    # `llm-manager` / `llm-registry` (the `-` is a word boundary).
    case ",${COMPOSE_PROFILES:-}," in
        *,llm-legacy,*) return 0 ;;   # #1447: `llm` removed, `llm-cpu` folded in
    esac
    return 1
}

# WHICH backend this box is wired to. The PROFILE is the authority — exact
# comma-delimited token match, mirroring _gpustack_profile_active above. It
# deliberately does NOT require the container to be running: a transient
# outage must delay an authoritative write, never change the ANSWER (#976).
_llm_manager_profile_active() {
    case ",${COMPOSE_PROFILES:-}," in
        *,llm-manager,*) return 0 ;;
    esac
    return 1
}

# #1265: the name of the backend this box serves through — for OPERATOR-FACING
# TEXT ONLY (verify labels, hints, warnings). It decides nothing: every wiring
# path reads the profile helpers directly. Anchored on the PROFILE, not on
# liveness, because a momentarily stopped manager does not turn a Manager box
# into a GPUStack box — and reading "GPUStack" on a box that has none sends the
# operator looking in the wrong place (Phase-3 round 3: the verify report said
# "Open WebUI: GPUStack connection configured" while the connection was
# http://llm-manager:8080/v1).
_llm_backend_display_name() {
    # #1445 (C5c / E1): every consumer address is http://llm:8080/v1, which is
    # the manager — so on a box that runs the manager the label says so, even
    # when GPUStack holds the models behind it (the manager fronts them, #1442).
    # The rule this file already stated stays the rule: the label must name what
    # the URL points at. Following OWNERSHIP here printed "GPUStack" next to a
    # manager URL on a dual, not-yet-federated box — the exact defect #1265
    # fixed, in the other direction.
    if _llm_manager_profile_active; then
        printf '%s' 'LLM Manager'
    else
        printf '%s' 'GPUStack'
    fi
}

# Is the manager reachable right now? Liveness decides whether to SKIP an
# authoritative write — never which backend a consumer is pointed at.
_llm_manager_running() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx llm-manager
}

# "profile on AND up" — for call sites that need a live manager (a mint).
_llm_manager_active() {
    _llm_manager_profile_active && _llm_manager_running
}

# #1441 (cutover C1): THE one question every consumer, both deploy arms and the
# verify gate ask — does the LLM Manager own this box's models? Today: the
# manager profile is on and no GPUStack profile is. Until #1442 fronts GPUStack
# through the manager, a box that RUNS GPUStack has its models there, so every
# consumer follows GPUStack and the manager idles; the moment #1442 lands this
# predicate is the single line that flips the whole box to manager-first.
# Lives here (not in lib-llm-manager-deploy.sh) because cli/upgrade.sh sources
# only this file and the resolvers below have to answer the same question.
# #1442: WHO DEPLOYS the standard set is a different question from who fronts
# it. A box with a GPUStack profile deploys its models in GPUStack (the manager
# cannot deploy into an external backend; #330 reserves the GPU for it) and,
# once federated, the manager fronts them. Both deploy arms ask THIS.
llm_manager_deploys_standard_set() {
    _llm_manager_profile_active || return 1
    if _gpustack_profile_active; then return 1; fi
    return 0
}

llm_manager_owns_standard_set() {
    _llm_manager_profile_active || return 1
    if _gpustack_profile_active; then
        # #1442 (cutover C2): once post-install has registered GPUStack as the
        # manager's external backend (llm_manager_federate_gpustack writes the
        # marker into .env; load_env exports it), the manager fronts GPUStack
        # and owns the box's models — every consumer wires to it.
        [ "${LLM_MANAGER_GPUSTACK_FEDERATED:-}" = "true" ] && return 0
        return 1
    fi
    return 0
}

_llm_manager_mint_service_key() {
    # $1 = service (openwebui|dify) → plaintext key on stdout, rc 1 on failure.
    # docker exec = operator privilege; deliberately NO new network endpoint
    # (the #612 agent-key mint is peer-anchored to agent-manager and stays so).
    # -i is load-bearing: without it docker exec does not forward stdin, the
    # heredoc never reaches python3 - and the mint "succeeds" empty (#666
    # live-verify on 0.91).
    docker exec -i llm-manager python3 - "$1" <<'MINTEOF'
import sys
sys.path.insert(0, "/app")
from app.auth import generate_api_key
from app.config import get_settings
from app.db import session_scope
from app.models import ApiKey, CostCenter

service = sys.argv[1].strip()
cc_name = f"stack/{service}"
plaintext, key_hash, display = generate_api_key(get_settings().key_prefix)
with session_scope() as s:
    cc = s.query(CostCenter).filter(CostCenter.name == cc_name).first()
    if cc is None:
        cc = CostCenter(name=cc_name, team="stack")
        s.add(cc)
        s.flush()
    s.add(ApiKey(key_hash=key_hash, key_prefix=display, cost_center_id=cc.id))
print(plaintext)
MINTEOF
}

# ── #1185: key validation ────────────────────────────────────────────────────
# Is $1 accepted by the manager's metered /v1 surface? Prints exactly one of
#   ok      — 200 from GET /v1/models
#   dead    — 401/403 (the key is not in the manager's store)
#   unknown — anything else (manager down, 5xx, no answer): NOT a verdict.
# Loopback published port, the same probe wire_llm_manager_consumers uses for
# the Dify key. An empty key is dead by definition.
_llm_manager_key_status() {
    local key="$1" code
    [ -n "$key" ] || { printf 'dead'; return 0; }
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        -H "Authorization: Bearer $key" \
        "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/v1/models" 2>/dev/null) || code=""
    case "$code" in
        200)     printf 'ok' ;;
        401|403) printf 'dead' ;;
        *)       printf 'unknown' ;;
    esac
}

# The keys OWUI currently HOLDS for base URL $1 (connection entries + the RAG
# key rows), one per line — read from openwebui_db by the python helper. Empty
# when postgres is not up or the schema is not the per-key one.
_owui_app_key_candidates() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres || return 0
    python3 "${_OWUI_REPO_ROOT}/scripts/owui_config_reconcile.py" candidates \
        --url "$1" --db "${OPENWEBUI_DB:-openwebui_db}" \
        --pg-user "${POSTGRES_USER:-docker}" 2>/dev/null || true
}

# THE stack/openwebui service key, VALIDATED (#1185). Prints the key to use:
#   1. LLM_MANAGER_OWUI_KEY from .env when the manager accepts it — or when
#      the manager cannot be asked (unknown): an unverifiable key is left
#      alone, never replaced on a guess.
#   2. else a key OWUI already holds that the manager DOES accept: the app
#      config wins, .env is corrected to it (WARN — this is the 0.91 drift,
#      an in-app fix that .env never learned about; the seed must not punish
#      it).
#   3. else a freshly minted key (.env updated); on a failed mint the .env
#      value stays, whatever it is, and a WARN says so.
# Runs inside $(...) — every message goes to stderr so it cannot leak into
# the key the caller captures.
_owui_resolve_manager_key() {
    local env_key st cand k url
    env_key=$(read_env_value "$ENV_FILE" LLM_MANAGER_OWUI_KEY 2>/dev/null) || env_key=""
    if [ -n "$env_key" ]; then
        st=$(_llm_manager_key_status "$env_key")
        case "$st" in
            ok|unknown) printf '%s' "$env_key"; return 0 ;;
        esac
    fi
    # #1441 rev-D (review N1): the key this function resolves is the MANAGER's,
    # so the candidate lookup asks for the MANAGER's URL — not the
    # ownership-following resolver, which on a dual box answers GPUStack's
    # address. With that address the #1185 adoption step ("a key OWUI already
    # holds and the manager accepts wins and is written back") looked in the
    # wrong place and minted a fresh key on every run.
    url="http://llm-manager:8080/v1"
    while IFS= read -r cand; do
        [ -n "$cand" ] || continue
        [ "$cand" = "$env_key" ] && continue
        if [ "$(_llm_manager_key_status "$cand")" = "ok" ]; then
            # rzfz review #1218 F2: `${env_key:+…}${env_key:-…}` on the SAME variable
            # printed the dead key itself on the DEAD branch. Two sentences, no value.
            if [ -n "$env_key" ]; then
                print_warning "LLM_MANAGER_OWUI_KEY in .env is DEAD (the manager rejects it) but Open WebUI already holds a key the manager accepts — keeping the app's key and writing it back to .env (#1185)." >&2
            else
                print_warning "LLM_MANAGER_OWUI_KEY in .env is empty but Open WebUI already holds a key the manager accepts — keeping the app's key and writing it back to .env (#1185)." >&2
            fi
            update_env_value "$ENV_FILE" "LLM_MANAGER_OWUI_KEY" "$cand"
            printf '%s' "$cand"; return 0
        fi
    done <<< "$(_owui_app_key_candidates "$url")"
    if [ -n "$env_key" ]; then
        print_warning "LLM_MANAGER_OWUI_KEY in .env is DEAD (the manager rejects it) and Open WebUI holds no valid key either — minting a fresh stack/openwebui service key (#1185)." >&2
    fi
    k=$(_llm_manager_mint_service_key openwebui 2>/dev/null) || k=""
    if [ -n "$k" ]; then
        update_env_value "$ENV_FILE" "LLM_MANAGER_OWUI_KEY" "$k"
        printf '%s' "$k"; return 0
    fi
    if [ -n "$env_key" ]; then
        print_warning "Could not mint a stack/openwebui service key — the dead LLM_MANAGER_OWUI_KEY stays in .env; OWUI will keep failing with 401 until 'rzfz post-install --refresh' runs with llm-manager up (#1185)." >&2
    fi
    printf '%s' "$env_key"
}

# The OpenAI-compatible base URL OWUI (and other OpenAI-client consumers) use
# for chat + embeddings.
_owui_llm_base_url() {
    # #1445 (cutover C5c / Operator-Entscheidung E1): EINE kanonische,
    # backend-unabhängige Adresse. `llm` ist ein Netz-Alias des immer laufenden
    # Managers (#1443); der Manager bedient eigene Worker oder frontet GPUStack
    # als registriertes externes Backend (#1442). Ein Konsument nennt damit nie
    # wieder ein Backend, und beim Backend-Wechsel ist nichts umzuschreiben.
    printf 'http://llm:8080/v1'
}

# The matching key. #1445 (C5c / E1): the address is the manager's, so the key
# is too — the VALIDATED stack/openwebui SERVICE key (#1185: .env, app-held, or
# minted — see _owui_resolve_manager_key), so metering attributes OWUI traffic
# to its cost-centre. GPUSTACK_API_KEY is no longer a consumer's business: the
# manager holds it as the credential of its external backend (#1442).
_owui_llm_key() {
    _owui_resolve_manager_key
}

# OWUI's external reranker wants the FULL endpoint (it POSTs the body AS-IS, no
# "/rerank" append — see project_openwebui_external_reranker_url). #1445 (C5c /
# E1): the same canonical address as every other consumer path; the manager
# gateway proxies /v1/rerank (#976).
_owui_rerank_url() {
    printf 'http://llm:8080/v1/rerank'
}

# ── #1185: .env + app config as a PAIR ───────────────────────────────────────
# $1=bases $2=keys (both ;-joined, aligned by position) $3=url $4=key →
# prints the keys list with the entry paired to $3 set to $4. Pure: the
# counterpart of _owui_append_endpoint (post-install) for the "URL already
# present, key stale" case that append cannot see.
_owui_set_endpoint_key() {
    local -a _bs _ks
    IFS=';' read -r -a _bs <<< "$1"
    IFS=';' read -r -a _ks <<< "$2"
    # rzfz review #1218 F1: join POSITIONALLY. `${out:+$out;}` suppressed the
    # separator while `out` was still "" — i.e. after a LEADING empty key — so
    # bases `a;b` + keys `;K2` collapsed to one element and every key shifted
    # onto the wrong base (the misalignment class this file exists to close).
    local i out=""
    for i in "${!_bs[@]}"; do
        [ "${_bs[$i]}" = "$3" ] && _ks[i]="$4"
        [ "$i" -gt 0 ] && out="$out;"
        out="$out${_ks[$i]:-}"
    done
    printf '%s' "$out"
}

# The key .env currently pairs with base URL $3 ($1=bases, $2=keys, both
# ;-joined and aligned by position). Prints the empty string when the URL is
# not listed at all — the caller cannot tell those two apart from
# `_owui_set_endpoint_key`'s output. Pure; used by the env-vs-DB verify (#1252).
_owui_env_endpoint_key() {
    local -a _bs _ks
    IFS=';' read -r -a _bs <<< "$1"
    IFS=';' read -r -a _ks <<< "$2"
    local i
    for i in "${!_bs[@]}"; do
        if [ "${_bs[$i]}" = "$3" ]; then
            printf '%s' "${_ks[$i]:-}"
            return 0
        fi
    done
    return 0
}

# $1=bases $2=keys $3=url $4=key → TWO lines (bases / keys) in which $3 appears
# exactly once carrying $4: its key pinned when the URL is already listed,
# appended positionally when it is not. Pure.
#
# This is the PAIR-level primitive. The two halves already in the tree each
# cover only one case — `_owui_set_endpoint_key` (above) fixes a stale key but
# cannot add a missing URL, `_owui_append_endpoint` (cli/post-install.sh) adds a
# URL but cannot fix a stale key — and .env needs both in one call. It is
# deliberately NOT a call to post-install's helper: this library is sourced by
# cli/upgrade.sh too, which does not define it, and depending on a function the
# caller happens to have is the coupling #908 moved these resolvers here to end.
_owui_upsert_endpoint_pair() {
    local bases="$1" keys="$2" url="$3" key="$4"
    case ";${bases};" in
        *";${url};"*)
            printf '%s\n%s\n' "$bases" "$(_owui_set_endpoint_key "$bases" "$keys" "$url" "$key")"
            return 0 ;;
    esac
    # An EMPTY list must yield the url ALONE — not ";url", whose leading empty
    # entry OWUI would try to query as a zero-length base URL (#976).
    if [ -z "$bases" ]; then
        printf '%s\n%s\n' "$url" "$key"
        return 0
    fi
    printf '%s;%s\n%s;%s\n' "$bases" "$url" "$keys" "$key"
}

# Keep the .env pair (OWUI_OPENAI_BASE_URLS / OWUI_OPENAI_KEYS — what
# modules/chat/compose.yml feeds to the container as OPENAI_API_BASE_URLS /
# OPENAI_API_KEYS) in agreement with the connection just asserted in the app
# for base URL $1 with key $2. Sets _OWUI_ENV_PAIR_CHANGED=true when .env was
# written.
#
# #1252: this used to REALIGN only — `[ -n "$bases" ] || return 0` plus a
# key-only rewrite, on the theory that "the wiring functions own the append".
# They do, but they run ONCE, at the very top of post-install, and on a clean
# Manager box the manager is frequently not answering yet: the OWUI arm of
# wire_llm_manager_consumers is then skipped for want of a service key, and
# nothing writes the pair again. The authoritative reconcile that runs later
# (owui_configure_connection, with a key it minted itself) fixed the DB rows and
# returned here — where the early `return 0` on an empty list dropped the .env
# half on the floor. Result: the DB held the manager connection while the
# container env still carried compose's gpustack default and the
# `gpustack_CHANGEME_AFTER_FIRST_START` placeholder — two truths, and the one an
# upgrade re-seeds from is the WRONG one (#1185's class). The pair is now
# UPSERTed, so env and DB can no longer disagree.
_owui_realign_env_pair() {
    local url="$1" key="$2"
    _OWUI_ENV_PAIR_CHANGED=false
    [ -n "$url" ] && [ -n "$key" ] || return 0
    local bases keys out new_bases new_keys
    bases=$(read_env_value "$ENV_FILE" OWUI_OPENAI_BASE_URLS 2>/dev/null) || bases=""
    keys=$(read_env_value "$ENV_FILE" OWUI_OPENAI_KEYS 2>/dev/null) || keys=""
    out=$(_owui_upsert_endpoint_pair "$bases" "$keys" "$url" "$key")
    new_bases=$(printf '%s' "$out" | sed -n 1p)
    new_keys=$(printf '%s' "$out" | sed -n 2p)
    if [ "$new_bases" != "$bases" ]; then
        update_env_value "$ENV_FILE" "OWUI_OPENAI_BASE_URLS" "$new_bases"
        _OWUI_ENV_PAIR_CHANGED=true
    fi
    if [ "$new_keys" != "$keys" ]; then
        update_env_value "$ENV_FILE" "OWUI_OPENAI_KEYS" "$new_keys"
        _OWUI_ENV_PAIR_CHANGED=true
    fi
    return 0
}

# Assert OWUI's PERSISTED OpenAI connection + RAG key family for base URL $1
# with key $2 (reranker endpoint $3): one entry per base URL (upsert, never a
# duplicate), `rag.openai.api_key` + `rag.external_reranker_api_key` set to
# the same key, the legacy `openai` blob mirrored when present. On a box
# wired to the manager the persisted gpustack endpoints (first-boot env seed)
# are moved to the manager in the same pass — the #976 transition, now
# JSON-aware instead of a text replace. Recreates openwebui ONLY when a row
# actually changed. Best-effort: never returns non-zero.
owui_reconcile_openai_keys() {
    local url="$1" key="$2" rerank_url="$3"
    [ -n "$url" ] || return 0
    if [ -z "$key" ]; then
        print_warning "OWUI key reconcile: no key available for ${url} — persisted connection left as-is."
        return 0
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres; then
        print_warning "OWUI key reconcile: postgres is not running — skipped."
        return 0
    fi
    local -a repl=()
    if [ "$url" != "http://gpustack:9090/v1-openai" ]; then
        repl=(--replace "http://gpustack:9090/v1-openai=${url}"
              --replace-rerank "http://gpustack:9090/v1/rerank=${rerank_url}")
    fi
    local out rc=0
    out=$(python3 "${_OWUI_REPO_ROOT}/scripts/owui_config_reconcile.py" apply \
        --url "$url" --key "$key" --rerank-url "$rerank_url" "${repl[@]}" \
        --db "${OPENWEBUI_DB:-openwebui_db}" --pg-user "${POSTGRES_USER:-docker}" 2>&1) || rc=$?
    case "$rc" in
        0) ;;
        3) print_substep "OWUI persisted config is still the pre-0.11 single-row schema — key reconcile skipped (OWUI migrates it on its next start)."
           return 0 ;;
        *) print_warning "OWUI key reconcile FAILED (rc=${rc}): $(printf '%s\n' "$out" | head -n1)"
           return 0 ;;
    esac
    local changed
    changed=$(printf '%s\n' "$out" | sed -n 's/^CHANGED=//p' | head -n1)
    changed="${changed:-0}"
    _owui_realign_env_pair "$url" "$key"
    # rzfz review #1218 F4: `_OWUI_ENV_PAIR_CHANGED` was a dead flag — an .env-only
    # realignment reported "already consistent" and skipped the recreate. It now
    # counts as a change (openwebui reads OWUI_OPENAI_KEYS at start).
    if [ "${_OWUI_ENV_PAIR_CHANGED:-false}" = true ] && ! [ "$changed" -gt 0 ] 2>/dev/null; then
        changed=1
        print_substep "OWUI .env pair (OWUI_OPENAI_BASE_URLS/OWUI_OPENAI_KEYS) aligned with the DB for ${url} — recreating openwebui so it is read (#1185/#1252)."
    fi
    if [ "$changed" -gt 0 ] 2>/dev/null; then
        # OWUI reads the per-key rows live, but its model list is cached —
        # recreate so the new key is used everywhere at once (#427: report
        # what actually happened).
        if docker compose up -d --no-deps --force-recreate openwebui >/dev/null 2>&1; then
            print_substep "OWUI OpenAI connection + RAG keys reconciled for ${url} (${changed} row(s) rewritten; openwebui recreated)."
        else
            print_warning "OWUI OpenAI connection + RAG keys rewritten in the DB (${changed} row(s)), but the openwebui recreate FAILED — OWUI keeps its cached model list until it is restarted."
        fi
    else
        print_substep "OWUI OpenAI connection + RAG keys already consistent for ${url}."
    fi
    return 0
}

# ── #1266: OWUI's `model` rows on a box whose backend is the LLM Manager ─────
# A GPUStack box gets its rows from the `model-sync` container (one INSERT per
# backend model) and post-install then patches `meta` on them. A Manager box
# runs no model-sync: NOTHING wrote the rows, so `openwebui_db.model` stayed
# EMPTY after a clean install — the embedder, the reranker and granite-docling
# appeared in the user's chat picker (a row is what carries `meta.hidden` /
# `is_active`), the chat models carried no capabilities, and `core/llm/sync.py`
# had no row to put `params` on and said so as
# "ADD <name> (row missing — sync after OWUI auto-discovery)". Auto-discovery
# is a GPUStack-era event that never comes here.
#
# The SET comes from the manager, so a console-deployed model gets a row too;
# scripts/owui_model_rows.py owns the row shape, the classification and the
# idempotency (see its module docstring).

# The model ids the manager's metered /v1 surface serves ($1 = service key),
# one per line. Loopback published port, the same probe the #1250b verify uses.
_owui_manager_served_models() {
    local key="$1"
    [ -n "$key" ] || return 1
    curl -sf --max-time 15 -H "Authorization: Bearer $key" \
        "http://127.0.0.1:${LLM_MANAGER_PORT:-8091}/v1/models" 2>/dev/null | \
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for row in (data.get("data") or []) if isinstance(data, dict) else []:
    mid = (row or {}).get("id") if isinstance(row, dict) else None
    if mid:
        print(mid)
'
}

# #1442 rev-D (journey C on 0.79): the name of the reranker the manager SERVES
# for key $1, on stdout — empty when it serves none. rc 2 when the manager could
# not be asked (no key, no answer, nothing served at all): a caller keeps its
# current wiring on rc 2 and changes it only on a measured answer. Task from the
# admin surface where available, else the same name rule the federation and
# `_infer_task` use.
_owui_served_reranker() {
    local key="$1" served tasks
    served=$(_owui_manager_served_models "$key" 2>/dev/null) || return 2
    [ -n "$served" ] || return 2
    tasks=$(_owui_manager_model_tasks 2>/dev/null) || tasks=""
    SERVED="$served" TASKS="$tasks" python3 -c '
import os
served = [l.strip() for l in os.environ["SERVED"].splitlines() if l.strip()]
tasks = {}
for l in os.environ["TASKS"].splitlines():
    if "\t" in l:
        n, t = l.split("\t", 1)
        tasks[n.strip()] = t.strip().lower()
for n in served:
    if tasks.get(n) == "rerank" or (n not in tasks and "rerank" in n.lower()):
        print(n)
        break
'
    return 0
}

# "<model_name>\t<task>" per deployment the manager holds.
#
# The task lives ONLY on the admin surface (`GET /api/deployments`), which is
# ingress-anchored — `_llmm_admin_api` (cli/lib-llm-manager-deploy.sh) is the
# one implementation of that `docker exec caddy wget` call style, and
# cli/post-install.sh sources it. A caller that does not (cli/upgrade.sh) gets
# NO tasks and the manifest classifies alone, which is the normal case for the
# standard set anyway — so this degrades instead of failing.
_owui_manager_model_tasks() {
    declare -F _llmm_admin_api >/dev/null 2>&1 || return 0
    _llmm_admin_api GET /api/deployments 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for row in rows if isinstance(rows, list) else []:
    if not isinstance(row, dict):
        continue
    name = row.get("model_name")
    if name:
        print("%s\t%s" % (name, row.get("task") or ""))
'
}

# `--model ID=TASK` arguments for scripts/owui_model_rows.py, one per line, for
# every model the manager serves. Empty output = the manager served nothing (or
# could not be asked), which the callers treat as "not my turn".
#
# $1 = the service key to probe with. Optional: without it the key is RESOLVED
# (`_owui_llm_key`), which is what a provisioning run wants — but that resolver
# may mint and write .env, so a READ-ONLY caller (the --verify suite) passes the
# .env key explicitly instead of mutating the box from a verify.
_owui_manager_model_args() {
    local key ids tasks
    key="${1:-}"
    [ -n "$key" ] || key=$(_owui_llm_key) || key=""
    ids=$(_owui_manager_served_models "$key") || ids=""
    [ -n "$ids" ] || return 0
    tasks=$(_owui_manager_model_tasks) || tasks=""
    IDS="$ids" TASKS="$tasks" python3 -c '
import os
tasks = {}
for line in (os.environ.get("TASKS") or "").splitlines():
    name, _, task = line.partition("\t")
    if name.strip():
        tasks[name.strip()] = task.strip()
for mid in (os.environ.get("IDS") or "").splitlines():
    mid = mid.strip()
    if mid:
        print("%s=%s" % (mid, tasks.get(mid, "")))
'
}

# Write/reconcile the rows. Returns 0 when it OWNED the job (rows are now
# right), 1 when it could not — the caller then falls back to the GPUStack-era
# path rather than leaving the models unconfigured.
owui_reconcile_model_rows() {
    llm_manager_owns_standard_set || return 1   # #1441: rows belong to the backend that owns the models
    if ! _llm_manager_running; then
        print_warning "llm-manager profile is enabled but the container is not running — OWUI model rows not written (#1266). Re-run 'rzfz post-install --refresh' once it is up."
        return 1
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx postgres; then
        print_warning "postgres is not running — OWUI model rows not written (#1266)."
        return 1
    fi
    local args
    args=$(_owui_manager_model_args) || args=""
    if [ -z "$args" ]; then
        print_warning "The LLM Manager serves no model yet — OWUI model rows not written (#1266). Deploy the standard set ('rzfz post-install --preset standard') or from the LLM Manager console, then re-run 'rzfz post-install --refresh'."
        return 1
    fi
    local -a model_args=()
    local line
    while IFS= read -r line; do
        [ -n "$line" ] && model_args+=(--model "$line")
    done <<< "$args"
    local out rc=0
    out=$(python3 "${_OWUI_REPO_ROOT}/scripts/owui_model_rows.py" apply \
        "${model_args[@]}" \
        --manifest "${_OWUI_REPO_ROOT}/core/llm/standard-models.yaml" \
        --db "${OPENWEBUI_DB:-openwebui_db}" --pg-user "${POSTGRES_USER:-docker}" 2>&1) || rc=$?
    case "$rc" in
        0) ;;
        3) print_warning "OWUI model rows skipped: $(printf '%s\n' "$out" | head -n1) (#1266). Re-run 'rzfz post-install --refresh' once Open WebUI has been seeded."
           return 1 ;;
        *) print_warning "OWUI model rows FAILED (rc=${rc}): $(printf '%s\n' "$out" | head -n1) (#1266)."
           return 1 ;;
    esac
    local changed
    changed=$(printf '%s\n' "$out" | sed -n 's/^CHANGED=//p' | head -n1)
    changed="${changed:-0}"
    if [ "$changed" -gt 0 ] 2>/dev/null; then
        printf '%s\n' "$out" | sed -n 's/^ROW /  model: /p'
        print_success "Open WebUI model rows written from the LLM Manager's model list (${changed} row(s); embedding/reranker/doc-conversion models hidden from the chat picker)."
    else
        print_substep "Open WebUI model rows already consistent with the LLM Manager's model list."
    fi
    return 0
}

# #908: THE retrieval defaults, in ONE place.
#
# They used to be written down twice — once as `env_vars_needed` (the .env seed)
# and once as the python merge dict (the authoritative API push) — in two
# different vocabularies, because OWUI spells the env var `RAG_TOP_K` and the
# config field `TOP_K`. Two hand-maintained lists is one list that has not
# drifted yet, and this one already had: `ENABLE_RETRIEVAL_QUERY_GENERATION`
# was in the env seed and NOT in the API push. The env seed is a FIRST-BOOT-ONLY
# write (that is what PersistentConfig means), so on every box past its first
# start the LLM retrieval-query rewrite stayed ON while post-install reported
# success — and that rewrite is the one that mistranslates domain terms
# ("Tagsätze" -> bank "Tagesgeldsatz"), which is the retrieval failure #908 was
# filed against.
#
# Columns: <env-var name>|<retrieval-config field>|<value>|<json type>
#
# Only the STATIC defaults live here. The endpoint/key rows next to them
# (RAG_OPENAI_API_BASE_URL, RAG_EXTERNAL_RERANKER_URL, …) are resolved per box
# at run time (#976) and stay where they are — folding them in would mean
# either recomputing them here or making this table impure, and neither buys
# anything: they are not what silently reverts to an OWUI factory value.
# #1380 — column 5 is the OWUI endpoint that CARRIES the key. On OWUI 0.11
# `ENABLE_RETRIEVAL_QUERY_GENERATION` lives in the TASK config
# (`task.query.retrieval.enable`, GET/POST /api/v1/tasks/config[/update]); the
# retrieval endpoint ignores it on POST and never reports it, so pushing and
# reading it there warned "did NOT take" on EVERY run while the LLM query
# rewrite stayed ON (0.79, round 5). ENVKEY|APIKEY|VALUE|TYPE|ENDPOINT — the
# env seed reads columns 1+3, the push and the read-back route by column 5.
_owui_retrieval_defaults() {
    cat <<'RAGDEFAULTS'
ENABLE_RAG_HYBRID_SEARCH|ENABLE_RAG_HYBRID_SEARCH|true|bool|retrieval
HYBRID_BM25_WEIGHT|HYBRID_BM25_WEIGHT|0.5|float|retrieval
RAG_RERANKING_MODEL|RAG_RERANKING_MODEL|qwen3-reranker|str|retrieval
RAG_RERANKING_ENGINE|RAG_RERANKING_ENGINE|external|str|retrieval
ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER|ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER|false|bool|retrieval
CHUNK_SIZE|CHUNK_SIZE|5000|int|retrieval
CHUNK_OVERLAP|CHUNK_OVERLAP|500|int|retrieval
ENABLE_RETRIEVAL_QUERY_GENERATION|ENABLE_RETRIEVAL_QUERY_GENERATION|false|bool|tasks
RAG_TOP_K|TOP_K|10|int|retrieval
RAGDEFAULTS
}

# #908: read the retrieval config back and name every default that did NOT take.
#
# #258 added a read-back, but it judged ONE field (`RAG_RERANKING_MODEL`). The
# box in #908 was measured at OWUI's FACTORY values — top_k=3, chunk_size=1000,
# markdown splitter ON, no hybrid — which that verdict cannot see. "Never
# written" and "written and later replaced by an admin Save" are the same defect
# to a customer, so an absent field counts as a mismatch too.
#
# Never raises, never aborts: an unreachable OWUI must produce a diagnosis, not
# a dead `post-install --preset` (the #755/#793 shape, and the review find on
# #258 — under `set -eo pipefail` a bare `x=$(...)` inherits the substitution's
# exit status).
_owui_retrieval_mismatches() {
    local cfg tcfg
    cfg=$(owui_api GET "/api/v1/retrieval/config" 2>/dev/null) || cfg=""
    # #1380: the `tasks` rows are judged against the task config — the endpoint
    # that actually carries them (OWUI 0.11).
    tcfg=$(owui_api GET "/api/v1/tasks/config" 2>/dev/null) || tcfg=""
    # No shell-level "is it empty" branch: empty stdin and a 502 HTML page both
    # fail json.load, and the python below answers both with the same
    # "unreadable" string. A second guard for the same condition is a branch
    # nothing can drive, which is how untested code gets in.
    printf '%s' "$cfg" | RZFZ_TASKS_CFG="$tcfg" RZFZ_RAG_DEFAULTS="$(_owui_retrieval_defaults)" python3 -c '
import json, os, sys

def want(value, typ):
    if typ == "bool":
        return value == "true"
    if typ == "int":
        return int(value)
    if typ == "float":
        return float(value)
    return value

try:
    cfg = json.load(sys.stdin)
    assert isinstance(cfg, dict)
except Exception:
    sys.stdout.write("retrieval config unreadable")
    sys.exit(0)
try:
    tcfg = json.loads(os.environ.get("RZFZ_TASKS_CFG", ""))
    assert isinstance(tcfg, dict)
except Exception:
    tcfg = None

bad = []
for row in os.environ.get("RZFZ_RAG_DEFAULTS", "").splitlines():
    if not row.strip():
        continue
    parts = row.split("|")
    _env_key, api_key, value, typ = parts[:4]
    endpoint = parts[4] if len(parts) > 4 else "retrieval"
    expected = want(value, typ)
    if endpoint == "tasks":
        if tcfg is None:
            bad.append("%s=<task config unreadable> (expected %r)" % (api_key, expected))
            continue
        src = tcfg
    else:
        src = cfg
    if api_key not in src:
        bad.append("%s=<absent> (expected %r)" % (api_key, expected))
        continue
    got = src[api_key]
    # OWUI returns ints for float fields and vice versa; compare numerically
    # so 0.5 vs 0.50 is not reported as drift, but keep bool strict (True == 1
    # in python, and an ENABLE_* of 1 is not the same statement as true).
    if typ in ("int", "float") and not isinstance(got, bool):
        try:
            if float(got) == float(expected):
                continue
        except (TypeError, ValueError):
            pass
        bad.append("%s=%r (expected %r)" % (api_key, got, expected))
    elif got != expected:
        bad.append("%s=%r (expected %r)" % (api_key, got, expected))
sys.stdout.write("; ".join(bad))
' 2>/dev/null || printf 'retrieval config unreadable'
}

# ── #908: the authoritative retrieval push, ONE implementation ─────────────
# $1 = RAG completion/embedding base URL, $2 = its key, $3 = the FULL reranker
# endpoint (resolved per box by _owui_llm_base_url/_owui_llm_key/
# _owui_rerank_url). Pure API push (GET -> merge -> POST) of the declared
# defaults table + the resolved reranker, then a read-back that JUDGES the
# whole set (#258/#908). Needs an admin session (owui_ensure_admin). Never
# returns non-zero: an unreachable OWUI is a diagnosis, not a dead run.
owui_push_retrieval_defaults() {
    local _rag_url="$1" _rag_key="$2" _rerank_url="$3"
    # #1442 rev-D: wired ≠ served. On 0.79 the upgrade pointed OWUI at
    # http://llm:8080/v1/rerank while the manager served no reranker (GPUStack's
    # was in error and never federated) — every rerank call 404'd. Ask the
    # manager, with this consumer's key, which reranker it serves: none → no
    # reranker URL and no reranking model, said out loud; cannot ask (rc 2) →
    # the wiring stays as resolved.
    local _served_rerank _srr=0
    _served_rerank=$(_owui_served_reranker "$_rag_key") || _srr=$?
    if [ "$_srr" -eq 0 ] && [ -z "$_served_rerank" ] && [ -n "$_rerank_url" ]; then
        print_warning "  The LLM Manager serves NO reranker — retrieval is configured WITHOUT reranking (no dead ${_rerank_url}). Deploy one (e.g. qwen3-reranker) and re-run 'rzfz post-install --refresh' (#1442)."
        _rerank_url=""
    fi
    # AUTHORITATIVE: env vars are only read by OWUI's PersistentConfig on the FIRST
    # start, so on a re-run / already-booted stack they are silently ignored and the
    # document/web-search settings end up missing. Push the same settings via the
    # retrieval config API (GET -> merge -> POST) so they land in the DB regardless.
    print_substep "Applying retrieval (RAG) + web search via OWUI API (persists in DB; completion ${_rag_url}, reranker ${_rerank_url})..."
    local cfg merged
    cfg=$(owui_api GET "/api/v1/retrieval/config")
    # #976: this API push is AUTHORITATIVE (it overrides the env seed above), so
    # it must carry the resolved reranker endpoint + key too — else a re-run
    # would stamp the dead gpustack reranker straight back into OWUI's DB on a
    # manager-only box.
    merged=$(printf '%s' "$cfg" | RZFZ_RERANK_URL="$_rerank_url" RZFZ_RERANK_KEY="$_rag_key" \
        RZFZ_RAG_DEFAULTS="$(_owui_retrieval_defaults)" python3 -c "
import sys, json, os
rerank_url = os.environ.get('RZFZ_RERANK_URL', '')
key = os.environ.get('RZFZ_RERANK_KEY', '')
try:
    d = json.load(sys.stdin)
    assert isinstance(d, dict)
except Exception:
    sys.exit(1)
d.pop('status', None)
# #908: the static defaults come from the SAME table the .env seed used and the
# read-back verifies. They were a second hand-maintained copy here, in OWUI's
# other vocabulary (TOP_K vs RAG_TOP_K), and it had already drifted:
# ENABLE_RETRIEVAL_QUERY_GENERATION was seeded into .env and never pushed, so on
# every already-booted box the query rewrite stayed on.
_cast = {'bool': lambda v: v == 'true', 'int': int, 'float': float, 'str': str}
for _row in os.environ.get('RZFZ_RAG_DEFAULTS', '').splitlines():
    if not _row.strip():
        continue
    _p = _row.split('|')
    if len(_p) > 4 and _p[4] != 'retrieval':
        continue    # #1380: task-config rows go to their own endpoint
    _env_key, _api_key, _value, _typ = _p[:4]
    d[_api_key] = _cast[_typ](_value)
d.update({
    'RAG_EXTERNAL_RERANKER_URL': rerank_url,
    'RAG_EXTERNAL_RERANKER_API_KEY': key,
})
if not rerank_url:
    # #1442 rev-D: no reranker served — the static default (qwen3-reranker,
    # engine external) would make OWUI call a dead endpoint on every query.
    d['RAG_RERANKING_MODEL'] = ''
    d['RAG_RERANKING_ENGINE'] = ''
w = d.setdefault('web', {})
w.update({
    'ENABLE_WEB_SEARCH': True,
    'WEB_SEARCH_ENGINE': 'searxng',
    'SEARXNG_QUERY_URL': 'http://searxng:8080/search?q=<query>',
    'WEB_SEARCH_RESULT_COUNT': 5,
})
print(json.dumps(d))
" 2>/dev/null)
    if [ -z "$merged" ]; then
        print_warning "  Could not read OWUI retrieval config — left env-var seed only."
    else
        # #1380: ENABLE_RETRIEVAL_QUERY_GENERATION lives in OWUI 0.11's TASK
        # config (task.query.retrieval.enable). Same GET -> merge -> POST, against
        # the endpoint that carries the key; the retrieval POST below no longer
        # carries it (it was ignored there and read back as <absent> every run).
        local tcfg tmerged
        tcfg=$(owui_api GET "/api/v1/tasks/config" 2>/dev/null) || tcfg=""
        tmerged=$(printf '%s' "$tcfg" | RZFZ_RAG_DEFAULTS="$(_owui_retrieval_defaults)" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
    assert isinstance(d, dict)
except Exception:
    sys.exit(1)
_cast = {"bool": lambda v: v == "true", "int": int, "float": float, "str": str}
n = 0
for _row in os.environ.get("RZFZ_RAG_DEFAULTS", "").splitlines():
    _p = _row.split("|")
    if len(_p) < 5 or _p[4] != "tasks":
        continue
    d[_p[1]] = _cast[_p[3]](_p[2])
    n += 1
if n:
    print(json.dumps(d))
' 2>/dev/null) || tmerged=""
        if [ -n "$tmerged" ]; then
            owui_api POST "/api/v1/tasks/config/update" "$tmerged" >/dev/null 2>&1
        else
            print_warning "  Could not read OWUI task config — the query-generation default was not applied (#1380)."
        fi
        owui_api POST "/api/v1/retrieval/config/update" "$merged" >/dev/null 2>&1
        # #258: READ BACK and JUDGE, do not just print. The old form emitted the
        # values into a success line, so a POST that did not take looked exactly
        # like one that did — and the whole defect this reconcile answers is a
        # config that is silently absent. `RAG_RERANKING_MODEL` is the one field
        # that matters here: with hybrid search on and no reranker, web-search
        # retrieval cannot complete and OWUI falls back to answering directly,
        # with no sources and no error anywhere.
        #
        # #908: judge the whole DECLARED SET, not just that one field. The box
        # this issue was filed from sat at OWUI's FACTORY values — top_k=3,
        # chunk_size=1000, markdown splitter ON, no hybrid — with nothing
        # reporting it, because a healthy reranker was enough for the verdict
        # above to print success. Chunking is THE lever measured there (price
        # list rank #18/55 → #3 into top-k), so a silently-reverted CHUNK_SIZE
        # is not a cosmetic drift.
        local rerank web drifted
        rerank=$(owui_api GET "/api/v1/retrieval/config" | python3 -c \
            "import sys,json;d=json.load(sys.stdin);v=d.get('RAG_RERANKING_MODEL');print(v if v else '')" 2>/dev/null) || true
        web=$(owui_api GET "/api/v1/retrieval/config" | python3 -c \
            "import sys,json;d=json.load(sys.stdin);print(d.get('web',{}).get('ENABLE_WEB_SEARCH'))" 2>/dev/null) || true
        drifted=$(_owui_retrieval_mismatches) || drifted=""
        if [ -z "$rerank" ]; then
            print_warning "  RAG_RERANKING_MODEL is EMPTY after the update — web search will answer without sources."
            print_info    "  OWUI's /admin/settings/web Save does not round-trip this field and REPLACES the"
            print_info    "  retrieval config, so an admin save nulls it (#258). Re-run:"
            print_info    "    rzfz post-install --refresh"
        elif [ -n "$drifted" ]; then
            print_warning "  Retrieval defaults did NOT take: ${drifted}"
            print_info    "  RAG will still answer, with worse retrieval and no error anywhere: at OWUI's"
            print_info    "  factory chunking the facts are split away from the entity and never reach"
            print_info    "  top-k (#908). Same cause as above — an admin Save REPLACES the retrieval"
            print_info    "  config instead of merging it (#258). Re-run:"
            print_info    "    rzfz post-install --refresh"
        else
            print_success "Retrieval + web search applied via API (rerank=${rerank}, web=${web})."
        fi
    fi
    print_info "  Web search: SearXNG, Hybrid search: enabled, Reranker: qwen3-reranker"
    return 0
}

# Poll OWUI's /health for up to $1 seconds (5 s interval). Quiet: the callers
# print the verdict.
# #1401: register the Pipelines server as an Open WebUI connection.
#
# OWUI runs pipeline FILTERS (openlit_filter — the #245 chat attribution) only
# through a registered Pipelines server: `http://pipelines:9099` + its API key
# as an additional OpenAI-compatible connection. Nothing in the stack ever
# registered it — the compose seed and owui_configure_connection carry the
# inference backend only — so `GET /api/v1/pipelines/list` was `[]` on every
# box and the filter never ran (0.91, 2026-09-05: 0 spans; with the
# connection registered by hand: span with user.id/user.name in 10 s).
#
# Idempotent: reads /openai/config, appends the pair only when absent, POSTs
# the merged config back (the same authoritative POST owui_configure_connection
# uses), and reads /api/v1/pipelines/list back to judge. Never fatal.
owui_register_pipelines_connection() {
    local pkey cur merged listed
    pkey=$(read_env_value "${ENV_FILE:-.env}" PIPELINES_API_KEY 2>/dev/null) || pkey=""
    if [ -z "$pkey" ]; then
        print_warning "  PIPELINES_API_KEY is empty — Pipelines server NOT registered in Open WebUI (pipeline filters such as openlit_filter stay inert, #1401)."
        return 0
    fi
    cur=$(owui_api GET "/openai/config" 2>/dev/null) || cur=""
    merged=$(printf '%s' "$cur" | PIPELINES_KEY="$pkey" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin); assert isinstance(d, dict)
except Exception:
    sys.exit(1)
urls = list(d.get("OPENAI_API_BASE_URLS") or []); keys = list(d.get("OPENAI_API_KEYS") or [])
url = "http://pipelines:9099"
if url in urls:
    i = urls.index(url)
    while len(keys) < len(urls): keys.append("")
    if keys[i] == os.environ["PIPELINES_KEY"]:
        print("__present__"); sys.exit(0)
    keys[i] = os.environ["PIPELINES_KEY"]
else:
    while len(keys) < len(urls): keys.append("")
    urls.append(url); keys.append(os.environ["PIPELINES_KEY"])
print(json.dumps({"ENABLE_OPENAI_API": True, "OPENAI_API_BASE_URLS": urls, "OPENAI_API_KEYS": keys, "OPENAI_API_CONFIGS": d.get("OPENAI_API_CONFIGS") or {}}))
' 2>/dev/null) || merged=""
    if [ -z "$merged" ]; then
        print_warning "  Could not read Open WebUI connections (/openai/config) — Pipelines server not registered (#1401)."
        return 0
    fi
    if [ "$merged" = "__present__" ]; then
        print_substep "Pipelines server already registered in Open WebUI (http://pipelines:9099) — pipeline filters active."
        return 0
    fi
    owui_api POST "/openai/config/update" "$merged" >/dev/null 2>&1 || true
    listed=$(owui_api GET "/api/v1/pipelines/list" 2>/dev/null) || listed=""
    if printf '%s' "$listed" | grep -q 'pipelines:9099'; then
        print_success "Pipelines server registered in Open WebUI (http://pipelines:9099) — pipeline filters (openlit_filter) active."
    else
        print_warning "  Pipelines registration did NOT take: /api/v1/pipelines/list has no http://pipelines:9099 (#1401)."
    fi
}

_owui_wait_ready() {
    local max_wait="${1:-60}" waited=0
    while [ "$waited" -lt "$max_wait" ]; do
        if curl -sf --max-time 5 "http://127.0.0.1:${OPENWEBUI_PORT:-8080}/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    return 1
}

# #908 follow-up — re-assert the OWUI retrieval defaults on a box that is
# ALREADY provisioned (the post-upgrade step in cli/upgrade.sh). Every box
# whose last full post-install predates the defaults push kept the LLM
# query rewrite ON: `rzfz upgrade` only reached the push through `post-install
# --refresh`, a whole provisioning run that can die or skip before it gets to
# OWUI. This is the push alone: chat-profile gated, the same manager-down
# guard the authoritative writers use (#976), admin login, then
# owui_push_retrieval_defaults with the per-box resolved endpoints. No model
# deploy, no env seed. Returns 1 only when it could NOT run (so the caller
# can say so loudly); a drifted-but-unrepairable config is reported by the
# push's own verdict.
owui_reconcile_retrieval_defaults() {
    owui_lib_env_from_file
    case ",${COMPOSE_PROFILES:-}," in
        *,chat,*) ;;
        *) print_substep "chat profile not active — OWUI retrieval-defaults reconcile skipped."; return 0 ;;
    esac
    # #1445 (C5c / E1): the address this writes is http://llm:8080/v1 on
    # EVERY box, so the precondition follows the address, not ownership.
    # Measured on a dual, not-yet-federated box: owns_standard_set was
    # false, the guard did not fire, and a stopped manager got
    # {"url": "http://llm:8080/v1", "key": ""} stamped into OWUI's
    # persisted RAG config. Ownership decides which backend holds the
    # MODELS; it stopped deciding which address a consumer is given.
    if _llm_manager_profile_active && ! _llm_manager_running; then
        print_warning "llm-manager profile is enabled but the container is not running — SKIPPING the OWUI retrieval-defaults reconcile so RAG is not repointed at a backend this box does not run (#976). Re-run 'rzfz post-install --refresh' once llm-manager is up."
        return 1
    fi
    if ! _owui_wait_ready 60; then
        print_warning "Open WebUI is not reachable on 127.0.0.1:${OPENWEBUI_PORT:-8080} — retrieval-defaults reconcile skipped (#908). Re-run 'rzfz post-install --refresh' once chat is up."
        return 1
    fi
    if ! owui_ensure_admin >/dev/null 2>&1; then
        print_warning "Open WebUI admin login failed — retrieval-defaults reconcile skipped (#908). Re-run 'rzfz post-install --refresh'."
        return 1
    fi
    local _rag_url _rag_key _rerank_url
    _rag_url=$(_owui_llm_base_url)
    _rag_key=$(_owui_llm_key)
    _rerank_url=$(_owui_rerank_url)
    owui_push_retrieval_defaults "$_rag_url" "$_rag_key" "$_rerank_url"
    return 0
}

# ── Open WebUI admin API ─────────────────────────────────────────────────────
OWUI_TOKEN="${OWUI_TOKEN:-}"

owui_api() {
    local method="$1" path="$2" data="$3"
    local url="http://127.0.0.1:${OPENWEBUI_PORT:-8080}${path}"
    if [ -n "$data" ]; then
        curl -s -X "$method" "$url" \
            -H "Authorization: Bearer $OWUI_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data" 2>/dev/null
    else
        curl -s -X "$method" "$url" \
            -H "Authorization: Bearer $OWUI_TOKEN" 2>/dev/null
    fi
}

# #1165 — the Open WebUI admin password candidates, in priority order (the
# #1139 pattern, same defect class): `set-admin-password.sh openwebui` writes
# the rotated password to OPENWEBUI_ADMIN_PASSWORD and re-hashes the account,
# so that key is what this box CLAIMS the account holds; the fleet bootstrap
# password is only the pre-rotation fallback. Using the bootstrap password as
# the one and only source made a rotation self-destruct: every signin failed
# and the DB reset below quietly rewrote the account back to the FLEET
# password. Empty values are dropped, a duplicate is emitted once.
_owui_admin_passwords() {
    local primary="${OPENWEBUI_ADMIN_PASSWORD:-}"
    local fallback="${AUTHENTIK_BOOTSTRAP_PASSWORD:-}"
    [ -n "$primary" ] && printf '%s\n' "$primary"
    if [ -n "$fallback" ] && [ "$fallback" != "$primary" ]; then
        printf '%s\n' "$fallback"
    fi
    return 0
}

# owui_signin <url> <email> <password> — sets OWUI_TOKEN on success.
owui_signin() {
    # #1165 (#1164 class): body built by json.dumps, password on STDIN — a
    # rotated password with a quote or backslash no longer breaks the JSON,
    # and it never appears in curl's argv.
    local login_resp
    login_resp=$(printf '%s' "$3" | OWUI_EMAIL="$2" python3 -c '
import json, os, sys
print(json.dumps({"email": os.environ["OWUI_EMAIL"], "password": sys.stdin.read()}))' \
        | curl -s -X POST "$1/api/v1/auths/signin" \
        -H "Content-Type: application/json" -d @- 2>/dev/null)
    OWUI_TOKEN=$(echo "$login_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    [ -n "$OWUI_TOKEN" ]
}

owui_ensure_admin() {
    local owui_url="http://127.0.0.1:${OPENWEBUI_PORT:-8080}"
    local admin_email="razzfazz-ai-admin@${MAIN_DOMAIN}"
    local admin_name="razzfazz.ai Admin"
    # #1165: candidates in priority order; [0] is the password this box CLAIMS
    # the account has — it creates the account on a fresh box and the reset
    # below converges the account onto it.
    local -a admin_pw_candidates=()
    local _owui_pw_line
    while IFS= read -r _owui_pw_line; do
        [ -n "$_owui_pw_line" ] && admin_pw_candidates+=("$_owui_pw_line")
    done < <(_owui_admin_passwords)
    local admin_pass="${admin_pw_candidates[0]:-}"
    if [ -z "$admin_pass" ]; then
        print_error "Neither OPENWEBUI_ADMIN_PASSWORD nor AUTHENTIK_BOOTSTRAP_PASSWORD is set in .env — cannot provision the Open WebUI admin account."
        return 1
    fi
    # Try login first (user might already exist) — every candidate, in order.
    local _cand
    for _cand in "${admin_pw_candidates[@]}"; do
        if owui_signin "$owui_url" "$admin_email" "$_cand"; then
            print_success "Logged in as existing admin ($admin_email)."
            return 0
        fi
    done
    # Try signup (first user becomes admin)
    print_substep "Creating admin user ($admin_email)..."
    local signup_resp
    signup_resp=$(printf '%s' "$admin_pass" | OWUI_EMAIL="$admin_email" OWUI_NAME="$admin_name" python3 -c '
import json, os, sys
print(json.dumps({"email": os.environ["OWUI_EMAIL"], "password": sys.stdin.read(),
                  "name": os.environ["OWUI_NAME"]}))' \
        | curl -s -X POST "$owui_url/api/v1/auths/signup" \
        -H "Content-Type: application/json" -d @- 2>/dev/null)

    OWUI_TOKEN=$(echo "$signup_resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

    if [ -n "$OWUI_TOKEN" ]; then
        print_success "Admin user created."
        # rc6.7 #75: backfill `username` on the admin user record. OpenWebUI's
        # /api/v1/auths/signup payload accepts {email, password, name} — there
        # is no username field, so the column stays NULL after local-password
        # signup. Per-user agent pipes (M020 hermes/moltis/opencode/openhands/
        # paperclip) build their X-Authentik-Username from
        # `__user__["username"] or __user__["id"]` — when username is NULL,
        # they fall through to the OpenWebUI internal UUID, agent-manager
        # derives a different user_slug, and the find returns 404 so every
        # pipe greets the user with "You don't have a … agent yet" even
        # right after they provisioned one. OAUTH_USERNAME_CLAIM=preferred_username
        # IS set in env, but it only fires on OIDC login; admins seeded via
        # this signup flow never trigger it. Backstop here with a SQL update
        # mirroring the Authentik username (the local part of the admin
        # email is "razzfazz-ai-admin", but Authentik knows them by the
        # configured superuser name — so we use that explicit operator-facing
        # slug).
        #
        # #1148 review: this was the literal "akadmin", justified by a comment
        # that assumed the name is fixed — the very assumption #1148 lifts. On
        # a new box Authentik holds `rzfz-admin`, so seeding `akadmin` here
        # CREATES the mismatch the comment describes ("You don't have a … agent
        # yet"), because per-user resources are keyed on the slug.
        docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
            "UPDATE \"user\" SET username='${RAZZFAZZ_ADMIN_USERNAME:-akadmin}' WHERE email='${admin_email}' AND username IS NULL;" \
            > /dev/null 2>&1 || true
        return 0
    fi

    # Signup disabled (user exists but password wrong) — reset via DB
    print_substep "Resetting admin password via database..."

    # Find existing admin email (may differ from current domain)
    local existing_email
    existing_email=$(docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -t -c \
        "SELECT a.email FROM auth a JOIN \"user\" u ON a.id = u.id WHERE u.role = 'admin' LIMIT 1;" 2>/dev/null | xargs)

    if [ -z "$existing_email" ]; then
        print_error "No admin user found in database."
        return 1
    fi

    if [ "$existing_email" != "$admin_email" ]; then
        print_info "Found existing admin: $existing_email (updating to $admin_email)"
        # Update email to match current domain
        docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
            "UPDATE auth SET email='${admin_email}' WHERE email='${existing_email}';" > /dev/null 2>&1
        docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
            "UPDATE \"user\" SET email='${admin_email}' WHERE email='${existing_email}';" > /dev/null 2>&1
    fi

    # #1165 (#1164 class): the password goes in through the environment, never
    # into a Python literal — a rotated password with a quote broke the
    # program (or worse). Same transport cli/set-admin-password.sh uses.
    local hash
    hash=$(docker exec -e OPW="$admin_pass" openwebui python3 -c \
        "import bcrypt, os; print(bcrypt.hashpw(os.environ['OPW'].encode(), bcrypt.gensalt(12)).decode())" 2>/dev/null)

    if [ -z "$hash" ]; then
        print_error "Failed to generate password hash."
        return 1
    fi

    docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
        "UPDATE auth SET password='${hash}' WHERE email='${admin_email}';" > /dev/null 2>&1
    # rc6.7 #75: same username backfill on the password-reset path —
    # see the matching block in the signup branch above (#1148: configured
    # name, not the literal).
    docker exec postgres psql -U "${POSTGRES_USER:-docker}" -d "${OPENWEBUI_DB:-openwebui_db}" -c \
        "UPDATE \"user\" SET username='${RAZZFAZZ_ADMIN_USERNAME:-akadmin}' WHERE email='${admin_email}' AND username IS NULL;" \
        > /dev/null 2>&1 || true

    # Retry login — with the password the account was just converged onto.
    if owui_signin "$owui_url" "$admin_email" "$admin_pass"; then
        print_success "Admin password reset and logged in."
        return 0
    fi

    print_error "Failed to authenticate with Open WebUI."
    return 1
}
