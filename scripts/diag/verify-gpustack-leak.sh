#!/bin/bash
# verify-gpustack-leak.sh — measure-then-prove the qwen3-coder-next deploy bug.
#
# Tracks gpustack master process RSS + thread count + load-avg every 5 s.
# Auto-stops gpustack at a safe RSS threshold to prevent OOM-panic on this
# 32 GiB Strix Halo box. Designed to be run TWICE: once to reproduce the
# bug (expect RSS + threads to climb), then again after applying the
# Patch 2 fix (expect both to stay flat).
#
# Usage:
#   ./verify-gpustack-leak.sh                        # run with defaults
#   PHASE=before ./verify-gpustack-leak.sh            # pre-fix run; tags output
#   PHASE=after  ./verify-gpustack-leak.sh            # post-fix run; tags output
#   STOP_RSS_MB=12000 ./verify-gpustack-leak.sh       # custom auto-stop
#   PLATEAU_SECS=120 ./verify-gpustack-leak.sh        # how long flat = "fixed"
#   MAX_RUNTIME_SECS=600 ./verify-gpustack-leak.sh    # safety upper bound
#
# Pre-conditions:
#   - Stage 1 + Stage 2 of stage-up.sh have run; gpustack container is up
#   - qwen3-coder-next is at replicas=1 (i.e. disarm was SKIPPED, or the
#     operator deliberately re-armed it via the GPUStack UI)
#
# Output:
#   ~/freeze-watch/verify/<PHASE>-<ts>/
#     metrics.tsv       second-by-second RSS + threads + load
#     pyspy-N.txt       py-spy dumps every TRIGGER_RSS_MB bump
#     summary.txt       final verdict + key data points

set -u
PHASE="${PHASE:-run}"
STOP_RSS_MB="${STOP_RSS_MB:-15000}"        # auto-stop at this RSS (MiB)
TRIGGER_RSS_MB="${TRIGGER_RSS_MB:-3000}"   # capture py-spy dump above this
PLATEAU_SECS="${PLATEAU_SECS:-90}"         # if RSS hasn't moved this long → exit "stable"
MAX_RUNTIME_SECS="${MAX_RUNTIME_SECS:-900}"
INTERVAL=5

ts=$(date +%Y%m%dT%H%M%S)
OUTDIR="$HOME/freeze-watch/verify/${PHASE}-${ts}"
mkdir -p "$OUTDIR"
METRICS="$OUTDIR/metrics.tsv"
SUMMARY="$OUTDIR/summary.txt"

# Header
printf 'ts_iso\telapsed_s\tgpustack_rss_mb\tgpustack_threads\thost_load1\thost_mem_avail_mb\thf_open_conns\n' > "$METRICS"

log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
  printf '[%s] %s\n' "$(date -Is)" "$*" >> "$OUTDIR/run.log"
}

# Find gpustack PID inside the container
gpustack_pid() {
  docker exec gpustack pgrep -f "gpustack start" 2>/dev/null | head -1
}

# Read RSS in MiB from /proc/PID/status
rss_mb() {
  local pid="$1"
  local kb
  kb=$(docker exec gpustack awk '/^VmRSS:/ {print $2}' /proc/"$pid"/status 2>/dev/null) || return 1
  [[ -z "$kb" ]] && return 1
  echo $((kb / 1024))
}

# Read thread count from /proc/PID/status
threads() {
  local pid="$1"
  docker exec gpustack awk '/^Threads:/ {print $2}' /proc/"$pid"/status 2>/dev/null
}

# Count open HF API connections (TCP to huggingface.co or *.hf.co)
hf_conns() {
  local pid="$1"
  # ESTABLISHED connections owned by this PID, count rows
  docker exec gpustack sh -c "ss -tnp 2>/dev/null | grep ESTAB | grep -E ':443.*pid=$pid,' | wc -l" 2>/dev/null || echo 0
}

capture_diag() {
  local rss_mb="$1"
  local label="$2"
  local pid out
  pid=$(gpustack_pid) || return
  out="$OUTDIR/diag-rss${rss_mb}mb-${label}"
  log "📸 capturing diag at RSS=${rss_mb} MiB → $out.*"
  docker exec gpustack cat /proc/"$pid"/status        > "$out.status.txt" 2>&1
  docker exec gpustack cat /proc/"$pid"/maps          > "$out.maps.txt"   2>&1
  docker exec gpustack cat /proc/"$pid"/smaps_rollup  > "$out.smaps.txt"  2>&1
  docker exec gpustack ls -la /proc/"$pid"/fd         > "$out.fd.txt"     2>&1
  docker exec gpustack pmap -x "$pid"                 > "$out.pmap.txt"   2>&1
  docker exec gpustack sh -c 'pip install --quiet py-spy 2>/dev/null || true' \
    > "$out.pyspy-install.log" 2>&1
  docker exec gpustack py-spy dump --pid "$pid"       > "$out.pyspy-dump.txt" 2>&1
  # Top per-thread frame summary (which library are most threads parked in?)
  awk '/^Thread/ {tid=$0; next} /^[ \t]+[a-zA-Z_]/ && top=="" {top=$0; print tid": "top; tid=""; top=""}' \
    "$out.pyspy-dump.txt" \
    | sort | uniq -c | sort -nr | head -20 > "$out.pyspy-thread-summary.txt"
  sync "$out".* 2>/dev/null
}

stop_gpustack() {
  local reason="$1"
  log "🛑 STOPPING gpustack: $reason"
  docker compose stop gpustack 2>&1 | tee -a "$OUTDIR/run.log" || true
  docker compose stop gpustack 2>&1 >> "$OUTDIR/run.log"
}

trap 'log "interrupted"; exit 130' INT TERM

# ─── pre-flight ──────────────────────────────────────────────────────────
log "verify-gpustack-leak.sh: PHASE=$PHASE  STOP_RSS_MB=$STOP_RSS_MB  PLATEAU_SECS=$PLATEAU_SECS"
log "output dir: $OUTDIR"

if ! docker compose ps gpustack --format '{{.Status}}' 2>/dev/null | grep -qi 'up\|running'; then
  log "✗ gpustack container is not running. Bring it up via stage-up.sh --stage 2 first."
  exit 1
fi

# Confirm qwen3-coder-next is at replicas=1 (so the bug WILL fire)
log "checking qwen3-coder-next replicas in gpustack_db…"
status_q=$(docker compose exec -T postgres psql -U docker -d gpustack_db -At -c \
  "SELECT name, replicas FROM models WHERE name ILIKE '%qwen3-coder%' OR name ILIKE '%coder-next%';" 2>/dev/null)
if [[ -z "$status_q" ]]; then
  log "⚠ no qwen3-coder model found in gpustack_db.models. Bug will not fire — exiting."
  echo "no qwen3-coder model present" > "$SUMMARY"
  exit 2
fi
log "qwen3-coder model state:"
echo "$status_q" | sed 's/^/    /' | tee -a "$OUTDIR/run.log"

if echo "$status_q" | awk -F'|' '{print $2}' | grep -qx '0'; then
  log "⚠ qwen3-coder is at replicas=0 (disarmed). Bug will NOT fire."
  log "  → set replicas=1 in GPUStack UI, or run:"
  log "    docker compose exec postgres psql -U docker -d gpustack_db -c \"UPDATE models SET replicas=1 WHERE name ILIKE '%qwen3-coder%'\""
  echo "qwen3-coder disarmed; re-arm before re-running" > "$SUMMARY"
  exit 2
fi

# ─── main loop ───────────────────────────────────────────────────────────
start=$(date +%s)
peak_rss=0
peak_threads=0
plateau_since=$start
plateau_rss=0
captured_at=()

log "starting watch (interval=${INTERVAL}s) — Ctrl-C to stop early"

while true; do
  now=$(date +%s)
  elapsed=$((now - start))
  iso=$(date -Is)

  pid=$(gpustack_pid) || pid=""
  if [[ -z "$pid" ]]; then
    log "✗ gpustack PID not found — container exited or restarted. Aborting."
    echo "gpustack PID disappeared at +${elapsed}s" >> "$SUMMARY"
    break
  fi

  cur_rss=$(rss_mb "$pid" 2>/dev/null) || cur_rss=0
  cur_threads=$(threads "$pid" 2>/dev/null) || cur_threads=0
  cur_load=$(awk '{print $1}' /proc/loadavg)
  cur_avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
  cur_hf=$(hf_conns "$pid" 2>/dev/null)
  [[ -z "$cur_hf" ]] && cur_hf=0

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$iso" "$elapsed" "$cur_rss" "$cur_threads" "$cur_load" "$cur_avail" "$cur_hf" >> "$METRICS"
  sync "$METRICS" 2>/dev/null

  # Status line: stays on screen for the operator
  printf '\r[+%4ds] RSS=%6d MiB  threads=%4d  load=%-7s  avail=%6d MiB  hf-conns=%-3s  ' \
    "$elapsed" "$cur_rss" "$cur_threads" "$cur_load" "$cur_avail" "$cur_hf"

  # Track peaks
  (( cur_rss    > peak_rss     )) && peak_rss=$cur_rss
  (( cur_threads > peak_threads )) && peak_threads=$cur_threads

  # Capture diagnostics at threshold (only on first crossing of each 1 GiB band > TRIGGER)
  if (( cur_rss >= TRIGGER_RSS_MB )); then
    band=$(( cur_rss / 1000 ))
    already=0
    for c in "${captured_at[@]}"; do
      [[ "$c" == "$band" ]] && already=1 && break
    done
    if (( ! already )); then
      echo
      capture_diag "$cur_rss" "band${band}gb"
      captured_at+=("$band")
    fi
  fi

  # Auto-stop guard
  if (( cur_rss >= STOP_RSS_MB )); then
    echo
    capture_diag "$cur_rss" "final-prestop"
    stop_gpustack "RSS=${cur_rss} MiB ≥ STOP_RSS_MB=${STOP_RSS_MB}"
    echo "verdict: BUG REPRODUCED — gpustack RSS reached ${cur_rss} MiB, threads ${cur_threads}, load ${cur_load}" > "$SUMMARY"
    break
  fi

  # Plateau detection (RSS hasn't moved >50 MiB in PLATEAU_SECS)
  if (( cur_rss > plateau_rss + 50 )); then
    plateau_rss=$cur_rss
    plateau_since=$now
  fi
  if (( now - plateau_since >= PLATEAU_SECS )); then
    echo
    log "✓ RSS plateau: stayed within 50 MiB for ${PLATEAU_SECS}s — bug appears NOT to be firing"
    capture_diag "$cur_rss" "stable-plateau"
    echo "verdict: STABLE — RSS plateau at ${cur_rss} MiB, threads ${cur_threads}, load ${cur_load} for ${PLATEAU_SECS}s" > "$SUMMARY"
    break
  fi

  # Hard time limit
  if (( elapsed >= MAX_RUNTIME_SECS )); then
    echo
    log "⏱ MAX_RUNTIME_SECS=${MAX_RUNTIME_SECS} reached — stopping"
    capture_diag "$cur_rss" "timeout"
    echo "verdict: TIMEOUT — peak RSS ${peak_rss} MiB, peak threads ${peak_threads}" > "$SUMMARY"
    break
  fi

  sleep "$INTERVAL"
done

# ─── summary ─────────────────────────────────────────────────────────────
echo
{
  echo "phase: $PHASE"
  echo "start_iso: $(date -Is -d @$start)"
  echo "end_iso: $(date -Is)"
  echo "duration_s: $((now - start))"
  echo "peak_rss_mb: $peak_rss"
  echo "peak_threads: $peak_threads"
  echo "diag_captures: ${#captured_at[@]} (at GiB bands: ${captured_at[*]})"
  echo
  echo "── metrics tail (last 10 rows) ──"
  tail -10 "$METRICS"
  echo
  echo "── verdict ──"
  cat "$SUMMARY"
} | tee "$OUTDIR/final-summary.txt"
log "all output: $OUTDIR"
