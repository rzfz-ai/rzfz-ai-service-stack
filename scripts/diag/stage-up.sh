#!/bin/bash
# stage-up.sh — instrumented staged bring-up of razzfazz.ai stack on box-004.
#
# Each stage adds another tier of profiles, then PAUSES for the operator
# to test UIs in a browser before continuing. Designed to identify which
# stage triggers the panic_on_oom=2 reboot loop seen on 2026-05-03.
#
# This script does NOT touch .env. It overrides COMPOSE_PROFILES via env var
# only for the lifetime of each docker-compose call. Your .env's COMPOSE_FILE
# (compose.yml:modules/llm/compose.devices.amd.yml) is preserved.
#
# Usage (from ~/razzfazz-ai-service-stack):
#   ./stage-up.sh             # interactive, run all stages with pauses
#   ./stage-up.sh --stage 1   # run only stage 1
#   ./stage-up.sh --from 3    # start at stage 3
#   ./stage-up.sh --list      # show what each stage will bring up
#
# Pre-flight (operator does once):
#   sudo -v                                     # prime sudo
#   ./freeze-watch.sh > ~/freeze-watch/launcher.log 2>&1 &
#   ./stage-up.sh

set -u
STACK_DIR="${STACK_DIR:-$HOME/razzfazz-ai-service-stack}"
LOG="$HOME/freeze-watch/stage-transitions.log"
mkdir -p "$(dirname "$LOG")"

# ────────────────────────────────────────────────────────────────────
# Stages. Each stage's PROFILES is a CUMULATIVE set (adds onto previous).
# ────────────────────────────────────────────────────────────────────
declare -a STAGE_NAMES STAGE_PROFILES STAGE_DESCS
STAGE_NAMES[1]="core"
STAGE_PROFILES[1]=""
STAGE_DESCS[1]="Caddy, Postgres, Authentik, Valkey, SMTP, backup, setup, config, help, licenses"

STAGE_NAMES[2]="+llm"
STAGE_PROFILES[2]="llm"
STAGE_DESCS[2]="GPUStack v2.1.x + model-sync + ollama proxy (HARDWARE=amd, ROCm runner)"

STAGE_NAMES[3]="+chat,dify"
STAGE_PROFILES[3]="llm,chat,dify"
STAGE_DESCS[3]="Open WebUI + Pipelines, Dify api/worker/web/sandbox/plugin-daemon"

STAGE_NAMES[4]="+observability"
STAGE_PROFILES[4]="llm,chat,dify,observability"
STAGE_DESCS[4]="OpenLit + ClickHouse (recently edited config — likely freeze trigger)"

STAGE_NAMES[5]="+openhands"
STAGE_PROFILES[5]="llm,chat,dify,observability,openhands"
STAGE_DESCS[5]="OpenHands (host-network sandbox per rc6.7 #46; UFW 172.16/12 open ephemeral)"

STAGE_NAMES[6]="+rest"
STAGE_PROFILES[6]="agents,chat,coding-tools,cognee,crawl4ai,dify,docling,gitea,gotenberg,hermes,lightrag,llm,monitor,observability,openhands,presidio,searxng,stirling-pdf,stts,tika"
STAGE_DESCS[6]="Cognee, Crawl4AI, LightRAG, agents, gitea, monitor, searxng, gotenberg, stts, docling, presidio, stirling-pdf, tika"

usage() {
  cat <<EOF
Usage: stage-up.sh [--stage N | --from N] [--list] [--no-pause]
  --stage N       Run only stage N (1-6)
  --from N        Start at stage N, run through stage 6
  --list          Show stage definitions and exit
  --no-pause      Skip operator pause prompts (NOT recommended for diagnosis)

Stages:
EOF
  for i in "${!STAGE_NAMES[@]}"; do
    printf "  %d. %-15s — %s\n" "$i" "${STAGE_NAMES[$i]}" "${STAGE_DESCS[$i]}"
  done
}

LIST=0; ONLY=""; FROM=1; PAUSE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --stage) ONLY="$2"; shift 2 ;;
    --from)  FROM="$2"; shift 2 ;;
    --no-pause) PAUSE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1"; usage; exit 1 ;;
  esac
done

if (( LIST )); then usage; exit 0; fi

cd "$STACK_DIR" || { echo "ERROR: cannot cd to $STACK_DIR"; exit 1; }

log() {
  local msg="$*"
  printf '[%s] %s\n' "$(date -Is)" "$msg" | tee -a "$LOG"
  sync "$LOG" 2>/dev/null
}

snap_health() {
  local label="$1"
  log "=== health@${label} ==="
  log "  free -h:        $(free -h | awk '/^Speicher:|^Mem:/ {print $2,$3,$4,$7}')"
  log "  swap:           $(free -h | awk '/^Auslager:|^Swap:/ {print $2,$3,$4}')"
  log "  load1:          $(awk '{print $1}' /proc/loadavg)"
  log "  containers up:  $(docker ps --format '{{.Status}}' | grep -c '^Up')"
  log "  containers unh: $(docker ps --format '{{.Status}}' | grep -c 'unhealthy')"
  log "  PSI memory:     $(awk -F'[= ]' '/^some/ {print $3}' /proc/pressure/memory)"
  if [[ -d /var/lib/systemd/pstore ]] && sudo -n true 2>/dev/null; then
    local n_panics
    n_panics=$(sudo -n find /var/lib/systemd/pstore -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    log "  pstore captures: $n_panics"
  fi
}

# ────────────────────────────────────────────────────────────────────
# Disarm: prevent gpustack from auto-redeploying qwen3-coder-next on Stage 2.
# Run between Stage 1 (postgres up) and Stage 2 (gpustack starts). Sets
# replicas=0 in gpustack_db.models so the scheduler skips placement.
# Reversible: in the GPUStack UI, set replicas back to 1 when ready to
# deliberately reproduce the bug under instrumented conditions.
# ────────────────────────────────────────────────────────────────────
disarm_qwen3_coder() {
  local out_before="$HOME/freeze-watch/gpustack-state-before-disarm.txt"
  local out_after="$HOME/freeze-watch/gpustack-state-after-disarm.txt"

  log ""
  log "════════════════════════════════════════════════════════════════"
  log "DISARM: prevent qwen3-coder-next auto-redeploy on Stage 2"
  log "════════════════════════════════════════════════════════════════"

  # Postgres must be up from Stage 1.
  if ! docker compose ps postgres --format '{{.Status}}' 2>/dev/null | grep -qi 'up\|running'; then
    log "✗ postgres is not running — Stage 1 must be up first. skipping disarm."
    return 1
  fi

  log "→ dumping current gpustack_db state to $out_before"
  docker compose exec -T postgres psql -U docker -d gpustack_db -P pager=off <<'SQL' >"$out_before" 2>&1
\echo === \dt
\dt
\echo === models (running/desired) ===
SELECT id, name, replicas, ready_replicas, source, backend,
       huggingface_repo_id, huggingface_filename, model_scope_file_path,
       created_at
  FROM models
 ORDER BY created_at DESC;
\echo === model_instances (active) ===
SELECT id, model_id, model_name, state, worker_id, created_at, updated_at
  FROM model_instances
 WHERE state NOT IN ('error','stopped','deleted','inactive')
 ORDER BY updated_at DESC;
SQL
  log "  before-state saved (showing first 30 lines):"
  head -30 "$out_before" | sed 's/^/      /' | tee -a "$LOG" >/dev/null

  if (( PAUSE )); then
    cat <<'EOF'

╔══════════════════════════════════════════════════════════════════════╗
║  Inspect ~/freeze-watch/gpustack-state-before-disarm.txt
║
║  Identify the model row(s) that should be disarmed (replicas=0).
║  Suspected culprits: qwen3-coder-next or any model whose deployment
║  was hanging in "estimating" before the freeze.
║
║  Choose ONE option below and respond at the prompt:
║    1)  Auto-disarm by name match: name ILIKE '%qwen3-coder-next%'
║        (also matches qwen3_coder_next, Qwen3-Coder-Next-... etc.)
║    2)  Auto-disarm by name match: name ILIKE '%qwen3-coder%' (broader)
║    3)  Custom: open a psql shell, you run the SQL by hand, then continue
║    4)  Skip disarm (NOT RECOMMENDED — Stage 2 will likely re-trigger OOM)
║    q)  Abort stage-up.sh
╚══════════════════════════════════════════════════════════════════════╝

Choice [1/2/3/4/q]:
EOF
    local choice
    read -r choice
    case "${choice:-q}" in
      1) _disarm_sql "name ILIKE '%qwen3-coder-next%'" ;;
      2) _disarm_sql "name ILIKE '%qwen3-coder%'" ;;
      3) log "→ opening psql shell for manual disarm — exit \\q to continue"
         docker compose exec postgres psql -U docker -d gpustack_db
         ;;
      4) log "⚠ disarm SKIPPED by operator choice"; ;;
      *) log "✗ aborting"; exit 1 ;;
    esac
  else
    log "→ PAUSE=0: auto-disarming default match name ILIKE '%qwen3-coder-next%'"
    _disarm_sql "name ILIKE '%qwen3-coder-next%'"
  fi

  log "→ dumping post-disarm state to $out_after"
  docker compose exec -T postgres psql -U docker -d gpustack_db -P pager=off <<'SQL' >"$out_after" 2>&1
\echo === models (post-disarm) ===
SELECT id, name, replicas, ready_replicas, backend, source FROM models ORDER BY created_at DESC;
\echo === models with replicas=0 (will be skipped by scheduler) ===
SELECT id, name, replicas, backend FROM models WHERE replicas=0;
SQL
  log "  after-state saved (showing first 25 lines):"
  head -25 "$out_after" | sed 's/^/      /' | tee -a "$LOG" >/dev/null
  log "→ disarm complete; safe to proceed to Stage 2"
}

_disarm_sql() {
  local where="$1"
  log "→ executing: UPDATE models SET replicas=0 WHERE $where"
  docker compose exec -T postgres psql -U docker -d gpustack_db -P pager=off <<SQL 2>&1 | tee -a "$LOG"
SELECT id, name, replicas, backend FROM models WHERE $where;
UPDATE models SET replicas=0 WHERE $where RETURNING id, name, replicas;
SQL
}

# ────────────────────────────────────────────────────────────────────
# Stage-2 watchdog: every 30s while gpustack is up, capture diagnostic
# state if the master process RSS climbs past a threshold. This produces
# the proof for the HF retry / asyncio.to_thread leak hypothesis.
# Runs in background, killed when stage-up.sh exits.
# ────────────────────────────────────────────────────────────────────
gpustack_diag_watchdog() {
  local threshold_mb="${GPUSTACK_DIAG_THRESHOLD_MB:-3000}"
  local outdir="$HOME/freeze-watch/gpustack-diag"
  mkdir -p "$outdir"
  log "→ gpustack-diag watchdog started (RSS threshold ${threshold_mb} MiB → dump diagnostics)"
  (
    while true; do
      sleep 30
      local pid rss_kb rss_mb
      pid=$(docker exec gpustack pidof -s gpustack 2>/dev/null) || { sleep 10; continue; }
      [[ -z "$pid" ]] && continue
      rss_kb=$(docker exec gpustack awk '/^VmRSS:/ {print $2}' /proc/"$pid"/status 2>/dev/null) || continue
      rss_mb=$(( rss_kb / 1024 ))
      printf '%s\tgpustack_pid=%s\trss_mb=%s\n' "$(date -Is)" "$pid" "$rss_mb" >> "$outdir/rss-watch.tsv"
      if (( rss_mb > threshold_mb )); then
        local ts
        ts=$(date +%Y%m%dT%H%M%S)
        log "⚠ gpustack RSS = ${rss_mb} MiB > ${threshold_mb} MiB threshold — capturing diagnostics under $outdir/$ts/"
        mkdir -p "$outdir/$ts"
        docker exec gpustack cat /proc/"$pid"/status     > "$outdir/$ts/status.txt" 2>&1
        docker exec gpustack cat /proc/"$pid"/maps        > "$outdir/$ts/maps.txt" 2>&1
        docker exec gpustack cat /proc/"$pid"/smaps_rollup > "$outdir/$ts/smaps_rollup.txt" 2>&1
        docker exec gpustack ls -la /proc/"$pid"/fd       > "$outdir/$ts/fd.txt" 2>&1
        docker exec gpustack pmap -x "$pid"               > "$outdir/$ts/pmap.txt" 2>&1
        # py-spy may not be installed in the upstream image; install on the fly
        # and capture both a one-shot dump and a 10s sample.
        docker exec gpustack sh -c 'pip install --quiet py-spy 2>/dev/null || true' \
          > "$outdir/$ts/pyspy-install.log" 2>&1
        docker exec gpustack py-spy dump --pid "$pid"     > "$outdir/$ts/pyspy-dump.txt" 2>&1
        docker exec gpustack timeout 12 py-spy record --pid "$pid" --rate 50 --duration 10 \
          --output - --format speedscope > "$outdir/$ts/pyspy.speedscope" 2>"$outdir/$ts/pyspy-record.err"
        sync "$outdir/$ts/" 2>/dev/null
        log "  diagnostics captured to $outdir/$ts/ — rss=${rss_mb} MiB"
        # only capture once per stage to avoid spamming and inflating gpustack via py-spy attaches
        return 0
      fi
    done
  ) &
  GPUSTACK_DIAG_PID=$!
  trap 'kill ${GPUSTACK_DIAG_PID:-0} 2>/dev/null; exit' EXIT INT TERM
}

operator_pause() {
  local stage_n="$1" stage_name="$2"
  if (( ! PAUSE )); then return; fi
  cat <<EOF

╔══════════════════════════════════════════════════════════════════════╗
║  STAGE $stage_n ($stage_name) is up. Operator action required.
╠══════════════════════════════════════════════════════════════════════╣
║  1. Open the relevant UIs in your browser. Verify they load and that
║     basic operations work. Suggested checks per stage are below.
║  2. Watch ~/freeze-watch/timeline.tsv for memory/PSI trends.
║  3. If anything goes wrong (UI 502, container restart loop, memory
║     climbing fast), HIT CTRL-C now.
║  4. Otherwise press ENTER to continue to the next stage.
╠══════════════════════════════════════════════════════════════════════╣
EOF
  case "$stage_n" in
    1) cat <<'X'
║  Test (stage 1 — core):
║    https://config.<DOMAIN>          (Configuration portal — main control)
║    https://auth.<DOMAIN>            (Authentik admin login)
║    https://setup.<DOMAIN>           (Setup wizard)
║    https://help.<DOMAIN>            (Documentation)
║    https://backup.<DOMAIN>          (Backup management)
X
       ;;
    2) cat <<'X'
║  Test (stage 2 — +llm):
║    https://llm.<DOMAIN>             (GPUStack — confirm models load)
║    Verify ROCm runner spawns when you click "Deploy" on a model
║    Watch: docker stats gpustack — should sit under 1-2 GiB until inference
X
       ;;
    3) cat <<'X'
║  Test (stage 3 — +chat,dify):
║    https://chat.<DOMAIN>            (Open WebUI — log in, send a message)
║    https://dify.<DOMAIN>            (Dify — open a workflow)
║    Run one chat completion. Watch memory in timeline.tsv during inference.
X
       ;;
    4) cat <<'X'
║  Test (stage 4 — +observability):  ⚠ recently-edited ClickHouse config
║    OpenLit UI (admin port) — verify it renders the dashboard
║    Watch for ClickHouse memory: it preallocates large arena.
║    THIS STAGE is a prime suspect for the freeze. Watch PSI carefully.
X
       ;;
    5) cat <<'X'
║  Test (stage 5 — +openhands):  ⚠ host-network sandbox + UFW changes
║    https://openhands.<DOMAIN>       (start an agent session)
║    Click "Open Workspace" — that spawns the runtime sandbox container
║    on host network mode. Confirm it doesn't UFW-block itself.
║    Watch dmesg for nf_conntrack/ufw lines: tail -f /var/log/ufw.log
X
       ;;
    6) cat <<'X'
║  Test (stage 6 — +rest):  the kitchen sink — Cognee, Crawl4AI, LightRAG,
║  agents, gitea, monitor, searxng, gotenberg, stts, docling, presidio,
║  stirling-pdf, tika. ~30+ containers added. Memory will climb.
║  If panic_on_oom=2 fires, this is where it'll go.
X
       ;;
  esac
  cat <<'EOF'
╚══════════════════════════════════════════════════════════════════════╝

Press ENTER to continue to the next stage (or Ctrl-C to stop)... 
EOF
  read -r _
}

run_stage() {
  local n="$1"
  local name="${STAGE_NAMES[$n]}"
  local profiles="${STAGE_PROFILES[$n]}"
  local desc="${STAGE_DESCS[$n]}"

  log ""
  log "════════════════════════════════════════════════════════════════"
  log "STAGE $n: $name"
  log "  profiles: ${profiles:-<none — core only>}"
  log "  desc:     $desc"
  log "════════════════════════════════════════════════════════════════"

  snap_health "before-stage-$n"

  log "→ COMPOSE_PROFILES='$profiles' docker compose up -d"
  if COMPOSE_PROFILES="$profiles" docker compose up -d 2>&1 | tee -a "$LOG"; then
    log "→ docker compose up returned 0"
  else
    log "✗ docker compose up FAILED for stage $n"
    snap_health "fail-stage-$n"
    return 1
  fi

  log "→ sleeping 30s for containers to stabilise"
  sleep 30

  snap_health "after-stage-$n"

  operator_pause "$n" "$name"
}

# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────
log "stage-up.sh starting; FROM=$FROM ONLY=$ONLY PAUSE=$PAUSE"
log "stack dir: $STACK_DIR"
log "current .env COMPOSE_PROFILES: $(grep -E '^COMPOSE_PROFILES=' "$STACK_DIR/.env" | head -1)"
log "current .env COMPOSE_FILE:     $(grep -E '^COMPOSE_FILE=' "$STACK_DIR/.env" | head -1)"
log "current .env HARDWARE:         $(grep -E '^HARDWARE=' "$STACK_DIR/.env" | head -1)"

snap_health "baseline"

if [[ -n "$ONLY" ]]; then
  # Targeted stage. If running Stage 2, run disarm + watchdog around it.
  if [[ "$ONLY" == "2" ]]; then
    disarm_qwen3_coder
    gpustack_diag_watchdog
  fi
  run_stage "$ONLY"
else
  for n in 1 2 3 4 5 6; do
    (( n < FROM )) && continue
    run_stage "$n" || { log "✗ aborting at stage $n"; exit 1; }
    # Disarm + diag-watchdog between Stage 1 and Stage 2.
    if (( n == 1 )); then
      disarm_qwen3_coder
      gpustack_diag_watchdog
    fi
  done
fi

log "stage-up.sh finished."
