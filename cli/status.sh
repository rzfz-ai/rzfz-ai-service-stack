#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# razzfazz-status.sh — read-only assessment of stack + host posture
#
# Produces a structured PASS/WARN/FAIL/INFO/SKIP report covering: code state,
# running stack, image versions vs manifest, LLM profile coherence, network
# exposure, host hardening (ufw/fail2ban/auditd/sysctl/Docker daemon),
# secrets, SSH posture (informational), TLS, backups, and the latest audit
# report's open-finding count.
#
# No mutations. No sudo (gracefully degrades any check that needs root).
# Read-only on .env / manifests / docker / filesystem.
#
# Usage:
#   rzfz status                # full report, colored, exit 0/1
#   rzfz status --short        # one-line summary per category
#   rzfz status --json         # machine-parseable
#   rzfz status --no-color     # plain text (for log capture)
#
# Exit codes:
#   0 — all PASS or only WARN/INFO/SKIP
#   1 — at least one FAIL
#   2 — usage error
# =============================================================================
# Note: deliberately NOT using `set -u` — too aggressive in subshells that
# re-source the operator's shell snapshot (which may reference unset vars).
# We rely on explicit empty-checks instead.
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
MANIFEST_FILE="$SCRIPT_DIR/config/manifests/versions.json"
HARDENING_MARKER="/etc/razzfazz/host-hardened"

# M026 / S02 #3: source the shared library for color constants. We keep the
# script-local single-letter aliases (R/G/Y/B/N) so the existing $COLOR toggle
# (--no-color, --json) still works; D (dim/grey, 0;90m) stays local because
# lib.sh does not provide a dim equivalent.
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

# ---- Flags ------------------------------------------------------------------
SHORT=false
JSON=false
COLOR=true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --short)    SHORT=true; shift ;;
        --json)     JSON=true; COLOR=false; shift ;;
        --no-color) COLOR=false; shift ;;
        -h|--help)
            sed -n '2,/^# ===/p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# ---- Output helpers ---------------------------------------------------------
if $COLOR; then
    R="$RED"; G="$GREEN"; Y="$YELLOW"; B="$BLUE"; D='\033[0;90m'; N="$NC"
else
    R=''; G=''; Y=''; B=''; D=''; N=''
fi

# Counters (per category we track items; per item we record status + msg)
declare -a CATEGORIES=()
declare -A ITEMS_PASS=() ITEMS_WARN=() ITEMS_FAIL=() ITEMS_INFO=() ITEMS_SKIP=()
declare -A LINES=()           # CATEGORY|N → "STATUS|message"
declare -A LINE_COUNT=()      # CATEGORY → N

PASS=0; WARN=0; FAIL=0; INFO=0; SKIP=0
CURRENT_CAT=""

cat_begin() {
    CURRENT_CAT="$1"
    CATEGORIES+=("$CURRENT_CAT")
    LINE_COUNT["$CURRENT_CAT"]=0
}
add_line() {
    local status="$1" msg="$2"
    local n=${LINE_COUNT["$CURRENT_CAT"]}
    LINES["${CURRENT_CAT}|${n}"]="${status}|${msg}"
    LINE_COUNT["$CURRENT_CAT"]=$((n + 1))
    case "$status" in
        PASS) PASS=$((PASS + 1)) ;;
        WARN) WARN=$((WARN + 1)) ;;
        FAIL) FAIL=$((FAIL + 1)) ;;
        INFO) INFO=$((INFO + 1)) ;;
        SKIP) SKIP=$((SKIP + 1)) ;;
    esac
}
pass() { add_line PASS "$1"; }
warn() { add_line WARN "$1"; }
fail() { add_line FAIL "$1"; }
info() { add_line INFO "$1"; }
skip() { add_line SKIP "$1"; }

# Safe .env read (no shell sourcing — operator-edited .env carries spaces and
# shell metachars; per memory feedback_dotenv_no_source).
env_get() {
    local key="$1"
    [ -f "$ENV_FILE" ] || { echo ""; return; }
    grep "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//;s/"$//' | tr -d '\r'
}

# ============================================================================
# CATEGORY 1 — CODE STATE
# ============================================================================
cat_begin "CODE STATE"
if ! command -v git >/dev/null 2>&1 || [ ! -d "$SCRIPT_DIR/.git" ]; then
    skip "git not available or not a git repo"
else
    cd "$SCRIPT_DIR"
    version_file=$(cat VERSION 2>/dev/null | tr -d '\n')
    head_commit=$(git rev-parse --short HEAD 2>/dev/null)
    head_ref=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    if [ -n "$version_file" ]; then
        pass "VERSION file: $version_file"
    else
        warn "VERSION file empty/missing"
    fi
    if [ "$head_ref" = "HEAD" ]; then
        warn "git on detached HEAD at $head_commit (operator should checkout main if no local edits pending)"
    else
        pass "git on branch '$head_ref' at $head_commit"
    fi
    if git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
        pass "git working tree clean"
    else
        warn "git working tree has uncommitted changes ($(git status --porcelain | wc -l) entries)"
    fi

    # M033 S29 — tag-move drift detection.
    # razzfazz-init / razzfazz-upgrade record the commit a release tag resolved
    # to at deploy time in RAZZFAZZ_COMMIT. GA tags must never be re-pointed
    # (the release-cycle skill cuts ga.N+1 instead), but if one was, the same
    # tag name on origin now resolves to a *different* commit than this box
    # deployed — "v2026.05-ga.4 here" silently means different code than
    # "v2026.05-ga.4 on the dev box". Surface that so an operator never debugs
    # a phantom version mismatch again. Best-effort + offline-safe.
    recorded_version=$(env_get RAZZFAZZ_VERSION)
    recorded_commit=$(env_get RAZZFAZZ_COMMIT)
    upgrade_method=$(env_get RAZZFAZZ_UPGRADE_METHOD)
    if [ "$upgrade_method" = "offline-package" ]; then
        # #272: offline-package boxes carry a fresh VERSION/RAZZFAZZ_VERSION, but
        # the offline rsync intentionally does NOT move .git HEAD (and origin may
        # be unreachable anyway). RAZZFAZZ_COMMIT records the package's baked
        # commit, not this box's git HEAD, so a git-vs-origin tag-move comparison
        # is meaningless and produced a false "tag MOVED". Trust the version.
        pass "code version (offline-package): ${recorded_version:-unknown} — git tag-drift check skipped (#272)"
        recorded_version=""   # make the case below a no-op
    fi
    case "$recorded_version" in
        v*-ga*|*-ga.*)
            # RAZZFAZZ_VERSION is stored without the leading 'v' (from the
            # VERSION file), but git tags carry it (v2026.05-ga.4). Query both
            # forms so the lookup hits regardless of which convention is recorded.
            tag="$recorded_version"
            vtag="v${recorded_version#v}"
            # ls-remote is lightweight (no fetch); short timeout so an offline
            # or slow box just skips the check instead of hanging the status run.
            # GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS=true: NEVER prompt for git
            # credentials. origin is the auth-gated Gitea; on a customer box with
            # no cached creds, plain `git ls-remote` PROMPTS for a username on the
            # TTY and hangs (timeout can't interrupt the terminal read). With
            # prompting disabled git fails fast → the empty-result skip below
            # fires cleanly. Where creds ARE cached (vendor boxes) the check still
            # works via the non-interactive credential helper.
            remote_line=$(GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true \
                timeout 8 git ls-remote --tags origin \
                "$tag" "${tag}^{}" "$vtag" "${vtag}^{}" 2>/dev/null)
            if [ -z "$remote_line" ]; then
                skip "tag-drift check: origin unreachable or '$tag' not on origin"
            else
                # Prefer the dereferenced (^{}) line — that's the commit for an
                # annotated tag; lightweight tags only have the plain line.
                remote_commit=$(printf '%s\n' "$remote_line" | awk '/\^\{\}$/{print $1}' | head -1)
                [ -z "$remote_commit" ] && remote_commit=$(printf '%s\n' "$remote_line" | awk 'NR==1{print $1}')
                remote_short=$(printf '%s' "$remote_commit" | cut -c1-7)
                if [ -z "$recorded_commit" ] || [ "$recorded_commit" = "unknown" ]; then
                    skip "tag-drift check: no RAZZFAZZ_COMMIT recorded to compare against"
                elif [ "${remote_commit#$recorded_commit}" != "$remote_commit" ] \
                     || [ "$remote_short" = "$recorded_commit" ]; then
                    pass "tag '$tag' on origin matches deployed commit ($recorded_commit)"
                else
                    fail "tag '$tag' has MOVED on origin: deployed=$recorded_commit, origin now=$remote_short"
                    warn "  same tag name = different code. GA tags must not be re-pointed (M033 S29);"
                    warn "  reconcile by upgrading to the next ga.N rather than re-pulling this tag."
                fi
            fi
            ;;
    esac
fi

# ============================================================================
# CATEGORY 2 — STACK STATE
# ============================================================================
cat_begin "STACK STATE"
if ! command -v docker >/dev/null 2>&1; then
    fail "docker CLI not available"
else
    running=$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l)
    unhealthy=$(docker ps --format '{{.Status}}' 2>/dev/null | grep -c -E "unhealthy|Restarting" || true)
    pass "$running containers running"
    if [ "$unhealthy" -eq 0 ]; then
        pass "no unhealthy/restarting containers"
    else
        fail "$unhealthy unhealthy/restarting containers"
        while read -r l; do
            warn "$l"
        done < <(docker ps --format '  {{.Names}}: {{.Status}}' 2>/dev/null | grep -E "unhealthy|Restarting" | head -3)
    fi
    # Orphan ad-hoc containers (no compose project label) — F-RC5-4 territory.
    # Skip gpustack-spawned model pods: names ending in -pause or -run-<N>
    # are containers that gpustack creates dynamically as inference workers
    # (one pause + one or more run-N per loaded model). These legitimately
    # lack a compose project label — they're managed by gpustack, not compose.
    orphan_count=0
    orphan_names=""
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        if [ -z "$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null)" ]; then
            case "$c" in
                *-pause|*-run-[0-9]*) continue ;;  # gpustack model pod
            esac
            orphan_count=$((orphan_count + 1))
            orphan_names="$orphan_names $c"
        fi
    done
    if [ "$orphan_count" -eq 0 ]; then
        pass "no ad-hoc orphan containers (gpustack model pods excluded)"
    else
        warn "$orphan_count ad-hoc orphan container(s):${orphan_names}"
    fi
fi

# ============================================================================
# CATEGORY 3 — IMAGE VERSIONS (deployed vs manifest)
# ============================================================================
cat_begin "IMAGE VERSIONS"
if [ ! -f "$MANIFEST_FILE" ] || ! command -v python3 >/dev/null 2>&1; then
    skip "manifest or python3 unavailable"
else
    # Capture both stdout and stderr to /tmp for debugging if it goes blank
    # Pass active COMPOSE_PROFILES so the probe skips manifest entries on
    # profiles the operator hasn't enabled (e.g. `llm-legacy` rollback path
    # contains `gpustack-legacy` which would otherwise false-flag against
    # the v2.1.2 running gpustack on the `llm` profile).
    active_profiles=$(env_get COMPOSE_PROFILES)
    drift=$(MANIFEST="$MANIFEST_FILE" ACTIVE_PROFILES="$active_profiles" python3 - 2>/dev/null <<'PY'
import json, os, subprocess
manifest = os.environ["MANIFEST"]
active = set(p.strip() for p in os.environ.get("ACTIVE_PROFILES","").split(",") if p.strip())
m = json.load(open(manifest))
align = 0
drift_lines = []
try:
    r = subprocess.run(["docker","ps","--format","{{.Image}}"], capture_output=True, text=True, timeout=5)
    running = r.stdout.splitlines()
except Exception:
    running = []
for section in ("images","hardcoded"):
    for name, e in m.get(section, {}).items():
        # Skip entries whose profile isn't active (when the entry declares one)
        prof = e.get("profile")
        if prof and prof not in active and prof != "core":
            continue
        manifest_v = str(e.get("current",""))
        repo = (e.get("image","") or "").split(":", 1)[0]
        if not manifest_v or not repo:
            continue
        # Match running images against this repo (allow optional -cpu form)
        matched = [i for i in running if i.startswith(repo + ":") or i == repo]
        if not matched:
            continue
        for img in matched:
            tag = img.split(":", 1)[1] if ":" in img else ""
            if tag in (manifest_v, manifest_v + "-cpu") or tag.startswith(manifest_v):
                align += 1
                break
            else:
                drift_lines.append(f"DRIFT|{name}: {img} (manifest expects :{manifest_v})")
                break
print(f"COUNT|{align}")
for l in drift_lines:
    print(l)
PY
)
    align=$(echo "$drift" | grep '^COUNT|' | cut -d'|' -f2)
    drift_n=$(echo "$drift" | grep -c '^DRIFT|' || true)
    if [ -z "$align" ]; then
        warn "image-version probe produced no output (manifest schema mismatch?)"
    elif [ "$drift_n" -eq 0 ]; then
        pass "all $align comparable services aligned with manifest"
    else
        # Use process substitution (not pipe) so `fail` updates the parent
        # shell's counters/arrays — `cmd | while` runs the loop in a subshell
        # whose state is discarded on close.
        while IFS='|' read -r _ msg; do
            fail "$msg"
        done < <(echo "$drift" | grep '^DRIFT|')
    fi
fi

# ============================================================================
# CATEGORY 4 — LLM PROFILE COHERENCE
# ============================================================================
cat_begin "LLM PROFILE"
profiles=$(env_get COMPOSE_PROFILES)
hardware=$(env_get HARDWARE)
compose_file=$(env_get COMPOSE_FILE)
case ",$profiles," in
    *,llm,*)
        pass "COMPOSE_PROFILES contains 'llm' (M018 unified path)"
        case "$hardware" in
            amd|nvidia|cpu)
                pass "HARDWARE=$hardware"
                if echo "$compose_file" | grep -q "compose.devices.${hardware}.yml"; then
                    pass "COMPOSE_FILE includes correct device overlay"
                else
                    fail "COMPOSE_FILE='$compose_file' missing compose.devices.${hardware}.yml"
                fi
                # F-RC5-6 (rc6.5 update): the `-cpu` slim variant of the
                # upstream gpustack image was discontinued when v2.x
                # consolidated to a single unified tag. The pre-rc6.5 check
                # here flagged a CPU box running `gpustack/gpustack:v2.1.2`
                # as wrong, when it is in fact the only correct tag (the
                # `-cpu` form returns `manifest unknown` from Docker Hub).
                # F-RC5-6 stays open as residual CVE-debt for CPU
                # deployments — see releases/2026.05-rc6.5/RELEASE_NOTES.md.
                if [ "$hardware" = "cpu" ] && command -v docker >/dev/null 2>&1; then
                    img=$(docker inspect gpustack --format '{{.Config.Image}}' 2>/dev/null)
                    if [ -n "$img" ]; then
                        case "$img" in
                            gpustack/gpustack:v2.*-cpu|gpustack/gpustack:v2.*-rocm|gpustack/gpustack:v2.*-cuda)
                                fail "gpustack image='$img' uses a v2.x hardware-suffixed tag that does not exist on Docker Hub — should be the unified ':v2.<patch>'"
                                ;;
                            gpustack/gpustack:v0.7.1-cpu)
                                pass "gpustack image is the legacy slim CPU variant ($img) — fine on the stable llm-cpu profile"
                                ;;
                            gpustack/gpustack:v2.*)
                                pass "gpustack image is the upstream unified ($img); F-RC5-6 CVE-debt accepted as residual"
                                ;;
                            *)
                                info "gpustack image='$img' (non-standard tag — verify is intentional)"
                                ;;
                        esac
                    fi
                fi
                ;;
            "") fail "HARDWARE not set in .env" ;;
            *)  fail "HARDWARE='$hardware' invalid (expected amd|nvidia|cpu)" ;;
        esac
        ;;
    *,llm-box,*|*,llm-experimental,*)
        fail "legacy LLM profile in COMPOSE_PROFILES — should be migrated to 'llm' (M018 / S06.6)"
        ;;
    *,llm-cpu,*)
        pass "COMPOSE_PROFILES on 'llm-cpu' (stable CPU default — M029-S04, un-deprecated)"
        ;;
    *,llm-legacy,*)
        warn "COMPOSE_PROFILES on 'llm-legacy' (rollback safety net) — should move to 'llm' once stable"
        ;;
    *)
        skip "no LLM profile active"
        ;;
esac

# ============================================================================
# CATEGORY 5 — NETWORK EXPOSURE
# ============================================================================
cat_begin "NETWORK EXPOSURE"
if ! command -v docker >/dev/null 2>&1; then
    skip "docker unavailable"
else
    # Acceptable 0.0.0.0 binds: caddy 80/443, gitea SSH 2222
    bad_binds=""
    while read line; do
        [ -z "$line" ] && continue
        name=$(echo "$line" | awk '{print $1}')
        ports=$(echo "$line" | cut -d$'\t' -f2-)
        # Look for 0.0.0.0:<port>-> patterns
        for bind in $(echo "$ports" | grep -oE "0\.0\.0\.0:[0-9-]+->[0-9-]+/(tcp|udp)" || true); do
            host_port=$(echo "$bind" | sed -E 's|0\.0\.0\.0:([0-9-]+)->.*|\1|')
            case "$name" in
                caddy)
                    case "$host_port" in
                        80|443) ;;
                        *) bad_binds="$bad_binds\n$name $bind (unexpected port)" ;;
                    esac
                    ;;
                gitea)
                    case "$host_port" in
                        2222) ;;
                        *) bad_binds="$bad_binds\n$name $bind (unexpected port)" ;;
                    esac
                    ;;
                *)
                    bad_binds="$bad_binds\n$name $bind"
                    ;;
            esac
        done
    done < <(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null)
    if [ -z "$bad_binds" ]; then
        pass "only intended LAN binds (caddy 80/443, gitea 2222)"
    else
        while IFS= read -r b; do
            [ -n "$b" ] && fail "unexpected LAN bind: $b"
        done < <(echo -e "$bad_binds" | tail -n +2)
    fi
    # F-RC5-1 specific check: gpustack admin UI loopback-only
    gpustack_ports=$(docker port gpustack 2>/dev/null || true)
    if [ -n "$gpustack_ports" ]; then
        if echo "$gpustack_ports" | grep -q "0\.0\.0\.0"; then
            fail "gpustack ports bound on 0.0.0.0 (F-RC5-1 NOT closed — set GPUSTACK_HOST_BIND=127.0.0.1)"
        else
            pass "gpustack on 127.0.0.1 only (F-RC5-1 closed)"
        fi
    fi
fi

# ============================================================================
# CATEGORY — OFFLINE / NETWORK (#184 P2)
# ============================================================================
# The box's egress axis (online|proxied|offline) + registry mirror, which
# overlays are composed, and the verify-images / verify-models present/missing
# counts. lib.sh (sourced above) provides razzfazz_network_mode, the overlay
# constants, and compose_file_overlay_present.
cat_begin "OFFLINE / NETWORK"
net_mode=$(razzfazz_network_mode "$ENV_FILE" 2>/dev/null)
case "$net_mode" in
    offline) info "network mode: offline (no internet egress; LAN stays up)" ;;
    proxied) info "network mode: proxied (egress via corporate proxy)" ;;
    online)  info "network mode: online (direct egress — default)" ;;
    *)        info "network mode: ${net_mode:-unknown}" ;;
esac
offline_flag=$(env_get RAZZFAZZ_OFFLINE)
info "RAZZFAZZ_OFFLINE=${offline_flag:-0}"
mirror=$(env_get RAZZFAZZ_REGISTRY_MIRROR)
if [ -n "$mirror" ]; then
    info "registry mirror: $mirror (install/upgrade pulls redirected)"
else
    info "registry mirror: none (upstream registries)"
fi
# Overlays composed (COMPOSE_FILE chain)
if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_NOBUILD_OVERLAY"; then
    pass "no-build overlay composed (up/enable/disable never build)"
else
    warn "no-build overlay NOT composed (compose.no-build.yml) — 'rzfz upgrade' wires it"
fi
if [ "$net_mode" = "offline" ]; then
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_OFFLINE_OVERLAY"; then
        pass "offline overlay composed (pull_policy: never + telemetry off)"
    else
        warn "offline mode but compose.offline.yml not composed — 'rzfz setup --network-mode --mode offline'"
    fi
fi
if [ -n "$mirror" ]; then
    if compose_file_overlay_present "$ENV_FILE" "$RAZZFAZZ_REGISTRY_MIRROR_OVERLAY"; then
        pass "registry-mirror overlay composed"
    else
        warn "RAZZFAZZ_REGISTRY_MIRROR set but compose.registry-mirror.yml not composed"
    fi
fi
# verify-images / verify-models present/missing counts (best-effort — needs docker;
# `timeout` guards a large image set). Same summary line the release gate parses.
if command -v docker >/dev/null 2>&1; then
    for tool in verify-images verify-models; do
        vscript="$SCRIPT_DIR/cli/${tool}.sh"
        if [ ! -x "$vscript" ] && [ ! -f "$vscript" ]; then
            skip "$tool: script not present"
            continue
        fi
        vout=$(timeout 90 bash "$vscript" --quiet 2>&1)
        vline=$(echo "$vout" | grep -oE '[0-9]+ expected, [0-9]+ present, [0-9]+ missing' | head -1)
        if [ -n "$vline" ]; then
            v_exp=$(echo "$vline" | awk '{print $1}')
            v_pres=$(echo "$vline" | awk '{print $3}')
            v_miss=$(echo "$vline" | awk '{print $5}')
            if [ "${v_miss:-1}" -eq 0 ]; then
                pass "$tool: $v_pres/$v_exp present, 0 missing (offline-ready)"
            else
                fail "$tool: $v_miss of $v_exp missing (not offline-ready)"
            fi
        else
            skip "$tool: could not determine (enumeration degraded / no docker exec)"
        fi
    done
else
    skip "verify-images/verify-models need docker (unavailable)"
fi

# ============================================================================
# CATEGORY 6 — HOST HARDENING
# ============================================================================
cat_begin "HOST HARDENING"
if [ -f "$HARDENING_MARKER" ]; then
    ts=$(grep '^hardened_at=' "$HARDENING_MARKER" | cut -d= -f2-)
    pass "host-hardened marker present ($ts)"
else
    fail "host-hardened marker missing — scripts/harden-host.sh has not been run"
fi
# UFW (needs sudo to query status; degrade gracefully)
if command -v ufw >/dev/null 2>&1; then
    ufw_out=$(sudo -n ufw status 2>/dev/null | head -1 || true)
    if [ -z "$ufw_out" ]; then
        # check the systemd unit instead — works without sudo
        case "$(systemctl is-active ufw 2>/dev/null)" in
            active) pass "ufw active (via systemctl)" ;;
            *)      fail "ufw inactive" ;;
        esac
    else
        case "$ufw_out" in
            *active*|*Aktiv*|*aktiv*) pass "ufw active" ;;
            *)                        fail "ufw inactive" ;;
        esac
    fi
fi
# fail2ban
if command -v systemctl >/dev/null 2>&1; then
    case "$(systemctl is-active fail2ban 2>/dev/null)" in
        active) pass "fail2ban active" ;;
        *) fail "fail2ban inactive" ;;
    esac
    case "$(systemctl is-active auditd 2>/dev/null)" in
        active) pass "auditd active" ;;
        *) warn "auditd inactive (F-071)" ;;
    esac
    case "$(systemctl is-active unattended-upgrades 2>/dev/null)" in
        active) pass "unattended-upgrades active" ;;
        *) warn "unattended-upgrades inactive (F-058)" ;;
    esac
fi
# sysctl files
if [ -f /etc/sysctl.d/99-razzfazz-stability.conf ]; then
    pass "sysctl stability tunables present (M018/S03.5)"
else
    warn "sysctl stability tunables missing (run scripts/harden-host.sh OR sudo cp core/sysctl/...)"
fi
if [ -f /etc/sysctl.d/99-razzfazz-hardening.conf ]; then
    pass "sysctl hardening tunables present (F-060)"
else
    warn "sysctl hardening tunables missing (F-060)"
fi
# Docker daemon
if [ -f /etc/docker/daemon.json ]; then
    if grep -q "no-new-privileges" /etc/docker/daemon.json 2>/dev/null; then
        pass "Docker daemon has no-new-privileges (F-059)"
    else
        warn "Docker daemon present but missing no-new-privileges (F-059)"
    fi
else
    warn "/etc/docker/daemon.json absent (F-059)"
fi

# ============================================================================
# CATEGORY 7 — SECRETS
# ============================================================================
cat_begin "SECRETS"
required_secrets="AUTHENTIK_BOOTSTRAP_PASSWORD AUTHENTIK_SECRET_KEY POSTGRES_PASSWORD VALKEY_PASSWORD WEBUI_SECRET_KEY"
for k in $required_secrets; do
    v=$(env_get "$k")
    if [ -z "$v" ]; then
        fail "$k empty (required)"
    fi
done
backup_pw=$(env_get BACKUP_ENCRYPTION_PASSWORD)
if [ -n "$backup_pw" ]; then
    pass "BACKUP_ENCRYPTION_PASSWORD set (F-A2 closed)"
else
    fail "BACKUP_ENCRYPTION_PASSWORD empty (F-A2)"
fi
gpu_key=$(env_get GPUSTACK_API_KEY)
case "$gpu_key" in
    ""|*CHANGEME*|*PLACEHOLDER*) fail "GPUSTACK_API_KEY is placeholder/empty — init-backends.py won't authenticate" ;;
    *) pass "GPUSTACK_API_KEY non-placeholder (length ${#gpu_key})" ;;
esac
# count empty secrets in general
empty_secrets=0
while IFS= read -r line; do
    case "$line" in
        *PASSWORD=|*SECRET=|*TOKEN=|*KEY=) empty_secrets=$((empty_secrets + 1)) ;;
    esac
done < <(grep -E '^[A-Z_]+=$' "$ENV_FILE" 2>/dev/null)
if [ "$empty_secrets" -eq 0 ]; then
    pass "no empty *_PASSWORD/SECRET/TOKEN/KEY entries in .env"
else
    info "$empty_secrets empty secret-shaped entries in .env (may be optional / per-profile)"
fi

# ============================================================================
# CATEGORY 8 — SSH POSTURE (informational only)
# ============================================================================
cat_begin "SSH POSTURE"
if [ -r /etc/ssh/sshd_config ]; then
    pa=$(grep -E '^[[:space:]]*PasswordAuthentication[[:space:]]+' /etc/ssh/sshd_config 2>/dev/null | tail -1 | awk '{print $2}')
    pr=$(grep -E '^[[:space:]]*PermitRootLogin[[:space:]]+' /etc/ssh/sshd_config 2>/dev/null | tail -1 | awk '{print $2}')
    case "${pa:-default-yes}" in
        no) info "PasswordAuthentication=no (key-only)" ;;
        *)  info "PasswordAuthentication=${pa:-default-yes} (operator choice for dev/test boxes)" ;;
    esac
    case "${pr:-default-prohibit-password}" in
        no) info "PermitRootLogin=no" ;;
        *)  info "PermitRootLogin=${pr:-default-prohibit-password}" ;;
    esac
else
    skip "sshd_config not readable (need sudo)"
fi
# authorized_keys count for STACK_USER
stack_user=$(env_get STACK_USER)
[ -z "$stack_user" ] && stack_user="razzfazz-ai-admin"
ak="/home/$stack_user/.ssh/authorized_keys"
if [ -r "$ak" ]; then
    n=$(grep -c '^ssh-' "$ak" 2>/dev/null | head -1 | tr -d '\n ')
    info "${n:-0} pubkey(s) in $stack_user's authorized_keys"
else
    info "no authorized_keys for $stack_user (or not readable)"
fi

# ============================================================================
# CATEGORY 9 — TLS
# ============================================================================
cat_begin "TLS"
domain=$(env_get MAIN_DOMAIN)
tls_mode=$(env_get TLS_MODE)
if [ -z "$domain" ]; then
    skip "MAIN_DOMAIN not set"
else
    info "MAIN_DOMAIN=$domain  TLS_MODE=${tls_mode:-letsencrypt}"
    if command -v openssl >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
        cert_info=$(echo | openssl s_client -servername "$domain" -connect "127.0.0.1:443" 2>/dev/null | openssl x509 -noout -dates 2>/dev/null || true)
        if [ -n "$cert_info" ]; then
            not_after=$(echo "$cert_info" | grep notAfter | cut -d= -f2-)
            pass "TLS cert present, expires: $not_after"
        else
            warn "could not retrieve TLS cert from caddy (may be starting up)"
        fi
    fi
fi

# ============================================================================
# CATEGORY 10 — BACKUPS
# ============================================================================
cat_begin "BACKUPS"
backup_dir="$SCRIPT_DIR/backups"
if [ -d "$backup_dir" ]; then
    # Look only for actual backup tarballs (skip env-snapshots/ subdir etc)
    last=$(ls -1t "$backup_dir"/*.tar.gz* 2>/dev/null | head -1)
    if [ -n "$last" ]; then
        fname=$(basename "$last")
        last_ts=$(stat -c '%y' "$last" 2>/dev/null | cut -d. -f1)
        case "$fname" in
            *.gpg) pass "last backup: $fname (GPG-encrypted, $last_ts)" ;;
            *)     fail "last backup: $fname NOT GPG-encrypted ($last_ts) — F-A2 risk" ;;
        esac
    else
        warn "no .tar.gz backup files in $backup_dir — first backup hasn't run yet"
    fi
else
    info "no backups/ directory (first install)"
fi

# ============================================================================
# CATEGORY 11 — AUDIT POSTURE
# ============================================================================
cat_begin "AUDIT POSTURE"
latest_audit=$(ls -1t "$SCRIPT_DIR/security-run/"*assessment*.md 2>/dev/null | head -1)
if [ -n "$latest_audit" ]; then
    audit_date=$(basename "$latest_audit" | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' | head -1)
    info "latest audit: $(basename "$latest_audit")"
    if [ -n "$audit_date" ]; then
        days_old=$(( ( $(date +%s) - $(date -d "$audit_date" +%s) ) / 86400 ))
        if [ "$days_old" -gt 30 ]; then
            warn "audit is $days_old days old (>30 — recommend re-run)"
        else
            pass "audit is $days_old days old"
        fi
    fi
    # Count critical/high lines roughly (simple heuristic)
    crit=$(grep -cE '^\| [0-9]+ \| \*\*F-[A-Z0-9-]+\*\*.*\| Critical' "$latest_audit" 2>/dev/null || echo 0)
    high=$(grep -cE '^\| [0-9]+ \| \*\*F-[A-Z0-9-]+\*\*.*\| High' "$latest_audit" 2>/dev/null || echo 0)
    info "top-N table indicates ~$crit Critical, ~$high High items in latest audit"
else
    info "no security audit report found in security-run/"
fi

# ============================================================================
# RENDER
# ============================================================================
echo ""
if $JSON; then
    # JSON output
    printf '{\n  "summary": {"pass": %d, "warn": %d, "fail": %d, "info": %d, "skip": %d},\n  "categories": [\n' \
        "$PASS" "$WARN" "$FAIL" "$INFO" "$SKIP"
    first_cat=true
    for cat in "${CATEGORIES[@]}"; do
        $first_cat && first_cat=false || printf ',\n'
        printf '    {"name": %q, "items": [' "$cat"
        n=${LINE_COUNT["$cat"]}
        first_item=true
        for ((i=0; i<n; i++)); do
            line="${LINES["${cat}|${i}"]}"
            status="${line%%|*}"; msg="${line#*|}"
            $first_item && first_item=false || printf ', '
            printf '{"status": "%s", "msg": %q}' "$status" "$msg"
        done
        printf ']}'
    done
    printf '\n  ]\n}\n'
    exit $([ "$FAIL" -gt 0 ] && echo 1 || echo 0)
fi

# Human-readable rendering
host=$(hostname)
ip=$(hostname -I 2>/dev/null | awk '{print $1}')
date_now=$(date '+%Y-%m-%d %H:%M:%S')
echo -e "${B}rzfz.ai stack — assessment ${date_now}${N}"
echo -e "${D}host: $host ($ip)  user: $(id -un)${N}"
echo ""

for i in "${!CATEGORIES[@]}"; do
    cat="${CATEGORIES[$i]}"
    n=${LINE_COUNT["$cat"]}
    [ "$n" -eq 0 ] && continue
    num=$((i + 1))
    echo -e "${B}[$num] $cat${N}"
    if $SHORT; then
        # one-line summary per category
        c_pass=0; c_warn=0; c_fail=0; c_info=0; c_skip=0
        for ((j=0; j<n; j++)); do
            line="${LINES["${cat}|${j}"]}"
            status="${line%%|*}"
            case "$status" in
                PASS) c_pass=$((c_pass+1));;
                WARN) c_warn=$((c_warn+1));;
                FAIL) c_fail=$((c_fail+1));;
                INFO) c_info=$((c_info+1));;
                SKIP) c_skip=$((c_skip+1));;
            esac
        done
        echo -e "  ${G}${c_pass}P${N} ${Y}${c_warn}W${N} ${R}${c_fail}F${N} ${B}${c_info}i${N} ${D}${c_skip}-${N}"
    else
        for ((j=0; j<n; j++)); do
            line="${LINES["${cat}|${j}"]}"
            status="${line%%|*}"; msg="${line#*|}"
            case "$status" in
                PASS) printf "  ${G}✓${N} %s\n" "$msg" ;;
                WARN) printf "  ${Y}!${N} %s\n" "$msg" ;;
                FAIL) printf "  ${R}✗${N} %s\n" "$msg" ;;
                INFO) printf "  ${B}i${N} %s\n" "$msg" ;;
                SKIP) printf "  ${D}-${N} %s${N}\n" "$msg" ;;
            esac
        done
    fi
    echo ""
done

echo -e "${B}Summary:${N} ${G}${PASS} PASS${N}  ${Y}${WARN} WARN${N}  ${R}${FAIL} FAIL${N}  ${B}${INFO} info${N}  ${D}${SKIP} n/a${N}"
[ "$FAIL" -gt 0 ] && exit 1 || exit 0
