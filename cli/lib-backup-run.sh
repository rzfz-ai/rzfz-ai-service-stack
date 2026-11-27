# shellcheck shell=bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# cli/lib-backup-run.sh — did the last backup run write its archive? (#2584)
# ==============================================================================
# Sourced by cli/backup.sh (`rzfz backup status`) and cli/status.sh
# (`rzfz status`). A sourced library: no top-level `set`.
#
# Measured 2026-09-30 on a production box: the nightly wrote no archive at all (offen aborted with
# "archive/tar: write too long"), and nothing on the box said so — `rzfz
# status` only named the newest archive, a day old and still a PASS.
#
# What a run leaves (core/backup/pre-backup-run.sh, the archive-pre runner):
#   /backup/databases/pre-backup.status
#     rc=running + started_epoch         written BEFORE the hook
#     … + finished_epoch + archive_began_early   when the hook has finished
# offen copies the archive to backups/ after that; its mtime is the end of
# that copy (os.Create + io.Copy), so it is only ever compared with the run's
# START here. Whether the archive BEGAN before the hook finished is measured
# by the runner itself (offen's own temp archive already existing at the
# hook's end) — the one moment an mtime cannot tell.
#
# The verdict is one or more lines, "LEVEL|text", LEVEL one of OK, RUNNING,
# FAIL, INFO. Two questions, each answered on its own:
#
# 1. The schedule: did a run start at the latest scheduled slot? Judged from
#    BACKUP_CRON_EXPRESSION alone, whatever the previous run's verdict was,
#    and only while offen does not hold its run lock (a run waiting for a
#    manual one is not missed). A run that never started — socket proxy down,
#    offen crash-looping — leaves no report of its own: without this line the
#    status would keep naming the run before it.
#      no report start and no archive since the last slot (+15 min) → FAIL
#    This line comes first.
#
# 2. The last run the report names:
#    no finished_epoch (the hook never reported its end):
#      offen holds its lock                          → RUNNING (pre-backup step)
#      an archive since started_epoch                → FAIL (b): that archive
#          began before the pre-backup step finished — named, probably
#          incomplete, not the only restore point
#      no archive                                    → FAIL (a): no archive
#      Either FAIL also says whether the pre-backup step is still running:
#      the runner holds its own lock (BACKUP_RUN_HOOK_LOCK) for as long as the
#      hook runs, and offen's cut stream does not stop it.
#    archive_began_early=1 (the runner saw offen's temp archive):
#      an archive since started_epoch                → FAIL (b), named
#      none, offen holds its lock                    → FAIL: it is still being
#                                                      written, and began early
#      none                                          → FAIL (a)
#    no archive since started_epoch, lock held       → RUNNING (archive being written)
#    no archive since started_epoch                  → FAIL (a)
#    newest archive before finished_epoch            → FAIL (b), named
#    otherwise                                       → OK, named
#    After a missed slot (1.), an OK here is INFO: it is not the last night's.
#
# Before both: a BACKUP_CRON_EXPRESSION that offen's parser rejects → FAIL
# alone (offen cannot even start). After both, whatever they said (#3458): the
# newest COMPLETE archive (completion mark, see backup_run_archive_is_complete)
# older than the interval + 2 h → FAIL, naming it and its age (26 h for a
# daily schedule, and when the parser cannot run, e.g. no python3 — never a
# blind OK); archives but none complete → FAIL. A schedule without any slot
# (scheduled backups off) has no bound. An OK beside such a FAIL becomes INFO.
# A box with neither a report nor an archive (first install) gets a verdict
# only when backup-service has been up since before a passed slot (its
# State.StartedAt): FAIL "no backup run has happened since the backup service
# started"; otherwise none.
#
# offen holds an flock on /var/lock/dockervolumebackup.lock for the whole run
# (cmd/backup/run_script.go, gofrs/flock → flock(2)); busybox `flock -n` in the
# same container reads it. All times are this host's clock: the container's
# `date` and a local backups/ mtime share it. A backups/ on a network mount
# carries the server's clock in its mtimes — keep the two in sync there.
# ==============================================================================

BACKUP_RUN_CONTAINER="${BACKUP_RUN_CONTAINER:-backup-service}"
BACKUP_RUN_REPORT=/backup/databases/pre-backup.status
BACKUP_RUN_LOCK=/var/lock/dockervolumebackup.lock
# Held by core/backup/pre-backup-run.sh for as long as the hook runs.
BACKUP_RUN_HOOK_LOCK=/var/lock/rzfz-pre-backup.lock

# #3199: the marker a running upgrade leaves in backup-service's /tmp (a volume:
# it survives the upgrade recreating the container). core/backup/pre-backup-run.sh
# makes a scheduled backup wait while it exists.
BACKUP_RUN_UPGRADE_MARK=/tmp/rzfz-upgrade-in-progress

# backup_run_wait_until_idle MAX_S [POLL_S] — wait while a backup run holds
# offen's lock (a scheduled backup that started before the upgrade): the upgrade
# must not replace containers that run stopped. 0 when idle (or the lock cannot
# be read: no backup-service), 1 when still running after MAX_S.
backup_run_wait_until_idle() {
    local max="${1:-3600}" poll="${2:-15}" waited=0 told=""
    while [ "$(backup_run_lock_state)" = held ]; do
        if [ -z "$told" ]; then
            told=1
            if command -v print_info >/dev/null 2>&1; then
                print_info "A backup is running; the upgrade waits for it to finish (at most ${max}s)."
            else
                echo "A backup is running; the upgrade waits for it to finish (at most ${max}s)."
            fi
        fi
        [ "$waited" -lt "$max" ] || return 1
        sleep "$poll"
        waited=$((waited + poll))
    done
    return 0
}

# backup_run_pause_for_upgrade — leave the marker; 0 when it was written.
backup_run_pause_for_upgrade() {
    # shellcheck disable=SC2016 — expanded by the container's sh
    _backup_run_docker exec "$BACKUP_RUN_CONTAINER" sh -c 'date +%s > "$0"' "$BACKUP_RUN_UPGRADE_MARK"         >/dev/null 2>&1
}

# backup_run_resume_after_upgrade — remove the marker (also after a failed upgrade).
backup_run_resume_after_upgrade() {
    _backup_run_docker exec "$BACKUP_RUN_CONTAINER" rm -f "$BACKUP_RUN_UPGRADE_MARK" >/dev/null 2>&1
}

# _backup_run_docker ARGS… — docker, cut off after RZFZ_UPGRADE_PROBE_TIMEOUT
# seconds (default 15), as upgrade.sh's _backup_probe does: a Docker socket proxy
# that stops answering must not hang an upgrade or its EXIT trap (#3199). A shell
# function standing in for docker cannot run under timeout(1); it runs as it is.
_backup_run_docker() {
    if [ "$(type -t docker)" = file ] && command -v timeout >/dev/null 2>&1; then
        timeout "${RZFZ_UPGRADE_PROBE_TIMEOUT:-15}" docker "$@"
    else
        docker "$@"
    fi
}

# backup_run_report — the hook's report of the last run; empty when none.
backup_run_report() {
    docker exec "$BACKUP_RUN_CONTAINER" cat "$BACKUP_RUN_REPORT" 2>/dev/null || true
}

# backup_run_lock_state [LOCKFILE] — held | free | unknown, for offen's run
# lock by default. By output, not exit code: a stopped container also makes
# `docker exec` exit 1.
backup_run_lock_state() {
    local out lockfile="${1:-$BACKUP_RUN_LOCK}"
    # shellcheck disable=SC2016 — expanded by the container's sh
    out=$(_backup_run_docker exec "$BACKUP_RUN_CONTAINER" sh -c \
        'command -v flock >/dev/null 2>&1 || exit 0
         if flock -n "$0" true; then echo free; else echo held; fi' \
        "$lockfile" 2>/dev/null) || true
    case "$out" in
        free|held) echo "$out" ;;
        *) echo unknown ;;
    esac
}

# backup_run_hook_lock_state — held while the pre-backup step still runs.
backup_run_hook_lock_state() {
    backup_run_lock_state "$BACKUP_RUN_HOOK_LOCK"
}

# backup_run_service_started — epoch at which the running backup-service
# container started; empty when it is not running or docker cannot say. offen
# arms its cron at start and never backs up at start, so on a box with no
# report and no archive a slot after this time is a missed run. Every failure
# here (inspect error, stopped container, unparsable time) is silence, never
# a FAIL; a restart or crash loop moves the time forward, which is silence too.
backup_run_service_started() {
    local out running ts epoch
    out=$(docker inspect -f '{{.State.Running}} {{.State.StartedAt}}' "$BACKUP_RUN_CONTAINER" 2>/dev/null) || return 0
    running=${out%% *}; ts=${out#* }
    [ "$running" = true ] && [ -n "$ts" ] && [ "$ts" != "$out" ] || return 0
    epoch=$(date -d "$ts" +%s 2>/dev/null) || return 0
    # Digits only: "never started" (0001-01-01) is a negative epoch.
    case "$epoch" in ''|*[!0-9]*) return 0 ;; esac
    echo "$epoch"
}

# _backup_run_when EPOCH — in the stack's zone (BACKUP_RUN_TZ, set by the
# verdict from .env TZ; offen runs in it), so a 03:00 schedule reads 03:00.
_backup_run_when() {
    TZ="${BACKUP_RUN_TZ:-UTC}" date -d "@$1" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || echo "epoch $1"
}

# _backup_run_schedule CRON TZ NOW — how offen reads BACKUP_CRON_EXPRESSION,
# one line:
#   LAST <t> [<prev>]   the most recent scheduled time(s) at or before NOW
#   EVERY <seconds>     `@every <duration>` (no wall-clock slot)
#   NONE                legal, but no slot within the search window
#   BAD <reason>        offen's parser rejects it — offen exits at start
# The grammar is robfig/cron v3.0.1's as offen v2.50.0 configures it
# (SecondOptional|Minute|Hour|Dom|Month|Dow|Descriptor): 5 or 6 fields,
# bounds sec/min 0-59, hour 0-23, dom 1-31, month 1-12 + jan-dec, dow 0-6 +
# sun-sat (7 is out of range), `*`/`?`, a-b, N/step = N-max/step, lists;
# descriptors @yearly/@annually/@monthly/@weekly/@daily/@midnight/@hourly and
# `@every <Go duration>`; CRON_TZ=/TZ= prefix with a loadable zone. Day of
# month and day of week are ORed only when neither carries robfig's star bit
# (`*`/`?` with a step of 1). A wall time that does not exist (DST
# spring-forward) is skipped, as robfig does. No output at all means the
# parser could not run (no python3) — the caller then assumes a default bound.
_backup_run_schedule() {
    python3 - "$1" "$2" "$3" <<'PY' 2>/dev/null
import datetime as dt, re, sys
expr, tzname, now = sys.argv[1], sys.argv[2].strip() or "UTC", int(sys.argv[3])

def bad(why):
    print("BAD", why)
    sys.exit(0)

def zone(name):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)

if expr == "":
    bad("empty")
if expr.startswith("TZ=") or expr.startswith("CRON_TZ="):
    if " " not in expr:
        bad("a time zone prefix without a schedule")
    tzname, expr = expr[expr.index("=") + 1:expr.index(" ")], expr[expr.index(" "):].strip()
    try:
        tz = zone(tzname)
    except Exception:
        bad(f"unknown time zone {tzname!r}")
else:
    try:
        tz = zone(tzname)
    except Exception:
        tz = dt.timezone.utc          # Go falls back to UTC for a bad TZ
if expr.startswith("@"):
    if expr.startswith("@every "):
        m = re.fullmatch(r"([-+]?)((?:(?:\d+\.?\d*|\.\d+)(?:ns|us|µs|μs|ms|s|m|h))+|0)",
                         expr[len("@every "):])
        if not m:
            bad(f"not a duration: {expr[len('@every '):]!r}")
        units = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6,
                 "μs": 1e-6, "ns": 1e-9}
        secs = sum(float(n) * units[u] for n, u in
                   re.findall(r"(\d+\.?\d*|\.\d+)(ns|us|µs|μs|ms|s|m|h)", m.group(2)))
        print("EVERY", max(1, int(secs)) if m.group(1) != "-" else 1)
        sys.exit(0)
    expr = {"@yearly": "0 0 0 1 1 *", "@annually": "0 0 0 1 1 *", "@monthly": "0 0 0 1 * *",
            "@weekly": "0 0 0 * * 0", "@daily": "0 0 0 * * *", "@midnight": "0 0 0 * * *",
            "@hourly": "0 0 * * * *"}.get(expr)
    if expr is None:
        bad("unknown descriptor")
f = expr.split()
if len(f) == 5:
    f = ["0"] + f
if len(f) != 6:
    bad(f"expected 5 or 6 fields, found {len(f)}")
MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DOW = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}

def num(x, names):
    if names and x.lower() in names:
        return names[x.lower()]
    if not re.fullmatch(r"[+]?\d+", x):
        raise ValueError(f"not a number: {x!r}")
    return int(x)

def field(spec, lo, hi, names=None):
    out, star = set(), False
    for part in [p for p in spec.split(",") if p]:
        rs = part.split("/")
        if len(rs) > 2:
            raise ValueError(f"too many slashes: {part!r}")
        lh = rs[0].split("-")
        if lh[0] in ("*", "?"):
            a, b, is_star = lo, hi, True
        else:
            if len(lh) > 2:
                raise ValueError(f"too many hyphens: {part!r}")
            a = num(lh[0], names)
            b = num(lh[1], names) if len(lh) == 2 else a
            is_star = False
        step = 1
        if len(rs) == 2:
            step = num(rs[1], None)
            if len(lh) == 1:
                b = hi
            if step > 1:
                is_star = False
        if a < lo or b > hi or a > b or step == 0:
            raise ValueError(f"out of range: {part!r}")
        out.update(range(a, b + 1, step))
        star |= is_star
    return out, star

try:
    field(f[0], 0, 59)
    mi, _ = field(f[1], 0, 59)
    ho, _ = field(f[2], 0, 23)
    dom, dom_star = field(f[3], 1, 31)
    mon, _ = field(f[4], 1, 12, MON)
    dow, dow_star = field(f[5], 0, 6, DOW)
except ValueError as e:
    bad(str(e))

def day_ok(d):
    a, b = d.day in dom, (d.isoweekday() % 7) in dow
    return (a and b) if (dom_star or dow_star) else (a or b)

def exists(w):
    """A wall time that DST skips has no instant; robfig never fires it."""
    back = dt.datetime.fromtimestamp(w.replace(tzinfo=tz).timestamp(), tz).replace(tzinfo=None)
    return back == w

w = dt.datetime.fromtimestamp(now, tz).replace(tzinfo=None, second=0, microsecond=0)
found, limit = [], w - dt.timedelta(days=800)
while w > limit and len(found) < 2:
    if w.month not in mon or not day_ok(w):
        w = w.replace(hour=23, minute=59) - dt.timedelta(days=1)
        continue
    if w.hour in ho and w.minute in mi and exists(w):
        found.append(int(w.replace(tzinfo=tz).timestamp()))
    w -= dt.timedelta(minutes=1)
print("LAST", *found) if found else print("NONE")
PY
}

# backup_run_cron_from_env ENVFILE — BACKUP_CRON_EXPRESSION as offen gets it:
# compose's env_file unquotes "…" and '…' and drops an unquoted ` # comment`;
# offen's envconfig applies its default (@daily) only when the key is ABSENT —
# present and empty is an empty schedule, which offen refuses.
backup_run_cron_from_env() {
    local line v
    line=$(grep -m1 '^BACKUP_CRON_EXPRESSION=' "$1" 2>/dev/null) || { echo "@daily"; return 0; }
    v=${line#BACKUP_CRON_EXPRESSION=}
    case "$v" in
        \"*) v=${v#\"}; v=${v%%\"*} ;;
        \'*) v=${v#\'}; v=${v%%\'*} ;;
        *)   v=$(printf '%s' "$v" | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//') ;;
    esac
    printf '%s\n' "$v"
}

# _backup_run_archives DIR FILENAME — "<mtime> <name>" of every archive in DIR
# whose name fits BACKUP_FILENAME (strftime fields and {{ … }} as wildcards;
# offen's default when empty), plain or .gpg; checksum sidecars are not archives.
_backup_run_archives() {
    local dir="$1" fname="$2" glob f
    [ -n "$fname" ] || fname='backup-%Y-%m-%dT%H-%M-%S.{{ .Extension }}'
    glob=$(printf '%s' "$fname" | sed -e 's/%[A-Za-z]/*/g' -e 's/{{[^}]*}}/*/g')
    for f in "$dir"/$glob "$dir"/$glob.gpg; do
        [ -f "$f" ] || continue
        case "$f" in *.sha256|*.md5) continue ;; esac
        printf '%s %s\n' "$(stat -c %Y "$f" 2>/dev/null || echo 0)" "${f##*/}"
    done
}

# backup_run_archive_verdict <backups-dir> <report-text> <lock-state> <cron> [<tz> <filename> <hook-lock-state> <service-started>]
# <cron> as backup_run_cron_from_env gives it (offen's default when absent);
# <hook-lock-state> as backup_run_hook_lock_state gives it (unknown when
# omitted); <service-started> as backup_run_service_started gives it (empty:
# a fresh box gets no verdict). Prints one or more "LEVEL|text" lines (see the header), the
# missed slot first; nothing for a box with neither a report nor an archive.
# $BACKUP_RUN_NOW overrides the clock (tests).
backup_run_archive_verdict() {
    local dir="$1" report="$2" lock="$3" cron="${4:-}" tz="${5:-}" fname="${6:-}" hook="${7:-unknown}" svc="${8:-}"
    local now started finished early newest=0 newest_name="" m n sched last="" prev="" every=""
    local verdict="" missed="" hookstate="" BACKUP_RUN_TZ="${tz:-UTC}"
    local incomplete="it is probably incomplete (database dumps or agent volumes written after that are missing) — do not use it as the only restore point; run a backup again"
    now=${BACKUP_RUN_NOW:-$(date +%s)}
    started=$(printf '%s\n' "$report" | sed -n 's/^started_epoch=//p' | head -n 1)
    finished=$(printf '%s\n' "$report" | sed -n 's/^finished_epoch=//p' | head -n 1)
    early=$(printf '%s\n' "$report" | sed -n 's/^archive_began_early=//p' | head -n 1)
    case "$started" in *[!0-9]*) started="" ;; esac
    case "$finished" in *[!0-9]*) finished="" ;; esac
    while read -r m n; do
        [ -n "$m" ] || continue
        if [ "$m" -gt "$newest" ]; then newest=$m; newest_name=$n; fi
    done <<EOF
$(_backup_run_archives "$dir" "$fname")
EOF
    # offen refuses a malformed expression at start and exits: backup-service
    # restarts in a loop and no backup runs at all — on a first install too.
    sched=$(_backup_run_schedule "$cron" "$tz" "$now")
    case "$sched" in BAD\ *)
        echo "FAIL|the backup schedule is not a valid cron expression: '$cron' (${sched#BAD }) — the backup service cannot start, so no backup runs; fix BACKUP_CRON_EXPRESSION in .env"
        return 0 ;;
    esac
    if [ -z "$started" ] && [ "$newest" -eq 0 ]; then
        # A fresh box: no run on record at all. Only the container's start
        # can say a slot has passed since the box could have run one.
        case "$svc" in ''|*[!0-9]*) return 0 ;; esac
        case "$sched" in LAST\ *) last=${sched#LAST }; last=${last%% *} ;; *) return 0 ;; esac
        if [ "$lock" != held ] && [ $((now - last)) -gt 900 ] && [ "$svc" -lt $((last - 300)) ]; then
            echo "FAIL|no backup run has happened since the backup service started at $(_backup_run_when "$svc") — the scheduled time $(_backup_run_when "$last") passed without one (BACKUP_CRON_EXPRESSION $cron); the reason is in: docker logs backup-service"
        fi
        return 0
    fi

    # 1. the run the report names
    if [ -n "$started" ]; then
        if [ -z "$finished" ]; then
            # The hook never reported its end. offen's cut stream ends offen's
            # wait, not the hook: it may still be running, and offen may
            # already have written its archive.
            case "$hook" in
                held) hookstate="its pre-backup step is still running" ;;
                free) hookstate="its pre-backup step stopped without finishing" ;;
                *)    hookstate="its pre-backup step never reported its end" ;;
            esac
            if [ "$lock" = held ]; then
                verdict="RUNNING|the backup run started $(_backup_run_when "$started") is in its pre-backup step"
            elif [ "$newest" -ge "$started" ]; then
                verdict="FAIL|$newest_name ($(_backup_run_when "$newest")) was written by the backup run started $(_backup_run_when "$started") before its pre-backup step finished ($hookstate) — $incomplete"
            else
                verdict="FAIL|the backup run started $(_backup_run_when "$started") produced no archive, and $hookstate — the reason is in: docker logs backup-service"
            fi
        elif [ "$early" = 1 ]; then
            if [ "$newest" -ge "$started" ]; then
                verdict="FAIL|$newest_name ($(_backup_run_when "$newest")), the archive of the backup run started $(_backup_run_when "$started"), began before its pre-backup step finished ($(_backup_run_when "$finished")) — $incomplete"
            elif [ "$lock" = held ]; then
                verdict="FAIL|the archive of the backup run started $(_backup_run_when "$started") began before its pre-backup step finished ($(_backup_run_when "$finished")) and is still being written — it will be incomplete; run a backup again once it ends"
            else
                verdict="FAIL|the backup run started $(_backup_run_when "$started") produced no archive (its archive step began before its pre-backup step finished, $(_backup_run_when "$finished")) — the reason is in: docker logs backup-service"
            fi
        elif [ "$newest" -ge "$started" ]; then
            if [ "$newest" -lt "$finished" ]; then
                verdict="FAIL|$newest_name ($(_backup_run_when "$newest")) was written before the pre-backup step of its run finished ($(_backup_run_when "$finished")) — $incomplete"
            else
                verdict="OK|$newest_name ($(_backup_run_when "$newest"))"
            fi
        elif [ "$lock" = held ]; then
            verdict="RUNNING|the backup run started $(_backup_run_when "$started") is still writing its archive"
        else
            verdict="FAIL|the backup run started $(_backup_run_when "$started") produced no archive — the reason is in: docker logs backup-service"
        fi
    fi

    # 2. the schedule: a run that never started leaves no report at all, so
    # the latest slot is checked on its own, whatever the run above says.
    case "$sched" in
        EVERY\ *) every=${sched#EVERY } ;;
        LAST\ *\ *) last=${sched#LAST }; prev=${last#* }; last=${last%% *}; every=$((last - prev)) ;;
        LAST\ *) last=${sched#LAST } ;;
        NONE) ;;
        *) every=93600 ;;   # the parser could not run: assume a daily schedule (26 h)
    esac
    # Evidence of the last run: its report's start, or — on a box without a
    # report yet (before its first run on this version) — its archive.
    m=${started:-0}; [ "$newest" -gt "$m" ] && m=$newest
    if [ "$lock" != held ] && [ -n "$last" ] && [ $((now - last)) -gt 900 ] \
        && [ "$m" -lt $((last - 300)) ]; then
        missed="FAIL|no backup run started at the scheduled time $(_backup_run_when "$last") (BACKUP_CRON_EXPRESSION $cron) — the reason is in: docker logs backup-service"
    fi
    # 3. #3458: the age of the newest COMPLETE archive (its completion mark),
    # on its own, whatever 1. and 2. say: after two failed nights the operator
    # needs the last restore point and its age, not only "no archive tonight".
    # The bound is the schedule's interval + 2 h (26 h for a daily schedule;
    # 26 h too when the parser cannot run). A schedule with no slot at all
    # (NONE) means scheduled backups are off: no bound. Not while a run holds
    # offen's lock — its archive is on its way.
    local stale="" cname="" cm=0 bound=""
    case "$sched" in
        NONE) ;;
        *) [ -n "$every" ] && bound=$((every + 7200)) ;;
    esac
    [ -z "$sched" ] && bound=93600
    if [ -n "$bound" ] && [ "$lock" != held ] && [ "$newest" -gt 0 ]; then
        while read -r m n; do
            [ -n "$m" ] || continue
            backup_run_archive_is_complete "$dir" "$n" "$m" || continue
            if [ "$m" -gt "$cm" ]; then cm=$m; cname=$n; fi
        done <<EOF
$(_backup_run_archives "$dir" "$fname")
EOF
        local logs="the backup log: docker logs backup-service (and docker exec backup-service cat /backup/databases/pre-backup.log); rzfz backup status"
        if [ -z "$cname" ]; then
            stale="FAIL|no backup archive is marked complete — every archive in backups/ was cut off by a failed run, so there is no restore point newer than the backup schedule allows (BACKUP_CRON_EXPRESSION $cron); see $logs"
        elif [ $((now - cm)) -gt "$bound" ]; then
            stale="FAIL|the newest complete archive, $cname ($(_backup_run_when "$cm")), is $(( (now - cm) / 3600 )) h old — older than the backup schedule allows (BACKUP_CRON_EXPRESSION $cron: at most $((bound / 3600)) h); the scheduled backup is failing or not running, see $logs"
        fi
    fi
    # Order: the missed slot, the last run's own FAIL, the stale restore point,
    # then whatever is only information — a FAIL is always first.
    if [ -n "$missed" ] || [ -n "$stale" ]; then
        [ -n "$missed" ] && echo "$missed"
        case "$verdict" in FAIL\|*) echo "$verdict"; verdict="" ;; esac
        [ -n "$stale" ] && echo "$stale"
        # The run before the missed slot (or before the last complete archive
        # went stale): still shown, but never a PASS beside a FAIL.
        case "$verdict" in
            OK\|*) verdict="INFO|the backup run before it wrote its archive: ${verdict#OK|}" ;;
            "")    [ -n "$missed" ] && [ "$newest" -gt 0 ] && [ -z "$started" ] \
                       && verdict="INFO|the newest archive: $newest_name ($(_backup_run_when "$newest"))" ;;
        esac
    elif [ -z "$verdict" ] && [ "$newest" -gt 0 ]; then
        verdict="OK|$newest_name ($(_backup_run_when "$newest"))"
    fi
    [ -n "$verdict" ] && echo "$verdict"
    return 0
}

# backup_run_archive_is_complete <backups-dir> <name> <mtime> — the archive
# guard's rule (core/backup/archive-guard.sh): complete when it carries its
# completion mark, when no mark exists yet (marks not in use), or when it
# predates the marks (.since).
backup_run_archive_is_complete() {
    local marks="$1/.rzfz-complete" f any="" since
    [ -f "$marks/$2" ] && return 0
    for f in "$marks"/*; do [ -f "$f" ] && { any=1; break; }; done
    [ -n "$any" ] || return 0
    since=$(cat "$marks/.since" 2>/dev/null || echo 0)
    case "$since" in ''|*[!0-9]*) since=0 ;; esac
    [ "$3" -le "$since" ]
}

# backup_run_quality_verdict <report-text> — #3458: a finished run whose
# archive was written but does not hold everything. Prints one "WARN|text"
# line for a DEGRADED or PARTIAL run (the razzfazz-backup wrapper's rules:
# PARTIAL = an agent volume with data is not in the archive; DEGRADED = the
# pre-backup step reported errors, warnings or failed agent snapshots);
# nothing for a clean run, a failed one (rc != 0, or an archive that began
# early: those are FAILs elsewhere) or a run still going.
backup_run_quality_verdict() {
    local report="$1" fin rc early errs warns agents detail skipped not_in started
    _brq() { printf '%s\n' "$report" | sed -n "s/^$1=//p" | head -n 1; }
    fin=$(_brq finished_epoch); rc=$(_brq rc); early=$(_brq archive_began_early)
    started=$(_brq started_epoch)
    case "$fin" in ''|*[!0-9]*) return 0 ;; esac
    [ "$rc" = 0 ] || return 0
    [ "$early" = 1 ] && return 0
    errs=$(_brq errors); warns=$(_brq warnings); agents=$(_brq agent_failures)
    case "$errs" in ''|*[!0-9]*) errs=0 ;; esac
    case "$warns" in ''|*[!0-9]*) warns=0 ;; esac
    case "$agents" in ''|*[!0-9]*) agents=0 ;; esac
    detail=$(_brq skipped_detail); skipped=$(_brq skipped_volumes)
    not_in=$(printf '%s' "$detail" | tr ',' '\n' | awk -F: 'NF >= 2 && $1 != "" && $2 != "missing_volume" {print $1}' | paste -sd, -)
    [ -n "$not_in" ] || not_in=$(printf '%s' "$skipped" | tr ',' '\n' | sed 's/:.*//' | grep -v '^$' | paste -sd, -)
    local run="the last backup run (started $(_backup_run_when "${started:-$fin}"))"
    local log="the backup log names what: docker exec backup-service cat /backup/databases/pre-backup.log (or rzfz backup status)"
    if [ -n "$not_in" ]; then
        echo "WARN|$run is PARTIAL: agent volume(s) with data are NOT in its archive: $not_in — $log"
    elif [ "$errs" -gt 0 ] || [ "$warns" -gt 0 ] || [ "$agents" -gt 0 ]; then
        echo "WARN|$run is DEGRADED: $errs error(s), $warns warning(s), $agents agent volume(s) not in its archive — the archive was written, but $log"
    fi
    return 0
}

# backup_run_unmarked_archives <backups-dir> — archives a run never finished
# (#2833). core/backup/archive-guard.sh marks every archive offen stored
# (backups/.rzfz-complete/<name>, the copy-post hook); an archive newer than
# the marks' start (.since) without its mark was written by a run that failed
# while writing it — measured on a production box: a 27.8 GB truncated
# archive that looked like the newest backup. One name per line; nothing while no mark exists yet
# (a box before its first marked run, or an offen without copy-post).
backup_run_unmarked_archives() {
    local dir="$1" marks="$1/.rzfz-complete" since f n any=""
    for f in "$marks"/*; do [ -f "$f" ] && { any=1; break; }; done
    [ -n "$any" ] || return 0
    since=$(cat "$marks/.since" 2>/dev/null || echo 0)
    case "$since" in ''|*[!0-9]*) since=0 ;; esac
    for f in "$dir"/backup-*; do
        [ -f "$f" ] || continue
        n=${f##*/}
        case "$n" in *.sha256|*.md5) continue ;; esac
        [ -f "$marks/$n" ] && continue
        [ "$(stat -c %Y "$f" 2>/dev/null || echo 0)" -gt "$since" ] || continue
        printf '%s\n' "$n"
    done
    return 0
}
