#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# llm-loadtest.sh — sustained-load test for the OpenAI-compat endpoint
# ==============================================================================
# Submits chat-completions requests with 1-3 randomly-attached images and a
# prompt-cache-defeating prompt variation. Runs for DURATION_SEC with
# CONCURRENCY parallel workers, then prints a stability + performance report.
#
# ENV / config:
#   LLM_API_KEY      (required)   Bearer token for the endpoint
#   ENDPOINT         default      https://llm.razzfazz.ai/v1-openai
#   IMAGE_DIR        default      ~/llm-loadtest-images   (drop *.png/jpg here)
#   MODEL            default      both                    (qwen3.6 | gemma4 | both)
#   DURATION_SEC     default      3600                    (≥ 1 h)
#   CONCURRENCY      default      2                       (parallel workers)
#   PER_REQ_TIMEOUT  default      300                     (curl --max-time)
#   MAX_TOKENS       default      (unset)                 OpenAI max_tokens cap
#   OUT_DIR          default      /tmp/llm-loadtest-<UTC-stamp>/
#
# Usage:
#   LLM_API_KEY=gpustack_... ./scripts/llm-loadtest.sh
#   LLM_API_KEY=gpustack_... MODEL=qwen3.6  ./scripts/llm-loadtest.sh
#   LLM_API_KEY=gpustack_... MODEL=gemma4   DURATION_SEC=600 ./scripts/llm-loadtest.sh
#
# Output:
#   $OUT_DIR/requests.csv     one row per request
#   $OUT_DIR/worker-N.log     per-worker stderr
#   $OUT_DIR/report.md        printed to stdout AND saved at end
# ==============================================================================
set -uo pipefail

: "${LLM_API_KEY:?LLM_API_KEY env var is required}"

ENDPOINT="${ENDPOINT:-https://llm.razzfazz.ai/v1-openai}"
IMAGE_DIR="${IMAGE_DIR:-$HOME/llm-loadtest-images}"
MODEL="${MODEL:-both}"
DURATION_SEC="${DURATION_SEC:-3600}"
CONCURRENCY="${CONCURRENCY:-2}"
PER_REQ_TIMEOUT="${PER_REQ_TIMEOUT:-300}"
OUT_DIR="${OUT_DIR:-/tmp/llm-loadtest-$(date -u +%Y%m%dT%H%M%SZ)}"

case "$MODEL" in
    qwen3.6|gemma4|both) ;;
    *) echo "ERROR: MODEL must be qwen3.6 | gemma4 | both (got: $MODEL)" >&2; exit 2 ;;
esac

# --- pre-flight ---------------------------------------------------------- #
command -v jq >/dev/null    || { echo "ERROR: jq required" >&2; exit 2; }
command -v curl >/dev/null  || { echo "ERROR: curl required" >&2; exit 2; }
command -v base64 >/dev/null || { echo "ERROR: base64 required" >&2; exit 2; }

if [ ! -d "$IMAGE_DIR" ]; then
    echo "ERROR: IMAGE_DIR not found: $IMAGE_DIR" >&2
    echo "  Drop 1+ jpg/png test images there, then re-run." >&2
    exit 2
fi

mapfile -t IMAGES < <(find "$IMAGE_DIR" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) | sort)
if [ "${#IMAGES[@]}" -lt 1 ]; then
    echo "ERROR: no jpg/png/webp images found in $IMAGE_DIR" >&2
    exit 2
fi

mkdir -p "$OUT_DIR"
LOG_CSV="$OUT_DIR/requests.csv"
echo "ts_iso,worker,seq,model,n_images,image_names,latency_ms,http_code,resp_bytes,prompt_tokens,completion_tokens,error" > "$LOG_CSV"

# --- pre-encode all images to base64 (once) ------------------------------ #
declare -A IMG_DATAURL_FILE
DATAURL_DIR="$OUT_DIR/dataurls"
mkdir -p "$DATAURL_DIR"
for img in "${IMAGES[@]}"; do
    case "${img,,}" in
        *.jpg|*.jpeg) mime=image/jpeg ;;
        *.png)        mime=image/png ;;
        *.webp)       mime=image/webp ;;
        *)            continue ;;
    esac
    # Write each image's data:URL to its own file. Avoids ARG_MAX on jq --arg
    # when we later assemble request bodies — large base64 blobs are passed
    # via jq --rawfile (file input) rather than --arg (argv).
    target="$DATAURL_DIR/$(basename "$img" | tr ' ' '_').dataurl"
    { printf 'data:%s;base64,' "$mime"; base64 -w0 "$img"; } > "$target"
    IMG_DATAURL_FILE["$img"]="$target"
done

echo "─────────────────────────────────────────────────────────────────────"
echo " llm-loadtest.sh"
echo "─────────────────────────────────────────────────────────────────────"
echo " endpoint     : $ENDPOINT"
echo " model        : $MODEL"
echo " duration     : ${DURATION_SEC}s ($(( DURATION_SEC / 60 )) min)"
echo " concurrency  : $CONCURRENCY"
echo " image pool   : ${#IMAGES[@]} files in $IMAGE_DIR"
echo " timeout/req  : ${PER_REQ_TIMEOUT}s"
echo " output       : $OUT_DIR"
echo "─────────────────────────────────────────────────────────────────────"

DEADLINE=$(( $(date +%s) + DURATION_SEC ))

# --- worker -------------------------------------------------------------- #
worker() {
    local wid="$1"
    local seq=0
    local images_csv=""

    # Choose first model for "both" alternation seed
    while [ "$(date +%s)" -lt "$DEADLINE" ]; do
        seq=$((seq + 1))

        # Pick 1, 2, or 3 images at random (uniform 1..min(3,pool_size))
        local max_n=$(( ${#IMAGES[@]} < 3 ? ${#IMAGES[@]} : 3 ))
        local n_images=$(( RANDOM % max_n + 1 ))
        # Shuffle indices, take first n
        local -a pick_idx=()
        mapfile -t pick_idx < <(seq 0 $((${#IMAGES[@]} - 1)) | shuf | head -n "$n_images")

        # Resolve model
        local model_used
        case "$MODEL" in
            both) [ $(( seq % 2 )) -eq 0 ] && model_used="qwen3.6" || model_used="gemma4" ;;
            *)    model_used="$MODEL" ;;
        esac

        # Prompt-cache-defeat: append a unique per-request suffix
        local ts_iso; ts_iso="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
        local nonce="${wid}-${seq}-${RANDOM}${RANDOM}"
        local prompt="Analyse the image content and create a markdown file from it. [request-id ${nonce} at ${ts_iso}]"

        # Build content array (text + N image_url blocks) via jq
        local content_args=(--arg p "$prompt")
        local content_filter='[{type:"text", text:$p}'
        local image_names_csv=""
        local i=0
        for idx in "${pick_idx[@]}"; do
            local img="${IMAGES[$idx]}"
            local dataurl_file="${IMG_DATAURL_FILE[$img]}"
            # --rawfile reads file contents as a string in jq — avoids the
            # ARG_MAX-bursting --arg path when images are >~700 KB encoded.
            content_args+=(--rawfile "u$i" "$dataurl_file")
            content_filter+=", {type:\"image_url\", image_url:{url:\$u$i}}"
            [ -n "$image_names_csv" ] && image_names_csv+="|"
            image_names_csv+="$(basename "$img")"
            i=$((i + 1))
        done
        content_filter+=']'

        # Write the body to a tempfile too — the JSON contains 1-3 base64 data
        # URLs, totalling several MB; passing it via curl --data-binary @-
        # avoids re-exporting it through the command line.
        local body_file="$OUT_DIR/.body-w${wid}.json"
        local max_tokens_field=""
        [ -n "${MAX_TOKENS:-}" ] && max_tokens_field=", max_tokens: ${MAX_TOKENS}"
        jq -nc "${content_args[@]}" --arg model "$model_used" "
            { model: \$model, messages: [ { role: \"user\", content: $content_filter } ]${max_tokens_field} }
        " > "$body_file"

        # Issue the request, capture timing + HTTP code + size
        local resp_file="$OUT_DIR/last-response-w${wid}.json"
        local t_start_ns; t_start_ns=$(date +%s%N)
        local code size
        # shellcheck disable=SC2086
        IFS='|' read -r code size < <(
            curl -sk --max-time "$PER_REQ_TIMEOUT" \
                 -o "$resp_file" \
                 -w '%{http_code}|%{size_download}' \
                 -H "Authorization: Bearer $LLM_API_KEY" \
                 -H "Content-Type: application/json" \
                 -X POST "$ENDPOINT/chat/completions" \
                 --data-binary "@$body_file" 2>/dev/null
        )
        local t_end_ns; t_end_ns=$(date +%s%N)
        local latency_ms=$(( (t_end_ns - t_start_ns) / 1000000 ))

        # Extract token counts if present
        local pt ct err=""
        if [ "$code" = "200" ] && [ -s "$resp_file" ]; then
            pt=$(jq -r '.usage.prompt_tokens // ""'     "$resp_file" 2>/dev/null)
            ct=$(jq -r '.usage.completion_tokens // ""' "$resp_file" 2>/dev/null)
        else
            pt=""; ct=""
            err=$(jq -r '.error.message // empty' "$resp_file" 2>/dev/null | head -c 200 | tr ',\n' '; ')
            [ -z "$err" ] && err="http_$code"
        fi

        # CSV row (basenames-pipe is escape-safe vs commas)
        printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
            "$ts_iso" "$wid" "$seq" "$model_used" "$n_images" "$image_names_csv" \
            "$latency_ms" "$code" "$size" "$pt" "$ct" "$err" >> "$LOG_CSV"

        # Brief stderr heartbeat
        echo "[w${wid} #${seq}] model=${model_used} n_img=${n_images} latency=${latency_ms}ms http=${code}" >&2
    done
}

# --- spawn workers, wait, then analyse ----------------------------------- #
PIDS=()
for w in $(seq 1 "$CONCURRENCY"); do
    worker "$w" 2> "$OUT_DIR/worker-${w}.log" &
    PIDS+=("$!")
done

echo "Spawned ${#PIDS[@]} workers — running until $(date -d "@$DEADLINE" '+%Y-%m-%d %H:%M:%S')"
echo "Tail per-worker logs: tail -f $OUT_DIR/worker-*.log"

for pid in "${PIDS[@]}"; do wait "$pid"; done
echo
echo "─────────────────────────────────────────────────────────────────────"
echo " all workers finished — analysing $LOG_CSV"
echo "─────────────────────────────────────────────────────────────────────"

REPORT="$OUT_DIR/report.md"
python3 - "$LOG_CSV" "$REPORT" "$ENDPOINT" "$MODEL" "$DURATION_SEC" "$CONCURRENCY" "${#IMAGES[@]}" <<'PYEOF'
import csv, os, statistics, sys
from collections import defaultdict
from datetime import datetime, timezone

csv_path, report_path, endpoint, model_cfg, dur_sec, conc, n_imgs_pool = sys.argv[1:]
dur_sec  = int(dur_sec); conc = int(conc); n_imgs_pool = int(n_imgs_pool)

rows = []
with open(csv_path) as f:
    for r in csv.DictReader(f):
        try:    r['latency_ms']  = int(r['latency_ms'])
        except: r['latency_ms']  = None
        try:    r['n_images']    = int(r['n_images'])
        except: r['n_images']    = None
        rows.append(r)

n_total = len(rows)
ok = [r for r in rows if r['http_code'] == '200']
fail = [r for r in rows if r['http_code'] != '200']
n_ok = len(ok); n_fail = len(fail)
success_rate = (100.0 * n_ok / n_total) if n_total else 0.0

def stats(latencies):
    if not latencies: return None
    s = sorted(latencies)
    return dict(
        n      = len(s),
        min    = s[0],
        max    = s[-1],
        avg    = round(statistics.mean(s), 1),
        median = round(statistics.median(s), 1),
        p50    = s[len(s)//2] if s else None,
        p95    = s[max(0, int(len(s)*0.95)-1)],
        p99    = s[max(0, int(len(s)*0.99)-1)],
        stdev  = round(statistics.pstdev(s), 1) if len(s) > 1 else 0,
    )

by_n_images = defaultdict(list)
by_model    = defaultdict(list)
by_model_n  = defaultdict(list)
for r in ok:
    by_n_images[r['n_images']].append(r['latency_ms'])
    by_model[r['model']].append(r['latency_ms'])
    by_model_n[(r['model'], r['n_images'])].append(r['latency_ms'])

err_counts = defaultdict(int)
for r in fail:
    key = r['error'] or f"http_{r['http_code']}"
    err_counts[key.split(';')[0][:80]] += 1

def md_table(rows, headers):
    lines = ['| ' + ' | '.join(headers) + ' |',
             '|' + '|'.join(['---']*len(headers)) + '|']
    for row in rows:
        lines.append('| ' + ' | '.join(str(c) for c in row) + ' |')
    return '\n'.join(lines)

L = []
L.append(f"# LLM endpoint load-test report\n")
L.append(f"**Endpoint:** `{endpoint}`  ")
L.append(f"**Model setting:** `{model_cfg}`  ")
L.append(f"**Duration:** {dur_sec}s (~{dur_sec//60} min)  ")
L.append(f"**Concurrency:** {conc} parallel workers  ")
L.append(f"**Image pool size:** {n_imgs_pool} files (1–{min(3,n_imgs_pool)} per request, randomly attached)  ")
L.append(f"**Report generated:** {datetime.now(timezone.utc).isoformat()}\n")

L.append("## Headline\n")
L.append(f"- **Requests submitted:** {n_total}")
L.append(f"- **Successful (HTTP 200):** {n_ok}")
L.append(f"- **Failed:** {n_fail}")
L.append(f"- **Success rate:** {success_rate:.2f}%")
if ok:
    all_lat = stats([r['latency_ms'] for r in ok])
    L.append(f"- **Throughput (successful only):** {n_ok / dur_sec:.2f} req/s ({n_ok / (dur_sec/60):.1f} req/min)")
    L.append(f"- **Latency (ms) overall:** avg={all_lat['avg']}  median={all_lat['median']}  p95={all_lat['p95']}  p99={all_lat['p99']}  min={all_lat['min']}  max={all_lat['max']}\n")

L.append("## Latency by image count (successful requests)\n")
rows_n = []
for n in sorted(by_n_images):
    s = stats(by_n_images[n])
    rows_n.append([n, s['n'], s['avg'], s['median'], s['p95'], s['p99'], s['min'], s['max'], s['stdev']])
L.append(md_table(rows_n, ['n_images', 'count', 'avg_ms', 'median_ms', 'p95_ms', 'p99_ms', 'min_ms', 'max_ms', 'stdev_ms']))
L.append("")

L.append("## Latency by model (successful requests)\n")
rows_m = []
for m in sorted(by_model):
    s = stats(by_model[m])
    rows_m.append([m, s['n'], s['avg'], s['median'], s['p95'], s['p99'], s['min'], s['max'], s['stdev']])
L.append(md_table(rows_m, ['model', 'count', 'avg_ms', 'median_ms', 'p95_ms', 'p99_ms', 'min_ms', 'max_ms', 'stdev_ms']))
L.append("")

L.append("## Latency by model × image count\n")
rows_mn = []
for (m, n) in sorted(by_model_n):
    s = stats(by_model_n[(m, n)])
    rows_mn.append([m, n, s['n'], s['avg'], s['median'], s['p95'], s['p99']])
L.append(md_table(rows_mn, ['model', 'n_images', 'count', 'avg_ms', 'median_ms', 'p95_ms', 'p99_ms']))
L.append("")

if err_counts:
    L.append("## Error distribution\n")
    rows_e = sorted(err_counts.items(), key=lambda x: -x[1])
    L.append(md_table([[k, v] for k, v in rows_e], ['error', 'count']))
    L.append("")
else:
    L.append("## Error distribution\n\nNo failures.\n")

L.append("## Stability + performance verdict\n")
verdict = "🟢 stable" if success_rate >= 99.0 else ("🟡 mostly stable" if success_rate >= 95.0 else "🔴 unstable")
L.append(f"- **Stability:** {verdict} (success {success_rate:.2f}%)")
if ok:
    if len(by_n_images) > 1:
        ns = sorted(by_n_images)
        s_lo = stats(by_n_images[ns[0]]);  s_hi = stats(by_n_images[ns[-1]])
        if s_lo and s_hi and s_lo['avg'] > 0:
            scaling = s_hi['avg'] / s_lo['avg']
            L.append(f"- **Latency scales {scaling:.2f}× from {ns[0]}-image to {ns[-1]}-image requests** "
                     f"(avg {s_lo['avg']}→{s_hi['avg']} ms).")
    if len(by_model) > 1:
        ms_avg = {m: stats(by_model[m])['avg'] for m in by_model}
        faster = min(ms_avg, key=ms_avg.get); slower = max(ms_avg, key=ms_avg.get)
        if faster != slower and ms_avg[faster] > 0:
            L.append(f"- **Model comparison:** `{faster}` faster avg-latency than `{slower}` "
                     f"({ms_avg[faster]} vs {ms_avg[slower]} ms, ratio {ms_avg[slower]/ms_avg[faster]:.2f}×).")
    p99 = stats([r['latency_ms'] for r in ok])['p99']
    L.append(f"- **Tail latency:** p99 = {p99} ms.")
L.append("")
L.append(f"_Raw data: `{csv_path}`._\n")

text = '\n'.join(L)
with open(report_path, 'w') as f: f.write(text)
print(text)
PYEOF

echo
echo "Report saved: $REPORT"
echo "Raw CSV     : $LOG_CSV"
