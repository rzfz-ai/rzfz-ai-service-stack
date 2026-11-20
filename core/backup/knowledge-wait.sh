#!/bin/sh
# ==============================================================================
# knowledge-wait.sh — the nightly backup waits for running knowledge ingestion
# ==============================================================================
# The archive step stops cognee and lightrag (docker-volume-backup.stop-during-
# backup=true: their graph files must not change while they are archived). A
# knowledge graph build (cognee "cognify") or a LightRAG document ingest that is
# running at that moment is killed. One 50-document cognee dataset takes over an
# hour, so a nightly run started in the evening was lost every time.
#
# Called by pre-backup.sh before any dump, so the dumps and the archive describe
# the state after the ingest. A scheduled run waits until neither is busy, for at
# most BACKUP_KNOWLEDGE_WAIT_MAX_S seconds (default 7200; 0 = never wait). If the
# work is still running then, the backup goes ahead and says which work it
# interrupts. A backup asked for now (`rzfz backup backup`, the Backup page, the
# backup before an upgrade) does not wait; it says what it interrupts. When the
# wait ends on its own, the log says why: the work finished, or a run grew older
# than the bound and stopped being treated as live (the WARNING for it names it).
#
# Busy means:
#   cognee   — a LIVE pipeline run: its newest lifecycle row in cognee's
#              pipeline_runs is still DATASET_PROCESSING_STARTED, and none of these
#              shows it is dead:
#                * it began before the cognee container last started (a run lives
#                  in cognee's process; a stopped container ended it);
#                * a newer run of the same pipeline on the same dataset has ended
#                  (COMPLETED or ERRORED after it began). cognee serialises the runs
#                  of one dataset, so that run could only start once this one had
#                  stopped;
#                * it began more than MAX_RUN_AGE_H (8) hours ago. The longest
#                  cognify measured is 79 min (50 documents at 40 documents/h);
#                  8 h covers one build of about 320 documents. This is only the
#                  backstop for a run that died without either sign above.
#              A run that is dead by the first two signs is recorded as ERRORED (a
#              new lifecycle row, as cognee itself closes a failed run), so cognee's
#              own state stops showing it as running. So is every live run this
#              backup interrupts: it stops cognee seconds later. A run skipped only
#              for its age is never closed, and the skip is never silent: the backup
#              log carries one WARNING per such run naming the run, its dataset and
#              its age — also when a poll during the wait is the first to see it,
#              and never a second warning for the same run.
#   lightrag — its /documents/pipeline_status answers busy=true.
# A check that cannot be made (a container down, a query error) counts as not
# busy: this step never blocks or fails a backup.
# ==============================================================================

POLL_S="${RZFZ_KNOWLEDGE_POLL_S:-30}"
MAX_S="${BACKUP_KNOWLEDGE_WAIT_MAX_S:-7200}"
case "$POLL_S" in ''|*[!0-9]*|0) POLL_S=30 ;; esac
case "$MAX_S" in ''|*[!0-9]*) MAX_S=7200 ;; esac
MAX_RUN_AGE_H=8
ON_DEMAND_MARK="${RZFZ_BACKUP_ON_DEMAND_MARK:-/tmp/rzfz-backup-on-demand}"

LIGHTRAG_BUSY_PY="import json, os, urllib.request as u
r = u.Request('http://127.0.0.1:' + os.environ.get('PORT', '9621') + '/documents/pipeline_status',
              headers={'X-API-Key': os.environ.get('LIGHTRAG_API_KEY', '')})
print(bool(json.load(u.build_opener(u.ProxyHandler({})).open(r, timeout=15)).get('busy')))"

running() {
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"
}

cognee_sql() {
    PGPASSWORD="${POSTGRES_PASSWORD:-}" docker exec -e PGPASSWORD postgres psql -X -q -At \
        -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-docker}" -d "${COGNEE_DB:-cognee_db}" -c "$1" 2>/dev/null
}

# When the cognee container last started (RFC 3339), or "" when unknown.
cognee_started_at() {
    s=$(docker inspect -f '{{.State.StartedAt}}' cognee 2>/dev/null)
    case "$s" in
        ''|0001-*|*[!0-9TZ:.+-]*) printf '' ;;
        *) printf '%s' "$s" ;;
    esac
}

# One line per run whose newest row is STARTED:
# "<row id>|<pipeline run id>|live|dead|old|<dataset>|<age in hours>".
cognee_runs() {
    started=$(cognee_started_at)
    if [ -n "$started" ]; then
        restarted="r.created_at < '$started'::timestamptz"
    else
        restarted="false"
    fi
    cognee_sql "WITH newest AS (
        SELECT DISTINCT ON (pipeline_run_id) id, pipeline_run_id, status::text AS status,
               created_at, dataset_id, pipeline_name
          FROM pipeline_runs
         WHERE pipeline_name IS NOT NULL AND pipeline_run_id IS NOT NULL
         ORDER BY pipeline_run_id, created_at DESC)
      SELECT r.id || '|' || r.pipeline_run_id || '|' || CASE
          WHEN $restarted THEN 'dead'
          WHEN EXISTS (SELECT 1 FROM pipeline_runs t
                        WHERE t.dataset_id = r.dataset_id AND t.pipeline_name = r.pipeline_name
                          AND t.pipeline_run_id <> r.pipeline_run_id
                          AND t.status::text IN ('DATASET_PROCESSING_COMPLETED', 'DATASET_PROCESSING_ERRORED')
                          AND t.created_at > r.created_at) THEN 'dead'
          WHEN r.created_at < now() - interval '$MAX_RUN_AGE_H hours' THEN 'old'
          ELSE 'live' END || '|' || coalesce(r.dataset_id::text, '') || '|' ||
          (EXTRACT(EPOCH FROM now() - r.created_at) / 3600)::int
        FROM newest r WHERE r.status = 'DATASET_PROCESSING_STARTED'
       ORDER BY r.created_at;"
}

# Record the runs of these STARTED rows as ERRORED, unless a newer row closed them.
# $1: space-separated row ids, $2: the reason.
mark_errored() {
    ids=""
    for id in $1; do
        case "$id" in *[!0-9a-f-]*|'') continue ;; esac     # uuids only
        ids="${ids:+$ids,}$id"
    done
    [ -n "$ids" ] || return 0
    cognee_sql "DO \$\$ DECLARE t text; BEGIN
      SELECT format_type(atttypid, atttypmod) INTO t FROM pg_attribute
       WHERE attrelid = 'pipeline_runs'::regclass AND attname = 'status';
      EXECUTE format(\$q\$INSERT INTO pipeline_runs (id, created_at, status, pipeline_run_id,
            pipeline_name, pipeline_id, dataset_id, run_info, user_id, tenant_id, operation_name,
            started_at, ended_at, outcome, error_class, error_message, origin, parent_operation_id)
          SELECT gen_random_uuid(), now(), %L::%s, p.pipeline_run_id, p.pipeline_name, p.pipeline_id,
                 p.dataset_id, json_build_object('error', %L), p.user_id, p.tenant_id, p.pipeline_name,
                 p.started_at, now(), 'failed', 'BackupInterruption', %L, p.origin, p.parent_operation_id
            FROM pipeline_runs p
           WHERE p.id = ANY(%L::uuid[])
             AND NOT EXISTS (SELECT 1 FROM pipeline_runs n WHERE n.pipeline_run_id = p.pipeline_run_id
                                AND n.created_at > p.created_at)\$q\$,
        'DATASET_PROCESSING_ERRORED', t, '$2', '$2', '{$ids}');
    END \$\$;" >/dev/null || echo "WARNING: could not record interrupted cognee runs as failed; cognee still lists them as running."
}

# Row ids of the runs of one class, from cognee_runs output.
runs_of() {
    printf '%s\n' "$1" | sed -n "s/^\([^|]*\)|[^|]*|$2|.*/\1/p" | tr '\n' ' '
}

# One WARNING per run skipped only for its age, each run once however often a poll
# reclassifies it (warned_old lists the pipeline run ids already warned): the skip is
# never silent, and the run itself is left untouched.
warn_old_runs() {
    for row in $(printf '%s\n' "$COGNEE_RUNS" |
                 sed -n 's/^\([^|]*\)|\([^|]*\)|old|\([^|]*\)|\([^|]*\)$/\1|\2|\3|\4/p'); do
        run=${row#*|}; run=${run%%|*}
        case " $warned_old " in *" $run "*) continue ;; esac
        dataset=${row#*|*|}; dataset=${dataset%%|*}
        age=${row#*|*|*|}
        echo "WARNING: cognee run $run (dataset $dataset) not treated as live: started ${age}h ago, no completion — backup proceeds."
        warned_old="$warned_old $run"
    done
}

COGNEE_RUNS=""
cognee_live=0
warned_old=""

# Prints what is busy ("" when nothing is), one phrase per service. Sets COGNEE_RUNS.
knowledge_busy() {
    busy=""
    COGNEE_RUNS=""
    if running cognee && running postgres; then
        COGNEE_RUNS=$(cognee_runs)
        n=$(printf '%s\n' "$COGNEE_RUNS" | grep -c '|live|[^|]*|[^|]*$')
        case "$n" in
            ''|*[!0-9]*|0) ;;
            *) busy="cognee: ${n} knowledge graph build(s) running" ;;
        esac
    fi
    if running lightrag; then
        b=$(docker exec lightrag python -c "$LIGHTRAG_BUSY_PY" 2>/dev/null)
        if [ "$b" = "True" ]; then
            busy="${busy:+$busy; }lightrag: a document ingest running"
        fi
    fi
    BUSY="$busy"
}

on_demand() {
    [ -f "$ON_DEMAND_MARK" ] || return 1
    stamp=$(cat "$ON_DEMAND_MARK" 2>/dev/null)
    case "$stamp" in ''|*[!0-9]*) return 1 ;; esac
    [ $(( $(date +%s) - stamp )) -lt 21600 ]      # a mark older than 6 h is stale
}

knowledge_busy
dead=$(runs_of "$COGNEE_RUNS" dead)
if [ -n "$dead" ]; then
    echo "cognee: closing $(echo $dead | wc -w) build run(s) that ended without a record (cognee restarted or a later run finished) as failed."
    mark_errored "$dead" "Interrupted: cognee stopped while this run was building; build the dataset again."
fi
warn_old_runs
if [ -z "$BUSY" ]; then
    echo "Knowledge ingestion: nothing running; the backup goes ahead."
    exit 0
fi
if on_demand; then
    echo "WARNING: this backup stops cognee and lightrag and interrupts running knowledge ingestion (${BUSY}). Start it again afterwards."
    mark_errored "$(runs_of "$COGNEE_RUNS" live)" "Interrupted by a backup, which stopped cognee; build the dataset again."
    exit 0
fi
echo "Knowledge ingestion is running (${BUSY}); the backup waits for it, at most ${MAX_S}s, before it stops cognee and lightrag."
waited=0
age_skipped=0
while [ -n "$BUSY" ] && [ "$waited" -lt "$MAX_S" ]; do
    sleep "$POLL_S"
    waited=$((waited + POLL_S))
    knowledge_busy
    warned_before="$warned_old"
    warn_old_runs
    [ "$warned_old" != "$warned_before" ] && age_skipped=1
done
if [ -n "$BUSY" ]; then
    echo "WARNING: knowledge ingestion was still running after ${waited}s (${BUSY}); the backup stops cognee and lightrag now and interrupts it. Start it again afterwards, or raise BACKUP_KNOWLEDGE_WAIT_MAX_S."
    mark_errored "$(runs_of "$COGNEE_RUNS" live)" "Interrupted by the nightly backup, which stopped cognee; build the dataset again."
elif [ "$age_skipped" = 1 ]; then
    echo "Knowledge ingestion is no longer treated as running after ${waited}s; the backup goes ahead."
else
    echo "Knowledge ingestion finished after ${waited}s; the backup goes ahead."
fi
exit 0
