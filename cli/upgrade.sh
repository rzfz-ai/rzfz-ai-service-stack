#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# rzfz.ai Stack - Upgrade Script
# ==============================================================================
# Upgrades the rzfz.ai service stack from the installed version to a
# target version. Supports online (git pull) and offline (upgrade package)
# modes. Automatically migrates .env and .env.dify based on the migration
# manifest in config/migrations/env-changes.json.
#
# Usage:
#   rzfz upgrade                        # Upgrade to latest (git pull)
#   rzfz upgrade --target v1.1.0        # Upgrade to specific tag
#   rzfz upgrade --package upgrade.tar.gz  # Offline upgrade from package
#   rzfz upgrade --check                # Dry run: show what would change
#   rzfz upgrade --help                 # Show all options
#
# ==============================================================================

set -eo pipefail

# #2543: every repo file this run writes (a git checkout, a package's files,
# generated config) must be readable by the containers that bind-mount it.
# Under a CIS-hardened umask (027, or 077) git writes a changed file 0640/0600,
# and a non-root container whose uid is not the owner then cannot read it:
# on 0.91 razzfazz-help crash-looped on versions.json after `rzfz upgrade`.
# Secrets never relied on the umask; they are chmod'ed 600/700 explicitly.
umask 022

# ------------------------------------------------------------------------------
# #427: unconditional run log. Every upgrade run tees its full output to a
# timestamped file — "did the image load fail, or was it skipped?" must never
# again require re-running a 30-minute provisioning step because the console
# scrollback is gone. Skipped when a parent razzfazz process already captures
# (upgrade re-exec), so one run = one log.
# --help/-h/help runs stay un-teed: the command-reference generator captures
# their stdout verbatim (a tee process-substitution can swallow it on fast
# exit), and a help call must not mint a junk run log.
_rzfz_wants_help=false
for _rzfz_a in "$@"; do case "$_rzfz_a" in -h|--help|help) _rzfz_wants_help=true ;; esac; done
if [ "$_rzfz_wants_help" = false ] && [ -z "${RZFZ_RUN_LOG:-}" ]; then
    RZFZ_LOG_DIR="${RZFZ_LOG_DIR:-$HOME/.razzfazz/logs}"
    # review #653: logs can carry env-adjacent output (set -x under
    # DEBUG_MODE would land here in full) — operator-only.
    # #2138: a check keeps its run logs in a bucket of its own, so ten
    # --check runs never push a real upgrade's run log out of the retention.
    # Real runs are razzfazz-upgrade-<timestamp>.log (the [0-9] in the glob
    # leaves the check bucket alone), checks razzfazz-upgrade-check-*.log.
    _rzfz_bucket="razzfazz-upgrade-"
    for _rzfz_a in "$@"; do case "$_rzfz_a" in --check|--dry-run|--_preview) _rzfz_bucket="razzfazz-upgrade-check-" ;; esac; done
    export RZFZ_LOG_DIR  # nested runs keep their kept files here, not in /tmp
    # #2697: the log is the operator's alone (0600 whatever the umask, set just
    # before the tee), and the fallback gets an
    # unpredictable mktemp name in TMPDIR, never a guessable /tmp path.
    _rzfz_ts="$(date -u +%Y%m%dT%H%M%SZ)"
    if mkdir -p "$RZFZ_LOG_DIR" 2>/dev/null && chmod 700 "$RZFZ_LOG_DIR" 2>/dev/null; then
        RZFZ_RUN_LOG="$RZFZ_LOG_DIR/${_rzfz_bucket}${_rzfz_ts}.log"
    else
        RZFZ_LOG_DIR="${TMPDIR:-/tmp}"
        RZFZ_RUN_LOG="$(mktemp --tmpdir="$RZFZ_LOG_DIR" --suffix=.log "${_rzfz_bucket}${_rzfz_ts}-XXXXXX")" || RZFZ_RUN_LOG=/dev/null
    fi
    export RZFZ_RUN_LOG
    # retention: keep the last 10 runs per verb (and per bucket)
    # #667: on the verb's first-ever run the glob matches nothing → ls exits 2
    # → pipefail + set -e killed the script BEFORE any output. Cleanup must
    # never kill the run.
    ls -1t "$RZFZ_LOG_DIR"/"${_rzfz_bucket}"[0-9]*.log 2>/dev/null | tail -n +11 | xargs -r rm -f -- || true
    # #2697: 0600 before the tee opens it, and after the trim above, so this
    # run's own log never counts against the ten kept.
    if [ "$RZFZ_RUN_LOG" != /dev/null ]; then (umask 077; : >>"$RZFZ_RUN_LOG") 2>/dev/null || true; chmod 600 "$RZFZ_RUN_LOG" 2>/dev/null || true; fi
    exec > >(tee -a "$RZFZ_RUN_LOG") 2>&1
    echo "[log] full run log: $RZFZ_RUN_LOG"
    trap 'echo "[log] full run log: $RZFZ_RUN_LOG"' EXIT
fi

# Script directory. NOTE (#34/#26 cli/ move): this file lives in cli/, but
# SCRIPT_DIR must remain the REPO ROOT (one level up) — every downstream
# ${SCRIPT_DIR}/... path (scripts/lib.sh, .env, modules/…) is repo-root-relative.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
# #2706: the GIT_ASKPASS helper of a token typed in for this run — this
# upgrader's own copy (no temp file: nothing to clean up, nothing on TMPDIR).
RZFZ_GIT_ASKPASS_HELPER="${SCRIPT_DIR}/scripts/git-askpass-env.sh"

# Upgrade log — opt into lib.sh's RAZZFAZZ_LOG_FILE feature so every print_*
# call also appends an ANSI-stripped, timestamped line to .upgrade.log. This
# subsumes the historical per-print_* `>> "$UPGRADE_LOG"` tee that lived in
# this script's own print_* helpers.
UPGRADE_LOG="${SCRIPT_DIR}/.upgrade.log"
export RAZZFAZZ_LOG_FILE="$UPGRADE_LOG"

# ==============================================================================
# M026 / S02 #9: source the shared library for colors, print_*,
# read_env_value, update_env_value, and check_container.
#
# Local definitions kept further down (intentional name-collision overrides):
#   - print_banner   — upgrade-specific banner text/box.
#   - migrate_env    — upgrade-specific .env migration logic with rc6.2 fixes
#                      and env-snapshot side-effects (DO NOT touch).
#   - restart_stack  — upgrade-specific orchestrator (DO NOT touch).
#   - verify_upgrade — upgrade-specific health verification.
#   - generate_secret_upgrade / generate_password_upgrade /
#     generate_hex_secret_upgrade — thin re-exports of lib's generators under
#     their suffixed names so all 71 existing call sites stay byte-identical.
# ==============================================================================
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck source=scripts/lib-journal.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-journal.sh"
# #908 follow-up: the OWUI retrieval-defaults reconcile is the SAME table +
# push post-install uses — one implementation, sourced by both.
# shellcheck source=scripts/lib-owui.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-owui.sh"
# #2578: the Synapse config re-render an upgrade owes a box that runs Matrix.
# shellcheck source=scripts/lib-matrix.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib-matrix.sh"

# ==============================================================================
# Utility Functions (consistent with razzfazz-init.sh)
# ==============================================================================
print_banner() {
    echo -e "${CYAN}"
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║                                                                  ║"
    echo "║                 rzfz.ai Service Stack Upgrade                    ║"
    echo "║                                                                  ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ==============================================================================
# Help
# ==============================================================================
show_help() {
    echo "rzfz.ai Stack - Upgrade Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Upgrade Modes:"
    echo "  (no options)                 Pull latest from current git branch"
    echo "  --target TAG                 Checkout specific git tag (e.g., v1.1.0)"
    echo "  --package FILE               Offline upgrade from .tar.gz package"
    echo "  --network-mode MODE          Set the egress posture for this box"
    echo "                               (online|proxied|offline) before the upgrade"
    echo "                               builds or pulls. An air-gapped box MUST be"
    echo "                               offline or the build reaches a registry it"
    echo "                               cannot see. NOT implied by --package: a"
    echo "                               package is a cache and is used on online"
    echo "                               boxes too."
    echo "  --expect-sha256 HASH         (with --package) Verify the archive against a"
    echo "                               hash transported OUTSIDE it — release notes or"
    echo "                               fleet channel. MANIFEST.sha256 rides inside the"
    echo "                               package and cannot prove it is genuine (#781)."
    echo "  --model-bundle FILE          (with --package) Also stage this hardware class's"
    echo "                               model bundle (razzfazz-<version>-models-<class>"
    echo "                               .tar.gz) into the models volume. A bundle of"
    echo "                               another class than this box's is refused before"
    echo "                               anything changes."
    echo "  --model-bundle-sha256 HASH   (with --model-bundle) The bundle's SHA-256 from"
    echo "                               the release notes or fleet channel (#781)."
    echo ""
    echo "Migrations Only (data restored from an older release):"
    echo "  --migrations-only --from VERSION"
    echo "                               Run only the migration and reconcile steps of"
    echo "                               VERSION -> this tree on the tree and stack as"
    echo "                               they are: the env-changes.json blocks, the DB"
    echo "                               role/ownership reconcile, the profile and"
    echo "                               compose migrations, the Authentik two-hop check"
    echo "                               and the data migrations (the stack is started"
    echo "                               for those). No code update, no image pull or"
    echo "                               build, no backup, no host changes. Refuses a"
    echo "                               VERSION newer than this tree or one it cannot"
    echo "                               order. Safe to re-run. With --check: the plan."
    echo "                               Needs --force without a terminal. Containers it"
    echo "                               starts or recreates: the stack (rzfz start),"
    echo "                               authentik-server/-worker (the 2025.12 hop, only"
    echo "                               for older Authentik data), authentik-init (the"
    echo "                               blueprints, when behind) and the project up that"
    echo "                               releases its dependents, openwebui (OAuth sign-up"
    echo "                               repair), komodo-core (auto-prune off), vaultwarden"
    echo "                               (after its SQLite migration). A box still on a"
    echo "                               retired LLM profile also loses those containers."
    echo "  --from-env FILE              (with --migrations-only) The .env the data came"
    echo "                               with: its AUTHENTIK_VERSION, DIFY_VERSION and"
    echo "                               OPENWEBUI_VERSION are the data's app versions."
    echo ""
    echo "Update Mode (container versions only):"
    echo "  --update                     Apply vendor-curated container version bumps"
    echo "  --update --check             Show what would change without applying"
    echo "  --update --file FILE         Apply from a local manifest file (offline)"
    echo "  --update --rollback          Restore previous container versions"
    echo ""
    echo "Options:"
    echo "  --check, --dry-run           Dry run: show changes without applying."
    echo "                               Writes nothing on this box but .upgrade.log."
    echo "                               It may fetch the --target branch from origin,"
    echo "                               which updates only git's record of the remote."
    echo "                               Another release is previewed by that release's"
    echo "                               own upgrader, run in a temporary copy of it."
    echo "                               Its run log goes to ~/.razzfazz/logs as"
    echo "                               razzfazz-upgrade-check-*.log (last 10 kept),"
    echo "                               apart from the logs of real upgrade runs."
    echo "  --status                     Read-only posture check (delegates to"
    echo "                               razzfazz-status.sh; passes through its flags)"
    echo "  --skip-backup                Skip pre-upgrade backup (not recommended)."
    echo "                               The only way past a pre-upgrade backup that"
    echo "                               cannot run, fails, or reports errors."
    echo "  --stop-stack-for-build       Stop the stack during the image-build phase"
    echo "                               (frees RAM on small boxes; the stack is"
    echo "                               brought back up afterwards, also when the"
    echo "                               build fails). An interactive run is offered"
    echo "                               this when a source build is pending; an"
    echo "                               unattended run never stops the stack on its own."
    echo "  --skip-verify                Skip post-upgrade health verification"
    echo "  --no-engine-relaunch         Leave the LLM engines as they are. By default the"
    echo "                               upgrade ends by switching every engine of this box"
    echo "                               that is not on the release's runner or not hardened"
    echo "                               (LLM Manager runner switch: the old engine serves"
    echo "                               until the new one is healthy when the weights fit"
    echo "                               twice, else the model is down while it reloads; an"
    echo "                               engine that does not come up goes back to its"
    echo "                               previous runner)."
    echo "                               Run it later with: rzfz relaunch-engines"
    echo "  --skip-host-updates          Skip sysctl + host-hardening installs (no sudo needed)."
    echo "                               Required for non-interactive runs (nohup / SSH disowned)."
    echo "  --force                      Skip confirmation prompts"
    echo "  --allow-downgrade            Permit a target OLDER than the installed version (#1587)"
    echo "  --rollback                   Roll back the last upgrade"
    echo "  --with-acceptance            (M032-S06) After successful upgrade, wait"
    echo "                               for containers to become healthy (5 min)"
    echo "                               then run rzfz test --ci-mode"
    echo "                               --acceptance all. Probe failures → exit 4"
    echo "                               (distinct from 1 = upgrade-failed). Tests"
    echo "                               only currently-enabled profiles."
    echo "  --with-acceptance-include-disabled"
    echo "                               Same as --with-acceptance but ALSO cycles"
    echo "                               each disabled profile up → probe → down"
    echo "                               (~5 min/profile; intended for handover)."
    echo "                               Override healthz timeout with"
    echo "                               RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT=600 (s)."
    echo "  -h, --help                   Show this help"
    echo ""
    echo "Environment Migration:"
    echo "  The upgrade script automatically migrates .env and .env.dify based"
    echo "  on the migration manifest in config/migrations/env-changes.json."
    echo "  User-customized values are preserved. New variables are added with"
    echo "  defaults. Removed variables trigger a warning."
    echo ""
    echo "Examples:"
    echo "  $0                                    # Pull latest, upgrade"
    echo "  $0 --target v1.2.0                    # Upgrade to v1.2.0"
    echo "  $0 --check --target v1.2.0            # Preview v1.2.0 changes"
    echo "  $0 --package razzfazz-v1.2.0.tar.gz   # Offline upgrade"
    echo "  $0 --package razzfazz-v1.2.0.tar.gz --expect-sha256 3b1f...c9a2"
    echo "                                        # Offline upgrade, authenticity checked"
    echo "  $0 --rollback                          # Undo last upgrade"
    echo "  $0 --update                            # Apply latest container updates"
    echo "  $0 --update --check                    # Preview container updates"
    echo "  $0 --migrations-only --from 2026.09-ga.5 --from-env /path/source.env"
    echo "                                        # Migrate restored data to this tree"
    echo ""
    exit 0
}

# ==============================================================================
# .env Helper
# ==============================================================================
# update_env_value and read_env_value are provided by scripts/lib.sh sourced
# at the top of this file. lib.sh's read_env_value is the canonical rc6.2-fixed
# implementation originally lifted from this script (handles double- AND
# single-quoted values + inline-comment stripping); lib.sh's update_env_value
# uses `|` as the sed delimiter so values containing `/` (paths, URLs) work
# correctly without per-call escape rules.

# ==============================================================================
# Version Comparison
# ==============================================================================
# Supports CalVer (YYYY.MM-rcN / YYYY.MM-ga / YYYY.MM-ga.N) and semver (X.Y.Z).
# Returns 0 if $1 < $2, 1 if equal, 2 if $1 > $2
compare_versions() {
    local v1="${1#v}"
    local v2="${2#v}"

    if [ "$v1" = "$v2" ]; then
        return 1
    fi

    # #1334: ONE parser, in scripts/version_order.py. This function, the
    # manifest comparator inside migrate_env's Python heredoc and that
    # heredoc's sort_key were three copies of the same regex pair, and all
    # three shared the same hole: everything that was not `rcN[.M]`/`ga[.N]`
    # fell into a split-on-dots guess. That guess parses `2026.04-M011` as
    # (2026, 4011) — newer than every real release — and an installed value in
    # that shape selects ZERO migration blocks, for ever.
    #
    # Exit codes are unchanged (0 = <, 1 = ==, 2 = >); NEW is 3 for a version
    # the module refuses to order, with the reason on stderr. The callers below
    # (version_lt/version_lte) treat 3 as "not less than", which is the same
    # conservative answer they gave before — but it is no longer SILENT.
    local _vo="${SCRIPT_DIR:-.}/scripts/version_order.py"
    if [ ! -f "$_vo" ]; then
        echo "compare_versions: version ordering not found at ${_vo} — refusing to guess (#1334)" >&2
        return 3
    fi
    python3 "$_vo" "$v1" "$v2"
    return $?
}

# CALL THESE ONLY IN A CONDITIONAL CONTEXT (`if`, `&&`, `||`).
#
# #1334 review: `cli/upgrade.sh` runs under `set -eo pipefail`, and both
# wrappers call `compare_versions` bare. A non-zero return would abort the whole
# upgrade — bash suspends errexit for the entire call chain only while it is
# evaluating a condition. All 15 call sites are conditions today, which is why
# nobody has been bitten; the ordering module now also returns **3** for a
# version it refuses to order, so there is one more non-zero value that depends
# on the same rule. A bare `version_lt "$a" "$b"` on its own line would end the
# upgrade at that point, silently as far as the operator can tell.
#
# Returns 0 (true) if v1 <= v2
version_lte() {
    compare_versions "$1" "$2"
    local rc=$?
    [ $rc -eq 0 ] || [ $rc -eq 1 ]
}

# Returns 0 (true) if v1 < v2
version_lt() {
    compare_versions "$1" "$2"
    [ $? -eq 0 ]
}

# ==============================================================================
# Detect Installed Version
# ==============================================================================
detect_installed_version() {
    local version=""
    local commit=""

    # Primary: read from .env (written by razzfazz-init.sh / razzfazz-upgrade.sh)
    if [ -f ".env" ]; then
        version=$(read_env_value ".env" "RAZZFAZZ_VERSION")
        commit=$(read_env_value ".env" "RAZZFAZZ_COMMIT")
    fi

    # Fallback for commit only: use current git HEAD
    # NOTE: Do NOT fall back to VERSION file for installed version —
    # after 'git pull' VERSION already reflects the TARGET version.
    if [ -z "$commit" ] && command -v git &>/dev/null && [ -d ".git" ]; then
        commit=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    fi

    INSTALLED_VERSION="${version:-unknown}"
    INSTALLED_COMMIT="${commit:-unknown}"

    # #1809: the 0.0.0 baseline is right for MIGRATIONS and a lie for DIRECTION.
    #
    # `0.0.0` orders below every real release, so a direction check fed this
    # value reports a confident "UPGRADE 0.0.0 → <anything>" — including for a
    # target that is eighteen days OLDER than the box. Not a missing guard: a
    # guard given a fabricated fact, which is worse, because "UPGRADE" reads as
    # confirmation where "unknown" would have made an operator stop.
    #
    # FOUND WHILE investigating the 2026-09-08 back-date of 0.91, and NOT its
    # cause — that box ran an `upgrade.sh` from 2026-09-05 which had no
    # direction check at all (`check_upgrade_direction` landed the same day in
    # be2103bb, which is not an ancestor of the box's HEAD). The hole below is
    # real and mutation-proven as behaviour; the back-date is explained by the
    # guard's absence. Saying so here because a comment that names a cause
    # becomes the history somebody trusts.
    #
    # So: keep the baseline (migrations need it), and remember that it was
    # SYNTHESISED. Before believing it, try the sources that actually know —
    # the VERSION file at the current HEAD, then the tag HEAD points at. On a
    # trunk box VERSION says `2026.09-rc1`, and that alone would have refused
    # the run above.
    INSTALLED_VERSION_SYNTHETIC=false
    INSTALLED_DIRECTION_VERSION=""
    if [ "$INSTALLED_VERSION" = "unknown" ]; then
        local _derived=""
        [ -f VERSION ] && _derived=$(tr -d '[:space:]' < VERSION 2>/dev/null || true)
        if [ -z "$_derived" ] && command -v git >/dev/null 2>&1 && [ -d .git ]; then
            _derived=$(git describe --tags --exact-match 2>/dev/null || true)
        fi
        if [ -n "$_derived" ]; then
            # Review of #1809 (agent-seqis): INSTALLED_VERSION is NOT just the
            # direction basis — it gates `_UPGRADE_FROM_VERSION` (:1206), the
            # manifest migration (:1710) and a dozen hardcoded
            # `version_lt "$INSTALLED_VERSION" "2026.0x-…"` steps. Overwriting it
            # with the derived value would make every one of those compare
            # against 2026.09 and SKIP — on exactly the box whose migration
            # history is unknown. The first cut of this change did that, and its
            # test asserted the printed sentence instead of the effect.
            #
            # So the two answers stay apart: the baseline stays 0.0.0
            # (conservative, nothing skipped) and the DIRECTION uses what the
            # tree knows.
            print_warning "No RAZZFAZZ_VERSION in .env — using ${_derived} (from VERSION/git) to judge the upgrade direction."
            print_info "Migrations still run from a v0.0.0 baseline, so nothing is skipped."
            INSTALLED_VERSION="0.0.0"
            INSTALLED_DIRECTION_VERSION="$_derived"
        else
            print_warning "No RAZZFAZZ_VERSION found in .env, and neither VERSION nor a git tag names this box's version."
            print_warning "The DOWNGRADE CHECK CANNOT RUN — a target older than this box would not be refused."
            print_info "  Pass --target <tag> to name the version you mean."
            print_info "The upgrade will treat this as a v0.0.0 baseline; all migrations up to the target apply."
            INSTALLED_VERSION="0.0.0"
            INSTALLED_VERSION_SYNTHETIC=true
        fi
    fi
}

# ==============================================================================
# Detect Target Version (after code update)
# ==============================================================================
detect_target_version() {
    if [ -f "VERSION" ]; then
        TARGET_VERSION=$(cat VERSION | tr -d '[:space:]')
    else
        TARGET_VERSION="unknown"
    fi

    if command -v git &>/dev/null && [ -d ".git" ]; then
        TARGET_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    else
        TARGET_COMMIT="unknown"
    fi
}

# ==============================================================================
# Pre-Flight Checks
# ==============================================================================

# ------------------------------------------------------------------------------
# Release-channel / box-role constants (#28)
# ------------------------------------------------------------------------------
# INTERNAL fleet box  -> origin MUST be the SEQIS Gitea (asserted; abort on drift,
#                        per the CLAUDE.md git-remote-standard hard rule).
# PUBLIC customer box  -> origin is REDIRECTED to the public Codeberg mirror and
#                        anonymous pulls are allowed (no git credential needed).
RAZZFAZZ_INTERNAL_REMOTE="https://git.razzfazz.ai/razzfazz.ai/razzfazz-ai-service-stack.git"
# Public remote is single-sourced in scripts/lib.sh (#27) — shared with cli/init.sh
# and `.env`-overridable (RAZZFAZZ_PUBLIC_REMOTE env > .env > Codeberg default) so a
# staging/fork box can redirect origin at an alternate Codeberg for upgrade tests.
RAZZFAZZ_PUBLIC_REMOTE="$(razzfazz_public_remote)"

# Resolve the box's release channel: `internal` (default) or `public`.
# An explicit RAZZFAZZ_CHANNEL environment variable wins over the .env value so
# an operator can override per-invocation; the default is `internal` to preserve
# fleet behaviour for every box that predates this flag. Operates on
# ${SCRIPT_DIR}/.env so it is unit-testable without the network.
razzfazz_channel() {
    local channel
    channel="${RAZZFAZZ_CHANNEL:-}"
    if [ -z "$channel" ]; then
        channel=$(read_env_value "${SCRIPT_DIR}/.env" "RAZZFAZZ_CHANNEL" 2>/dev/null || echo "")
    fi
    case "$channel" in
        public)   printf 'public\n' ;;
        internal) printf 'internal\n' ;;
        "")       printf 'internal\n' ;;   # unset key = pre-#602 box = fleet
        *)        printf 'internal\n' ;;    # unrecognised => fail safe to internal
    esac
}

# ==============================================================================
# #602: fleet guard — a fleet-shaped box must not run on the public mirror
# ==============================================================================
# The 2026.09 default flipped to `public` so a FORGOTTEN customer box fails
# in-house instead of at the customer. The compensating guard: a box that
# carries a Gitea PAT is fleet-shaped — running it against the nachlaufenden
# GitHub mirror would be fail-silent-wrong (stale releases, no fleet
# hot-patches). Fail loudly HERE with the one-line fix. Override for the rare
# legitimate case (a dev box testing the public path) is explicit and logged.
# #635 review (sudo/HOME caveat): under `sudo ./razzfazz-upgrade.sh` HOME is
# /root and the admin user's PAT would be invisible to a bare $HOME grep —
# the guard (and status) therefore check the invoking user's home too.
_pat_present_for_gitea() {
    local f
    for f in "$HOME/.git-credentials"              "${SUDO_USER:+/home/$SUDO_USER/.git-credentials}"; do
        [ -n "$f" ] && [ -f "$f" ] && grep -q "git.razzfazz.ai" "$f" 2>/dev/null && return 0
    done
    return 1
}

# #3265 (operator 2026-11-16): rental boxes run the public channel only. A rental
# box on the internal channel would fetch development tags from git.razzfazz.ai;
# it is refused before origin is touched, with the fixes. A run that fetches
# nothing (an offline package, a tag already on the box) passes, with the same advice.
check_rental_box_on_internal_channel() {
    [ "$(razzfazz_channel)" = internal ] || return 0
    razzfazz_internal_channel_allowed "$SCRIPT_DIR" && return 0
    local line
    if ! upgrade_run_fetches_from_origin; then
        print_warning "This rental box is set to the internal channel; this upgrade fetches nothing, so it goes on."
        while IFS= read -r line; do print_info "$line"; done < <(razzfazz_rental_internal_fixes)
        return 0
    fi
    if [ "$RAZZFAZZ_FLEET_MAY_USE_INTERNAL_CHANNEL" = 1 ]; then
        print_error "This is a rental box on the internal channel: it would fetch from git.razzfazz.ai."
    else
        print_error "This box is on the internal channel: it would fetch from git.razzfazz.ai, and every box runs the public channel."
    fi
    while IFS= read -r line; do print_info "$line"; done < <(razzfazz_rental_internal_fixes)
    exit 1
}

check_public_channel_on_fleet_box() {
    [ "$(razzfazz_channel)" = "public" ] || return 0
    # #3265: a box marked rental runs the public channel by rule; a PAT left on it is
    # removed by this upgrade (settle_git_credentials), never a reason to stop.
    [ "$(razzfazz_store_pat_marker "${SCRIPT_DIR}/.env")" = false ] && return 0
    if _pat_present_for_gitea; then
        if [ "${RAZZFAZZ_ALLOW_PUBLIC_WITH_PAT:-0}" = "1" ]; then
            print_warning "RAZZFAZZ_CHANNEL=public on a box holding a Gitea PAT — proceeding (RAZZFAZZ_ALLOW_PUBLIC_WITH_PAT=1)."
            return 0
        fi
        print_error "RAZZFAZZ_CHANNEL=public, but this box holds a git.razzfazz.ai PAT — it looks like a FLEET box."
        print_info "Fleet boxes must track the SEQIS Gitea (the mirror lags and misses fleet hot-patches)."
        print_info "Fix:      set RAZZFAZZ_CHANNEL=internal in ${SCRIPT_DIR}/.env and re-run."
        print_info "Override: RAZZFAZZ_ALLOW_PUBLIC_WITH_PAT=1 $0 ... (dev boxes testing the public path only)."
        exit 1
    fi
}

# ==============================================================================
# Stored Gitea credentials and the Settings portal's mount (rc5; #2690)
# ==============================================================================
# #2690: rental is the default, RAZZFAZZ_STORE_PAT=true marks one of our fleet
# boxes. A box without the marker is settled ONCE here
# (razzfazz_settle_pat_mode): one that already stores a credential is marked
# true and keeps it, so an upgrade never breaks a fleet box; any other box is
# marked false. Then:
#   * a rental box loses every stored token, through the same code as
#     `rzfz forget-pat` (razzfazz_git_credential_scan ... remove), reported by
#     name only. That includes STACK_GIT_CREDENTIALS_PATH: no credential is
#     mounted into a container;
#   * a store-pat box keeps its own credentials, but #2804: nothing mounts
#     them into razzfazz-config any more. STACK_GIT_CREDENTIALS_PATH is cleared
#     and the read-only STACK_UPDATE_TOKEN is explained.
# Best-effort: never blocks the upgrade.
settle_git_credentials() {
    local said found kind what where cur
    said=$(razzfazz_settle_pat_mode "$SCRIPT_DIR")
    case "$said" in
        adopted$'\t'*) print_warning "${said#*$'\t'}" ;;
        ?*)             print_substep "${said#*$'\t'}" ;;
    esac
    if razzfazz_rental_box "${SCRIPT_DIR}/.env"; then
        found=$(razzfazz_git_credential_scan "$SCRIPT_DIR" remove 2>/dev/null || true)
        if [ -n "$found" ]; then
            print_substep "Rental box: stored Gitea credentials removed (the PAT is entered per use):"
            while IFS=$'\t' read -r kind what where; do
                [ -n "$what" ] && print_substep "  ${kind}: ${what} (${where})"
            done <<< "$found"
            if printf '%s\n' "$found" | grep -q 'STACK_GIT_CREDENTIALS_PATH'; then
                print_substep "  the Settings portal is recreated below without the credential mount"
            fi
        fi
        return 0
    fi
    # #2804: compose no longer mounts ~/.git-credentials into the Settings
    # portal; the retired path is cleared and the read-only token is explained.
    cur=$(read_env_value "${SCRIPT_DIR}/.env" "STACK_GIT_CREDENTIALS_PATH")
    if [ -n "$cur" ]; then
        update_env_value "${SCRIPT_DIR}/.env" "STACK_GIT_CREDENTIALS_PATH" ""
        print_substep "STACK_GIT_CREDENTIALS_PATH cleared: the Settings portal no longer mounts your git credentials"
    fi
    if [ -z "$(read_env_value "${SCRIPT_DIR}/.env" "STACK_UPDATE_TOKEN")" ]; then
        print_info "Update check: set a read-only Gitea token (scope read:repository) as STACK_UPDATE_TOKEN in ${SCRIPT_DIR}/.env (chmod 600), then: docker compose up -d razzfazz-config"
    fi
    return 0
}

# ==============================================================================
# Git credential pre-flight (#3)
# ==============================================================================
# #2706: a target already on this box — a commit given by its hash. Tags are
# razzfazz_resolve_release_tag's (either spelling). A branch name is neither:
# a branch target always means the remote tip (#1466).
_upgrade_target_is_local_commit() {
    [[ "${1:-}" =~ ^[0-9a-f]{7,40}$ ]] \
        && git -C "$SCRIPT_DIR" rev-parse -q --verify "${1}^{commit}" >/dev/null 2>&1
}

# #2706: will this run fetch from origin? Asked without the network, with the
# same choices code_update_git makes: a package fetches nothing; no target
# means "the latest release", fetched; a tag or commit already on the box (a
# git bundle, an scp'd tag) is checked out as it is; anything else is fetched.
upgrade_run_fetches_from_origin() {
    [ -z "${PACKAGE_FILE:-}" ] || return 1
    [ -n "${TARGET_TAG:-}" ] || return 0
    (cd "$SCRIPT_DIR" && razzfazz_resolve_release_tag "$TARGET_TAG" >/dev/null 2>&1) && return 1
    _upgrade_target_is_local_commit "$TARGET_TAG" && return 1
    return 0
}

# #2706 (the #2690 "PAT entered now" path): at a terminal, ask for a PAT for
# this run only. It is kept in shell variables that are never exported, and
# razzfazz_git_origin (scripts/lib.sh) hands it to the git commands that reach
# origin, and to nothing else; forget_git_pat_for_this_run drops it once the
# code update has fetched. xtrace is off in here: `set -x` never shows it.
read_git_pat_for_this_run() {
    { local _xt=0; case $- in *x*) _xt=1; set +x ;; esac; } 2>/dev/null
    local host="$1" user="" pat="" rc=1 _r=0
    if [ -t 0 ] && [ "${GIT_TERMINAL_PROMPT:-1}" != 0 ] && [ -x "$RZFZ_GIT_ASKPASS_HELPER" ]; then
        print_info "No stored git credential for ${host}. Enter one for this run; it is not stored."
        if read -r -p "  ${host} user name: " user \
           && { read -r -s -p "  Personal access token (hidden): " pat; _r=$?; echo; [ "$_r" -eq 0 ]; }; then
            if [ -n "$user" ] && [ -n "$pat" ]; then
                _RZFZ_GIT_USER="$user" _RZFZ_GIT_PAT="$pat" _RZFZ_GIT_ASKPASS="$RZFZ_GIT_ASKPASS_HELPER"
                print_substep "Token kept for this run only (not stored)."
                rc=0
            else
                print_warning "No user name or token entered."
            fi
        fi
    fi
    pat=""
    [ "$_xt" = 0 ] || set -x
    return "$rc"
}

# #2706: the token is needed only by the fetches of the code update.
forget_git_pat_for_this_run() {
    unset _RZFZ_GIT_PAT _RZFZ_GIT_USER _RZFZ_GIT_ASKPASS
}

# A freshly-installed box has no git credentials configured for the https
# `origin` (git.razzfazz.ai), so the upgrade's `git fetch` fails — historically
# with a cryptic "could not read Username for 'https://...': terminal prompts
# disabled" or an interactive hang. Detect the no-usable-credential condition
# here, BEFORE any fetch, and exit cleanly with an actionable message instead.
#
# Only applies to https remotes (ssh remotes carry their own auth via keys/agent)
# and only on the online git path (skipped for offline --package upgrades).
# Operates on $SCRIPT_DIR and $HOME so it is unit-testable without the network.
check_git_credentials() {
    # Offline upgrade: the git path is never taken — nothing to check.
    [ -n "${PACKAGE_FILE:-}" ] && return 0
    # #2706: nor when the target is already on the box (a git bundle, a
    # sideloaded tag, a commit): code_update_git checks it out without a fetch.
    upgrade_run_fetches_from_origin || return 0

    # No git repo (offline tree) — code_update_git handles that separately.
    [ -d "${SCRIPT_DIR}/.git" ] || return 0

    # #28: PUBLIC/customer boxes pull from an anonymous-clone public repo, so no
    # git credential is required — carve them out of the token check entirely.
    # (By the time preflight_checks calls us it has already redirected origin to
    # the public remote; we key off the channel, not the URL, so this holds even
    # when invoked standalone / before the redirect.)
    if [ "$(razzfazz_channel)" = "public" ]; then
        return 0
    fi

    local origin
    origin=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || echo "")
    # No origin is handled by the remote check in preflight_checks; not our job.
    [ -n "$origin" ] || return 0

    # ssh / git+ssh remotes authenticate via keys/agent — the https credential
    # mechanism does not apply.
    case "$origin" in
        https://*) : ;;
        *) return 0 ;;
    esac

    # Credentials are considered usable if a credential helper is configured
    # AND, for the default `store` helper, ~/.git-credentials carries a line for
    # the remote host. (A non-store helper — e.g. a custom/manager helper — is
    # trusted to supply the credential.)
    local helper host
    helper=$(git -C "$SCRIPT_DIR" config --get credential.helper 2>/dev/null || echo "")
    # Extract host from https://host[:port]/path
    host=$(printf '%s' "$origin" | sed -E 's#^https://([^/@]*@)?([^/:]+).*#\2#')

    if [ -n "$helper" ]; then
        case "$helper" in
            store*|*--file*)
                # `store` (default) keeps creds in ~/.git-credentials (or a
                # --file=... path). Require a matching host line so we catch the
                # "helper set but no token ever entered" fresh-box case too.
                local creds_file="${HOME}/.git-credentials"
                if [ -f "$creds_file" ] && grep -q "@${host}\b" "$creds_file" 2>/dev/null; then
                    return 0
                fi
                ;;
            *)
                # Some other helper (cache, manager, libsecret, …) is configured
                # — trust it to provide credentials; don't second-guess.
                return 0
                ;;
        esac
    fi

    # #2706: a PAT typed in now, for this run only.
    if read_git_pat_for_this_run "$host"; then
        return 0
    fi

    # No usable credential — fail cleanly with an actionable message.
    print_error "No usable git credential for https origin '${origin}'."
    print_info  "A fresh box has no credentials, so 'git fetch' against git.razzfazz.ai"
    print_info  "would fail with a cryptic error or hang on a username prompt."
    echo ""
    # #2690: a rental box (the default) never stores the token, so it is not
    # told to; a fleet box marked RAZZFAZZ_STORE_PAT=true is.
    if ! razzfazz_rental_box "${SCRIPT_DIR}/.env"; then
        print_info  "Fix (operator standard — persists the token across upgrades):"
        print_info  "    git -C \"$SCRIPT_DIR\" config credential.helper store"
        print_info  "    printf 'https://<user>:<PAT>@${host}\\n' >> \"\${HOME}/.git-credentials\""
        print_info  "    chmod 600 \"\${HOME}/.git-credentials\""
        print_info  "  (create a Personal Access Token at https://${host}/user/settings/applications)"
        echo ""
    else
        print_info  "This is a rental box: the token is never stored here."
    fi
    print_info  "Or run the upgrade at a terminal: it asks for a token for that run and does not store it."
    print_info  "Or upgrade from a tag already on this box (a git bundle; nothing is fetched):"
    print_info  "    rzfz upgrade --target <tag>"
    print_info  "Or upgrade offline with a package (no git creds needed):"
    print_info  "    rzfz upgrade --package <upgrade.tar.gz>"
    return 1
}

# #1200: the Settings portal moved from config.<domain> to settings.<domain> in
# 2026.09. Let's Encrypt (TLS_MODE empty) and `tls internal` boxes pick the new
# name up on their own. A TLS_MODE=certificate box serves the operator-provided
# certs/cert.pem for EVERY vhost, so a certificate issued before the rename may
# not cover settings.<domain> (a bare-SAN cert does not; a *.<domain> wildcard
# does). WARN with the fix, never abort — the old name keeps working as a 301
# and a cert gap is not a reason to block security upgrades. Runs under --check
# too (read-only). The upgrade-testing skill lists the portal login among the
# post-upgrade smoke checks, so a warning here is the operator's cue to re-issue.
# rzfz review #1209 F3: nothing guaranteed SETTINGS_DOMAIN != CONFIG_DOMAIN. Both
# are operator-editable; if they collide, Caddy has two site blocks on the same
# address and REFUSES TO START (total outage on upgrade), and §9c becomes a
# self-redirect loop. This is the one pre-flight in this family that aborts.
check_settings_and_config_domains_differ() {
    local settings_domain config_domain main_domain
    main_domain=$(read_env_value .env MAIN_DOMAIN 2>/dev/null || true)
    settings_domain=$(read_env_value .env SETTINGS_DOMAIN 2>/dev/null || true)
    config_domain=$(read_env_value .env CONFIG_DOMAIN 2>/dev/null || true)
    # .env may hold the literal template `${MAIN_DOMAIN}` (read_env_value does not
    # expand) — compare EXPANDED values, like check_custom_cert_covers_settings_domain
    # does, or `SETTINGS_DOMAIN=config.acme.com` next to `CONFIG_DOMAIN=config.${MAIN_DOMAIN}`
    # slips through and Caddy refuses to start (rzfz re-review #1209 F-neu 3).
    local tmpl='${MAIN_DOMAIN}'
    settings_domain="${settings_domain:-settings.${main_domain}}"
    config_domain="${config_domain:-config.${main_domain}}"
    settings_domain="${settings_domain//"$tmpl"/$main_domain}"
    config_domain="${config_domain//"$tmpl"/$main_domain}"
    if [ -n "$main_domain" ] && [ "$settings_domain" = "$config_domain" ]; then
        print_error "SETTINGS_DOMAIN and CONFIG_DOMAIN are BOTH '${settings_domain}' (#1200)."
        print_info  "Caddy would get two site blocks for one address and refuse to start, and the"
        print_info  "legacy redirect would loop onto itself. Set SETTINGS_DOMAIN=settings.<domain> and"
        print_info  "CONFIG_DOMAIN=config.<domain> in .env (they must differ), then re-run the upgrade."
        return 1
    fi
    return 0
}

# rzfz review #1209 F4: the cert check only covered TLS_MODE=certificate. On a
# Let's-Encrypt box with per-name A records (real in this fleet) a missing
# settings.<domain> record means ACME fails for the new vhost, TLS there breaks,
# and the 301 from config.<domain> lands nowhere — the portal is unreachable.
# WARN for EVERY TLS mode; never abort (DNS is operator-side and may follow).
check_settings_domain_resolves() {
    local main_domain settings_domain
    main_domain=$(read_env_value .env MAIN_DOMAIN 2>/dev/null || true)
    [ -n "$main_domain" ] || return 0
    settings_domain=$(read_env_value .env SETTINGS_DOMAIN 2>/dev/null || true)
    settings_domain="${settings_domain:-settings.${main_domain}}"
    command -v getent >/dev/null 2>&1 || return 0
    local resolved main_resolved
    resolved=$(getent ahostsv4 "$settings_domain" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)
    if [ -z "$resolved" ]; then
        print_warning "DNS: '${settings_domain}' does not resolve (#1200). The Settings portal moves to this"
        print_info    "name in 2026.09 — add an A/CNAME record (or a hosts entry) pointing at this box"
        print_info    "BEFORE users need it; Let's-Encrypt boxes cannot obtain a certificate without it."
        return 0
    fi
    main_resolved=$(getent ahostsv4 "$main_domain" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)
    local local_ips
    local_ips=$(hostname -I 2>/dev/null || true)
    local ip ok=false
    for ip in $resolved; do
        case " $main_resolved $local_ips " in *" $ip "*) ok=true; break;; esac
    done
    if [ "$ok" != true ]; then
        print_warning "DNS: '${settings_domain}' resolves to '${resolved}', which is neither this box nor where"
        print_info    "'${main_domain}' points (${main_resolved:-unresolved}). Check the record before relying on the portal."
    fi
    return 0
}

# #1444 (cutover C4) — the three #1200 pre-flights, for the llm/gpustack pair.
# The rochade moves GPUStack's console to gpustack.<domain> and gives
# llm.<domain> to the LLM Manager. Same failure modes, same three answers:
# a collision ABORTS (Caddy refuses the whole config), a missing DNS record and
# a certificate without the new name WARN (both are operator-side and may follow).
check_llm_and_gpustack_domains_differ() {
    local main_domain llm_domain gpustack_domain
    main_domain=$(read_env_value .env MAIN_DOMAIN 2>/dev/null || true)
    [ -n "$main_domain" ] || return 0
    llm_domain=$(read_env_value .env LLM_DOMAIN 2>/dev/null || true)
    gpustack_domain=$(read_env_value .env GPUSTACK_DOMAIN 2>/dev/null || true)
    local tmpl='${MAIN_DOMAIN}'
    # Pre-flight runs BEFORE migrate_env: on the first upgrade into this release
    # LLM_DOMAIN is absent (the migration will write llm.<domain>) and
    # GPUSTACK_DOMAIN still holds the OLD default — that pairing is exactly what
    # the migration fixes, so it is not a collision. Only a box where BOTH names
    # already resolve to the same string is one.
    [ -n "$llm_domain" ] || return 0
    llm_domain="${llm_domain//"$tmpl"/$main_domain}"
    gpustack_domain="${gpustack_domain//"$tmpl"/$main_domain}"
    [ -n "$gpustack_domain" ] || return 0
    if [ "$llm_domain" = "$gpustack_domain" ]; then
        print_error "LLM_DOMAIN and GPUSTACK_DOMAIN are BOTH '${llm_domain}' (#1444)."
        print_info  "Caddy would get two site blocks for one address and refuse to start — every vhost on"
        print_info  "this box would be down after the upgrade. Set LLM_DOMAIN=llm.<domain> (the LLM Manager"
        print_info  "console) and GPUSTACK_DOMAIN=gpustack.<domain> in .env, then re-run the upgrade."
        return 1
    fi
    return 0
}

check_gpustack_domain_resolves() {
    local main_domain gpustack_domain
    main_domain=$(read_env_value .env MAIN_DOMAIN 2>/dev/null || true)
    [ -n "$main_domain" ] || return 0
    # Only relevant while a GPUStack profile is on: a manager-only box has no
    # GPUStack console to reach.
    local profiles
    profiles=$(read_env_value .env COMPOSE_PROFILES 2>/dev/null || true)
    case ",${profiles}," in
        *,llm,*|*,llm-cpu,*|*,llm-legacy,*|*,llm-cuda,*) : ;;
        *) return 0 ;;
    esac
    gpustack_domain=$(read_env_value .env GPUSTACK_DOMAIN 2>/dev/null || true)
    local tmpl='${MAIN_DOMAIN}'
    gpustack_domain="${gpustack_domain:-gpustack.${main_domain}}"
    gpustack_domain="${gpustack_domain//"$tmpl"/$main_domain}"
    # After the migration this is the NEW name; warn about the name the box is
    # moving TO, which is the one that needs a record.
    case "$gpustack_domain" in llm.*) gpustack_domain="gpustack.${main_domain}" ;; esac
    command -v getent >/dev/null 2>&1 || return 0
    local resolved
    resolved=$(getent ahostsv4 "$gpustack_domain" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)
    if [ -z "$resolved" ]; then
        print_warning "DNS: '${gpustack_domain}' does not resolve (#1444). GPUStack's console moves to this name in"
        print_info    "2026.09 — add an A/CNAME record (or a hosts entry) pointing at this box; a Let's-Encrypt"
        print_info    "box cannot obtain a certificate for it without one."
    fi
    return 0
}

check_custom_cert_covers_gpustack_domain() {
    local tls_mode
    tls_mode=$(read_env_value .env TLS_MODE 2>/dev/null || true)
    [ "$tls_mode" = "certificate" ] || return 0
    local cert="${SCRIPT_DIR}/certs/cert.pem"
    [ -s "$cert" ] || return 0
    command -v openssl >/dev/null 2>&1 || return 0
    local main_domain
    main_domain=$(read_env_value .env MAIN_DOMAIN 2>/dev/null || true)
    [ -n "$main_domain" ] || return 0
    local want="gpustack.${main_domain}"
    local names
    names=$( { openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null || true; } | tr ',' '\n' | sed -n 's/.*DNS:\([^ ,]*\).*/\1/p' || true)
    [ -n "$names" ] || names=$( { openssl x509 -in "$cert" -noout -text 2>/dev/null || true; } | grep -o 'DNS:[^ ,]*' | cut -d: -f2 || true)
    local cn
    cn=$( { openssl x509 -in "$cert" -noout -subject 2>/dev/null || true; } | sed -n 's/.*CN *= *\([^,/]*\).*/\1/p' || true)
    names="${names}
${cn}"
    local n covered=false
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        case "$n" in
            "$want") covered=true; break ;;
            "*.${main_domain}") covered=true; break ;;
        esac
    done <<< "$names"
    if [ "$covered" != true ]; then
        print_warning "TLS: the operator certificate does not cover '${want}' (#1444). GPUStack's console moves to"
        print_info    "that name in 2026.09; re-issue the certificate with it in the SAN list, or the console is"
        print_info    "unreachable behind a certificate error after the upgrade."
    fi
    return 0
}

check_custom_cert_covers_settings_domain() {
    local tls_mode
    tls_mode=$(read_env_value .env TLS_MODE 2>/dev/null || true)
    [ "$tls_mode" = "certificate" ] || return 0
    local cert="${SCRIPT_DIR}/certs/cert.pem"
    # Missing files: Caddy's entrypoint already shouts and falls back to
    # `tls internal`; nothing to compare against.
    [ -s "$cert" ] || return 0
    command -v openssl >/dev/null 2>&1 || return 0

    local main_domain settings_domain
    main_domain=$(read_env_value .env MAIN_DOMAIN 2>/dev/null || true)
    settings_domain=$(read_env_value .env SETTINGS_DOMAIN 2>/dev/null || true)
    # Pre-flight runs BEFORE migrate_env, so on the first upgrade into 2026.09
    # the key is absent; the migration will write settings.${MAIN_DOMAIN}.
    # The .env value is the literal template — expand it here. Quoted "$tmpl"
    # in pattern position: an unquoted \} inside ${//} does NOT match a brace.
    local tmpl='${MAIN_DOMAIN}'
    settings_domain="${settings_domain:-settings.$tmpl}"
    settings_domain="${settings_domain//"$tmpl"/$main_domain}"
    [ -n "$main_domain" ] || return 0

    # DNS SANs (openssl >= 1.1.1 has -ext; fall back to the -text dump) + CN.
    local names
    # rzfz review #1209 F2: this script runs under `set -eo pipefail`. With an
    # openssl lacking -ext AND a cert without any SAN, the -text|grep fallback
    # returns non-zero → the pipeline would ABORT THE UPGRADE — for exactly the
    # SAN-less case this check exists to warn about. `|| true` on every branch:
    # a pre-flight WARN may never become an abort.
    names=$( { openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null \
               || openssl x509 -in "$cert" -noout -text 2>/dev/null \
                  | grep -A1 'Subject Alternative Name' || true; } \
             | tr ',' '\n' | sed -n 's/^[[:space:]]*DNS:[[:space:]]*//p' || true)
    names="$names
$(openssl x509 -in "$cert" -noout -subject -nameopt sep_multiline,lname 2>/dev/null \
      | sed -n 's/^[[:space:]]*commonName=//p' || true)"

    local covered=false name
    for name in $names; do
        if [ "$name" = "$settings_domain" ]; then
            covered=true; break
        fi
        case "$name" in
            \*.*)
                # RFC 6125 wildcard: exactly ONE label. *.example.com covers
                # settings.example.com but not a.b.example.com.
                if [ "${settings_domain#*.}" = "${name#\*.}" ]; then
                    covered=true; break
                fi
                ;;
        esac
    done
    if [ "$covered" = "true" ]; then
        print_substep "TLS_MODE=certificate: certs/cert.pem covers ${settings_domain}."
        return 0
    fi
    print_warning "TLS_MODE=certificate: certs/cert.pem does NOT cover ${settings_domain} (#1200)."
    print_info "2026.09 moves the Settings portal from config.${main_domain} to ${settings_domain};"
    print_info "the old name keeps working as a 301, but browsers will show a certificate error"
    print_info "on https://${settings_domain} until the certificate includes it (SAN or *.${main_domain})."
    print_info "Certificate names: $(echo $names | tr '\n' ' ')"
    print_info "Fix: re-issue the certificate with ${settings_domain} (or a *.${main_domain} wildcard), then"
    print_info "     rzfz setup --install-certificate <cert.pem> <key.pem>   (Caddy picks it up on restart)"
    print_info "Continuing — this is a warning, not a blocker."
    return 0
}

preflight_checks() {
    print_step "Running pre-flight checks..."

    # Check .env exists
    if [ ! -f ".env" ]; then
        print_error ".env file not found. Is the stack installed?"
        print_info "Run rzfz init for first-time setup."
        exit 1
    fi

    # BSB-03 / R-DEF-03 / R-COMP-13 — pre-create the host-side files
    # that are now bound as narrow :rw mounts in core/compose.yml. If
    # they don't exist, docker auto-creates them as DIRECTORIES on the
    # next `compose up`, which then breaks both the SQLite open
    # (.checksums.db) and the bind-target sanity. Older installs that
    # upgrade into BSB-03 may not have .checksums.db on disk because
    # the legacy broad :rw mount let it be created on first checksum-
    # take. Idempotent — no-op if files already exist.
    # #2138: neither on --check, which writes nothing to the box: a check runs
    # no `compose up` (restart_stack creates .checksums.db before one) and
    # writes no backup.
    if [ "${DRY_RUN:-false}" != "true" ]; then
        if [ ! -f ".checksums.db" ]; then
            touch .checksums.db && chmod 600 .checksums.db 2>/dev/null || true
        fi
        [ -d "backups" ] || { mkdir -p backups && chmod 700 backups 2>/dev/null || true; }
    fi

    # Check Docker
    if ! command -v docker &>/dev/null; then
        print_error "Docker is not installed."
        exit 1
    fi
    if ! docker compose version &>/dev/null; then
        print_error "Docker Compose v2 is required."
        exit 1
    fi

    # #855 rev-C GATE — Compose/Engine feature floor for `volume: { subpath: }`.
    # This runs in Step 1 (pre-flight), i.e. BEFORE Step 3 switches the code:
    # the target tree includes a module file using `subpath`, and a box whose
    # Compose predates 2.26.0 would be stranded mid-upgrade with a tree its
    # Compose cannot parse — no `docker compose` command left to recover with.
    # Refusing here leaves the box exactly as it was, on working code.
    if ! razzfazz_check_compose_floor; then
        print_error "Upgrade aborted BEFORE any code change — this box is untouched."
        exit 1
    fi

    # Check disk space (at least 5GB free)
    local free_gb
    free_gb=$(df -BG "$SCRIPT_DIR" | awk 'NR==2 {print $4}' | tr -d 'G')
    if [ "${free_gb:-0}" -lt 5 ]; then
        print_error "Less than 5GB disk space available (${free_gb}GB free). Upgrade aborted."
        exit 1
    fi

    # Check if stack is running (needed for DB dumps in backup)
    local running_containers
    running_containers=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l || true)
    if [ "${running_containers:-0}" -eq 0 ]; then
        print_warning "No containers running. Pre-upgrade backup will skip database dumps."
    fi

    # Origin-remote pre-flight. Behaviour depends on the release channel (#28):
    #   internal (default) — ASSERT the SEQIS Gitea origin; abort on drift
    #                        (protects the fleet from ad-hoc bundle/mirror remotes;
    #                        CLAUDE.md git-remote-standard hard rule).
    #   public             — REDIRECT origin to the public Codeberg mirror
    #                        (idempotent set-url); customers have no access to the
    #                        internal Gitea, so aborting would be wrong.
    local channel
    channel=$(razzfazz_channel)
    # #602: fleet-shaped box on the public channel fails loudly BEFORE any
    # origin redirect could point it at the lagging mirror.
    check_public_channel_on_fleet_box
    # #3265: and a rental box never runs the internal channel.
    check_rental_box_on_internal_channel
    local actual_remote
    # #1738: the RAW value — `remote get-url` would apply an insteadOf alias
    # and this comparison would abort on a box whose origin is exactly right.
    actual_remote=$(razzfazz_origin_url "$SCRIPT_DIR")

    if [ "$channel" = "public" ]; then
        # Customer/public box: point origin at the public release repo.
        # #643: NEVER under --check — a dry run that mutates remote config
        # violates the --check contract and stranded a fleet box on the
        # lagging mirror during the #635 live pass. Print the WOULD instead.
        if [ "$DRY_RUN" = "true" ]; then
            if [ -z "$actual_remote" ]; then
                print_info "[check] WOULD set origin to $RAZZFAZZ_PUBLIC_REMOTE (none configured)."
            elif [ "$actual_remote" != "$RAZZFAZZ_PUBLIC_REMOTE" ]; then
                print_info "[check] WOULD redirect origin '$actual_remote' -> $RAZZFAZZ_PUBLIC_REMOTE."
            fi
        elif [ -z "$actual_remote" ]; then
            print_warning "No 'origin' git remote configured; setting it to the public repo."
            git -C "$SCRIPT_DIR" remote add origin "$RAZZFAZZ_PUBLIC_REMOTE"
            print_info "origin set to $RAZZFAZZ_PUBLIC_REMOTE"
        elif [ "$actual_remote" != "$RAZZFAZZ_PUBLIC_REMOTE" ]; then
            print_warning "RAZZFAZZ_CHANNEL=public: redirecting origin '$actual_remote' -> public repo."
            git -C "$SCRIPT_DIR" remote set-url origin "$RAZZFAZZ_PUBLIC_REMOTE"
            print_info "origin now points at the public release repo ($RAZZFAZZ_PUBLIC_REMOTE)."
        fi
        # else: already correct — silent, idempotent.
    else
        # Internal fleet box: every razzfazz.ai box pulls from the SEQIS Gitea.
        # Bundle files in /tmp (a stale rc3-era pattern) get cleaned by
        # systemd-tmpfiles on reboot and break git-fetch with a cryptic error.
        # Catch this here with an actionable message instead.
        local expected_remote="$RAZZFAZZ_INTERNAL_REMOTE"
        if [ -z "$actual_remote" ]; then
            print_error "No 'origin' git remote configured."
            print_info "Fix: git -C \"$SCRIPT_DIR\" remote add origin $expected_remote"
            exit 1
        elif [ "$actual_remote" != "$expected_remote" ]; then
            print_warning "git remote 'origin' is '$actual_remote' (expected '$expected_remote')."
            print_info "Operator standard: all razzfazz.ai boxes pull from git.razzfazz.ai."
            print_info "Fix: git -C \"$SCRIPT_DIR\" remote set-url origin $expected_remote"
            print_info "(Customer/public boxes set RAZZFAZZ_CHANNEL=public to pull from the public repo instead.)"
            if [ "${FORCE:-false}" != "true" ]; then
                print_error "Aborting. Re-run with --force to ignore this check."
                exit 1
            fi
            print_warning "Continuing because --force was passed."
        fi
    fi

    # #3: usable git credential check for the online (git) upgrade path. A fresh
    # box with no credential.helper / no ~/.git-credentials would otherwise fail
    # the upcoming `git fetch` with a cryptic error. No-op for --package upgrades.
    if ! check_git_credentials; then
        exit 1
    fi

    # Detect installed version
    detect_installed_version
    print_substep "Installed version: ${INSTALLED_VERSION} (commit: ${INSTALLED_COMMIT})"

    # #1200: warn (never abort) when an operator certificate misses settings.<domain>.
    check_custom_cert_covers_settings_domain
    check_settings_and_config_domains_differ || return 1
    check_settings_domain_resolves
    # #1444: the same three questions for the llm/gpustack rochade.
    check_custom_cert_covers_gpustack_domain
    check_llm_and_gpustack_domains_differ || return 1
    check_gpustack_domain_resolves

    print_success "Pre-flight checks passed."
}

# ==============================================================================
# Pre-Upgrade Backup
# ==============================================================================
pre_upgrade_backup() {
    print_step "Creating pre-upgrade backup..."

    # #2137: the verdict first. A FAIL, or "n" at a PARTIAL, stops the upgrade
    # inside pre_upgrade_full_backup — before the checksum snapshot and before
    # .env.pre-upgrade-backup is rewritten, which the PREVIOUS upgrade's
    # rollback still needs (its record names that .env).
    pre_upgrade_full_backup

    # Governance checksum snapshot (#22: runs on the host now).
    if RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR" python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" \
            --checksum-take "Pre-upgrade from v${INSTALLED_VERSION}" 2>/dev/null; then
        print_substep "Governance checksum snapshot created."
    else
        print_warning "Could not create checksum snapshot (non-critical)."
    fi

    # Backup .env files
    cp -f ".env" ".env.pre-upgrade-backup"
    print_substep "Backed up .env → .env.pre-upgrade-backup"
    if [ -f ".env.dify" ]; then
        cp -f ".env.dify" ".env.dify.pre-upgrade-backup"
        print_substep "Backed up .env.dify → .env.dify.pre-upgrade-backup"
    fi
}

# #2705: is the path the backup takes ready? Prints why not. 0 = ready,
# 1 = not yet (worth waiting for), 2 = down (waiting will not help).
# The path: both backup containers running; the Docker socket proxy answering
# the backup service's own docker client (every docker call of the backup goes
# through it, and HAProxy answers "503 Service Unavailable" while it has no
# backend); and, where OpenBao runs, OpenBao unsealed (it answers 503 until its
# static seal has opened after a start, and the backup's OpenBao section then
# fails).
_backup_path_state() {
    local c st out t="${RZFZ_UPGRADE_PROBE_TIMEOUT:-15}"
    # Every probe is cut off after $t seconds. (A shell function standing in
    # for docker cannot run under timeout(1); it runs as it is.)
    _backup_probe() { if [ "$(type -t "$1")" = file ]; then timeout "$t" "$@"; else "$@"; fi; }
    for c in razzfazz-backup-management backup-service; do
        st=$(_backup_probe docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null) || st="missing"
        case "$st" in
            running) ;;
            created|restarting) echo "the container ${c} is ${st}"; return 1 ;;
            *) echo "the container ${c} is ${st:-missing}"; return 2 ;;
        esac
    done
    if ! out=$(_backup_probe docker exec backup-service docker version --format '{{.Server.Version}}' 2>&1); then
        echo "the Docker socket proxy does not answer the backup service: $(printf '%s\n' "${out:-no answer within ${t}s}" | grep -v '^[[:space:]]*$' | tail -n 1)"
        return 1
    fi
    if [ "$(_backup_probe docker inspect -f '{{.State.Status}}' openbao 2>/dev/null || true)" = running ] \
       && ! _backup_probe docker exec openbao bao status >/dev/null 2>&1; then
        echo "OpenBao is sealed or not ready yet (it answers 503 until it has unsealed)"
        return 1
    fi
    return 0
}

# #2705: wait (bounded on the clock: RZFZ_UPGRADE_BACKUP_WAIT_SECS, default
# 180, clamped to 1..1800; every probe has its own timeout) until the backup
# can run; stop the upgrade when it cannot. Nothing has changed yet.
wait_for_backup_path() {
    local limit="${RZFZ_UPGRADE_BACKUP_WAIT_SECS:-180}" step=5 why rc started deadline said=""
    if ! [[ "$limit" =~ ^[0-9]+$ ]]; then
        print_warning "RZFZ_UPGRADE_BACKUP_WAIT_SECS='${limit}' is not a number of seconds — using 180."
        limit=180
    elif [ "${#limit}" -gt 4 ] || [ "$((10#$limit))" -gt 1800 ]; then
        print_warning "RZFZ_UPGRADE_BACKUP_WAIT_SECS=${limit} is above 1800 — using 1800."
        limit=1800
    elif [ "$((10#$limit))" -lt 1 ]; then
        print_warning "RZFZ_UPGRADE_BACKUP_WAIT_SECS=${limit} is below 1 — using 1."
        limit=1
    fi
    limit=$((10#$limit))
    started=$SECONDS
    deadline=$((SECONDS + limit))
    while true; do
        rc=0
        why=$(_backup_path_state) || rc=$?
        [ "$rc" -eq 0 ] && return 0
        if [ "$rc" -eq 1 ] && [ "$SECONDS" -lt "$deadline" ]; then
            if [ -z "$said" ]; then
                print_substep "Waiting up to ${limit}s for the backup to be able to run: ${why}..."
                said=1
            fi
            sleep "$step"
            continue
        fi
        break
    done
    if [ "$rc" -eq 1 ]; then
        print_error "The pre-upgrade backup cannot run: ${why} (waited $((SECONDS - started))s). Upgrade stopped: the code and the .env were not changed."
    else
        print_error "The pre-upgrade backup cannot run: ${why}. Upgrade stopped: the code and the .env were not changed."
    fi
    print_info  "Retry once 'rzfz status' shows the backup service (and OpenBao) healthy: rzfz upgrade (the same arguments)."
    print_info  "Or upgrade without a backup (not recommended): rzfz upgrade --skip-backup"
    exit 1
}

# The full-stack backup and its verdict: returns only when the upgrade may go
# on (complete, or a PARTIAL the operator or --force accepted); exits 1
# otherwise. #2705: a backup that cannot run, failed, or whose report says it
# lacks something it should hold stops the upgrade; --skip-backup is the only
# way past those.
pre_upgrade_full_backup() {
    wait_for_backup_path
    # Full stack backup via razzfazz-backup.sh (if backup container is running)
    # #2705: grep reads all of docker's output (no -q): an early exit would
    # SIGPIPE docker, and under pipefail a running container read as stopped.
    if docker ps --format '{{.Names}}' 2>/dev/null | grep "razzfazz-backup-management" >/dev/null; then
        print_substep "Running full stack backup..."
        # #158: call the repo-root rzfz explicitly — `rzfz` is NOT guaranteed on PATH
        # (it isn't on prod 8.246), and a bare `rzfz` silently fails the full-stack
        # backup ("rzfz: command not found") so an upgrade proceeds WITHOUT a backup.
        # The backup's own status, not tail's: 0 = complete, 3 = PARTIAL
        # (written, but without some volumes), anything else = FAILED. The
        # code, the .env and the previous upgrade's .env backup have not been
        # touched yet, so a failed backup stops the upgrade here; a partial
        # one needs the operator's word.
        # PIPESTATUS inside the group: right with or without pipefail, and the
        # `|| true` keeps set -e from ending the run before the status is read.
        local _bk_rc=0 _bk_out _bk_errors
        # #2705: the whole report is kept for the verdict; five lines are shown.
        _bk_out=$(mktemp 2>/dev/null) || _bk_out=$(mktemp -p "${SCRIPT_DIR}/backups")
        { "${SCRIPT_DIR}/rzfz" backup backup 2>&1 | tee "$_bk_out" | tail -5; _bk_rc=${PIPESTATUS[0]}; } || true
        # #2705: exit 0 is not the whole verdict. The backup service exits 0
        # for a DEGRADED archive (its hook reported errors: a database or
        # OpenBao NOT in it) and for one whose hook left no report at all
        # (UNVERIFIED). Read as "complete", that let the upgrade go on. An
        # older backup service prints no verdict line: its 0 stays complete.
        if [ "$_bk_rc" -eq 0 ]; then
            if grep -q 'pre-backup hook left no report' "$_bk_out"; then
                _bk_rc=unverified
            elif grep -q '^razzfazz-backup: DEGRADED' "$_bk_out"; then
                _bk_errors=$(sed -nE 's/^razzfazz-backup: hook exit [^,]*, ([0-9]+) error\(s\).*/\1/p' "$_bk_out" | tail -n 1)
                if [ "${_bk_errors:-0}" -gt 0 ] 2>/dev/null; then _bk_rc=degraded; else _bk_rc=3; fi
            fi
        fi
        case "$_bk_rc" in 0|3) ;; *)
            # Name the cause: the report's own error lines, the 503 included.
            grep -E 'ERROR|CRITICAL|FAILED|UNVERIFIED|503|Service Unavailable|Error response' "$_bk_out" \
                | grep -v '^razzfazz-backup: FAILED (exit' | sed -n '1,8p' \
                | while IFS= read -r _l; do print_info "  backup: ${_l#"${_l%%[![:space:]]*}"}"; done || true
            ;;
        esac
        if grep -qE '503|Service Unavailable' "$_bk_out"; then
            print_info "A 503 means a service the backup talks to was not ready: the Docker socket proxy (every docker call of the backup goes through it) or OpenBao (it answers 503 until it has unsealed after a start)."
        fi
        rm -f "$_bk_out"
        case "$_bk_rc" in
            0)
                print_success "Pre-upgrade backup complete."
                ;;
            degraded|unverified)
                if [ "$_bk_rc" = unverified ]; then
                    print_error "The pre-upgrade backup is UNVERIFIED: its pre-backup step left no report, so nobody knows what the archive holds. Upgrade stopped: the code and the .env were not changed."
                else
                    print_error "The pre-upgrade backup is DEGRADED: its pre-backup step reported ${_bk_errors} error(s), so the archive lacks what those lines name. Upgrade stopped: the code and the .env were not changed."
                fi
                print_info  "Retry once the cause is fixed (a manual backup run shows the full report): rzfz upgrade (the same arguments)."
                print_info  "Or upgrade without a backup (not recommended): rzfz upgrade --skip-backup"
                exit 1
                ;;
            3)
                print_warning "The pre-upgrade backup is PARTIAL or DEGRADED: it was written without some volumes, or with warnings (see above)."
                if [ "${FORCE:-false}" != true ]; then
                    local _bk_confirm=""
                    # An unattended run (no terminal) is never answered by its
                    # input: piped `yes` is not the operator's word. --force is.
                    if [ -t 0 ]; then
                        read -r -p "Continue the upgrade with a partial backup? [y/N] " _bk_confirm || _bk_confirm=""
                    else
                        print_info "No terminal to ask at: an unattended run continues past a partial backup only with --force."
                    fi
                    if [[ ! "$_bk_confirm" =~ ^[Yy] ]]; then
                        print_error "Upgrade stopped: the pre-upgrade backup is partial. The code and the .env were not changed."
                        exit 1
                    fi
                else
                    print_warning "--force: continuing with the partial backup."
                fi
                ;;
            *)
                print_error "The pre-upgrade backup FAILED (exit ${_bk_rc}). Upgrade stopped: the code and the .env were not changed."
                print_info  "Fix the backup and re-run, or upgrade without one: rzfz upgrade --skip-backup"
                exit 1
                ;;
        esac
    else
        # #2705: no backup is not a backup. This used to warn and go on.
        print_error "The pre-upgrade backup cannot run: the container razzfazz-backup-management is not running. Upgrade stopped: the code and the .env were not changed."
        print_info  "Start the stack (rzfz start) and re-run, or upgrade without a backup (not recommended): rzfz upgrade --skip-backup"
        exit 1
    fi
}

# #2137: the pre-upgrade backup (it rewrites .env.pre-upgrade-backup) and the
# rollback record (#2178: it replaces the previous upgrade's record and
# snapshot) are an upgrade's first writes. They run only after the operator's
# "y": from the main flow on the git arm, from code_update_package on the
# package arm. Still before the code moves, which is what the record is for.
# #3210: lift the pause from the EXIT trap. The continuation after the re-exec
# never ran backup_and_record_before_changes, so the library that defines
# backup_run_resume_after_upgrade was not loaded there and `|| true` hid the
# missing command: load it here when it is not.
upgrade_resume_backup_schedule() {
    [ -n "${_UPGRADE_BACKUP_PAUSED:-}" ] || return 0
    if ! declare -F backup_run_resume_after_upgrade >/dev/null; then
        # shellcheck source=cli/lib-backup-run.sh disable=SC1091
        [ -f "${SCRIPT_DIR}/cli/lib-backup-run.sh" ] && source "${SCRIPT_DIR}/cli/lib-backup-run.sh" || {
            echo "WARNING: the scheduled backup stays paused: ${SCRIPT_DIR}/cli/lib-backup-run.sh is missing. Remove /tmp/rzfz-upgrade-in-progress in backup-service." >&2
            return 1
        }
    fi
    backup_run_resume_after_upgrade
}

backup_and_record_before_changes() {
    # #3199: the upgrade and the scheduled nightly backup never run together. A
    # backup already running finishes first; from here to the end of the upgrade
    # (the EXIT trap) a scheduled backup that comes due waits for it.
    # Sourced HERE, not at the top: the leftover-.env refusal (#2613) must stay
    # the first thing an upgrade does, and nothing new may run before it.
    local _bkrun_lib="${SCRIPT_DIR}/cli/lib-backup-run.sh" _bkrun=""
    # shellcheck source=cli/lib-backup-run.sh disable=SC1091
    if [ -f "$_bkrun_lib" ] && source "$_bkrun_lib"; then
        _bkrun=1
    else
        print_warning "The scheduled backup cannot be paused during this upgrade (${_bkrun_lib} is missing)."
    fi
    if [ -n "$_bkrun" ] && [ "${DRY_RUN:-false}" != "true" ]; then
        backup_run_wait_until_idle 3600 15 \
            || print_warning "A backup is still running after an hour; the upgrade goes ahead."
    fi
    if [ "${SKIP_BACKUP:-false}" = false ] && [ "${DRY_RUN:-false}" != "true" ]; then
        pre_upgrade_backup
    elif [ "${SKIP_BACKUP:-false}" = true ]; then
        print_warning "Skipping pre-upgrade backup (--skip-backup)."
    fi
    if [ -n "$_bkrun" ] && [ "${DRY_RUN:-false}" != "true" ] && backup_run_pause_for_upgrade; then
        _UPGRADE_BACKUP_PAUSED=1
        print_substep "The scheduled backup waits until this upgrade ends."
    fi
    # #2178: what a rollback must restore, recorded before the code moves.
    if [ "${DRY_RUN:-false}" != "true" ]; then
        # #2137: from here on the box changes; the next run keeps this log.
        [ -z "${UPGRADE_LOG:-}" ] || echo "=== changes begin: $(date -Iseconds) ===" >> "$UPGRADE_LOG"
        record_rollback_state
    fi
}

# ==============================================================================
# Code Update
# ==============================================================================
detect_latest_release_tag() {
    # Find the latest GA release tag (v*-ga or v*-ga.N pattern) from the remote.
    # RC tags (v*-rc*) are NOT treated as releases — only GA tags are.
    razzfazz_git_origin fetch --tags --force --quiet 2>&1   # #158: --force so a divergent old tag can't fail the latest-detector
    local latest
    # Match v*-ga and v*-ga.N but exclude v*-rc*
    # #665: tags from 2026.09 on are bare (2026.09-ga.N); older tags keep the v
    # prefix. razzfazz_list_ga_tags (lib.sh) lists both forms ordered by the
    # NORMALIZED version — a mixed-form `--sort=-version:refname` misorders
    # across the scheme change.
    latest=$(razzfazz_list_ga_tags | head -1)
    if [ -z "$latest" ]; then
        print_warning "No GA release tags found (v*-ga* / 20*-ga* patterns)."
        print_info "Use --target TAG to specify an explicit version."
    fi
    echo "$latest"
}

# ==============================================================================
# #1587: which DIRECTION is this?
# ==============================================================================
# A plain `rzfz upgrade` on a box whose stand is NEWER than the latest tag turns
# into a downgrade, and said so with no word at all: measured on 0.79, a box fell
# back 611 commits and `grep -i "downgrade|older|backward"` over the whole
# 2291-line log found nothing. The script knew both numbers the entire time and
# never compared them.
#
# A downgrade is NOT the mirror image of an upgrade, which is why this refuses
# rather than warns:
#   * `config/migrations/env-changes.json` only runs FORWARD. An `add` from the
#     newer release is not taken back, a `remove` is not restored — so the .env
#     keeps the NEW schema while the code becomes the old one.
#   * databases stay ahead. A Dify downgrade needs `alembic_version` reset by
#     hand or `dify-api` will not start; that is already written down in this
#     fleet's history.
#
# `compare_versions` (#1334, the ONE parser) answers: 0 = <, 1 = ==, 2 = >,
# 3 = cannot order. An unorderable pair is REPORTED and allowed — the same
# conservative answer the rest of this file gives, but not silent.
# #1890 — the VERSION a target ref carries, or empty.
#
# `check_upgrade_direction` was handed `$TARGET_TAG`, which for a branch target
# is a REF (`origin/main`), not a version. `compare_versions` cannot order a ref
# against a CalVer, so it returned 3 and the direction check warned and
# proceeded — the downgrade guard was off for every branch target. Measured on
# 0.91 during the E8 cutover run: "cannot order 2026.09-rc1 against origin/main
# — proceeding, check by hand."
#
# The migration side already does the right thing (it reads
# `git show "origin/<branch>:VERSION"`), so this is the same resolution moved to
# the check that needs it. A tag ref carries its VERSION too, so this works for
# both and the caller does not have to know which it has.
#
# Empty when the ref has no readable VERSION — the caller then keeps the honest
# "cannot order" warning instead of inventing a comparison.
target_ref_version() {
    local ref="$1"
    [ -n "$ref" ] || return 0
    [ -d "${SCRIPT_DIR}/.git" ] || return 0
    git -C "$SCRIPT_DIR" show "${ref}:VERSION" 2>/dev/null | tr -d '[:space:]'
}

check_upgrade_direction() {
    local installed="${1#v}" target="${2#v}"

    # #1809: a SYNTHESISED baseline is not a version. `0.0.0` orders below every
    # release, so comparing against it would report "UPGRADE" for a target that
    # is older than the box — an affirmative wrong answer, which an operator
    # reads as confirmation. Say the check is not running instead.
    if [ "${INSTALLED_VERSION_SYNTHETIC:-false}" = true ]; then
        print_warning "Direction: NOT CHECKED — this box's version is unknown, so a downgrade cannot be detected."
        print_info  "  Pass --target <tag> to name the version you mean."
        return 0
    fi

    if [ -z "$installed" ] || [ "$installed" = "unknown" ] || [ -z "$target" ]; then
        print_substep "Direction: unknown (installed=${installed:-?}, target=${target:-?})."
        return 0
    fi

    # #1890: a REF is not a version. Before giving up on the ordering, ask the
    # ref what VERSION it carries — the same lookup the migration side already
    # does. Without this the downgrade guard is simply off for `--target
    # <branch>`: the branch name orders against nothing, and the branch that
    # would print the reasons a downgrade is not the mirror image of an upgrade
    # is the one never reached.
    compare_versions "$installed" "$target"
    if [ $? -eq 3 ]; then
        local _ref_version
        _ref_version="$(target_ref_version "$2")"
        if [ -n "$_ref_version" ] && [ "${_ref_version#v}" != "$target" ]; then
            print_substep "Target ${2} carries VERSION ${_ref_version} — judging the direction against that."
            target="${_ref_version#v}"
        fi
    fi

    compare_versions "$installed" "$target"
    case $? in
        1)  print_substep "Direction: re-running ${target} (same version)." ;;
        0)  print_substep "Direction: UPGRADE ${installed} → ${target}." ;;
        2)
            print_error "This is a DOWNGRADE: ${installed} → ${target}."
            print_info  "  A downgrade is not the mirror image of an upgrade:"
            print_info  "  * .env migrations only run FORWARD — the file keeps the newer"
            print_info  "    schema while the code becomes the older one."
            print_info  "  * databases stay ahead (a Dify downgrade needs alembic_version"
            print_info  "    reset by hand, or dify-api will not start)."
            if [ "${ALLOW_DOWNGRADE:-false}" = true ]; then
                print_warning "Proceeding because --allow-downgrade was given."
                return 0
            fi
            print_info  "  Pass --allow-downgrade if you know why you want this,"
            print_info  "  or --target <tag> to name a version that is not older."
            return 1
            ;;
        *)  print_warning "Direction: cannot order ${installed} against ${target} — proceeding, check by hand." ;;
    esac
    return 0
}

code_update_git() {
    print_step "Updating code via git..."

    if [ ! -d ".git" ]; then
        print_error "Not a git repository. Use --package for offline upgrades."
        exit 1
    fi

    # Check for uncommitted changes (tracked OR untracked). rc6.3 fix:
    # `-u` (--include-untracked) catches operator-added files that would
    # otherwise block `git checkout <target-tag>` if the target tag has
    # that path tracked. Most common case: razzfazz-upgrade-from-2026.04-
    # GA.x.sh fetched out-of-band via `git show ... > ...` on a 2026.04-ga
    # box (the bootstrap), then `git checkout` aborts because the file is
    # untracked locally but tracked in the target. Files in .gitignore
    # (.env, .env.dify, …) are not touched by stash -u — only tree-visible
    # untracked files are stashed.
    # #185: containers write runtime files INTO the tree as root (docling OCR
    # data, caddy local CA, searxng config). The `git stash -u` below runs as
    # the stack user and CANNOT delete root-owned untracked files →
    # "Permission denied" → the entire upgrade aborts (hit prod + 0.208).
    # Pre-emptively reclaim ownership: a ROOT docker container (no host sudo,
    # headless-safe; same `alpine chown` pattern as the media-migrator), with a
    # `sudo -n` fallback. Best-effort: a warning here is better than an abort.
    if [ -n "$(find . -path ./.git -prune -o ! -user "$(id -u)" -print -quit 2>/dev/null)" ]; then
        print_substep "Reclaiming ownership of container-written files before stash (#185)..."
        local _reclaimed=false
        if command -v docker >/dev/null 2>&1 && docker run --rm -v "$PWD":/repo alpine \
               sh -c "find /repo -path /repo/.git -prune -o ! -user $(id -u) -exec chown $(id -u):$(id -g) {} +" >/dev/null 2>&1; then
            _reclaimed=true
        elif sudo -n find . -path ./.git -prune -o ! -user "$(id -u)" -exec chown "$(id -u):$(id -g)" {} + >/dev/null 2>&1; then
            _reclaimed=true
        fi
        [ "$_reclaimed" = true ] || print_warning "Could not reclaim file ownership pre-stash; stash may abort on root-owned files (see #185)."
    fi

    local has_tracked_changes=false
    local has_untracked=false
    # (#1590 removed STASH_UNTRACKED_ONLY: nothing reads it any more. It existed
    # only to decide whether to DELETE the stash, and that decision is gone —
    # leaving the flag behind is an invitation to wire it up again.)
    git diff --quiet 2>/dev/null || has_tracked_changes=true
    git diff --cached --quiet 2>/dev/null || has_tracked_changes=true
    [ -n "$(git ls-files --others --exclude-standard 2>/dev/null)" ] && has_untracked=true
    if [ "$has_tracked_changes" = true ] || [ "$has_untracked" = true ]; then
        print_warning "Uncommitted changes detected. Stashing..."
        git stash push -u -m "razzfazz-upgrade: auto-stash before upgrade to ${TARGET_TAG:-latest}" 2>&1
        STASHED=true
        # #2178: a rollback re-applies exactly this stash after its forced checkout.
        _rollback_state_set STASH "$(git rev-parse -q --verify refs/stash 2>/dev/null || true)" || true
        print_substep "Changes stashed (including untracked). Will restore after upgrade if needed."
    fi

    # #3265: on the public channel, tags and remote-tracking refs from
    # git.razzfazz.ai go BEFORE the target is resolved: a development tag must
    # never stand in for the public tag of the same name (the "already present
    # locally" shortcut below would check it out). Objects stay until the
    # upgrade has succeeded (a rollback still finds its commit); then
    # prune_dev_history_after_upgrade removes the rest.
    prune_dev_tags_before_fetch

    if [ -z "$TARGET_TAG" ]; then
        # No explicit target — resolve latest release tag
        print_substep "Fetching tags from remote..."
        TARGET_TAG=$(detect_latest_release_tag)
        if [ -z "$TARGET_TAG" ]; then
            print_error "No release tags found. Use --target TAG to specify a version."
            exit 1
        fi
        print_substep "Latest release: ${TARGET_TAG}"
    else
        # F3: skip remote fetch if the target tag is already present locally
        # (supports offline / air-gapped environments where the upgrade tag
        # was delivered out-of-band via `git bundle`, scp, or a sideloaded tag).
        # #665: accept EITHER tag spelling — pre-2026.09 tags carry a v prefix
        # forever, 2026.09+ tags are bare. Resolve onto whichever form exists.
        #
        # #1466: a BRANCH target means the remote tip, fetched now and checked
        # out detached like a tag — never the stale local branch (that path
        # reported success while leaving 0.91 a day behind origin).
        if _resolved=$(razzfazz_resolve_release_tag "$TARGET_TAG"); then
            # #2706: a tag on the box, in either spelling, FIRST — before the
            # branch check, which asks origin (ls-remote) and, at a terminal
            # with no credential, blocked on a user name prompt after the
            # backup and the stash. A tag wins over a branch anyway (#2287).
            if [ "$_resolved" != "$TARGET_TAG" ]; then
                print_substep "Target ${TARGET_TAG} resolved to existing tag ${_resolved}."
                TARGET_TAG="$_resolved"
            fi
            print_substep "Target tag ${TARGET_TAG} already present locally — skipping fetch."
        elif _upgrade_target_is_local_commit "$TARGET_TAG"; then
            # #2706: a commit already on the box (a git bundle) — no fetch,
            # and no ls-remote either (the branch check below asks origin).
            print_substep "Target ${TARGET_TAG} is a commit already present locally — skipping fetch."
        elif _branch=$(razzfazz_upgrade_target_branch "$TARGET_TAG"); then
            print_substep "Target ${TARGET_TAG} is a branch — fetching origin/${_branch}..."
            local _fb_rc=0
            TARGET_TAG=$(razzfazz_upgrade_fetch_branch "$_branch") || _fb_rc=$?
            if [ "$_fb_rc" -ne 0 ]; then
                # #2287: say which check failed. These are different problems
                # with different fixes, and the old text named the wrong one.
                if [ "$_fb_rc" -eq 2 ]; then
                    print_error "origin/${_branch} is not a branch on the remote — the fetch succeeded, but no such head exists. If you meant the release branch, give its full name (--target release/${_branch}); if you meant the release, give the tag (--target ${_branch})."
                else
                    print_error "git fetch origin ${_branch} failed — a branch target needs the remote. Offline boxes upgrade by tag (--target <tag>) or package (--package)."
                fi
                exit 1
            fi
            print_substep "Branch ${_branch} resolved to ${TARGET_TAG} ($(git rev-parse --short "$TARGET_TAG"))."
        else
            print_substep "Fetching from remote..."
            # #158 --force: a DIVERGENT *old* tag (e.g. a historically-moved
            # v2026.05-ga.4) makes a plain `git fetch --tags` return non-zero with
            # "! [rejected] ... would clobber existing tag" — which aborted the whole
            # upgrade even though the TARGET tag fetched fine (prod 8.246, 2026-07-11).
            # Force-update tags to match origin (authoritative for GA tags).
            if ! razzfazz_git_origin fetch --tags --force 2>&1; then
                print_error "git fetch failed. Seed the tag locally (git bundle / scp / manual add-remote) and retry."
                exit 1
            fi
            # #665: re-resolve after the fetch — the operator may have typed
            # the other spelling of a tag that only now exists locally.
            if _resolved=$(razzfazz_resolve_release_tag "$TARGET_TAG"); then
                [ "$_resolved" != "$TARGET_TAG" ] && print_substep "Target ${TARGET_TAG} resolved to existing tag ${_resolved}."
                TARGET_TAG="$_resolved"
            fi
        fi
    fi

    # #2706: every fetch of this run is done; the token typed in for it goes.
    forget_git_pat_for_this_run

    # #125 P1 retention guarantee: the box-local Enterprise-docs overlay lives at
    # overlay/ and is GITIGNORED. The `git stash push -u` above does NOT stash ignored
    # files (only tree-visible untracked ones), and the `git checkout <target-tag>`
    # below does NOT remove ignored files — so overlay/ (and the gated Enterprise docs
    # in it) SURVIVES an upgrade to a Codeberg tag whose tree has no docs/enterprise/.
    # Retention is by construction: do NOT add `-x`/`--include-ignored` to the stash,
    # and do NOT `git clean -x` overlay/.
    # #1587: name the direction while nothing has been touched yet. After the
    # checkout the answer would still be true and no longer useful.
    # #1809: the DIRECTION basis, which is not always the migration baseline —
    # a box without RAZZFAZZ_VERSION keeps a 0.0.0 baseline and still gets its
    # direction judged from VERSION/git.
    if ! check_upgrade_direction "${INSTALLED_DIRECTION_VERSION:-$INSTALLED_VERSION}" "$TARGET_TAG"; then
        if [ "${STASHED:-false}" = true ]; then
            git stash pop 2>/dev/null || true
        fi
        exit 1
    fi

    # A target known only now (latest, a branch, a fetched tag): the image check the
    # main flow could not run before the backup.
    if [ "${_IMAGE_PREFLIGHT_REF:-}" != "$TARGET_TAG" ]; then
        local _ipf_rc=0
        upgrade_image_preflight_git "$TARGET_TAG" "Not changed: the code, .env and the running stack. The pre-upgrade backup of this run and its rollback record were written before this could be checked, because the target had to be fetched first." || _ipf_rc=$?
        if [ "$_ipf_rc" -eq "${_IMAGE_PREFLIGHT_REFUSED:-10}" ]; then
            if [ "${STASHED:-false}" = true ]; then git stash pop >/dev/null 2>&1 || true; fi
            _REAL_UPGRADE_STARTED=""
            exit 1
        fi
    fi

    # #2215: ask the target's upgrader about the options this one deferred,
    # BEFORE the tree moves.
    if ! upgrade_script_knows_options "$(git show "${TARGET_TAG}:cli/upgrade.sh" 2>/dev/null || true)"; then
        if [ "${STASHED:-false}" = true ]; then git stash pop >/dev/null 2>&1 || true; fi
        exit 1
    fi

    # Checkout the target tag
    print_substep "Checking out ${TARGET_TAG}..."
    if ! git checkout "$TARGET_TAG" 2>&1; then
        print_error "Failed to checkout tag ${TARGET_TAG}."
        if [ "${STASHED:-false}" = true ]; then
            git stash pop 2>/dev/null || true
        fi
        exit 1
    fi

    detect_target_version
    print_success "Code updated to version ${TARGET_VERSION} (commit: ${TARGET_COMMIT})."

    # #1593: from here the box RUNS the new code while RAZZFAZZ_VERSION still
    # names the old one — the stamp is only rewritten by verify_upgrade at the
    # very end, and an aborted run leaves the two disagreeing with nothing
    # saying so. Moving the stamp forward would destroy the only honest signal
    # that the run is unfinished, so record the run instead: the marker is set
    # here and cleared with the stamp, and `rzfz status` reports the pair.
    update_env_value ".env" "RAZZFAZZ_UPGRADE_IN_PROGRESS" "$TARGET_VERSION"

    # #1590: the pre-upgrade auto-stash is RETAINED, whatever it holds.
    #
    # #158 dropped it when it held ONLY untracked files, on the reasoning that
    # untracked meant "stale runtime cruft the fresh tree supersedes" — and
    # named certs/caddy-ca.pem as an example of such cruft. On 2026-09-07 that
    # reasoning met a customer box: the stash held certs/cert.pem and
    # certs/key.pem, the customer's certificate and its PRIVATE KEY. There they
    # survived only because the stash ALSO held tracked edits, i.e. by accident.
    # A private key is not recoverable, it is re-issued — no accumulation
    # problem justifies a code path that can delete one.
    #
    # The accumulation #158 fought is real and is now handled where it belongs:
    # _prune_upgrade_stashes below bounds the backlog BY AGE (#1468/#1484),
    # which needs no guess about what a stash is worth.
    report_retained_stash
    # #1484: prune on EVERY upgrade — a clean tree creates no stash, and the
    # backlog (24 on 0.91) would otherwise only ever grow, never shrink.
    _prune_upgrade_stashes
}

# The reason for keeping the stash, counted WITH its untracked half: an
# untracked-only stash (the common case, see #1590 above) has no tracked
# diffstat, so `git stash show --stat` alone prints nothing.
report_retained_stash() {
    [ "${STASHED:-false}" = true ] || return 0
    local _files _n _head
    _files=$(git stash show --include-untracked --name-only 'stash@{0}' 2>/dev/null || true)
    _n=$(printf '%s\n' "$_files" | grep -c . || true)
    # sed reads ALL of its input: `head -n 3` would exit early and SIGPIPE the
    # writer, which under `set -eo pipefail` ends the run on a long file list.
    _head=$(printf '%s\n' "$_files" | sed -n '1,3p' | paste -sd, - | sed 's/,/, /g')
    if [ -z "$_files" ]; then
        # This git could not list the stash (see _stash_probe_works): do not claim 0 files.
        print_warning "Pre-upgrade auto-stash retained (file list unavailable) — review: git stash list / git stash show -u --name-only stash@{0}"
        journal_event "stash" "warn" "auto-stash retained: file list unavailable"
        return 0
    fi
    print_warning "Pre-upgrade auto-stash retained (${_n} file(s): ${_head:-?}) — review: git stash list / git stash show -u --name-only stash@{0}"
    journal_event "stash" "warn" "auto-stash retained: ${_n} file(s)"
}

# #1468: keep the newest RAZZFAZZ_STASH_KEEP `razzfazz-upgrade:` auto-stashes,
# drop older ones of OUR prefix only (a foreign stash is never touched), one
# journal line per drop. Drops run from the highest index down so the indices
# of the remaining entries stay valid while iterating.
RAZZFAZZ_STASH_KEEP=3
# #1590: and age alone may not decide. A stash is dropped only when it is NOT
# the last copy of anything — i.e. when every path it carries still exists in
# the working tree. Miss one, and that stash is the only place that file lives.
#
# Stated as a property rather than as a list of protected paths on purpose. The
# obvious version of this rule ("never drop a stash carrying certs/") describes
# the case we happen to have just been burnt by; the next unignored path with
# irreplaceable content would not be in it. Existence is computable, needs no
# list, and covers certs/ without naming it.
#
# `--include-untracked` is load-bearing: without it `git stash show --name-only`
# reports the TRACKED half only. Measured on git 2.53 against a stash holding
# certs/key.pem, loose.txt and an edit to tracked.txt — plain --name-only lists
# `tracked.txt` alone, so a guard built on it silently protects nothing, which
# is exactly the file that went missing on the customer box.
#
# The honest limit, since it decides what this rule can promise: it asks about
# EXISTENCE, not content. A stash holding an older revision of a file that does
# exist counts as redundant. For box-local material that is the right
# direction — the copy in the tree is the live one — and it is the price of a
# rule that carries no list. Anything unreadable (git error, empty output)
# counts as "cannot tell" and is KEPT; the safe direction is never dropping.
_stash_is_redundant() {
    local _idx="$1" _files _f
    _files=$(git stash show --include-untracked --name-only "$_idx" 2>/dev/null) || return 1
    [ -n "$_files" ] || return 1
    while IFS= read -r _f; do
        [ -n "$_f" ] || continue
        [ -e "$_f" ] || return 1
    done <<< "$_files"
    return 0
}
# `git stash show --include-untracked` arrived in git 2.32.0. Below that it is
# an unknown option, EVERY probe fails, `_stash_is_redundant` answers "cannot
# tell" for everything, and nothing is ever pruned — #1468's backlog (24 on
# 0.91) comes back with no line saying why, while each stash is reported as
# "the only copy", which is not the reason. Supported boxes are Ubuntu 24.04+
# (cli/init.sh) with git 2.43, so this is a diagnosis, not a fallback: say it
# once and skip, rather than print a misleading reason per stash.
# (Review agent-rzfz on #1600.)
# _git_version_at_least <major.minor>: 0 = at least, 1 = older, 2 = version unreadable — parsed from `git --version`, which also
# reads "git version 2.39.5 (Apple Git-154)" and "git version 2.43.0.windows.1".
# Numeric comparison per component (2.53 is above 2.32; a string compare says no).
_git_version_at_least() {
    local want="$1" have _hM _hm _wM _wm
    have=$(git --version 2>/dev/null | sed -nE 's/^git version ([0-9]+\.[0-9]+).*/\1/p')
    [ -n "$have" ] || return 2
    _hM="${have%%.*}"; _hm="${have#*.}"
    _wM="${want%%.*}"; _wm="${want#*.}"
    [ "$_hM" -gt "$_wM" ] && return 0
    [ "$_hM" -eq "$_wM" ] && [ "$_hm" -ge "$_wm" ]
}

# Bytes a stash pins: the blobs of its tracked and untracked commits.
_stash_size_bytes() {
    { git rev-list --objects "${1}^1..${1}" 2>/dev/null || true
      git rev-list --objects "${1}^3" 2>/dev/null || true; } \
        | cut -d' ' -f1 | sort -u \
        | { git cat-file --batch-check='%(objectsize)' 2>/dev/null || true; } \
        | awk '{s+=$1} END {print s+0}'
}

_STASH_PROBE_ERR=""
_stash_probe_works() {
    local _i _err
    _STASH_PROBE_ERR=""
    _i=$(git stash list --format='%gd' 2>/dev/null | head -n 1 || true)
    [ -n "$_i" ] || return 0
    # Keep the evidence: discarding the probe's output threw away the only
    # thing that could say why it failed, and the caller then asserted a cause.
    if _err=$(git stash show --include-untracked --name-only "$_i" 2>&1 >/dev/null); then
        return 0
    fi
    _err=$(printf '%s\n' "$_err" | sed -n '1p' | cut -c1-300)
    _STASH_PROBE_ERR="${_err:-exit non-zero, no message}"
    return 1
}

_prune_upgrade_stashes() {
    local keep="${RAZZFAZZ_STASH_KEEP:-3}" seen=0 idx msg dropped=0
    if ! _stash_probe_works; then
        local _vrc=0
        _git_version_at_least 2.32 || _vrc=$?
        case "$_vrc" in
        1)
            print_warning "Skipping auto-stash pruning: \`git stash show --include-untracked\` needs git >= 2.32; found $(git --version 2>/dev/null). The probe said: ${_STASH_PROBE_ERR}. Stashes will accumulate — review with \`git stash list\`."
            journal_event "stash" "warn" "prune skipped: git too old for --include-untracked: ${_STASH_PROBE_ERR}" ;;
        2)
            print_warning "Skipping auto-stash pruning: could not read the git version (\`git --version\` said: $(git --version 2>&1 | sed -n '1p' | cut -c1-100)); the stash probe failed: ${_STASH_PROBE_ERR}. Review with \`git stash list\`."
            journal_event "stash" "warn" "prune skipped: git version unreadable; probe failed: ${_STASH_PROBE_ERR}" ;;
        *)
            print_warning "Skipping auto-stash pruning: the stash probe failed: ${_STASH_PROBE_ERR} (git $(git --version 2>/dev/null | awk '{print $3}') supports it, so this is not a version gap). Review with \`git stash list\`."
            journal_event "stash" "warn" "prune skipped: probe failed: ${_STASH_PROBE_ERR}" ;;
        esac
        return 0
    fi
    while IFS= read -r line; do
        idx="${line%%|*}"; msg="${line#*|}"
        case "$msg" in
            *"razzfazz-upgrade: auto-stash"*) ;;
            *) continue ;;
        esac
        seen=$((seen + 1))
        [ "$seen" -le "$keep" ] && continue
        printf '%s\n' "$idx"
    done < <(git stash list --format='%gd|%gs' 2>/dev/null) | sort -t'{' -k2 -n -r | while IFS= read -r idx; do
        [ -n "$idx" ] || continue
        if ! _stash_is_redundant "$idx"; then
            print_warning "Kept old auto-stash ${idx} — it is the only copy of at least one file (#1590): git stash show -u --name-only ${idx}"
            journal_event "stash" "warn" "kept auto-stash ${idx}: sole copy of a file"
            continue
        fi
        if git stash drop "$idx" >/dev/null 2>&1; then
            print_substep "Dropped old auto-stash ${idx} (keeping the newest ${keep}, #1468)."
            journal_event "stash" "info" "dropped old auto-stash ${idx}"
        fi
    done
    # Another writer's stash is never pruned, by design — but a large one must
    # not stay silent. Report only; never drop it.
    local _min="${RAZZFAZZ_STASH_REPORT_MIN_BYTES:-1073741824}" _line _idx2 _msg2 _b
    while IFS= read -r _line; do
        _idx2="${_line%%|*}"; _msg2="${_line#*|}"
        case "$_msg2" in *"razzfazz-upgrade: auto-stash"*) continue ;; esac
        _b=$(_stash_size_bytes "$_idx2")
        [ "${_b:-0}" -ge "$_min" ] 2>/dev/null || continue
        print_warning "Stash ${_idx2} (\"${_msg2}\") pins $(( _b / 1048576 )) MiB and is never pruned automatically (not an upgrade stash). Review: git stash show -u --name-only ${_idx2}; drop it with git stash drop ${_idx2} once nothing in it is the only copy."
    done < <(git stash list --format='%gd|%gs' 2>/dev/null)
    return 0
}

reexec_if_updated() {
    # Re-execute the upgrade script from the new code if it changed.
    # After git checkout, the script on disk is the new version but bash
    # is still running the old one from memory. This re-launches the new
    # script with --_continue to skip code-update and proceed to migrations.
    # #1326: the entry is cli/upgrade.sh itself; legacy/razzfazz-upgrade.sh stays only for OLDER upgraders that exec it (E24).
    local new_script="${SCRIPT_DIR}/cli/upgrade.sh"
    if [ ! -f "$new_script" ]; then
        if [ -n "${DEFERRED_OPTS[*]:-}" ]; then
            print_error "Cannot hand ${DEFERRED_OPTS[*]} to the updated upgrader (${new_script} is missing). The code is updated, nothing after it ran: 'rzfz upgrade --rollback'. (#2215)"
            exit 1
        fi
        return 0  # Can't re-exec, continue in-place
    fi

    # Pass version info via environment so the new script doesn't re-detect
    export _UPGRADE_FROM_VERSION="$INSTALLED_VERSION"
    export _UPGRADE_FROM_COMMIT="$INSTALLED_COMMIT"
    export _UPGRADE_TO_VERSION="$TARGET_VERSION"
    export _UPGRADE_TO_COMMIT="$TARGET_COMMIT"
    export _UPGRADE_STASHED="${STASHED:-false}"
    # #2260: the package arm's image state (the continuation skips code_update_package).
    export _UPGRADE_SKIP_PULL="${SKIP_PULL:-false}"
    export _UPGRADE_PKG_SKIP_IMAGES="${_PKG_SKIP_IMAGES:-false}"
    export _UPGRADE_IMAGE_PREFLIGHT="${_IMAGE_PREFLIGHT_STATE:-}"
    # #3210: the scheduled backup was paused by THIS run (the marker in
    # backup-service); the continuation's EXIT trap must lift it. Lost here, every
    # upgrade left the marker and the next nightly backup waited an hour.
    [ -z "${_UPGRADE_BACKUP_PAUSED:-}" ] || export _UPGRADE_BACKUP_PAUSED

    print_substep "Re-executing upgrade script from ${TARGET_VERSION}..."
    echo "DEBUG[reexec_pre]: FORCE=$FORCE SKIP_BACKUP=$SKIP_BACKUP DRY_RUN=$DRY_RUN args=(--_continue ${UPGRADE_ORIG_ARGS[*]})" >&2
    # #2260/#2215: the operator's argv, whole — not a hand-picked subset.
    exec bash "$new_script" --_continue "${UPGRADE_ORIG_ARGS[@]}"
}

# #2260: what the continuation needs from the code-update step it skips.
restore_reexec_state() {
    INSTALLED_VERSION="${_UPGRADE_FROM_VERSION:-unknown}"
    INSTALLED_COMMIT="${_UPGRADE_FROM_COMMIT:-unknown}"
    TARGET_VERSION="${_UPGRADE_TO_VERSION:-unknown}"
    TARGET_COMMIT="${_UPGRADE_TO_COMMIT:-unknown}"
    STASHED="${_UPGRADE_STASHED:-false}"
    # The package arm loaded (or skipped) the images before the re-exec; without
    # these the new script would pull or rebuild what the package already gave.
    SKIP_PULL="${_UPGRADE_SKIP_PULL:-${SKIP_PULL:-false}}"
    _PKG_SKIP_IMAGES="${_UPGRADE_PKG_SKIP_IMAGES:-${_PKG_SKIP_IMAGES:-false}}"
}

# #2215: an option deferred at parse time survives only on a run that will hand
# it to a newer upgrader. Everywhere else it is what it always was: fatal.
validate_deferred_options() {
    [ -n "${DEFERRED_OPTS[*]:-}" ] || return 0
    local opt plain='^--[a-z0-9][a-z0-9-]*$'
    # Only a plain option NAME can be handed on and asked about. `--a.b`,
    # `--x|--force`, `--opt=value` or a bare `--` would pass a pattern match
    # against the target's case labels and then be refused by the continuation
    # itself — after the code moved. Refuse them here, before anything changes.
    for opt in "${DEFERRED_OPTS[@]}"; do
        if ! [[ "$opt" =~ $plain ]]; then
            print_error "Unknown option: ${opt} — not an option name (options are --lowercase-words; pass a value as '--option value', not '--option=value'). Nothing was changed. (#2215)"
            echo "Use --help for usage information."
            exit 1
        fi
    done
    if [ "${CONTINUE_AFTER_REEXEC:-false}" = true ] || [ "${DRY_RUN:-false}" = "true" ] \
       || [ "${DO_ROLLBACK:-false}" = true ] || [ "${DO_UPDATE:-false}" = true ]; then
        print_error "Unknown option: ${DEFERRED_OPTS[0]}"
        echo "Use --help for usage information."
        exit 1
    fi
    print_warning "This upgrader does not know: ${DEFERRED_OPTS[*]} — the target release's upgrader is asked before anything changes, and receives them after the code update (#2215)."
}

# #2137: ask BEFORE the checkout / package apply and the env migration, so
# "N" means nothing on the box changed. 0 = proceed, 1 = declined. The export
# reaches the re-exec'd continuation, whose late prompt then stays silent.
confirm_upgrade_before_changes() {
    [ "${FORCE:-false}" = true ] && return 0
    [ "${DRY_RUN:-false}" = "true" ] && return 0
    [ "${_UPGRADE_CONFIRMED:-false}" = true ] && return 0
    local to="${1:-the latest release}" confirm=""
    echo ""
    echo -e "${YELLOW:-}Upgrade: v${INSTALLED_VERSION:-unknown} (${INSTALLED_COMMIT:-unknown}) -> ${to}${NC:-}"
    echo    "  Nothing on this box has been changed yet."
    read -r -p "Proceed with upgrade? [y/N] " confirm || confirm=""
    if [[ ! "$confirm" =~ ^[Yy] ]]; then
        print_warning "Upgrade declined. Nothing on this box was changed."
        return 1
    fi
    export _UPGRADE_CONFIRMED=true
    return 0
}

# #2215: does the TARGET's cli/upgrade.sh (its text in $1) carry a case label
# for every deferred option? Empty text = cannot tell = 0 (the continuation's
# own parse is the last word). The labels are compared as FIXED strings: the
# option name is operator input and never becomes part of a pattern.
upgrade_script_knows_options() {
    local text="$1" opt labels unknown=() plain='^--[a-z0-9][a-z0-9-]*$'
    [ -n "${DEFERRED_OPTS[*]:-}" ] || return 0
    [ -n "$text" ] || { print_warning "Could not read the target's upgrader — it will judge ${DEFERRED_OPTS[*]} itself."; return 0; }
    # every alternative of every `  --a|--b)` case label, one per line. sed
    # reads all of its input: no early-exit reader under pipefail, so a large
    # script text cannot SIGPIPE the printf into a false "unknown".
    labels="$(printf '%s\n' "$text" \
        | sed -nE 's/^[[:space:]]*(-[-A-Za-z0-9_]*(\|-[-A-Za-z0-9_]*)*)\).*/\1/p' | tr '|' '\n')"
    for opt in "${DEFERRED_OPTS[@]}"; do
        if ! [[ "$opt" =~ $plain ]] \
           || ! grep -Fxq -e "$opt" <<<"$labels"; then
            unknown+=("$opt")
        fi
    done
    [ "${#unknown[@]}" -eq 0 ] && return 0
    print_error "Unknown option: ${unknown[*]} — neither this upgrader nor the target release's knows it. Nothing was changed. (#2215)"
    return 1
}

# #2215: a deferred option is only worth the preflight, the backup and the
# stash when a NEWER upgrader will receive it. Where this run can already tell
# that none will, refuse before any of that runs. The package arm is left to
# the check before "Applying package files..." (reading the archive here would
# be a second full read of it, #271); a --target tag that is not fetched yet is
# left to the check before the checkout.
refuse_undeliverable_deferred_options() {
    [ -n "${DEFERRED_OPTS[*]:-}" ] || return 0
    [ -z "${PACKAGE_FILE:-}" ] || return 0
    local why="" _t
    if [ -z "${TARGET_TAG:-}" ]; then
        why="no --target and no --package: the target (and its upgrader) would only be known after the preflight, the backup and the stash. Name it with --target <tag>."
    elif _t=$(git rev-parse -q --verify "refs/tags/${TARGET_TAG}^{commit}" 2>/dev/null); then
        # A tag already here: ask its upgrader now. A same-version target
        # lands here too — its upgrader is the installed one, which deferred
        # the option in the first place.
        if ! upgrade_script_knows_options "$(git show "${_t}:cli/upgrade.sh" 2>/dev/null || true)"; then
            why="the upgrader of ${TARGET_TAG} does not know it either."
        fi
    fi
    [ -n "$why" ] || return 0
    print_error "Unknown option: ${DEFERRED_OPTS[*]} — ${why} Nothing was changed. (#2215)"
    if [ -n "${DEFERRED_PKG_HINT:-}" ]; then
        echo "Did you mean: --package ${DEFERRED_PKG_HINT} ?"
    fi
    echo "Use --help for usage information."
    exit 1
}

# #1554: does the package actually carry image tarballs?
#
# The old test was `if ls "$dir"/*.tar 2>/dev/null | head -1 >/dev/null`, whose
# exit status is HEAD's — always 0. So a code-only package took the
# image-loading branch: the loop ran once on the unexpanded glob, `docker load`
# printed an error, and — the part that matters — `SKIP_PULL=true` was set, so
# the upgrade then skipped pulling images it did not have. Proved in a scratch
# dir with zero tars: the condition is true, and the loop sees the literal
# `.../images/*.tar`.
_pkg_has_image_tars() {
    local _d="$1" _f
    for _f in "$_d"/images/*.tar; do
        [ -e "$_f" ] && return 0
    done
    return 1
}

# #271/#2120 — decide before extraction whether the package's images need loading.
# Reads ONLY expected-images.json out of the archive (one small member near its
# start) and compares the image ids the packager recorded with the box's. Sets:
#   _PKG_SKIP_IMAGES true when every image is present under the package's id
# A package without image_ids (built before #2120) or a helper that cannot
# answer → _PKG_SKIP_IMAGES=false and the package is extracted as before.
# Deliberately NO package index here: `tar -tzf` over the archive is a full
# gunzip pass, and #271 exists because this path read the package twice. The
# manifest is one small member near the START of the archive (member #3 on the
# ga.15 stick), so `--occurrence=1` with a wildcard stops tar as soon as it has
# it — a bounded peek, not a pass. The images/ subtree is excluded under both
# member spellings (./images/*, images/*) so no index is needed for the prefix.
_PKG_SKIP_IMAGES=false
_PKG_PREDICTED_LOADS=""
package_images_pregate() {
    local pkg="$1" pre rows rc=2
    _PKG_SKIP_IMAGES=false
    _PKG_PREDICTED_LOADS=""
    # Without scripts/lib.sh (a harness that runs this function standalone) the
    # decision cannot be made — extract as before, never skip on silence.
    type appliance_package_needs_loading >/dev/null 2>&1 || return 0
    pre="$(mktemp)" || return 0
    if LC_ALL=C tar -xzf "$pkg" -O --wildcards --occurrence=1 '*expected-images.json' > "$pre" 2>/dev/null && [ -s "$pre" ]; then
        # `|| rc=$?` — upgrade.sh runs `set -eo pipefail`; a bare assignment would
        # die on the helper's designed rc 2 ("cannot decide") and abort the upgrade.
        rc=0
        rows="$(appliance_package_needs_loading "$pre")" || rc=$?
        case "$rc" in
            0) _PKG_SKIP_IMAGES=true
               print_substep "Every image this package carries is already present under the package's own image id — images/ will be neither extracted nor loaded (#271/#2120)." ;;
            1) _PKG_PREDICTED_LOADS="$(printf '%s\n' "$rows" | razzfazz_distinct_image_refs | grep -c . || true)"
               print_substep "Images to load from the package: ${_PKG_PREDICTED_LOADS} distinct image(s) (#2221):"
               printf '%s\n' "$rows" | razzfazz_distinct_image_refs | sed 's/^/      /' ;;
            *) print_substep "Package carries no image ids (built before #2120) — images/ is extracted and loaded as before." ;;
        esac
    fi
    rm -f "$pre"
    return 0
}

# #271/#2120 — load the extracted image archives, skipping those whose content
# is already local BY IMAGE ID; or nothing at all when the pre-gate decided so.
# Sets SKIP_PULL=true whenever the package's images are (now) present.
package_load_images() {
    local pkg_dir="$1" img have loaded=0 skipped=0 failed=0 failed_names=""
    if [ "${_PKG_SKIP_IMAGES:-false}" = true ]; then
        SKIP_PULL=true
        print_substep "Docker images: all present with the package's ids — nothing loaded (#271/#2120)."
        return 0
    fi
    _pkg_has_image_tars "$pkg_dir" || return 0
    print_substep "Loading Docker images from package..."
    have="$(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null)"
    for img in "${pkg_dir}"/images/*.tar; do
        if appliance_archive_images_present "$img" "$have"; then
            skipped=$((skipped + 1)); continue
        fi
        if docker load -i "$img" >/dev/null 2>&1; then
            loaded=$((loaded + 1))
        else
            failed=$((failed + 1)); failed_names="${failed_names} $(basename "$img")"
            print_warning "  image load FAILED: $(basename "$img") (#2221)"
        fi
    done
    SKIP_PULL=true
    print_substep "Docker images loaded: ${loaded}; already present by image id, not re-loaded: ${skipped}."
    # A shortfall or a failure is the discrepancy; MORE loads than predicted is
    # not: an archive the prediction did not count (a package built before
    # #2221 recorded no id for its runner variants) loaded fine (#2221).
    if [ "$failed" -gt 0 ] || { [ -n "${_PKG_PREDICTED_LOADS:-}" ] && [ "$loaded" -lt "$_PKG_PREDICTED_LOADS" ]; }; then
        print_warning "The pre-gate predicted ${_PKG_PREDICTED_LOADS:-?} image(s) to load; ${loaded} loaded, ${failed} FAILED:${failed_names:- none} — check 'rzfz verify-images' before the restart (#2221)."
    fi
}

# #2178: ONE list of what a package never copies over the tree. The rsync in
# code_update_package and the rollback snapshot both read it, so they cannot
# drift. rsync semantics: an entry without '/' matches that path component at
# any depth; an entry ending in '/' matches a directory of that name anywhere.
# images/ + models/ are docker-load / model-deploy payloads (#209).
PACKAGE_RSYNC_EXCLUDES=(
    .env .env.dify .git backups/ .checksums.db enterprise-overlay/
    images/ models/ .env.pre-upgrade-backup .env.dify.pre-upgrade-backup .upgrade.log
    /tests/    # #2348: the test tier has its own apply step (package_apply_tests_tier)
)

# _package_path_is_excluded <path relative to the package root>
_package_path_is_excluded() {
    local f="$1" e name
    for e in "${PACKAGE_RSYNC_EXCLUDES[@]}"; do
        name="${e%/}"
        if [ "$name" != "$e" ]; then
            case "/$f" in */"$name"/*) return 0 ;; esac    # a directory component
        else
            case "/$f/" in */"$name"/*) return 0 ;; esac   # any component
        fi
    done
    return 1
}

# ------------------------------------------------------------------------------
# #2138: a --check across a release boundary is answered by the TARGET's
# upgrader — the installed one has never heard of the versions being crossed
# (a test box: "No .env migrations needed" for 241 changes; another: no ga.3/ga.4 keys).
#
# That is running code from the target, so it runs under a contract:
#   fetched  — `git archive` of the target ref into a temp dir OUTSIDE the box
#              (no worktree, nothing written under .git), or the package's own
#              extraction, and a package only when its authenticity passed;
#   contract — the target must offer the preview-only mode `--_preview` (a
#              case label in its cli/upgrade.sh). A target without it is never
#              run: an older upgrader handed --_continue would apply for real;
#   staging  — regular files and directories only: a staged git tree with a
#              symlink (or any other special file) is refused, so nothing in
#              it can point back at the box; the env copies never write
#              through a link the target supplies;
#   limits   — it runs in its staged tree against COPIES of .env/.env.dify,
#              with no git repository reachable, docker/docker-compose limited
#              to read-only verbs and sudo refused BY PATH (an absolute path
#              bypasses both), stdin closed, and without this run's journal or
#              env file. That is not a sandbox: unprivileged namespaces are
#              not available on every box, so nothing stops the target's code
#              from writing wherever this user can.
#   detection — what the fingerprint below sees: a write into the box
#              directory (.git and the ignored paths included) that has
#              happened by the time the target's process returns. A NEW
#              REGULAR FILE directly in backups/ named like an archive of the
#              backup service (BACKUP_FILENAME, its .gpg and .sha256) is not
#              counted: the running stack writes those. A symlink, FIFO or any
#              other non-regular file under such a name is counted.
#              What it CANNOT see: a new regular file with an archive name
#              that the TARGET planted in backups/ (indistinguishable from
#              the stack's); a detached process that writes after the
#              "after" snapshot; writes outside the box ($HOME, /tmp, TMPDIR,
#              a PATH directory such as ~/.local/bin, a crontab); and this
#              run's own .upgrade.log, which is left out. So this is
#              detection of the common case, not containment — the target is
#              code the operator is about to upgrade to anyway (a real
#              upgrade from the same ref runs the same code), and the
#              preview is never a reason to run a ref one would not install.
# ------------------------------------------------------------------------------

# 0 when the upgrader in file $1 offers the preview-only mode. Its case labels
# are read as fixed strings (upgrade_script_knows_options reads them the same
# way); grep without -q reads all of its input, so no SIGPIPE under pipefail.
_target_upgrader_offers_preview() {
    [ -f "$1" ] || return 1
    sed -nE 's/^[[:space:]]*(-[-A-Za-z0-9_]*(\|-[-A-Za-z0-9_]*)*)\).*/\1/p' "$1" \
        | tr '|' '\n' | grep -Fx -e '--_preview' >/dev/null
}

# _preview_box_fingerprint [<box-relative path to leave out>] — what a
# preview must leave as it found it, for the comparison described above.
# Every non-directory path of the box, .git and the ignored paths (backups/,
# .checksums.db, overlays, certs) included: type, mode, owner, size, inode,
# link target and CHANGE time (an unprivileged process cannot set ctime back,
# so a write that restores content and mtime is still seen; the backup
# archives are fingerprinted this way, not hashed — they run to gigabytes).
# Directories by type, mode, owner and inode only: an added or removed entry
# shows as that entry, so a new backup archive leaves its directory's line
# alone. Plus the content of what decides the next run: git's config, HEAD,
# packed refs, hooks and info, the refs and stashes, every .env* (the
# .env*.pre-upgrade-backup files included), the checksums db and the rollback
# state. One `<name><TAB><value>` per line, so a difference can be named.
# Left out: this run's own .upgrade.log and (package arm) the staged tree.
_preview_box_fingerprint() {
    local skip="${1:-}" f
    local -a prune=(-path ./.upgrade.log)
    [ -z "$skip" ] || prune+=(-o -path "./${skip}")
    {
        find . \( "${prune[@]}" \) -prune -o -type d -printf '%P\t%y %m %U %G %i\n' 2>/dev/null
        find . \( "${prune[@]}" \) -prune -o ! -type d -printf '%P\t%y %m %U %G %s %i %C@ %l\n' 2>/dev/null
    } | LC_ALL=C sort || true
    for f in .git/config .git/HEAD .git/packed-refs .env* .checksums.db; do
        [ -f "$f" ] && printf 'sha:%s\t%s\n' "$f" "$(sha256sum < "$f" 2>/dev/null | cut -d' ' -f1)"
    done
    while IFS= read -r f; do
        [ -n "$f" ] && printf 'sha:%s\t%s\n' "$f" "$(sha256sum < "$f" 2>/dev/null | cut -d' ' -f1)"
    done <<EOF_FP
$(find .git/hooks .git/info backups/state -type f 2>/dev/null | LC_ALL=C sort)
EOF_FP
    printf 'refs\t%s\n' "$(git --no-optional-locks for-each-ref --format='%(refname) %(objectname)' 2>/dev/null | sha256sum | cut -d' ' -f1)"
    printf 'stashes\t%s\n' "$(git --no-optional-locks stash list 2>/dev/null | sha256sum | cut -d' ' -f1)"
    return 0
}

# The backup service's own archive names, as an awk regex over box-relative
# paths: BACKUP_FILENAME from .env (default backup-%Y-%m-%d-%H%M.tar.gz) with
# every strftime field as "any name characters", optionally suffixed .gpg
# (encrypted) and .sha256 (its checksum sidecar).
_preview_backup_archive_regex() {
    local pat
    pat=$(sed -n 's/^BACKUP_FILENAME=//p' .env 2>/dev/null | tail -n 1 | tr -d "\"'")
    [ -n "$pat" ] || pat='backup-%Y-%m-%d-%H%M.tar.gz'
    pat="${pat##*/}"
    pat=$(printf '%s' "$pat" | sed -e 's/[][\\.^$+?(){}|*]/\\&/g' -e 's/%[A-Za-z]/[^\/]*/g')
    printf '^backups/%s(\\.gpg)?(\\.sha256)?$\n' "$pat"
}

# _preview_without_new_backup_archives <before> <after> — <after> without the
# lines of paths that are NEW since <before>, REGULAR files (type f) and named
# like a backup archive. A pre-existing archive, a link or any other special
# file under an archive name, and every other path in backups/ stay compared.
_preview_without_new_backup_archives() {
    local re
    re=$(_preview_backup_archive_regex)
    # the regex through ENVIRON: `awk -v` would turn its \. into "any char"
    PREVIEW_ARCHIVE_RE="$re" awk -F'\t' 'BEGIN { re = ENVIRON["PREVIEW_ARCHIVE_RE"] }
        NR == FNR { seen[$1] = 1; next } !($1 in seen) && $2 ~ /^f / && $1 ~ re { next } { print }' \
        <(printf '%s\n' "$1") <(printf '%s\n' "$2") 2>/dev/null || printf '%s\n' "$2"
}

# The names that differ between two fingerprints, at most ten.
_preview_fingerprint_changes() {
    diff <(printf '%s\n' "$1") <(printf '%s\n' "$2") 2>/dev/null \
        | sed -n 's/^[<>] //p' | cut -f1 | sed 's/^sha://; s/^$/(the box directory)/' \
        | LC_ALL=C sort -u | head -n 10 | tr '\n' ' ' || true
}

# A PATH directory whose docker/docker-compose pass only read-only verbs on
# to the real binaries (RZFZ_PREVIEW_REAL_DOCKER[_COMPOSE]) and whose sudo
# refuses. Prints the directory.
_preview_guard_dir() {
    local d
    d=$(mktemp -d "$(_preview_temp_base)/rzfz-preview-guard-XXXXXX") || return 1
    if ! cat > "${d}/docker" <<'GUARD'
#!/usr/bin/env bash
# rzfz upgrade --check: a preview may look at docker, never change it.
refuse() { echo "[preview] refused: ${0##*/} $*" >&2; exit 1; }
ok=0 real="${RZFZ_PREVIEW_REAL_DOCKER:-}" args=("$@")
if [ "${0##*/}" = docker-compose ]; then
    real="${RZFZ_PREVIEW_REAL_DOCKER_COMPOSE:-}" args=(compose "$@")
fi
case "${args[0]:-}" in
    ps|inspect|images|version|info) ok=1 ;;
    image|volume|network|container|context)
        case "${args[1]:-}" in inspect|ls|list) ok=1 ;; esac ;;
    compose)
        i=1 verb=""
        while [ "$i" -lt "${#args[@]}" ]; do
            case "${args[$i]}" in
                -f|--file|-p|--project-name|--project-directory|--env-file|--profile|--ansi|--progress|--parallel)
                    i=$((i + 2)) ;;
                -*) i=$((i + 1)) ;;
                *) verb="${args[$i]}"; break ;;
            esac
        done
        case "$verb" in config|ps|ls|images|version) ok=1 ;; esac
        for a in "${args[@]}"; do
            case "$a" in -o|--output|--output=*) ok=0 ;; esac
        done ;;
esac
[ "$ok" = 1 ] || refuse "$@"
[ -n "$real" ] || { echo "${0##*/}: command not found" >&2; exit 127; }
exec "$real" "$@"
GUARD
    then
        rm -rf "$d"
        return 1
    fi
    if ! cp "${d}/docker" "${d}/docker-compose" \
       || ! printf '#!/bin/sh\necho "[preview] refused: sudo $*" >&2\nexit 1\n' > "${d}/sudo" \
       || ! chmod 755 "${d}/docker" "${d}/docker-compose" "${d}/sudo"; then
        rm -rf "$d"
        return 1
    fi
    printf '%s\n' "$d"
}

# Where the preview's temp dirs go: TMPDIR, unless that is inside the box —
# a stage or a guard there would sit in what the preview's fingerprint watches.
_preview_temp_base() {
    local base="${TMPDIR:-/tmp}" box_real base_real
    box_real=$(cd "${SCRIPT_DIR:-.}" 2>/dev/null && pwd -P) || box_real=""
    base_real=$(cd "$base" 2>/dev/null && pwd -P) || base_real=""
    case "${base_real}/" in "${box_real}/"*) base=/tmp ;; esac
    printf '%s\n' "$base"
}

# stage_target_tree <ref> — the ref's tree (without docs/, releases/, wiki/)
# in a new temp dir outside the box; prints the dir. rc 1 when it cannot be
# staged, rc 2 when the tree holds anything but regular files and directories
# (a symlink could point back at the box): those are listed on stderr and the
# stage is removed. `git archive` only reads the object store: no worktree,
# no index, no lock.
stage_target_tree() {
    local ref="$1" dir odd
    dir=$(mktemp -d "$(_preview_temp_base)/rzfz-upgrade-preview-XXXXXX") || return 1
    if ! git archive --format=tar "$ref" -- . ':(exclude)docs' ':(exclude)releases' ':(exclude)wiki' \
            2>/dev/null | tar -x -C "$dir" 2>/dev/null \
       || [ -z "$(ls -A "$dir")" ]; then
        rm -rf "$dir"
        return 1
    fi
    odd=$(find "$dir" ! -type f ! -type d -printf '  %P -> %l\n' 2>/dev/null | head -n 10)
    if [ -n "$odd" ]; then
        printf '%s\n' "$odd" >&2
        rm -rf "$dir"
        return 2
    fi
    printf '%s\n' "$dir"
}

# preview_with_target_tree <tree> — run <tree>/cli/upgrade.sh as a preview of
# this box and return its status; 3 when the target cannot be asked (it does
# not offer the preview mode), 1 when the preview changed the box. The box
# fingerprint is _PREVIEW_BOX_BEFORE when the caller took it before staging,
# else it is taken here, before anything is copied. Called in a condition
# (`|| rc=$?`), where set -e is off: every step checks itself.
preview_with_target_tree() {
    local tree="$1" rc=0 guard before after real_docker real_dc skip="" f
    local to="${TARGET_VERSION:-the target}"
    case "$tree" in "${SCRIPT_DIR:-$PWD}"/*) skip="${tree#"${SCRIPT_DIR:-$PWD}"/}" ;; esac
    before="${_PREVIEW_BOX_BEFORE:-}"
    [ -n "$before" ] || before=$(_preview_box_fingerprint "$skip")
    if ! _target_upgrader_offers_preview "${tree}/cli/upgrade.sh"; then
        print_warning "Preview unavailable for a cross-version jump: cannot preview with the target's upgrader. The upgrader of ${to} has no preview-only mode, and it is never run in a mode that could change anything. Nothing was changed."
        return 3
    fi
    # Never write through what the target supplied: whatever sits at the
    # copies' names in its tree (a symlink into the box, say) goes first.
    for f in .env .env.dify; do
        [ -f "$f" ] || continue
        rm -f -- "${tree}/${f}" 2>/dev/null
        if [ -e "${tree}/${f}" ] || [ -L "${tree}/${f}" ] || ! cp -p -- "$f" "${tree}/${f}"; then
            print_error "Could not copy this box's ${f} into the staged preview tree. Nothing was changed."
            return 1
        fi
    done
    if ! guard=$(_preview_guard_dir); then
        print_error "Could not set up the preview's docker/sudo guard. Nothing was changed."
        return 1
    fi
    real_docker=$(command -v docker 2>/dev/null || true)
    real_dc=$(command -v docker-compose 2>/dev/null || true)
    print_info "Preview by the upgrader of the target release (${to}), run in a staged copy of it against a copy of this box's .env. Docker reaches it only for read-only commands, sudo is refused:"
    (
        cd "$tree" || exit 1
        exec env -u RAZZFAZZ_JOURNAL_JSON -u RAZZFAZZ_JOURNAL_HUMAN -u RAZZFAZZ_LOG_FILE \
            -u _UPGRADE_CONFIRMED -u _UPGRADE_STASHED -u _UPGRADE_SKIP_PULL -u _UPGRADE_PKG_SKIP_IMAGES -u _UPGRADE_IMAGE_PREFLIGHT \
            -u RAZZFAZZ_ENV_FILE -u OLDPWD -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
            PATH="${guard}:${PATH}" GIT_CEILING_DIRECTORIES="$(dirname "$tree")" \
            RZFZ_PREVIEW_REAL_DOCKER="$real_docker" RZFZ_PREVIEW_REAL_DOCKER_COMPOSE="$real_dc" \
            _UPGRADE_FROM_VERSION="${INSTALLED_VERSION:-unknown}" _UPGRADE_FROM_COMMIT="${INSTALLED_COMMIT:-unknown}" \
            _UPGRADE_TO_VERSION="${TARGET_VERSION:-unknown}" _UPGRADE_TO_COMMIT="${TARGET_COMMIT:-unknown}" \
            bash cli/upgrade.sh --_continue --_preview ${UPGRADE_ORIG_ARGS[@]+"${UPGRADE_ORIG_ARGS[@]}"}
    ) </dev/null || rc=$?
    after=$(_preview_box_fingerprint "$skip")
    after=$(_preview_without_new_backup_archives "$before" "$after")
    rm -rf "$guard"
    # The target's own log, indented: none of its lines may read as this run's
    # "=== changes begin" marker (the log rotation keys on it).
    if [ -n "${UPGRADE_LOG:-}" ] && [ -f "${tree}/.upgrade.log" ] && [ ! -L "${tree}/.upgrade.log" ]; then
        { echo "--- the preview by the upgrader of ${to} ---"; sed 's/^/  | /' "${tree}/.upgrade.log"; } \
            >> "$UPGRADE_LOG" 2>/dev/null || true
    fi
    if [ "$before" != "$after" ]; then
        local changed re n only_archives=true
        changed=$(_preview_fingerprint_changes "$before" "$after")
        re=$(_preview_backup_archive_regex)
        # "already existed" = an archive name that was a regular file before
        # and still is one: a link or special file put there is the target's
        for n in $changed; do
            PREVIEW_ARCHIVE_RE="$re" PREVIEW_N="$n" awk -F'\t' '
                BEGIN { n = ENVIRON["PREVIEW_N"]; if (n !~ ENVIRON["PREVIEW_ARCHIVE_RE"]) exit 1 }
                FNR == 1 { file++ }
                $1 == n && $2 ~ /^f / { ok[file] = 1 }
                END { exit !(ok[1] && ok[2]) }' <(printf '%s\n' "$before") <(printf '%s\n' "$after") \
                || only_archives=false
        done
        if [ "$only_archives" = true ]; then
            # An archive that already existed and changed: most likely a
            # backup that was running when the check began. Still a FAIL —
            # but the message must not pin it on the target.
            print_error "FAIL: this box changed during the preview, only in backup archives that already existed. Changed: ${changed}— a backup running during the check would explain it: re-run the check when no backup runs. If none ran, the upgrader of ${to} wrote them."
        else
            print_error "FAIL: the upgrader of ${to} changed this box during the preview, which a preview must never do. Changed: ${changed}— inspect them (and .git/hooks, .git/config) before upgrading; compare .env with .env.pre-upgrade-backup."
        fi
        return 1
    fi
    return "$rc"
}

# The git arm of --check: a --target that is not this tree is previewed by its
# own upgrader, and this run ends with that preview's status. Exits 3 when it
# cannot be. Returns 0 (the local preview is the right one) without --target,
# for a target at HEAD, and inside a preview.
maybe_preview_with_target_ref() {
    local ref="" pb tree rc=0 src=0 v
    [ "${PREVIEW_ONLY:-false}" = true ] && return 0
    [ -n "${TARGET_TAG:-}" ] || return 0
    if pb=$(razzfazz_upgrade_target_branch "$TARGET_TAG") \
       && git rev-parse -q --verify "refs/remotes/origin/${pb}" >/dev/null 2>&1; then
        ref="origin/${pb}"
    elif git rev-parse -q --verify "${TARGET_TAG}^{commit}" >/dev/null 2>&1; then
        ref="$TARGET_TAG"
    fi
    if [ -z "$ref" ]; then
        print_warning "Preview unavailable for a cross-version jump: '${TARGET_TAG}' is not a ref this box has, so no upgrader can describe that jump. Fetch it first (git fetch --tags --force origin). Nothing was changed."
        exit 3
    fi
    [ "$(git rev-parse "${ref}^{commit}")" = "$(git rev-parse HEAD)" ] && return 0
    _PREVIEW_BOX_BEFORE=$(_preview_box_fingerprint)
    tree=$(stage_target_tree "$ref") || src=$?
    if [ "$src" -eq 2 ]; then
        print_warning "Preview unavailable for a cross-version jump: FAIL — ${ref} holds symbolic links or special files (listed above). A preview stages regular files only, so nothing in it can point back at this box. Nothing was changed."
        exit 3
    elif [ "$src" -ne 0 ] || [ -z "$tree" ]; then
        print_warning "Preview unavailable for a cross-version jump: could not stage ${ref} for its upgrader. Nothing was changed."
        exit 3
    fi
    # The jump is named by the staged tree itself, whatever form --target took
    # (a tag, a branch, a commit): never by a fallback to the installed code.
    v=$(tr -d '[:space:]' < "${tree}/VERSION" 2>/dev/null || true)
    if [ -z "$v" ]; then
        rm -rf "$tree"
        print_warning "Preview unavailable for a cross-version jump: ${ref} carries no readable VERSION, so the jump cannot be named. Nothing was changed."
        exit 3
    fi
    TARGET_VERSION="$v"
    TARGET_COMMIT=$(git rev-parse --short "${ref}^{commit}" 2>/dev/null || echo unknown)
    preview_with_target_tree "$tree" || rc=$?
    rm -rf "$tree"
    exit "$rc"
}

# #2348: the package's test tier replaces the box's, so a box's tier follows its
# VERSION. Files the new tier no longer has are removed (a stale test that still
# runs is a false red); the box's own venv, results and caches are kept. Nothing
# outside ${stack}/tests is written: a directory link planted under tests/ is
# removed (never followed) and files are copied over a link, not through it.
package_apply_tests_tier() {
    local src="$1/tests" dest="$2/tests" rel old new
    [ -d "$src" ] || return 0
    mkdir -p "$dest"
    old="$(mktemp)" && new="$(mktemp)" || return 1
    (cd "$dest" && find . \( -path ./.venv -o -path ./results \) -prune -o -type l -xtype d -exec rm -f {} + )
    (cd "$dest" && find . \( -path ./.venv -o -path ./results -o -name __pycache__ -o -path ./.pytest_cache \
        -o -path ./htmlcov \) -prune -o \( -type f -o -type l \) -not -name '.coverage*' -print | LC_ALL=C sort) > "$old"
    (cd "$src" && find . \( -type f -o -type l \) | LC_ALL=C sort) > "$new"
    LC_ALL=C comm -23 "$old" "$new" | while IFS= read -r rel; do
        rm -f "${dest}/${rel#./}"
    done
    rm -f "$old" "$new"
    cp -a --remove-destination "${src}/." "${dest}/"
    print_substep "Test tier applied from the package: $(sed -n 's/^tier_version=//p' "${dest}/TIER_SOURCE" 2>/dev/null || echo unknown) (#2348)."
}

# #2818: a package copies the new release over the old one and removed nothing,
# so files the new release deleted stayed in the tree — inside build contexts,
# where they made the package's own images read stale. Plan (before the copy:
# the tree's .release-files is still the OLD release's), then apply (after it).
# The planner is the PACKAGE's: the installed tree may predate it. It only plans
# files the old release shipped unchanged; everything else stays. An empty plan
# is a valid answer (no list, legacy box) and never stops the upgrade.
# RAZZFAZZ_PACKAGE_PRUNE=dry-run plans and logs but removes nothing.
package_plan_release_prune() {
    local pkg_dir="$1" tree="$2" plan="$3" n
    local py="${pkg_dir}/scripts/release_files.py"
    mkdir -p "$(dirname "$plan")"
    : > "$plan"
    if [ ! -f "$py" ]; then
        print_info "  Release prune: the package carries no planner (an older package) — nothing pruned."
        return 0
    fi
    if ! python3 "$py" plan "$tree" "${pkg_dir}/.release-files" > "$plan"; then
        print_warning "  Release prune: the planner failed — nothing pruned."
        : > "$plan"
        return 0
    fi
    n=$(grep -c . "$plan" || true)
    print_substep "Release prune: ${n:-0} file(s) the installed release shipped and this one no longer does."
    return 0
}

package_apply_release_prune() {
    local pkg_dir="$1" tree="$2" plan="$3" line
    local py="${pkg_dir}/scripts/release_files.py"
    [ -s "$plan" ] || return 0
    if [ "${RAZZFAZZ_PACKAGE_PRUNE:-}" = dry-run ]; then
        print_info "  Release prune (dry-run, RAZZFAZZ_PACKAGE_PRUNE=dry-run) — would remove:"
        sed 's/^/    /' "$plan"
        return 0
    fi
    while IFS= read -r line; do
        print_info "  ${line}"
    done < <(python3 "$py" apply "$tree" "$plan" 2>&1)
    return 0
}

# ------------------------------------------------------------------------------
# Image coverage, decided BEFORE the first change.
#
# An offline box cannot build or pull, so the pre-restart check (Step 7b) refuses
# when an image is missing or a custom image is not the new tree's build. A refusal
# there comes after the code switch, the .env migration and the database and secret
# steps: a half-upgraded box. So the same question is asked here, of a scratch copy
# of what the tree will be (scripts/upgrade_image_preflight.py says how, and where
# the answer can differ). Step 7b stays as the backstop.
#
# Only the network mode decides whether this applies: it says whether this box may
# fall back to building or pulling. It is never derived from the package. An online
# or proxied box builds what a package does not cover and is not asked.
#
# "Cannot tell" never refuses. RAZZFAZZ_UPGRADE_IMAGE_PREFLIGHT=off (process
# environment, for this one run) skips the check should it ever refuse wrongly on
# an offline box, which has no other way out; Step 7b still decides then.
# ------------------------------------------------------------------------------
_IMAGE_PREFLIGHT_STATE=""     # covered | refused | unknown | empty: not asked
_IMAGE_PREFLIGHT_KIND=""      # stale | missing
_IMAGE_PREFLIGHT_ITEMS=""     # "<ref><TAB><why>" lines
_IMAGE_PREFLIGHT_REF=""       # the git target already judged
_IMAGE_PREFLIGHT_REFUSED=10

_image_preflight_applies() {
    [ "${CONFIG_NETWORK_MODE:-$(razzfazz_network_mode)}" = offline ] || return 1
    if [ "${RAZZFAZZ_UPGRADE_IMAGE_PREFLIGHT:-}" = off ]; then
        print_warning "Image check before the upgrade skipped (RAZZFAZZ_UPGRADE_IMAGE_PREFLIGHT=off). If an image is not covered, the upgrade refuses before the restart, after the code and .env have changed."
        return 1
    fi
    return 0
}

# The release's version pins, applied to the scratch .env: the one part of the .env
# migration that decides custom images (their build arguments) and pulled tags.
# What the release wants comes from scripts/release_pins.py, the resolution the
# offline packager uses too (migrate_env's pin sync from .env.example, then the
# manifest reconcile), so a package and this check cannot name different images.
# Whether a stateful pin may move stays migrate_env's own rule.
_image_preflight_sync_pins() {
    local stage="$1" tool pin want_val have_val plan
    [ -f "${stage}/.env" ] || return 0
    tool="${SCRIPT_DIR:-.}/scripts/release_pins.py"
    [ -f "$tool" ] || tool="${stage}/scripts/release_pins.py"
    [ -f "$tool" ] || return 0
    plan=$(python3 "$tool" plan --tree "$stage" --env "${stage}/.env" 2>/dev/null) || return 0
    while IFS=$'\t' read -r pin have_val want_val; do
        [ -n "$pin" ] && [ -n "$want_val" ] || continue
        [ "$(pin_sync_decision "$pin" "$have_val" "$want_val" 2>/dev/null)" = sync ] || continue
        update_env_value "${stage}/.env" "$pin" "$want_val"
    done <<< "$plan"
    return 0
}

_image_preflight_unknown() {
    _IMAGE_PREFLIGHT_STATE=unknown
    print_substep "Image check before the upgrade: cannot tell here ($1). The check before the restart decides."
    [ -z "${2:-}" ] || rm -rf "$2"
    _UPGRADE_IMG_STAGE_PENDING=""
    return 0
}

# upgrade_image_coverage_preflight <extracted package> | "" <git ref>
# Sets _IMAGE_PREFLIGHT_*; always returns 0. Writes only below its own scratch dir.
upgrade_image_coverage_preflight() {
    local pkg="$1" ref="${2:-}" py="${SCRIPT_DIR:-.}/scripts/upgrade_image_preflight.py"
    local work stage guard out rc=0 tag rest e
    local -a kind_args=()
    _IMAGE_PREFLIGHT_STATE=""; _IMAGE_PREFLIGHT_KIND=""; _IMAGE_PREFLIGHT_ITEMS=""
    _image_preflight_applies || return 0
    print_substep "Image check before the upgrade: this box is offline, so what the new release runs has to be here already..."
    [ -f "$py" ] || { _image_preflight_unknown "the check is not available"; return 0; }
    work=$(mktemp -d "${SCRIPT_DIR}/.upgrade-pkg-XXXXXX") || { _image_preflight_unknown "no scratch directory"; return 0; }
    _UPGRADE_IMG_STAGE_PENDING="$work"
    stage="${work}/stack"
    if [ -n "$pkg" ]; then
        kind_args=(--package-dir "$pkg")
        for e in "${PACKAGE_RSYNC_EXCLUDES[@]}"; do kind_args+=("--exclude=${e}"); done
    else
        kind_args=(--git-ref "$ref")
    fi
    if ! python3 "$py" stage --box "$SCRIPT_DIR" --stage "$stage" "${kind_args[@]}" >/dev/null 2>&1; then
        _image_preflight_unknown "the new tree could not be staged" "$work"
        return 0
    fi
    _image_preflight_sync_pins "$stage" || true
    if [ -n "${CONFIG_NETWORK_MODE:-}" ] && [ -f "${stage}/.env" ]; then
        update_env_value "${stage}/.env" RAZZFAZZ_NETWORK_MODE "$CONFIG_NETWORK_MODE"
    fi
    if ! guard=$(_preview_guard_dir); then
        _image_preflight_unknown "no read-only docker guard" "$work"
        return 0
    fi
    kind_args=()
    if [ -n "$pkg" ]; then
        kind_args=(--package-dir "$pkg" --strict)
        [ "${_PKG_SKIP_IMAGES:-false}" != true ] || kind_args+=(--images-skipped)
    fi
    out=$(RZFZ_PREVIEW_REAL_DOCKER="$(command -v docker 2>/dev/null || true)" \
          RZFZ_PREVIEW_REAL_DOCKER_COMPOSE="$(command -v docker-compose 2>/dev/null || true)" \
          python3 "$py" judge --stage "$stage" --work "$work" --guard "$guard" "${kind_args[@]}" </dev/null 2>/dev/null) || rc=$?
    rm -rf "$guard" "$work"
    _UPGRADE_IMG_STAGE_PENDING=""
    [ "$rc" -eq 0 ] || { _image_preflight_unknown "the check exited ${rc}"; return 0; }
    while IFS=$'\t' read -r tag rest; do
        case "$tag" in
            VERDICT) _IMAGE_PREFLIGHT_STATE="$rest" ;;
            KIND) _IMAGE_PREFLIGHT_KIND="$rest" ;;
            ITEM) _IMAGE_PREFLIGHT_ITEMS="${_IMAGE_PREFLIGHT_ITEMS}${rest}"$'\n' ;;
            WHY) _image_preflight_unknown "$rest" ;;
        esac
    done <<< "$out"
    case "$_IMAGE_PREFLIGHT_STATE" in
        covered) print_substep "Image check before the upgrade: every image the new release runs is covered." ;;
        refuse) _IMAGE_PREFLIGHT_STATE=refused ;;
        unknown) ;;
        *) _image_preflight_unknown "no verdict" ;;
    esac
    return 0
}

# The refusal: what is missing, what has happened, what to do. <what happened> is
# one sentence that must be true where it is called from.
_image_preflight_say_refusal() {
    local to="$1" happened="$2" prefix="${3:-}" ref why
    if [ "$_IMAGE_PREFLIGHT_KIND" = missing ]; then
        print_error "${prefix}Images the new release runs are not on this box. This box is set to offline mode, so it cannot pull or build them:"
    elif [ -n "${PACKAGE_FILE:-}" ]; then
        print_error "${prefix}This package does not carry the custom images the new release needs, and this box is set to offline mode, so it cannot build them:"
    else
        print_error "${prefix}The custom images on this box are not the build of the new release, and this box is set to offline mode, so it cannot build them:"
    fi
    while IFS=$'\t' read -r ref why; do
        [ -n "$ref" ] && print_error "    ${ref}: ${why}"
    done <<< "$_IMAGE_PREFLIGHT_ITEMS"
    print_error "${happened}"
    print_error "What to do, either of these:"
    print_error "  - Use a package that carries the images: on a machine with network access build a package of ${to} with its images included (see 'an offline package (Enterprise) --help'), then 'rzfz upgrade --package <that file>' here."
    print_error "  - If this box does have network access, re-run this upgrade with '--network-mode online': it builds and pulls what is missing, and the box stays in online mode afterwards."
}

# The git arm's check. Returns the refused status after saying so; the caller stops.
upgrade_image_preflight_git() {
    local ref="$1" happened="$2"
    _IMAGE_PREFLIGHT_REF="$ref"
    upgrade_image_coverage_preflight "" "$ref"
    [ "$_IMAGE_PREFLIGHT_STATE" = refused ] || return 0
    _image_preflight_say_refusal "$ref" "$happened" "$([ "${DRY_RUN:-false}" = true ] && echo '[check] WOULD REFUSE: ')"
    return "$_IMAGE_PREFLIGHT_REFUSED"
}

# Before the backup, for a target this box already holds. Other targets are known
# only after code_update_git's fetch, which asks there.
upgrade_image_preflight_git_before_backup() {
    local ref="" rc=0
    [ -n "${TARGET_TAG:-}" ] || return 0
    ref=$(razzfazz_resolve_release_tag "$TARGET_TAG" 2>/dev/null) || ref=""
    if [ -z "$ref" ] && _upgrade_target_is_local_commit "$TARGET_TAG"; then
        ref="$TARGET_TAG"
    fi
    [ -n "$ref" ] || return 0
    upgrade_image_preflight_git "$ref" "Nothing was changed: no backup, no code switch, no .env change, and the stack keeps running on ${INSTALLED_VERSION:-the installed release}." || rc=$?
    if [ "$rc" -eq "$_IMAGE_PREFLIGHT_REFUSED" ]; then
        # Nothing failed: no diagnostic bundle for a run that only said no.
        _REAL_UPGRADE_STARTED=""
        exit 1
    fi
    return 0
}

# The continuation's check. An older upgrader switched the code and handed over
# without having asked (or this run's scratch copy could not tell): ask now, on the
# real tree, before the .env migration and the database and secret steps.
upgrade_image_gate_in_continuation() {
    [ "${CONTINUE_AFTER_REEXEC:-false}" = true ] && [ "${DRY_RUN:-false}" != true ] \
        && [ "${MIGRATIONS_ONLY:-false}" != true ] || return 0
    [ "${_UPGRADE_IMAGE_PREFLIGHT:-}" != covered ] || return 0
    _image_preflight_applies || return 0
    local verifier="${SCRIPT_DIR}/core/config/app/services/expected_images.py" out rc=0 ref why
    local -a strict=()
    [ -f "$verifier" ] || return 0
    [ -z "${PACKAGE_FILE:-}" ] || strict=(--strict)
    out=$(python3 "$verifier" --stack-root "$SCRIPT_DIR" --verify --quiet ${strict[@]+"${strict[@]}"} 2>/dev/null) || rc=$?
    _IMAGE_PREFLIGHT_ITEMS=""
    if [ "$rc" -eq 3 ] && printf '%s\n' "$out" | grep -q '^STALE custom images ('; then
        _IMAGE_PREFLIGHT_KIND=stale
        _IMAGE_PREFLIGHT_ITEMS="$(printf '%s\n' "$out" | sed -n 's/^  \[  stale\] \([^ ]*\): /\1\t/p')"
    elif [ "$rc" -eq 1 ] && printf '%s\n' "$out" | grep -q '^MISSING images ('; then
        _IMAGE_PREFLIGHT_KIND=missing
        _IMAGE_PREFLIGHT_ITEMS="$(printf '%s\n' "$out" | sed -n 's/^  - \(.*\)$/\1\tnot on this box/p')"
    else
        return 0
    fi
    _image_preflight_say_refusal "${TARGET_VERSION:-<tag>}" "The code on this box is already ${TARGET_VERSION:-the new release}: the installed upgrader switched it before handing over. Not changed: .env, the databases, the secrets, the running stack and the version stamp (${INSTALLED_VERSION:-the installed release}). 'rzfz upgrade --rollback' puts the previous code back; do that or one of the two below before anything else restarts the stack."
    exit 1
}

code_update_package() {
    print_step "Updating code from offline package..."

    if [ ! -f "$PACKAGE_FILE" ]; then
        print_error "Package file not found: ${PACKAGE_FILE}"
        exit 1
    fi

    # #781: AUTHENTICITY, before anything is unpacked.
    #
    # This is a different question from the MANIFEST.sha256 check further down,
    # and it has to be asked here, on the archive FILE, because the manifest
    # travels inside the archive and is read after extraction — it proves the
    # stick was not corrupted, not that the stick is ours. A prepared package
    # brings its own matching manifest. Running this before `tar xzf` is the
    # whole structural point: after extraction the archive has already decided
    # what it is, which is why #755's symlink guard had to exist at all.
    #
    # Tri-state (0 authentic / 1 no evidence / 2 evidence says no). A bare
    # assignment under `set -e` would inherit that status, so the rc is
    # captured explicitly — the same shape #755/#793 got wrong once already.
    # `${EXPECT_SHA256:-}`, not `$EXPECT_SHA256`: this function is also driven
    # directly by the #268/#271 guard harnesses, which lift the body out and
    # run it without the script's global defaults. Under `set -u` a bare
    # reference there aborts the whole function at this line — silently
    # skipping the extraction the harness exists to observe.
    local auth_rc=0
    RAZZFAZZ_PACKAGE_EXPECT_SHA256="${EXPECT_SHA256:-}" \
        razzfazz_verify_package_authenticity "$PACKAGE_FILE" || auth_rc=$?
    if [ "$auth_rc" -eq 2 ]; then
        # Evidence was available and it did not pass. Never overridable —
        # a hash the operator supplied that does not match, or a signature
        # that does not verify, is evidence ABOUT THIS STICK.
        print_error "Package failed its authenticity check. Aborting."
        exit 1
    fi
    # Anything that is not a clean 0 is treated as "not verified", not as a
    # pass: rc 1 is the no-evidence case, and any OTHER non-zero means the
    # helper could not be run at all (an old scripts/lib.sh, a sourcing
    # failure). "I could not check" must never render as "it is fine" — the
    # same fail-safe as #755's tri-state.
    if [ "$auth_rc" -ne 0 ]; then
        # No out-of-package evidence at all. Every package built before #781 is
        # in this state, so refusing here would be a fleet-wide flag day rather
        # than a patch.
        #
        # THE FLIP IS DECIDED, and this is not it yet (operator, 2026-09-02):
        # unsigned → refuse happens in the release AFTER the first GA that
        # actually ships signed packages, and the override env stays. Until that
        # GA exists there is nothing for a box to have, so refusing now would
        # only mean refusing every package in the fleet.
        #
        # Which release that is (checked 2026-11-08): no GA has shipped signed
        # yet; 2026.10 was never tagged, so its "2026.11 flips" note (#781,
        # 2026-09-29) assumed a signed 2026.10-ga that does not exist. The first
        # 2026.11-ga ships unsigned; signing starts with 2027.01-ga (operator,
        # 2026-11-08), so the flip is the release after it, 2027.03.
        # The offline delivery decision record (section 8) says the same.
        #
        # What has to happen before the flip, in this order: (1) a GA whose
        # packages carry `<package>.openssl.sig`, (2) the fleet public key rolled
        # out to /etc/razzfazz/package-keys/ — that rollout is the one piece of
        # #781 still open, and it lives in the fleet tooling, not here.
        # Until then, this warns loudly and proceeds.
        print_warning "Package authenticity was NOT verified (#781)."
        print_warning "  Re-run with --expect-sha256 <hash> using the hash from the release"
        print_warning "  notes or the fleet channel to check this stick against something"
        print_warning "  that did not travel on it."
        print_warning "  A signed package (<package>.openssl.sig) needs no flag, but this"
        print_warning "  box must hold the fleet public key — RAZZFAZZ_PACKAGE_PUBKEY or"
        print_warning "  /etc/razzfazz/package-keys/razzfazz-packages.pem."
    fi

    # Verify package integrity
    print_substep "Verifying package..."
    local pkg_dir
    pkg_dir=$(mktemp -d "${SCRIPT_DIR}/.upgrade-pkg-XXXXXX")
    # #2137: armed from here — whatever ends this run before the function's
    # own `rm -rf` (a refusal, Ctrl-C at the question, a stopped backup), the
    # EXIT trap removes the extraction. It can hold images/ (#209).
    _UPGRADE_PKG_DIR_PENDING="$pkg_dir"

    # Safe extraction in ONE pass over the archive. #271: this used to run
    # `tar tzf` across the whole package just to enumerate members for a path
    # check, and then `tar xzf` across it again to extract — two full reads of
    # something that is 66 GB when it carries images, before a single file is
    # applied. On the Care Solutions box (2026-08-12) that was the bulk of the
    # customer's downtime.
    #
    # tar itself already refuses the dangerous half: a member containing `..`
    # aborts the extraction with a non-zero exit and nothing written. What it
    # does NOT refuse is an ABSOLUTE member — it strips the leading `/`,
    # extracts the file, and exits 0. Relocated is not safe here, because the
    # rsync below carries whatever sits in $pkg_dir into the installation. So
    # the "Removing leading" diagnostic is treated as fatal, which is exactly
    # what the old pre-scan's `/*` case did.
    #
    # LC_ALL=C because that diagnostic is translated and the fleet's boxes run
    # a German locale (`Entferne führende „/“ …`). Matching English wording is
    # only correct if we force English wording.
    local tar_err
    tar_err=$(mktemp)
    # #271/#2120: decide from the package's own manifest — before anything is
    # extracted — whether its images/ subtree needs loading at all. On a re-run
    # (or a box that already holds this package's images under the package's
    # own image ids) the subtree is neither extracted nor loaded. This is the
    # half of #271 that survived three partial fixes on the customer's
    # offline-upgrade path. Sets _PKG_SKIP_IMAGES.
    package_images_pregate "$PACKAGE_FILE"
    local -a _tar_excl=()
    [ "${_PKG_SKIP_IMAGES:-false}" = true ] && _tar_excl=(--exclude='./images/*' --exclude='images/*')
    if ! LC_ALL=C tar xzf "$PACKAGE_FILE" -C "$pkg_dir" "${_tar_excl[@]}" 2>"$tar_err"; then
        print_error "Package extraction failed. Aborting."
        sed 's/^/    /' "$tar_err" >&2
        rm -f "$tar_err"
        rm -rf "$pkg_dir"
        exit 1
    fi
    if grep -q "Removing leading" "$tar_err"; then
        print_error "Package contains unsafe paths. Aborting."
        sed 's/^/    /' "$tar_err" >&2
        rm -f "$tar_err"
        rm -rf "$pkg_dir"
        exit 1
    fi
    rm -f "$tar_err"

    # #755: the checks above look at member NAMES. A member that IS a symlink
    # carries its danger in the link TARGET, which no name check inspects —
    # `certs/leak.pem -> /etc/shadow` extracts with exit 0 and an empty stderr,
    # and the rsync below then carries that link into the installation. certs/
    # is the directory containers mount their CA bundle from.
    #
    # This is a directory walk over what was just extracted, not a second pass
    # over the archive — the #271 saving (one read of a 66 GB package) stands.
    # razzfazz_find_escaping_links is TRI-STATE: 0 = escapes found, 1 = clean,
    # 2 = could not look. Under `set -e` a bare `x=$(...)` assignment inherits
    # that exit status, so the CLEAN case (1) killed the upgrade at this line —
    # silently, with no message, because set -e fires before the check. That
    # regression shipped in #755 and is what this shape prevents.
    local escaping="" link_rc=0
    escaping=$(razzfazz_find_escaping_links "$pkg_dir") || link_rc=$?
    if [ "$link_rc" -eq 2 ]; then
        # Fail-safe: "I could not inspect it" is not "it is fine". This is the
        # only barrier between a prepared USB stick and the installation.
        print_error "Could not inspect the extracted package for unsafe symlinks. Aborting."
        rm -rf "$pkg_dir"
        exit 1
    fi
    if [ -n "$escaping" ]; then
        print_error "Package contains symlinks pointing outside the package. Aborting."
        printf '%s\n' "$escaping" | sed "s|^${pkg_dir}/|    |" >&2
        print_info "A package is delivered on physical media, not over an authenticated channel, and MANIFEST.sha256 travels INSIDE it — a prepared package simply brings a matching manifest. This path check is the barrier; it does not get to be advisory."
        rm -rf "$pkg_dir"
        exit 1
    fi

    print_substep "Package extracted."

    # Check for manifest
    if [ -f "${pkg_dir}/MANIFEST.sha256" ]; then
        print_substep "Verifying package checksums..."
        # Drop the manifest's own line before -c: packages built before the
        # package.sh fix carry a self-entry hashed mid-write, which can never
        # match and flagged every offline upgrade as failed.
        if (cd "$pkg_dir" && grep -v ' MANIFEST.sha256$' MANIFEST.sha256 | sha256sum -c - --quiet 2>/dev/null); then
            print_substep "Package integrity verified."
        else
            print_warning "Some checksums did not match — proceeding with caution."
        fi
    fi

    # #2280: the model bundle is read, verified and extracted HERE — after the
    # package is extracted (its catalogue is the one the pins are checked
    # against) and BEFORE the question, the backup, the rsync and the image
    # load. One pass over the archive: hash, member types and paths, sizes,
    # per-file pins, records; only the GGUFs this box lacks are written, next
    # to the package extraction. A refusal leaves the box exactly as it was.
    # --check verifies the same way and writes nothing.
    if [ -n "${MODEL_BUNDLE_FILE:-}" ]; then
        local _mb_rc=0 _mb_dest=""
        if [ "$DRY_RUN" != "true" ]; then
            _mb_dest="${pkg_dir}.model-bundle"
            _UPGRADE_BUNDLE_DIR_PENDING="$_mb_dest"
            mkdir -m 0700 "$_mb_dest"
        fi
        razzfazz_model_bundle_prepare "$MODEL_BUNDLE_FILE" "${pkg_dir}/core/llm/standard-models.yaml" "$_mb_dest" || _mb_rc=$?
        if [ "$_mb_rc" -ne 0 ]; then
            print_error "Model bundle $([ "$_mb_rc" -eq 2 ] && echo refused || echo "could not be read") (see above). Nothing was changed (#2280)."
            rm -rf "$pkg_dir" ${_mb_dest:+"$_mb_dest"}
            _UPGRADE_PKG_DIR_PENDING=""; _UPGRADE_BUNDLE_DIR_PENDING=""
            exit 1
        fi
    fi

    # Copy files (exclude .env, .env.dify, .git, backups, volumes)
    # #125 P1.5: enterprise-overlay/ is a SELF-CONTAINED payload, not a tree path — keep
    # it out of the repo root and stage it into the box-local overlay/enterprise/ below.
    # #268: THE one upgrade step that ignored --check. A preview that rsyncs
    # the package over the installation and docker-loads its images is not a
    # preview — the Care Solutions box (2026-08-12) ended up with new code and
    # new images beside its old env and old containers, a state nobody chose.
    # Extraction + checksum verification above already ran: that is the part of
    # a preview worth having (it proves the package is sound). Everything from
    # here mutates, so it stops here in check mode.
    if [ "$DRY_RUN" = "true" ]; then
        print_info "[check] WOULD apply package files over ${SCRIPT_DIR} (excluding .env/.env.dify/.git/backups/certs)."
        if [ "${_PKG_SKIP_IMAGES:-false}" = true ]; then
            print_info "[check] every bundled image is already present under the package's own image id — WOULD NOT extract or load images/ (#271/#2120)."
        elif ls "${pkg_dir}"/images/*.tar >/dev/null 2>&1; then
            print_info "[check] WOULD docker-load $(ls "${pkg_dir}"/images/*.tar 2>/dev/null | wc -l) bundled image archive(s) (archives whose content is already local by image id are skipped)."
        fi
        if [ -d "${pkg_dir}/models" ]; then
            print_info "[check] WOULD stage bundled model GGUFs into the gpustack-data volume."
        fi
        if [ -n "${MODEL_BUNDLE_FILE:-}" ]; then
            print_info "[check] model bundle ${MODEL_BUNDLE_FILE} verified (class, hash or signature, members, sizes, pins, records); WOULD stage the GGUFs this box lacks (#2280)."
        fi
        # The package's OWN version, read from the extracted tree BEFORE it is
        # discarded. `detect_target_version` reads VERSION from the working
        # directory — which in check mode still holds the INSTALLED version,
        # because nothing was copied in. Using it here would make
        # TARGET_VERSION == INSTALLED_VERSION, and the migration preview would
        # then report "no .env migrations needed" for every package, whatever
        # it actually carries: the single most valuable line of a preview,
        # silently wrong, in the very change meant to make --check honest.
        if [ -f "${pkg_dir}/VERSION" ]; then
            TARGET_VERSION="$(tr -d '[:space:]' < "${pkg_dir}/VERSION")"
        else
            print_warning "[check] package has no VERSION file — migration preview may be incomplete."
            detect_target_version
        fi
        # Same argument one level down, for the commit. TARGET_COMMIT is set at
        # line ~268 from `git rev-parse HEAD` of the WORKING directory, so in
        # check mode it is the INSTALLED commit. It feeds the commit-moved
        # rebuild decision further down, and with
        # INSTALLED_COMMIT == TARGET_COMMIT that branch can never fire — the
        # preview would systematically UNDERSTATE the work, on a run whose whole
        # purpose is to say what the upgrade will do. The package carries the
        # answer; read it while the extraction still exists.
        if [ -f "${pkg_dir}/PACKAGE_INFO" ]; then
            local _pkg_commit
            _pkg_commit=$(grep '^package_commit=' "${pkg_dir}/PACKAGE_INFO" \
                          | cut -d= -f2- | tr -d '[:space:]')
            [ -n "$_pkg_commit" ] && TARGET_COMMIT="$_pkg_commit"
        fi
        print_info "[check] Package version: ${TARGET_VERSION} (installed: ${INSTALLED_VERSION:-unknown})."
        print_info "[check] Package commit: ${TARGET_COMMIT:-unknown} (installed: ${INSTALLED_COMMIT:-unknown})."
        # The image check runs the package's own verifier, so only for a package
        # that passed its authenticity check: a check runs no unvouched code.
        if [ "${PREVIEW_ONLY:-false}" != true ]; then
            if [ "${auth_rc:-1}" -eq 0 ]; then
                upgrade_image_coverage_preflight "$pkg_dir" || true
                if [ "$_IMAGE_PREFLIGHT_STATE" = refused ]; then
                    _image_preflight_say_refusal "$TARGET_VERSION" "Nothing was changed." "[check] WOULD REFUSE: "
                    rm -rf "$pkg_dir"
                    _UPGRADE_PKG_DIR_PENDING=""
                    exit 1
                fi
            elif _image_preflight_applies 2>/dev/null; then
                print_info "[check] Image check not run: the authenticity of this package was not verified, so its verifier is not run for a preview. The real upgrade runs it once you confirm."
            fi
        fi
        print_info "[check] Package verified; nothing applied. Re-run without --check to upgrade."
        # PKG_TARGET_PREVIEW-BEGIN
        # #2138: the package carries its own upgrader. A package of another
        # release is previewed by it, and only when the package is authentic:
        # a check must not run code nothing vouches for.
        if [ "${PREVIEW_ONLY:-false}" != true ] && [ "$TARGET_VERSION" != "$INSTALLED_VERSION" ]; then
            local _prc=0
            if [ "${auth_rc:-1}" -ne 0 ]; then
                print_warning "Preview unavailable for a cross-version jump: this package's authenticity was not verified, so its upgrader is not run for a preview. Re-run with --expect-sha256 <the package's SHA-256 from the release notes>, or use a signed package: <package>.openssl.sig next to it and the fleet public key on this box (RAZZFAZZ_PACKAGE_PUBKEY or /etc/razzfazz/package-keys/razzfazz-packages.pem). Nothing was applied."
                _prc=3
            else
                preview_with_target_tree "$pkg_dir" || _prc=$?
            fi
            rm -rf "$pkg_dir"
            _UPGRADE_PKG_DIR_PENDING=""
            exit "$_prc"
        fi
        # PKG_TARGET_PREVIEW-END
        rm -rf "$pkg_dir"
        _UPGRADE_PKG_DIR_PENDING=""
        return 0
    fi

    # #2215: the package carries the target's upgrader — ask it before applying.
    if ! upgrade_script_knows_options "$(cat "${pkg_dir}/cli/upgrade.sh" 2>/dev/null || true)"; then
        rm -rf "$pkg_dir"
        exit 1
    fi

    # #2137: the package's version is known once extracted; nothing applied yet.
    if ! confirm_upgrade_before_changes "$(tr -d '[:space:]' < "${pkg_dir}/VERSION" 2>/dev/null || echo "${PACKAGE_FILE##*/}")"; then
        rm -rf "$pkg_dir"
        exit 0
    fi

    # Would the pre-restart check refuse this? Decide now, before the backup and
    # the copy. After the question: this runs the package's verifier, code the
    # operator has just agreed to run.
    upgrade_image_coverage_preflight "$pkg_dir" || true
    if [ "${_IMAGE_PREFLIGHT_STATE:-}" = refused ]; then
        _image_preflight_say_refusal "$(tr -d '[:space:]' < "${pkg_dir}/VERSION" 2>/dev/null || echo '<tag>')" \
            "Nothing was changed: no backup, no code switch, no .env change, and the stack keeps running on ${INSTALLED_VERSION:-the installed release}."
        rm -rf "$pkg_dir"
        _UPGRADE_PKG_DIR_PENDING=""
        _REAL_UPGRADE_STARTED=""   # nothing failed: no diagnostic bundle for a run that only said no
        exit 1
    fi

    # #2137: the first writes, now that the operator said yes. A backup that
    # stops the upgrade exits from inside; the EXIT trap removes the extracted
    # package then (armed at the mktemp above).
    backup_and_record_before_changes

    print_substep "Applying package files..."
    # images/ + models/ are docker-load / model-deploy payloads consumed straight
    # from ${pkg_dir} below — they must NOT be rsync'd into the repo. Copying them
    # in left a 153 GB images/ dir in the working tree (never cleaned; only pkg_dir
    # is), which then hangs the next online upgrade's `git stash -u` on the huge
    # untracked tree. Exclude them here. Refs #209.
    PACKAGE_PRUNE_PLAN="$(_rollback_state_dir)/pre-package-pruned.txt"
    package_plan_release_prune "$pkg_dir" "$SCRIPT_DIR" "$PACKAGE_PRUNE_PLAN"   # #2818
    snapshot_files_the_package_replaces "$pkg_dir" "$PACKAGE_PRUNE_PLAN"   # #2178
    rsync -a "${PACKAGE_RSYNC_EXCLUDES[@]/#/--exclude=}" \
          "${pkg_dir}/" "${SCRIPT_DIR}/"
    print_substep "Package files applied."
    package_apply_release_prune "$pkg_dir" "$SCRIPT_DIR" "$PACKAGE_PRUNE_PLAN"   # #2818
    package_apply_tests_tier "$pkg_dir" "$SCRIPT_DIR"   # #2348

    # #125 P1.5: stage a bundled Enterprise overlay (subscription package) into the
    # box-local, gitignored overlay/enterprise/ so an airgapped box gets the gated docs +
    # security assessment + SBOM with no network fetch. Best-effort; community packages
    # carry no enterprise-overlay/ so this is a no-op for them. Uses the freshly-applied
    # (new) sync-enterprise-overlay.sh.
    if [ -d "${pkg_dir}/enterprise-overlay" ] && [ -x "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" ]; then
        print_substep "Staging bundled Enterprise overlay into overlay/enterprise/ (#125)..."
        "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" --from-payload "${pkg_dir}/enterprise-overlay" 2>&1 \
          || print_warning "Enterprise overlay stage failed (Help-UI may serve community-only)."
    fi

    # #1554: refuse an image payload built for another architecture.
    #
    # `docker save` writes the packaging machine's image, so images/ is
    # single-architecture by construction. `docker load` accepts a foreign one
    # without complaint and every container then dies with "exec format error"
    # — after this upgrade has already replaced the code and restarted the
    # stack, on a box that is offline by definition. The fleet really is mixed:
    # a GB10 worker is arm64 while every other box is amd64
    # (modules/llm/runners/runners.yaml).
    #
    # Only when the package SAYS which architecture it holds. A package built
    # before this check carries no `image_arch` and is let through with a
    # warning rather than blocked — the operator's existing packages must keep
    # working.
    if _pkg_has_image_tars "$pkg_dir"; then
        local _pkg_arch _box_arch
        _pkg_arch=""
        [ -f "${pkg_dir}/PACKAGE_INFO" ] && _pkg_arch=$(grep '^image_arch=' "${pkg_dir}/PACKAGE_INFO" \
                                                        | cut -d= -f2- | tr -d '[:space:]')
        _box_arch=$(docker version --format '{{.Server.Arch}}' 2>/dev/null || true)
        [ -n "$_box_arch" ] || _box_arch=$(uname -m)
        # #1570 review: normalise BOTH, or a docker that does not answer turns
        # `x86_64` vs `amd64` into a refusal of a valid package — on exactly the
        # air-gapped box this check exists for, advising a rebuild the operator
        # has already done.
        _pkg_arch=$(rzfz_normalize_arch "$_pkg_arch")
        _box_arch=$(rzfz_normalize_arch "$_box_arch")
        if [ -z "$_pkg_arch" ] || [ -z "$_box_arch" ]; then
            # Unknown is NOT "differs" — same rule as a package built before
            # #1554: warn and continue, because a wrong refusal here strands a
            # box that has no other way to get its images.
            if [ -z "$_pkg_arch" ]; then
                print_warning "Package does not say which architecture its images are for (built before #1554)."
            else
                print_warning "Cannot determine this box's architecture (docker did not answer and uname was empty)."
            fi
            print_warning "  Package: ${_pkg_arch:-unknown}, box: ${_box_arch:-unknown}. If they differ, every container will fail with 'exec format error' after the restart."
        elif [ "$_pkg_arch" != "$_box_arch" ]; then
            print_error "Package images are ${_pkg_arch}, this box is ${_box_arch} — refusing to load them. (#1554)"
            print_error "  'docker load' would accept them and every container would then die with 'exec format error',"
            print_error "  after this upgrade has replaced the code and restarted the stack."
            print_error "  Obtain an offline package for ${_box_arch} from your razzfazz.ai contact (Enterprise)"
            return 1
        fi
    fi

    # Load Docker images if included — #271/#2120: skipped wholesale when the
    # pre-gate found every image present under the package's ids, and
    # per-archive otherwise (an archive whose content is already local by image
    # id is not loaded).
    package_load_images "$pkg_dir"

    # #2402: say what the package put here (#2168 parity with init), so the
    # build decision can tell the package's own builds from stale images.
    if [ -s "${pkg_dir}/expected-images.json" ]; then
        razzfazz_record_custom_image_builds "" package "${pkg_dir}/expected-images.json"
        razzfazz_retag_digest_pins "${pkg_dir}/expected-images.json"
        razzfazz_record_digest_pins "${pkg_dir}/expected-images.json"
    else
        # #2757: a package without images ships no manifest. The previous package's
        # on-box copy must not go on claiming images for this tree; its ids would
        # still match the old images. No manifest = the package claims nothing.
        rm -f "$(razzfazz_package_manifest_state_path)"
    fi

    # #184 P1 / WS7a(load): copy bundled model GGUFs into the gpustack-data
    # volume at $RAZZFAZZ_LOCAL_MODELS_DIR, BEFORE the post-restart provisioning,
    # so the OFFLINE deploy path (WS7b, source=local_path) finds them. Uses
    # `docker cp` into the running gpustack container (the stack is still up at
    # code-update time) — NO helper image, so it works on a fully air-gapped box
    # where `docker run alpine` would fail. Harmless online (the GGUFs just sit
    # in the volume until an offline deploy references them). Gate = the package
    # actually carries models/. #2227: on an LLM-Manager box the weights go into
    # the node agent's models volume instead (razzfazz_stage_package_models).
    if ls "${pkg_dir}"/models/* >/dev/null 2>&1; then
        # #2227: the runtime switch lives in scripts/lib.sh; a box without either
        # runtime container gets a loud "NOT staged", never a silent "deferring".
        razzfazz_stage_package_models "${pkg_dir}/models" || true
    fi

    # #2280: the model bundle's GGUFs, verified and extracted before anything
    # changed (above), go into the runtime's volume at the same moment as the
    # package's own models (the stack is still up). Nothing is read from the
    # bundle archive here; a copy that fails is a warning, never a refusal.
    if [ -n "${_UPGRADE_BUNDLE_DIR_PENDING:-}" ]; then
        if razzfazz_stage_model_bundle_dir "$_UPGRADE_BUNDLE_DIR_PENDING"; then
            print_success "Model bundle staged into the box's models volume (#2280)."
        else
            print_warning "Model bundle could not be staged — offline deploys may report missing GGUFs ('rzfz verify-models'); re-run the upgrade or 'rzfz post-install' with APPLIANCE_MODEL_BUNDLE=${MODEL_BUNDLE_FILE}."
        fi
        _UPGRADE_BUNDLE_DIR_PENDING=""
    fi
    # #2280: the continuation must know this upgrader dealt with the bundle
    # (verified before any change, staged above) — only a pre-2026.11
    # upgrader's continuation has to take it up (model_bundle_after_crossing).
    [ -z "${MODEL_BUNDLE_FILE:-}" ] || export _UPGRADE_MODEL_BUNDLE_HANDLED=1

    # #272: the offline rsync excludes .git, so detect_target_version's
    # `git rev-parse HEAD` stays at the PRE-upgrade commit. Read the real target
    # commit the package baked in (PACKAGE_INFO.package_commit, written by
    # the Enterprise package tooling from `git rev-parse <TAG>^{commit}` (#2309)) so RAZZFAZZ_COMMIT reflects the
    # deployed code, not the stale HEAD — otherwise `rzfz status` reports a false
    # "tag has MOVED on origin".
    local pkg_commit=""
    if [ -f "${pkg_dir}/PACKAGE_INFO" ]; then
        pkg_commit=$(grep '^package_commit=' "${pkg_dir}/PACKAGE_INFO" \
                     | cut -d= -f2- | tr -d '[:space:]')
    fi

    rm -rf "$pkg_dir"
    _UPGRADE_PKG_DIR_PENDING=""

    detect_target_version            # TARGET_VERSION from the package VERSION file (fresh)
    if [ -n "$pkg_commit" ] && [ "$pkg_commit" != "unknown" ]; then
        TARGET_COMMIT="$pkg_commit"  # authoritative deployed commit from the package
    else
        TARGET_COMMIT="offline-package"   # explicit non-git sentinel
    fi
    # Record offline-package provenance so `rzfz status` treats VERSION/
    # RAZZFAZZ_VERSION as authoritative and skips the git tag-drift check (#272).
    update_env_value ".env" "RAZZFAZZ_UPGRADE_METHOD" "offline-package"
    print_success "Code updated to version ${TARGET_VERSION} from package."
}

# ==============================================================================
# #2626: the version-pin syncs never LOWER a service that migrates its data
# ==============================================================================
# PIN_SYNC_GUARD-BEGIN  (migrate_env and reconcile_version_pins_from_manifest
# call into this block; a harness that slices either function slices this
# block with it. Keep the banner comments — the slices are anchored on them.)
# The #169 sync (inside migrate_env) and the #177 reconcile both force every
# *_VERSION pin to the shipped value — in BOTH directions. For a service whose
# database the service itself migrates on start, "down" is not a version
# change, it is a broken box: measured on 0.91, `rzfz upgrade --target main`
# rewrote AUTHENTIK_VERSION 2026.8.3 → 2026.5.7 over a 2026.8.3-migrated
# database, and authentik-server came up "healthy" with every API call failing
# (ProgrammingError: column authentik_sources_saml_samlsource.issuer does not
# exist). Authentik has no supported downgrade; neither has any service below.
#
# The repo has no machine-readable "stateful" flag, so this is the ONE explicit
# list, each entry with the place the repo already says so:
#   AUTHENTIK_VERSION  authentik-server   versions.json: "runs DB migrations";
#                                         COMPATIBILITY.md: breaking schema changes; F12 hop
#   KOMODO_DB_VERSION  postgres-komodo    postgres-documentdb datadir (PG major + extension);
#                                         #182 treats postgres-komodo-data as a physical datadir
#   DIFY_VERSION       dify-api           versions.json: "Runs Dify DB migrations on first
#                                         start"; #1587: a Dify downgrade needs alembic reset
#   GITEA_VERSION      gitea              versions.json: "Runs automatic gitea_db migrations"
#   OPENWEBUI_VERSION  openwebui          versions.json: chat-search backfill on first boot;
#                                         run_data_migrations #881 (0.11 crossing)
#   VALKEY_VERSION     valkey             #182: valkey-data is a physical datadir. Conservative:
#                                         only an RDB-format change (a major line) makes an
#                                         older valkey refuse the file; a patch downgrade is
#                                         low-harm, but a release no longer lowers it silently
#   CLICKHOUSE_VERSION clickhouse         upstream 25.10/26.3 changelog: parts a newer server
#                                         writes cannot be read by an older one (one-way);
#                                         rollback is a restore of the pre-upgrade backup
#   OBSERVABILITY_VERSION openlit         OpenLIT runs `prisma migrate deploy` on its SQLite
#                                         state at every start (2.x adds 18 migrations)
# NOT listed: POSTGRES_VERSION. It reads like the postgres pin, but no compose
# file reads it — core postgres is the hard-coded pgvector/pgvector:<x>-pg17
# image (versions.json `hardcoded.postgres`, frozen), which arrives with the
# checkout and no pin sync ever sees. Guarding the decorative pin would protect
# nothing; tests/unit/upgrade/test_2626 fails if a compose file starts to read it.
#
# Also NOT guarded: the env-changes `change_default`/`update_default` arm in
# migrate_env. It writes only where the value is still the rule's old default,
# and no current rule moves a *_VERSION key down (every one moves it forward),
# so it cannot lower a pin today. A future rule that lowers a stateful pin
# would bypass this guard.
#
# Known limits of the shape-only tokenizer below: a suffix is ignored
# (2026.8.3-rc1 = 2026.8.3), as is KOMODO_DB's ferretdb part, so a change only
# there is synced like any other — it is not a proven downgrade.
#
# "PIN:container" — the container is where the RUNNING version is read (the
# same `docker inspect .Config.Image` F12 uses). A stopped container of a
# disabled module still answers, and still counts: enabling the module again
# would recreate it at the pin. Every pin NOT listed keeps the sync exactly as
# before.
STATEFUL_VERSION_PINS="AUTHENTIK_VERSION:authentik-server KOMODO_DB_VERSION:postgres-komodo DIFY_VERSION:dify-api GITEA_VERSION:gitea OPENWEBUI_VERSION:openwebui VALKEY_VERSION:valkey WAZUH_VERSION:wazuh-indexer CLICKHOUSE_VERSION:clickhouse OBSERVABILITY_VERSION:openlit"

# Images that migrate their data forward on start but have NO .env pin (the tag
# lives in the module's compose file, so they cannot be in the list above):
# Synapse upgrades its database schema on start; Onyx's api-server runs
# `alembic upgrade head` and Vespa rewrites its on-disk index; paperless-ngx
# runs its Django migrations on start (3.0 also drops the task history and
# rebuilds the search index, #2837); Vaultwarden runs its diesel migrations on
# start (1.37.4 adds the add_key_id column). None of them supports a downgrade
# over the migrated data. pgvector joins them: its image carries the extension
# files, core/init-db.sh runs ALTER EXTENSION vector UPDATE in each database on
# the next reconcile, and an older image cannot serve a database whose extension
# was updated past it. Stirling-PDF 3.x migrates the H2 database it keeps in
# its /configs volume on the first start. `update_apply` asks
# hardcoded_image_sync_decision before editing the compose tag (#2629).
STATEFUL_HARDCODED_IMAGES="matrixdotorg/synapse onyxdotapp/onyx-backend vespaengine/vespa ghcr.io/paperless-ngx/paperless-ngx pgvector/pgvector vaultwarden/server stirlingtools/stirling-pdf"

# The container that runs a stateful pin's image ("" = none), or return 1 when
# the pin is not stateful.
_stateful_pin_container() {
    local _e
    for _e in $STATEFUL_VERSION_PINS; do
        [ "${_e%%:*}" = "$1" ] && { printf '%s' "${_e#*:}"; return 0; }
    done
    return 1
}

# The orderable part of an image tag / pin, for compare_versions (#1334, the
# ONE ordering). Shapes only, no ordering here: `v1.5.7`→1.5.7, `pg17`→17.0,
# `0.6.10-local`→0.6.10, `17-0.107.0-ferretdb-2.7.0`→17.0.107.0. Empty when
# the value has no leading number.
_pin_version_token() {
    local _t
    _t=$(printf '%s' "$1" | sed -E 's/^(v|pg)//; s/-/./g' | grep -oE '^[0-9]+(\.[0-9]+)*' | cut -d. -f1-4)
    [ -n "$_t" ] || return 0
    case "$_t" in *.*) ;; *) _t="${_t}.0" ;; esac
    printf '%s' "$_t"
}

# lt | eq | gt | unknown — $1 against $2, through compare_versions.
_pin_order() {
    local a b rc
    a=$(_pin_version_token "$1"); b=$(_pin_version_token "$2")
    if [ -z "$a" ] || [ -z "$b" ]; then echo unknown; return 0; fi
    rc=0; compare_versions "$a" "$b" 2>/dev/null || rc=$?
    case "$rc" in 0) echo lt ;; 1) echo eq ;; 2) echo gt ;; *) echo unknown ;; esac
}

# The tag the pin's container runs now, or empty (no container, no docker, a
# digest-only reference).
_stateful_pin_running() {
    local _c _img
    _c=$(_stateful_pin_container "$1") || return 0
    [ -n "$_c" ] || return 0
    command -v docker >/dev/null 2>&1 || return 0
    _img=$(docker inspect "$_c" --format '{{.Config.Image}}' 2>/dev/null || true)
    case "$_img" in ""|*@*|"<no value>") return 0 ;; esac
    case "${_img##*/}" in *:*) printf '%s' "${_img##*:}" ;; esac
}

# The ONE decision both syncs ask before writing PIN := WANT over HAVE.
# Prints "sync" or "keep"; on "keep" it has already said why.
# Non-stateful pins: always "sync" — the #169/#177 behaviour, unchanged.
# The running image needs no arm of its own here: running > want with
# have >= running means have > want (kept below), and running > want with
# have < running is refused before any sync (refuse_stateful_running_newer).
pin_sync_decision() {
    local pin="$1" have="$2" want="$3" running o_have
    _stateful_pin_container "$pin" >/dev/null || { echo sync; return 0; }
    running=$(_stateful_pin_running "$pin")
    o_have=$(_pin_order "$have" "$want")
    if [ "$o_have" = gt ]; then
        print_warning "  ${pin}: NOT lowered ${have} → ${want} (running: ${running:-unknown}). Downgrading a migrated database is not supported — keeping ${have}. To lower it deliberately, edit .env by hand, at your own risk. (#2626)" >&2
        echo keep; return 0
    fi
    if [ "$o_have" = unknown ]; then
        print_warning "  ${pin}: cannot order ${have} against ${want} — not changed, because a downgrade of a migrated database is not supported. Set it by hand if ${want} is newer. (#2626)" >&2
        echo keep; return 0
    fi
    echo sync
}

# sync | keep — the direction rule for a hardcoded compose image tag, the
# counterpart of pin_sync_decision. $1 image repo, $2 tag in the compose file,
# $3 tag the update wants. Images not in STATEFUL_HARDCODED_IMAGES always sync.
hardcoded_image_sync_decision() {
    local image="$1" have="$2" want="$3" o
    case " $STATEFUL_HARDCODED_IMAGES " in *" $image "*) ;; *) echo sync; return 0 ;; esac
    [ -n "$have" ] || { echo sync; return 0; }
    o=$(_pin_order "$have" "$want")
    # #2629: a migrated database is never lowered.
    if [ "$o" = gt ]; then
        print_warning "  ${image}: NOT lowered ${have} → ${want}. Downgrading a migrated database is not supported — keeping ${have}. To lower it deliberately, edit the compose file by hand, at your own risk." >&2
        echo keep; return 0
    fi
    if [ "$o" = unknown ]; then
        print_warning "  ${image}: cannot order ${have} against ${want} — not changed, because a downgrade of a migrated database is not supported. Set it by hand if ${want} is newer." >&2
        echo keep; return 0
    fi
    echo sync
}

# Refuse BEFORE migrate_env writes anything when a stateful container already
# runs a version newer than both its .env pin and the shipped pin: whatever the
# syncs then decide, the next `compose up` recreates it OLDER over its migrated
# data. Returns 1 (the upgrade stops — exit 1 under set -e, like the other
# refusals after the code update; the file has no refusal-code convention);
# 0 otherwise. An unknown running version (latest, a digest, no tag, docker
# failing) or one equal to the release never refuses.
#
# On a real run this fires AFTER the backup, the code update and the re-exec,
# so it says so, like #2215 and #2137: RAZZFAZZ_UPGRADE_IN_PROGRESS stays set
# (as those refusals leave it), and `rzfz status` reports the unfinished run.
refuse_stateful_running_newer() {
    [ -f "config/.env.example" ] || return 0
    local _e pin running have want refused=0 _fix="" _ctrs=""
    for _e in $STATEFUL_VERSION_PINS; do
        pin="${_e%%:*}"
        running=$(_stateful_pin_running "$pin")
        [ -n "$running" ] || continue
        want=$(read_env_value "config/.env.example" "$pin")
        [ -n "$want" ] || continue
        have=$(read_env_value ".env" "$pin")
        [ "$(_pin_order "$running" "$want")" = gt ] || continue
        if [ -n "$have" ] && [ "$(_pin_order "$running" "$have")" != gt ]; then continue; fi
        print_error "${pin}: the running container is ${running}, newer than the .env pin (${have:-unset}) and the shipped pin (${want}). Downgrading a migrated database is not supported. (#2626)"
        refused=1
        _fix="${_fix:+${_fix}, }${pin}=${running}"
        _ctrs="${_ctrs:+${_ctrs}, }$(_stateful_pin_container "$pin")"
    done
    [ "$refused" -eq 1 ] || return 0
    if [ "${DRY_RUN:-false}" = "true" ]; then
        print_info "  Nothing was changed. The upgrade would stop here: set ${_fix} in .env first. (#2626)"
        return 1
    fi
    if [ "${SKIP_BACKUP:-false}" = "true" ]; then
        print_info "  The code is already updated to ${TARGET_VERSION:-the target}; NO backup was taken (--skip-backup). No container and no version pin was changed."
    else
        print_info "  The code is already updated to ${TARGET_VERSION:-the target} and the pre-upgrade backup was taken. No container and no version pin was changed."
    fi
    print_warning "  Do NOT run 'docker compose up', 'rzfz restart' or enable a module before the pin is fixed: that recreates ${_ctrs} at the older pin over its migrated data. (#2626)"
    print_info "  To continue: set ${_fix} in .env and re-run 'rzfz upgrade'."
    # --rollback copies .env.pre-upgrade-backup over .env and force-recreates
    # every container — at the very pin this refusal is about. So the way back
    # names the same fix in that file; its #2178 record check reads only
    # RAZZFAZZ_VERSION/RAZZFAZZ_COMMIT, so the edit does not void it.
    # --skip-backup wrote no .env.pre-upgrade-backup for THIS run (any file of
    # that name is a previous upgrade's), so there is no rollback to offer.
    if [ "${SKIP_BACKUP:-false}" = "true" ]; then
        print_warning "  There is NO way back by 'rzfz upgrade --rollback': --skip-backup wrote no .env.pre-upgrade-backup for this run. The only way on is to set the pin and continue. (#2626)"
        return 1
    fi
    print_info "  To go back instead: set ${_fix} in .env.pre-upgrade-backup too (the rollback restores that file and recreates every container from it), then run 'rzfz upgrade --rollback'."
    return 1
}
# PIN_SYNC_GUARD-END

# ==============================================================================
# Environment Migration
# ==============================================================================
migrate_env() {
    print_step "Migrating environment files..."

    # #2626: a stateful container running newer than every pin stops here,
    # before the first .env write.
    refuse_stateful_running_newer || return 1

    # Snapshot .env before migration. #2138: not on --check — a snapshot is a
    # restore point for a change, and a check changes nothing.
    if [ "${DRY_RUN:-false}" != "true" ] && [ -f "${SCRIPT_DIR}/scripts/env-snapshot.sh" ]; then
        source "${SCRIPT_DIR}/scripts/env-snapshot.sh"
        env_snapshot "pre-upgrade-migration"
    fi

    local manifest="${SCRIPT_DIR}/config/migrations/env-changes.json"
    if [ ! -f "$manifest" ]; then
        print_warning "No migration manifest found. Skipping .env migration."
        return 0
    fi

    # Parse manifest and apply changes for versions between installed and target
    local changes_applied=0
    local warnings=0

    # Use Python for JSON parsing (available in all Docker-capable systems)
    local migration_script
    migration_script=$(cat <<'PYEOF'
import json
import sys
import os

def _load_version_order(script_dir):
    """The ONE parser (#1334), loaded from scripts/version_order.py.

    This heredoc used to carry its own copy of the regex pair plus a
    split-on-dots fallback — and so did sort_key twenty lines below. Three
    copies of an ordering is three chances to be differently wrong, and this
    one was the copy that DECIDES which manifest blocks run.

    The stack root arrives as argv, not derived from the manifest path: the
    manifest is DATA and may legitimately live elsewhere (a test fixture, an
    operator checking a copy), while the code root is the thing the caller
    actually knows.
    """
    import importlib.util
    import os
    mod_path = os.path.join(script_dir, "scripts", "version_order.py")
    spec = importlib.util.spec_from_file_location("rzfz_version_order", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


installed = sys.argv[1]
target = sys.argv[2]
manifest_path = sys.argv[3]
check_only = sys.argv[4] == "true"
script_dir = sys.argv[5] if len(sys.argv) > 5 else os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(manifest_path))))

_vo = _load_version_order(script_dir)

# #1334: an installed or target version this module cannot ORDER must not be
# answered with "No .env migrations needed" — that is the sentence a box with
# `2026.04-M011` in its .env got on every upgrade while zero of 81 blocks ran.
for _label, _v in (("installed", installed), ("target", target)):
    if not _vo.orderable(_v):
        print(f"UNORDERABLE:{_label}:{_v}")
        sys.exit(0)


def compare_versions(v1, v2):
    return _vo.compare(v1, v2)

with open(manifest_path) as f:
    manifest = json.load(f)

# Find versions to apply (installed < version <= target).
#
# #1824 — ONE exception, and it is the case a pre-release box lives in.
#
# The manifest block of the version currently being developed KEEPS GROWING:
# `2026.09-rc1` gained the #1444 (C4) `GPUSTACK_DOMAIN` change_default on
# 2026-09-05 and the #1445 (C5) consumer defaults after that, while reference
# boxes had already been STAMPED `2026.09-rc1`. For those boxes the window
# `installed < v <= target` is empty, so everything added to their own block
# after the stamp is unreachable — not "later", but never, because the window
# stays empty on every future run too. Measured on 0.91: `.env.dify`
# OPENAI_API_BASE and `.env` GPUSTACK_DOMAIN both still on the ga.15 default,
# with "No .env migrations needed for this version range" in the log.
#
# So when installed == target — which `check_upgrade_direction` already names
# out loud, "re-running <v> (same version)" — the block for THAT version is
# included. This is safe by construction, not by hope: every arm of the apply
# side guards on current state. `change_default` writes only where the value is
# still the OLD default ("custom value — preserved" otherwise), `add` decides on
# PRESENCE (#1571) and leaves a deliberately empty `KEY=` alone, and `remove`
# only deletes a line that exists. Re-applying cannot overwrite an operator's
# value.
#
# It stays an exception on purpose: for installed < target the strict window is
# right, because those blocks have already run in their own upgrade.
applicable = []
unorderable_blocks = []
_same_version_rerun = compare_versions(installed, target) == 0
for entry in manifest["versions"]:
    v = entry["version"]
    if not _vo.orderable(v):
        # A block nobody can place is a block that silently never runs.
        unorderable_blocks.append(v)
        continue
    _lower = compare_versions(v, installed)
    _in_window = (_lower >= 0) if _same_version_rerun else (_lower > 0)
    if _in_window and compare_versions(v, target) <= 0:
        applicable.append(entry)
for v in unorderable_blocks:
    print(f"UNORDERABLE:block:{v}")

# Sort by version — the SAME parser, not a third copy of it (#1334).
applicable.sort(key=lambda e: _vo.parse(e["version"]))

if not applicable:
    print("NO_MIGRATIONS")
    sys.exit(0)

needs_build = False
needs_pull = False

# 2026.11: an `add` that a LATER `remove` in this same window takes back (a
# retired module's keys, added by an older block and removed by a newer one)
# is skipped. Applying both left the add's comment line behind, one more on
# every re-run of the window, so a second --migrations-only run changed .env.
# The end state (key absent) is the same either way.
_removed_later = set()
_pending = {}
for _i, _entry in enumerate(applicable):
    for _j, _c in enumerate(_entry.get("env_changes", [])):
        _k = (_c.get("file", ".env"), _c.get("key"))
        if _c.get("action") == "add":
            _pending[_k] = (_i, _j)
        elif _c.get("action") == "remove" and _k in _pending:
            _removed_later.add(_pending.pop(_k))

for _i, entry in enumerate(applicable):
    v = entry["version"]
    print(f"VERSION:{v}")

    if entry.get("requires_build"):
        needs_build = True
    if entry.get("requires_pull"):
        needs_pull = True

    for bc in entry.get("breaking_changes", []):
        print(f"BREAKING:{bc}")

    if entry.get("notes"):
        print(f"NOTES:{entry['notes']}")

    for _j, change in enumerate(entry.get("env_changes", [])):
        action = change["action"]
        target_file = change.get("file", ".env")
        if action == "add" and (_i, _j) in _removed_later:
            continue

        # `from_older_only`: the entry is a one-time move of a shipped default
        # whose NEW value the operator may deliberately set back to the OLD one.
        # (Its first user, RAZZFAZZ_REQUIRE_ADMIN_MFA, is a `note` since #3174:
        # an upgrade must not switch admin MFA enforcement off.) A same-version
        # re-run replays the block (see above), and change_default cannot tell
        # that deliberate value from the old default, so it would silently undo
        # the operator's choice. Such an entry applies only when coming from an
        # older version.
        if change.get("from_older_only") and _same_version_rerun:
            continue

        # Field separator is U+001F (ASCII Unit Separator) so URL-shaped
        # `default` values (e.g. https://creators.dify.ai) survive the bash
        # IFS=$'\x1f' read on the consumer side. Pre-rc6.2 used ':' which
        # collided with URLs and silently truncated values.
        SEP = "\x1f"
        if action == "add":
            print(f"ADD:{target_file}{SEP}{change['key']}{SEP}{change.get('default', '')}{SEP}{change.get('comment', '')}")
        elif action == "remove":
            print(f"REMOVE:{target_file}{SEP}{change['key']}")
        elif action == "rename":
            print(f"RENAME:{target_file}{SEP}{change['old_key']}{SEP}{change['new_key']}")
        elif action in ("change_default", "update_default"):
            # #1963: `update_default` is the same operation spelled differently
            # in 2026.04-version-updates. It was in the manifest, passed the
            # validator, and fell off the end of this chain — four image-version
            # bumps that looked like migrations and did nothing, for five months.
            print(f"CHANGE_DEFAULT:{target_file}{SEP}{change['key']}{SEP}{change.get('old_default', '')}{SEP}{change.get('new_default', '')}{SEP}{change.get('custom_warning', '')}")
        elif action == "note":
            # Not a migration step: an annotation that belongs to a version.
            # It is PRINTED rather than skipped — a note in the migration
            # manifest that the operator never sees has no reason to exist.
            print(f"NOTE:{change.get('key', '')}{SEP}{change.get('comment', '')}")
        else:
            # #1963: the `if/elif` chain had no `else`. An action nobody
            # implemented was silently nothing — no error, no output, and the
            # defect shows only as an ABSENCE. That is why it survived five
            # months and a whole release line. Refuse instead, in the same shape
            # #1334 gave an unorderable version.
            print(f"UNKNOWN_ACTION:{entry.get('version', '?')}{SEP}{action}{SEP}{change.get('key', '')}")

if needs_build:
    print("FLAG:REQUIRES_BUILD")
if needs_pull:
    print("FLAG:REQUIRES_PULL")
PYEOF
    )

    local migration_output
    migration_output=$(python3 -c "$migration_script" \
        "$INSTALLED_VERSION" "$TARGET_VERSION" "$manifest" "$DRY_RUN" \
        "$SCRIPT_DIR" 2>&1)

    if [ $? -ne 0 ]; then
        print_error "Failed to parse migration manifest: $migration_output"
        return 1
    fi

    # #1334: a version the ordering refuses is NOT "nothing to do". A box whose
    # RAZZFAZZ_VERSION reads `2026.04-M011` used to get "No .env migrations
    # needed" and RC 0 on every single upgrade while 0 of 81 blocks ran.
    if printf '%s\n' "$migration_output" | grep -q '^UNORDERABLE:'; then
        local _u
        while IFS= read -r _u; do
            case "$_u" in
                UNORDERABLE:installed:*)
                    print_error "Cannot order the INSTALLED version '${_u#UNORDERABLE:installed:}' — refusing to guess which migrations apply."
                    print_info  "  Set RAZZFAZZ_VERSION in .env to the release this box actually runs (e.g. 2026.08-ga.15) and re-run. (#1334)" ;;
                UNORDERABLE:target:*)
                    print_error "Cannot order the TARGET version '${_u#UNORDERABLE:target:}' — refusing to guess which migrations apply. (#1334)" ;;
                UNORDERABLE:block:*)
                    print_warning "Migration manifest block '${_u#UNORDERABLE:block:}' cannot be ordered and was SKIPPED — it can never run. (#1334)" ;;
            esac
        done < <(printf '%s\n' "$migration_output" | grep '^UNORDERABLE:')
        # An unorderable installed/target version is fatal; an unorderable
        # BLOCK is a manifest defect that must be visible but must not stop an
        # upgrade whose own two versions are fine.
        if printf '%s\n' "$migration_output" | grep -q '^UNORDERABLE:\(installed\|target\):'; then
            return 1
        fi
    fi

    # #1963: an action the migrator does not implement stops the upgrade. The
    # manifest DECLARES its vocabulary (`env_change_actions`) and both the
    # validator and the migrator read it from there, so this can only fire when
    # someone adds an action to the manifest without teaching the migrator what
    # it means — and then it must fire, because the alternative is the silent
    # skip this issue is named after.
    if printf '%s\n' "$migration_output" | grep -q '^UNKNOWN_ACTION:'; then
        local _ua _uv _uact _ukey
        while IFS= read -r _ua; do
            IFS=$'\x1f' read -r _uv _uact _ukey <<< "${_ua#UNKNOWN_ACTION:}"
            print_error "Migration manifest v${_uv}: action '${_uact}' (key ${_ukey:-?}) is declared but not implemented — refusing to apply a migration set whose steps would be silently skipped. (#1963)"
        done < <(printf '%s\n' "$migration_output" | grep '^UNKNOWN_ACTION:')
        return 1
    fi

    if [ "$migration_output" = "NO_MIGRATIONS" ]; then
        print_substep "No .env migrations needed for this version range."
        return 0
    fi

    REQUIRES_BUILD=false
    REQUIRES_PULL=false

    while IFS= read -r line; do
        local cmd="${line%%:*}"
        local rest="${line#*:}"

        case "$cmd" in
            VERSION)
                print_substep "Applying migrations for v${rest}..."
                ;;
            BREAKING)
                print_warning "BREAKING CHANGE: ${rest}"
                ((warnings++)) || true
                ;;
            NOTES)
                print_info "  ${rest}"
                ;;
            NOTE)
                # #1963: a per-key annotation from env_changes. Shown, never applied.
                local _note_key _note_text
                IFS=$'\x1f' read -r _note_key _note_text <<< "$rest"
                print_info "  note (${_note_key:-general}): ${_note_text}"
                ;;
            FLAG)
                case "$rest" in
                    REQUIRES_BUILD) REQUIRES_BUILD=true ;;
                    REQUIRES_PULL) REQUIRES_PULL=true ;;
                esac
                ;;
            ADD)
                IFS=$'\x1f' read -r target_file key default comment <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would add ${key}=${default} to ${target_file}"
                else
                    # #1571: decide on PRESENCE, not on emptiness.
                    # `read_env_value` returns "" for a key that is missing AND
                    # for one that is present but empty, and the old test
                    # (`[ -z "$current" ]`) folded the two together. That is not
                    # a cosmetic mislabel: `update_env_value` REPLACES an
                    # existing line, so an operator's deliberate `KEY=` was
                    # overwritten with the manifest default. For
                    # LLM_MANAGER_CA_PEM that is exactly backwards — an empty
                    # value switches the CA pin OFF on purpose (#1562:
                    # os.environ.get(key, DEFAULT) returns "" for a set-but-empty
                    # key, never the default), and the migration switched it back
                    # on. The comment also landed at the END of the file, because
                    # update_env_value edits the existing line in place while the
                    # `echo >>` appends regardless.
                    #
                    # The right primitive is three lines down in REMOVE): ask
                    # whether the line is there. Three states, not two.
                    # #1618: the same anchor the primitives use. Asking
                    # strictly here while `update_env_value` asks loosely is
                    # WORSE than both being strict: this arm would say
                    # "absent", the primitive "present", and the operator's
                    # line would be OVERWRITTEN instead of a duplicate
                    # appended — #1571 through the back door.
                    if ! grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$target_file" 2>/dev/null; then
                        if [ -n "$comment" ]; then
                            echo "# ${comment}" >> "$target_file"
                        fi
                        update_env_value "$target_file" "$key" "$default"
                        print_substep "  Added ${key} to ${target_file}"
                        ((changes_applied++)) || true
                    elif [ -z "$(read_env_value "$target_file" "$key")" ]; then
                        # Present and empty: the operator's value, and on this
                        # box an empty value usually MEANS something ("off",
                        # "ask the GPU", "no override"). Leave it, and say so —
                        # "already exists — skipped" would suggest it carries the
                        # default.
                        print_substep "  ${key} is present but empty in ${target_file} — left as the operator set it"
                    else
                        print_substep "  ${key} already exists in ${target_file} — skipped"
                    fi
                fi
                ;;
            REMOVE)
                IFS=$'\x1f' read -r target_file key <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would remove ${key} from ${target_file}"
                else
                    if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$target_file" 2>/dev/null; then
                        # #1618: delete the line however it was written.
                        # Same inode: the Settings portal binds this single file.
                        delete_env_lines "$target_file" "^[[:space:]]*(export[[:space:]]+)?${key}="
                        print_substep "  Removed ${key} from ${target_file}"
                        ((changes_applied++)) || true
                    fi
                fi
                ;;
            RENAME)
                IFS=$'\x1f' read -r target_file old_key new_key <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would rename ${old_key} → ${new_key} in ${target_file}"
                else
                    local old_val
                    old_val=$(read_env_value "$target_file" "$old_key")
                    if [ -n "$old_val" ]; then
                        update_env_value "$target_file" "$new_key" "$old_val"
                        delete_env_lines "$target_file" "^${old_key}="
                        print_substep "  Renamed ${old_key} → ${new_key} in ${target_file}"
                        ((changes_applied++)) || true
                    fi
                fi
                ;;
            # CHANGE_DEFAULT-BEGIN  (sliced verbatim by
            # tests/unit/migrations/test_245_an_upgrade_corrects_the_dify_otel_values.py,
            # which RUNS this branch against a staged .env.dify with the real
            # read_env_value/update_env_value. Keep the banner comments; the
            # slice is anchored on them.)
            #
            # The `current_val = old_default` comparison is the whole safety
            # property: a key the operator changed is PRESERVED, and only the
            # value we ourselves shipped is corrected. That is why a stale
            # default belongs here and not in sync_dify_env, which would have
            # to overwrite blindly.
            CHANGE_DEFAULT)
                IFS=$'\x1f' read -r target_file key old_default new_default custom_warning <<< "$rest"
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would change default of ${key}: ${old_default} → ${new_default}"
                else
                    local current_val
                    current_val=$(read_env_value "$target_file" "$key")
                    if [ "$current_val" = "$old_default" ]; then
                        update_env_value "$target_file" "$key" "$new_default"
                        print_substep "  Changed ${key} default in ${target_file}"
                        ((changes_applied++)) || true
                    elif [ "$current_val" = "$new_default" ]; then
                        # BS-OBSVOPS-BUG-01: this arm used to be missing, so a
                        # value ALREADY at the new default was reported as
                        # "custom value — preserved". It is not custom: it is
                        # what we ourselves set on the previous upgrade. The
                        # wrong label costs exactly when it hurts — an operator
                        # reading the upgrade journal to find out whether a
                        # migration took effect is told the box overrode it.
                        print_substep "  ${key} already at the new default"
                    else
                        print_substep "  ${key} has custom value — preserved"
                        # #2472: an entry may carry `custom_warning` for a key
                        # whose new default is an ADDITION to a list the operator
                        # has edited: we never rewrite their value, so we say
                        # what they must add themselves.
                        if [ -n "${custom_warning:-}" ]; then
                            print_warning "  ${key}: ${custom_warning}"
                        fi
                    fi
                fi
                ;;
            # CHANGE_DEFAULT-END
        esac
    done <<< "$migration_output"

    # ------------------------------------------------------------------
    # #169: vendor-managed version pins self-heal on upgrade.
    #
    # Every component version lives in .env as a *_VERSION pin — the
    # third-party image tags (GOTENBERG_VERSION, KOMODO_VERSION,
    # SEARXNG_VERSION, VALKEY_VERSION, AUTHENTIK_VERSION, …) AND the
    # custom-image build ARGs (OPENCODE_VERSION, GSD_PI_VERSION,
    # PAPERCLIP_VERSION). Historically a version bump touched .env.example +
    # the manifest (and, for builds, the Dockerfile ARG) but NOT the
    # operator's LIVE .env, because no change_default rule was added. So the
    # live pin drifted: third-party images stayed on the old tag and custom
    # images rebuilt from the stale pin — indefinitely, across upgrades.
    #
    # These are NOT operator-tunable configuration — version selection is a
    # release decision (to hold a version, the release holds it in
    # .env.example). So on every upgrade we force-sync EVERY non-empty
    # *_VERSION pin from the shipped .env.example to the live .env. Unlike a
    # change_default rule this is box-history-independent: it corrects ANY
    # drifted value, not just one known old default. Empty .env.example
    # values are skipped — crucially this leaves RAZZFAZZ_VERSION (empty in
    # the template, stamped by the upgrade itself) untouched. Any sync forces
    # a build + pull so the corrected pins actually take effect.
    if [ -f "config/.env.example" ]; then
        local pin want_val have_val
        for pin in $(grep -oE '^[A-Z0-9_]+_VERSION=' config/.env.example | sed 's/=$//' | sort -u); do
            want_val=$(read_env_value "config/.env.example" "$pin")
            [ -z "$want_val" ] && continue          # empty template default (e.g. RAZZFAZZ_VERSION) — never sync
            have_val=$(read_env_value ".env" "$pin")
            [ -z "$have_val" ] && continue          # absent in live .env → leave to any ADD rule
            [ "$have_val" = "$want_val" ] && continue
            # #2626: never lower a service that migrates its own data.
            [ "$(pin_sync_decision "$pin" "$have_val" "$want_val")" = sync ] || continue
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would sync version pin ${pin}: ${have_val} → ${want_val}"
            else
                update_env_value ".env" "$pin" "$want_val"
                print_substep "  Synced version pin ${pin}: ${have_val} → ${want_val}"
                ((changes_applied++)) || true
                REQUIRES_BUILD=true
                REQUIRES_PULL=true
            fi
        done
    fi

    # ------------------------------------------------------------------
    # #182: physical DB datadirs must ALWAYS be excluded from the volume archive.
    #
    # 2026.07 flipped postgres + postgres-komodo to stop-during-backup=false so
    # the nightly backup no longer stops postgres (which caused a ~24 s DB-
    # unavailability blip → dependent apps 500). That is ONLY safe because the
    # physical datadirs are captured LOGICALLY — core/backup/pre-backup.sh runs
    # `pg_dumpall` (postgres_core.sql + postgres_komodo.sql) and valkey SAVE —
    # and EXCLUDED from the tar, so offen never tars a churning pg_wal (the
    # `lstat …/pg_wal/…: no such file` race that silently failed the whole
    # nightly + upgrade backup, #157).
    #
    # The matching manifest `change_default` (old `gpustack-data|speaches-data`
    # → new 5-token default) only fires when the live value EXACTLY equals the
    # old shipped default, so it MISSES boxes with a customised regexp (models
    # included, dify-plugins excluded, or the PSA hot-fix already applied). On
    # such a box the label flip would leave postgres-data IN the tar with the
    # stop OFF = #157 again. This heal closes that gap: box-history-independent
    # (same principle as the #169 version-pin sync above), it force-APPENDS any
    # of the three datadir tokens that are missing, preserving every other
    # exclusion the operator set. Idempotent — a token already present (exact
    # `|`-delimited match, so `postgres-data` ≠ `postgres-komodo-data`) is left
    # untouched. Backup-service picks up the new .env value on the post-upgrade
    # `compose up -d` (env change → recreate), so no build/pull is forced here.
    local _cur_excl _new_excl _tok
    _cur_excl=$(read_env_value ".env" "BACKUP_EXCLUDE_REGEXP")
    _new_excl="$_cur_excl"
    for _tok in postgres-data valkey-data postgres-komodo-data; do
        case "|${_new_excl}|" in
            *"|${_tok}|"*) : ;;   # already excluded — leave alone
            *) if [ -n "$_new_excl" ]; then _new_excl="${_new_excl}|${_tok}"; else _new_excl="$_tok"; fi ;;
        esac
    done
    if [ "$_new_excl" != "$_cur_excl" ]; then
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would exclude DB datadirs from backup (#182): '${_cur_excl}' → '${_new_excl}'"
        else
            update_env_value ".env" "BACKUP_EXCLUDE_REGEXP" "$_new_excl"
            print_substep "  Excluded physical DB datadirs from backup (#182): '${_cur_excl}' → '${_new_excl}'"
            ((changes_applied++)) || true
        fi
    fi

    # ── #1444 (cutover C4): the llm.<domain> rochade ────────────────────────
    # GPUStack's console moves from llm.<domain> to gpustack.<domain>; the LLM
    # Manager takes llm.<domain>. The manifest `change_default` alone CANNOT do
    # this: cli/upgrade.sh compares the live value LITERALLY against the
    # manifest's `llm.${MAIN_DOMAIN}`, and every installed box holds the
    # EXPANDED value (`llm.acme.example`) — measured on the DevBox, where every
    # *_DOMAIN key is expanded. The migration was a no-op there, GPUSTACK_DOMAIN
    # stayed on llm.<domain>, the new LLM_DOMAIN claimed the same address and
    # Caddy refused the whole config ("ambiguous site definition") — a total
    # outage of every vhost after `rzfz upgrade` (review #1477, agent-seqis).
    # So: compare BOTH spellings, and only heal a value that IS the old default.
    # An operator's own name (gpu.kunde.tld) is never touched.
    local _mig_main _mig_gpu _mig_llm _tmpl='${MAIN_DOMAIN}'
    _mig_main=$(read_env_value ".env" "MAIN_DOMAIN" 2>/dev/null || true)
    _mig_gpu=$(read_env_value ".env" "GPUSTACK_DOMAIN" 2>/dev/null || true)
    _mig_llm=$(read_env_value ".env" "LLM_DOMAIN" 2>/dev/null || true)
    if [ -n "$_mig_main" ]; then
        local _old_literal="llm.${_tmpl}" _old_expanded="llm.${_mig_main}"
        if [ "$_mig_gpu" = "$_old_literal" ] || [ "$_mig_gpu" = "$_old_expanded" ]; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would move GPUStack's console to gpustack.${_mig_main} (#1444): '${_mig_gpu}' → 'gpustack.${_mig_main}'"
            else
                update_env_value ".env" "GPUSTACK_DOMAIN" "gpustack.${_mig_main}"
                print_substep "  GPUStack console moved to gpustack.${_mig_main} (#1444) — llm.${_mig_main} is the LLM Manager from this release on."
                ((changes_applied++)) || true
            fi
            _mig_gpu="gpustack.${_mig_main}"
        elif [ -n "$_mig_gpu" ]; then
            print_info "  GPUSTACK_DOMAIN is '${_mig_gpu}' (not the old default) — left as it is (#1444)."
        fi
        # LLM_DOMAIN: the manifest ADD writes the literal template, and every
        # other *_DOMAIN key on a real box is expanded. Write the expanded form
        # so the two keys are comparable (and so `docker compose config` shows
        # what Caddy will actually serve).
        if [ -z "$_mig_llm" ] || [ "$_mig_llm" = "llm.${_tmpl}" ]; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would set LLM_DOMAIN=llm.${_mig_main} (#1444)"
            else
                update_env_value ".env" "LLM_DOMAIN" "llm.${_mig_main}"
                print_substep "  LLM_DOMAIN=llm.${_mig_main} (the LLM Manager console, #1444)."
                ((changes_applied++)) || true
            fi
            _mig_llm="llm.${_mig_main}"
        fi
        # Last line of defence: never leave the box with two site blocks on one
        # address. A box whose operator set both names by hand reaches
        # `docker compose up` with a Caddyfile that cannot load — and Caddy
        # refuses the WHOLE configuration, so every vhost on the box goes down,
        # not just these two.
        #
        # rerevB finding 2: this used to print a warning and increment
        # `warnings`, which nothing reads — the migration returned 0 and the
        # upgrade walked straight into the outage it had just described. A
        # warning about a total outage is not a control. It RETURNS non-zero
        # now, so the caller stops; the operator fixes two lines in .env and
        # re-runs. That is a minute of work against every service on the box.
        local _g="${_mig_gpu//"$_tmpl"/$_mig_main}" _l="${_mig_llm//"$_tmpl"/$_mig_main}"
        if [ -n "$_g" ] && [ "$_g" = "$_l" ]; then
            print_error   "  GPUSTACK_DOMAIN and LLM_DOMAIN are BOTH '${_g}' (#1444) — Caddy would refuse the whole"
            print_info    "  configuration ('ambiguous site definition') and every vhost on this box would go down."
            print_info    "  Set them to different names in .env (gpustack.${_mig_main} / llm.${_mig_main}) and re-run."
            ((warnings++)) || true
            return 1
        fi
    fi

    # #906: every `add` above can bring a fresh `X.${MAIN_DOMAIN}` template into
    # .env, and a box installed before this fix still carries the old ones.
    # compose does NOT interpolate `env_file:` values, so any service reading
    # .env that way gets the literal `${MAIN_DOMAIN}` — measured as an OWUI
    # OIDC 500 on a re-domained box. Resolve them AFTER the migration, so this
    # run's new keys are covered too.
    local _dom_resolved _dom_mode="write"
    [ "$DRY_RUN" = "true" ] && _dom_mode="dry"
    _dom_resolved=$(resolve_domain_templates ".env" "$_dom_mode")
    if [ -n "$_dom_resolved" ]; then
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would resolve the subdomain keys against MAIN_DOMAIN (#906):"
        else
            print_substep "  Resolved the subdomain keys against MAIN_DOMAIN (#906):"
            ((changes_applied++)) || true
        fi
        printf '%s\n' "$_dom_resolved"
    fi

    # #1450 (cutover C10): the LLM Manager's model stores (llm-node-models,
    # llm-registry-data) are mounted into the backup service now, so they need
    # the same exclusion gpustack-data has — unless the operator deliberately
    # includes model files (BACKUP_INCLUDE_MODEL_FILES=true), in which case the
    # toggle owns the decision and nothing is appended. Same heal shape as #182
    # above: box-history-independent, append-only, idempotent.
    # rev-B (review Befund 1): the loop carries ALL FOUR model tokens, not just
    # this PR's two. On a box whose BACKUP_EXCLUDE_REGEXP was created by the
    # #182 heal, gpustack-data/speaches-data are missing — the compose default
    # was displaced — so the GPUStack weights went into the nightly tar while
    # this very block ran because model files are excluded. Befund 3: own
    # `local` line, so the block does not depend on the #182 heal above it.
    local _c10_cur _c10_new _c10_tok
    if [ "$(read_env_value ".env" "BACKUP_INCLUDE_MODEL_FILES" | tr -d '"' | tr '[:upper:]' '[:lower:]')" != "true" ]; then
        _c10_cur=$(read_env_value ".env" "BACKUP_EXCLUDE_REGEXP")
        _c10_new="$_c10_cur"
        for _c10_tok in gpustack-data speaches-data llm-node-models llm-registry-data; do
            case "|${_c10_new}|" in
                *"|${_c10_tok}|"*) : ;;
                *) if [ -n "$_c10_new" ]; then _c10_new="${_c10_new}|${_c10_tok}"; else _c10_new="$_c10_tok"; fi ;;
            esac
        done
        if [ "$_c10_new" != "$_c10_cur" ]; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would exclude the LLM Manager model stores from backup (#1450): '${_c10_cur}' → '${_c10_new}'"
            else
                update_env_value ".env" "BACKUP_EXCLUDE_REGEXP" "$_c10_new"
                print_substep "  Excluded the LLM Manager model stores from backup (#1450): '${_c10_cur}' → '${_c10_new}'"
                ((changes_applied++)) || true
            fi
        fi
    fi

    # #2833: the Wazuh event store follows BACKUP_INCLUDE_WAZUH_INDEX. Only an
    # explicit value acts: "false" puts wazuh-indexer-data into the exclude
    # regexp, "true" takes it out; an absent key leaves the regexp alone (an
    # operator's own token stays). Same shape as the heals above: idempotent,
    # every other token preserved.
    local _wz_flag _wz_cur _wz_new
    _wz_flag=$(read_env_value ".env" "BACKUP_INCLUDE_WAZUH_INDEX" | tr -d '"' | tr '[:upper:]' '[:lower:]' || true)
    if [ "$_wz_flag" = "false" ] || [ "$_wz_flag" = "true" ]; then
        _wz_cur=$(read_env_value ".env" "BACKUP_EXCLUDE_REGEXP")
        if [ "$_wz_flag" = "false" ]; then
            case "|${_wz_cur}|" in
                *"|wazuh-indexer-data|"*) _wz_new="$_wz_cur" ;;
                *) if [ -n "$_wz_cur" ]; then _wz_new="${_wz_cur}|wazuh-indexer-data"; else _wz_new="wazuh-indexer-data"; fi ;;
            esac
        else
            _wz_new=$(printf '%s' "$_wz_cur" | tr '|' '\n' | grep -vxF 'wazuh-indexer-data' | paste -sd'|' - || true)
        fi
        if [ "$_wz_new" != "$_wz_cur" ]; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would set the Wazuh event store's backup exclusion (BACKUP_INCLUDE_WAZUH_INDEX=${_wz_flag}): '${_wz_cur}' → '${_wz_new}'"
            else
                update_env_value ".env" "BACKUP_EXCLUDE_REGEXP" "$_wz_new"
                print_substep "  Wazuh event store backup exclusion set (BACKUP_INCLUDE_WAZUH_INDEX=${_wz_flag}): '${_wz_cur}' → '${_wz_new}'"
                ((changes_applied++)) || true
            fi
        fi
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_info "Dry run complete. No changes applied."
    else
        print_success "Environment migration complete: ${changes_applied} changes, ${warnings} warnings."
    fi
}

# ==============================================================================
# Reconcile stale *_VERSION pins against the shipped manifest (#177)
# ==============================================================================
# migrate_env() above only rewrites a *_VERSION pin when config/migrations/
# env-changes.json carries an explicit `change_default` entry for it (or, for
# the box-history-independent case, when the #169 .env.example-derived sync a
# few lines up actually runs). Either path can miss a component: a version
# bump that never got a change_default rule, or a box whose live .env already
# diverged before that bump shipped, leaves the pin stale in `.env` forever —
# `docker compose pull`/`up -d` can't fix it because the operator's explicit
# `.env` value always wins over the compose `${VAR:-<manifest-default>}`
# fallback. Observed stuck on old baselines: KOMODO_VERSION, GOTENBERG_VERSION,
# VALKEY_VERSION (#177).
#
# config/manifests/versions.json is the single authoritative statement of
# "what this release ships" — the check-and-bump-versions workflow keeps it,
# .env.example, compose defaults, and Dockerfile ARGs in lock-step. This pass
# walks every entry under `images` and `hardcoded` that carries BOTH an
# `env_var` and a `current` value and, for any such key ALREADY present in the
# live `.env` with a value that differs from `current`, forces it to
# `current`. Entries missing `env_var`/`current` (most of `hardcoded`, which
# has no operator-facing env var) are skipped.
#
# Deliberately additive-only, same rule as the #169 sync above: this NEVER
# creates a key that doesn't already exist in `.env` — introducing a brand
# new pin is what an `add` env-changes rule is for. Version selection itself
# is a release decision, not operator-tunable config, so an existing pin that
# drifted from the manifest is always corrected, never "preserved as custom".
reconcile_version_pins_from_manifest() {
    print_step "Reconciling *_VERSION pins against manifest (#177)..."

    local manifest="${SCRIPT_DIR}/config/manifests/versions.json"
    if [ ! -f "$manifest" ]; then
        print_substep "No manifest at ${manifest} — skipping version-pin reconcile."
        return 0
    fi

    # Field separator U+001F (ASCII Unit Separator) — same convention migrate_env
    # uses for its own Python→bash handoff above, so a `current` value that
    # happens to contain `:` (e.g. an OCI digest pin) can't be misparsed.
    local pin_script
    pin_script=$(cat <<'PYEOF'
import json
import sys

manifest_path = sys.argv[1]
SEP = "\x1f"

with open(manifest_path) as f:
    manifest = json.load(f)

seen = set()
for section in ("images", "hardcoded"):
    for _name, entry in manifest.get(section, {}).items():
        env_var = entry.get("env_var")
        current = entry.get("current")
        if not env_var or not current:
            continue
        if env_var in seen:
            continue
        seen.add(env_var)
        print(f"{env_var}{SEP}{current}")
PYEOF
    )

    local pin_output
    pin_output=$(python3 -c "$pin_script" "$manifest" 2>&1)
    if [ $? -ne 0 ]; then
        print_warning "Failed to parse manifest for version-pin reconcile: ${pin_output}"
        return 0
    fi

    local reconciled=0
    local env_var current_val live_val
    while IFS=$'\x1f' read -r env_var current_val; do
        [ -n "$env_var" ] || continue
        live_val=$(read_env_value ".env" "$env_var")
        [ -n "$live_val" ] || continue            # not set in live .env — leave to an `add` rule
        [ "$live_val" = "$current_val" ] && continue
        # #2626: the same rule as the #169 sync — never lower a stateful pin.
        [ "$(pin_sync_decision "$env_var" "$live_val" "$current_val")" = sync ] || continue

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would reconcile ${env_var}: ${live_val} → ${current_val} (manifest)"
        else
            update_env_value ".env" "$env_var" "$current_val"
            print_substep "  reconciled ${env_var}: ${live_val} → ${current_val} (manifest)"
            ((reconciled++)) || true
            REQUIRES_BUILD=true
            REQUIRES_PULL=true
        fi
    done <<< "$pin_output"

    if [ "$DRY_RUN" != "true" ]; then
        if [ "$reconciled" -gt 0 ]; then
            print_success "Reconciled ${reconciled} stale *_VERSION pin(s) to manifest."
        else
            print_substep "All *_VERSION pins already match manifest — nothing to reconcile."
        fi
    fi
}

# ==============================================================================
# #2401: the migration points services at per-service roles — make them exist.
# ==============================================================================
# The 2026.09-rc1 env-changes block carries `change_default` rules that move
# nine `*_DB_USER` keys from "" (the shared-superuser shape every pre-2026.05
# box still has) to the per-service role name. migrate_env applies them, and
# from that moment compose's `${X_DB_USER:-${POSTGRES_USER:-docker}}` fallback
# stops firing — every recreated container connects as a role that nothing had
# created: generate_missing_secrets deliberately never generates those
# `*_DB_PASSWORD` keys, init-db.sh creates a role only when user AND password
# are non-empty, and the #184 heal below probes only EMPTY keys. Measured on
# 0.208 (2026.08-ga.15 -> 2026.09-ga.1): Open WebUI crash-looping, Gitea
# unhealthy, two migrate-reconcile one-shots exit 2; prod carries the same six
# empty keys.
#
# Contract: for every populated `*_DB_USER` in .env whose role is not the
# superuser, (1) an empty `*_DB_PASSWORD` gets a fresh password (the stack's
# own generator — never base64, the value lands in a postgres:// URL), (2) the
# postgres-db-reconcile one-shot runs NOW (pre-restart) so init-db.sh creates
# and grants the role (BUG-6 re-applies the password on an existing role),
# (3) every role is then probed OVER THE STACK NETWORK — not from inside the
# postgres container, where pg_hba trusts local connections and every password
# "works" — and a role that still does not authenticate ABORTS the upgrade
# before a single container is recreated, with the repair spelled out.
# Nothing to do on a docker-superuser box (all keys empty) or a healthy box.
# ENSURE_DB_ROLES-BEGIN
ensure_service_db_roles() {
    print_step "Ensuring the per-service DB roles the env points at exist and authenticate (#2401)..."

    if [ ! -f "config/.env.example" ]; then
        print_substep "No .env.example template — skipping DB-role check."
        return 0
    fi
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping DB-role check."; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x postgres >/dev/null || {
        print_substep "postgres container not running — skipping DB-role check (init-db creates roles at restart)."; return 0; }

    local superuser superpw pg_image pg_net
    superuser=$(read_env_value ".env" "POSTGRES_USER"); [ -n "$superuser" ] || superuser="docker"
    superpw=$(read_env_value ".env" "POSTGRES_PASSWORD")
    pg_image=$(docker inspect -f '{{.Config.Image}}' postgres 2>/dev/null)
    pg_net=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' postgres 2>/dev/null | awk '{print $1}')

    # Existence, asked of the superuser (local connection inside the container
    # is fine for a catalogue read).
    _pg_role_exists() {
        PGPASSWORD="$superpw" docker exec -e PGPASSWORD postgres \
            psql -U "$superuser" -h 127.0.0.1 -d postgres -tAc \
            "SELECT 1 FROM pg_roles WHERE rolname='$1'" 2>/dev/null | grep -q '^1$'
    }
    # Authentication, asked over the stack network from a throwaway peer —
    # the only path where pg_hba applies the password (0.208 lesson: a probe
    # against 127.0.0.1 inside the container accepts every password).
    _pg_role_authenticates_over_network() {
        [ -n "$pg_image" ] && [ -n "$pg_net" ] || return 1
        PGPASSWORD="$2" docker run --rm --network "$pg_net" -e PGPASSWORD "$pg_image" \
            psql -U "$1" -h postgres -d postgres -tAc 'SELECT 1' 2>/dev/null | grep -q '^1$'
    }

    local key role pwkey pw needs_reconcile=0 generated=0
    local -a wanted=()
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        role=$(read_env_value ".env" "$key")
        [ -n "$role" ] || continue                       # docker-superuser box → nothing to ensure
        [ "$role" = "$superuser" ] && continue
        printf '%s' "$role" | grep -qE '^[a-zA-Z_][a-zA-Z0-9_]*$' || continue
        pwkey="${key%_USER}_PASSWORD"
        pw=$(read_env_value ".env" "$pwkey")
        if [ -z "$pw" ]; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would generate ${pwkey} (role '${role}' is configured without a password)"
            else
                pw=$(generate_password_upgrade 24)
                update_env_value ".env" "$pwkey" "$pw"
                print_substep "  Generated ${pwkey} for role '${role}'"
                ((generated++)) || true
            fi
            needs_reconcile=1
        elif ! _pg_role_exists "$role"; then
            print_substep "  Role '${role}' (${key}) does not exist yet"
            needs_reconcile=1
        elif ! _pg_role_authenticates_over_network "$role" "$pw"; then
            print_substep "  Role '${role}' (${key}) exists but rejects its configured password"
            needs_reconcile=1
        fi
        wanted+=("${key}=${role}")
    done < <(grep -oE '^[A-Z0-9_]+_DB_USER=' "config/.env.example" | sed 's/=$//')

    if [ "${#wanted[@]}" -eq 0 ]; then
        print_substep "No per-service DB role configured (shared-superuser box) — nothing to ensure."
        return 0
    fi
    if [ "$needs_reconcile" -eq 0 ]; then
        print_success "DB roles: all ${#wanted[@]} configured role(s) exist and authenticate."
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        print_info "  Would run postgres-db-reconcile now to create/align the role(s), then re-probe them."
        return 0
    fi

    # Create + grant + (re)apply passwords with the stack's own init-db.sh,
    # from the .env that now carries the passwords — the same one-shot that
    # runs at restart, pulled forward to before any consumer is recreated.
    print_substep "  Running postgres-db-reconcile to create/align the role(s) (${generated} password(s) generated)..."
    if ! docker compose run --rm --no-deps postgres-db-reconcile >/dev/null 2>&1; then
        print_warning "  postgres-db-reconcile exited non-zero — probing the roles anyway."
    fi

    local failed=0 entry
    for entry in "${wanted[@]}"; do
        key="${entry%%=*}"; role="${entry#*=}"
        pwkey="${key%_USER}_PASSWORD"; pw=$(read_env_value ".env" "$pwkey")
        if _pg_role_authenticates_over_network "$role" "$pw"; then
            print_substep "  ${role}: authenticates over the stack network — ok."
        else
            print_error "  ${role} (${key}): does NOT authenticate with ${pwkey} after the reconcile."
            ((failed++)) || true
        fi
    done
    if [ "$failed" -gt 0 ]; then
        print_error "DB roles: ${failed} configured role(s) cannot authenticate — refusing to recreate containers that would connect as them (#2401)."
        print_error "Repair: check the postgres-db-reconcile output (docker compose run --rm --no-deps postgres-db-reconcile),"
        print_error "        verify each role over the stack network (never from inside the postgres container),"
        print_error "        then re-run the upgrade. Emergency override: RAZZFAZZ_SKIP_DB_ROLE_PREFLIGHT=1 (logged; the box will break at the recreate)."
        if [ "${RAZZFAZZ_SKIP_DB_ROLE_PREFLIGHT:-}" = "1" ]; then
            print_warning "RAZZFAZZ_SKIP_DB_ROLE_PREFLIGHT=1 — continuing despite ${failed} unauthenticated role(s)."
            return 0
        fi
        return 1
    fi
    print_success "DB roles: all ${#wanted[@]} configured role(s) exist and authenticate."
    return 0
}
# ENSURE_DB_ROLES-END

# ==============================================================================
# Reconcile per-service DB users (#184)
# ==============================================================================
# A regression introduced in 2026.05-rc2 re-added every per-service `*_DB_USER`
# key to migrations/env-changes.json with an EMPTY default. A box that first
# enabled a profile while passing that migration got `<SVC>_DB_USER=` appended
# empty. compose then falls back to the global superuser, e.g.
#   modules/apps/onyx/compose.yml          POSTGRES_USER: ${ONYX_DB_USER:-${POSTGRES_USER:-docker}}
#   modules/doc-processing/paperless-ngx   PAPERLESS_DBUSER: ${PAPERLESS_DB_USER:-${POSTGRES_USER:-docker}}
# so the container connects as `docker` but presents the per-service password →
# "password authentication failed for user docker" crash-loop (hit onyx +
# paperless on the prod 8.246 ga.7→ga.2 upgrade, 2026-06-18).
#
# This heal is version-INDEPENDENT on purpose. A manifest `change_default` would
# only run from the *target* tree, so a box upgrading INTO an already-cut tag
# can never pick up a retro-fitted manifest line. We treat .env.example as the
# source of truth for the correct per-service role name.
#
# v2 (2026-06-20) — PROBE BEFORE HEALING. The naive heal broke OLDER boxes: on a
# box provisioned before per-service roles existed, every service connects as the
# global `docker` superuser via the empty→${POSTGRES_USER:-docker} fallback, and
# that WORKS (the docker user has the global password). Blindly setting
# `<SVC>_DB_USER=<svc>_user` there points the service at a role that doesn't
# exist / can't authenticate → "password authentication failed for user
# <svc>_user", taking authentik/openwebui/dify down (observed on the 0.208
# ga.2→ga.3 upgrade). So: only heal a given key if the per-service role can
# actually authenticate against the running postgres with its per-service
# password; otherwise leave it empty (the working docker-superuser fallback).
# - onyx/paperless box (per-service roles exist): probe succeeds → heal (the
#   original fix — un-breaks the per-service-password-vs-docker-user mismatch).
# - older docker-superuser box (no per-service role): probe fails → skip → the
#   box keeps working on the global superuser.
# Non-destructive: a populated `*_DB_USER` is always preserved.
reconcile_service_db_users() {
    print_step "Reconciling per-service DB users (#184)..."

    if [ ! -f "config/.env.example" ]; then
        print_substep "No .env.example template — skipping DB-user reconcile."
        return 0
    fi

    # Probe: can role $1 authenticate against the running postgres with
    # password $2? Returns 0 (yes) only on a clean SELECT 1. Runs inside the
    # postgres container (still up at migrate_env time, pre-restart); any
    # failure mode — role missing, wrong password, postgres down, no docker —
    # returns non-zero so we DON'T heal (safe default = leave the working
    # docker-superuser fallback in place). Connect to the maintenance `postgres`
    # DB (PUBLIC CONNECT is not revoked), so the probe tests auth, not grants.
    _db_role_authenticates() {
        local role="$1" pw="$2"
        [ -n "$role" ] || return 1
        command -v docker >/dev/null 2>&1 || return 1
        PGPASSWORD="$pw" docker exec -e PGPASSWORD postgres \
            psql -U "$role" -d postgres -h 127.0.0.1 -tAc 'SELECT 1' \
            >/dev/null 2>&1
    }

    local healed=0 skipped=0
    local key example_val live_val pwkey svc_pw
    # Candidates: every `*_DB_USER` key that ships a non-empty value in the
    # template. read_env_value is targeted (we never source .env — operator
    # values carry spaces/metachars).
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        example_val=$(read_env_value "config/.env.example" "$key")
        [ -n "$example_val" ] || continue
        live_val=$(read_env_value ".env" "$key")
        [ -z "$live_val" ] || continue   # populated → preserve
        # Only heal if the per-service role actually authenticates — otherwise
        # this is a docker-superuser box and healing would break it (v2).
        pwkey="${key%_USER}_PASSWORD"          # e.g. AUTHENTIK_DB_USER → AUTHENTIK_DB_PASSWORD
        svc_pw=$(read_env_value ".env" "$pwkey")
        if [ "$DRY_RUN" = "true" ]; then
            if _db_role_authenticates "$example_val" "$svc_pw"; then
                print_info "  Would heal empty ${key} → ${example_val} (role authenticates)"
            else
                print_info "  Would skip ${key} — role '${example_val}' does not authenticate (docker-superuser fallback preserved)"
            fi
            continue
        fi
        if _db_role_authenticates "$example_val" "$svc_pw"; then
            update_env_value ".env" "$key" "$example_val"
            print_substep "  Healed empty ${key} → ${example_val} (role authenticates)"
            ((healed++)) || true
        else
            print_substep "  Skipped ${key} — role '${example_val}' does not authenticate; left empty (global-superuser fallback)"
            ((skipped++)) || true
        fi
    done < <(grep -oE '^[A-Z0-9_]+_DB_USER=' "config/.env.example" | sed 's/=$//')

    if [ "$DRY_RUN" != "true" ]; then
        if [ "$healed" -gt 0 ] || [ "$skipped" -gt 0 ]; then
            print_success "DB-user reconcile: ${healed} healed, ${skipped} left as global-superuser fallback."
        else
            print_substep "All *_DB_USER values already populated — nothing to heal."
        fi
    fi
}

# #154 / #2524: reconcile_service_db_ownership lives in scripts/lib.sh (the
# restore re-owns the restored databases with the same code).

# ==============================================================================
# #2433: preserve cognee's storage from the container's writable layer.
# ==============================================================================
# cognee >= 1.x sets SYSTEM_ROOT_DIRECTORY/DATA_ROOT_DIRECTORY in its image env
# (/cognee-storage/{system,data}); our compose mounted the volumes at the
# pre-1.x paths and set neither variable, so on every 2026.09-ga/ga.1 box
# everything cognee ingested or built after install lives in the container's
# writable layer and dies with the next recreate. The compose fix (roots follow
# the mounts) takes effect at exactly that recreate — so the upgrade that
# carries the fix must first copy the live tree into the volumes, or the fix
# is what deletes the data. Runs BEFORE restart_stack, fail-closed: a copy that
# cannot be made safely aborts the upgrade with nothing recreated.
# #2455: every exec here runs as root (-u 0). The volumes on existing boxes are
# root-owned (pre-1.x cognee ran as root; ga.15 still does), the image since
# ga.1 runs as uid 1000 — as the container user, `mkdir _pre-2433` was denied
# and the upgrade aborted (0.208, 2026-09-23).
# PRESERVE_COGNEE-BEGIN
preserve_cognee_storage() {
    # #2524: a step of the image change; --migrations-only changes no image.
    [ "${MIGRATIONS_ONLY:-false}" = true ] && { [ "${DRY_RUN:-false}" = true ] || print_substep "preserve_cognee_storage: skipped (--migrations-only)."; return 0; }
    print_step "Preserving cognee storage from the container layer (#2433)..."
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping."; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x cognee >/dev/null || {
        print_substep "cognee container not running — nothing to preserve."; return 0; }

    local sys_root data_root
    sys_root=$(docker exec -u 0 cognee sh -c 'printf %s "${SYSTEM_ROOT_DIRECTORY:-}"' 2>/dev/null)
    data_root=$(docker exec -u 0 cognee sh -c 'printf %s "${DATA_ROOT_DIRECTORY:-}"' 2>/dev/null)

    local pair root target count need free after preserved=0
    for pair in "${sys_root}|/app/cognee/.cognee_system" "${data_root}|/app/cognee/.data_storage"; do
        root="${pair%%|*}"; target="${pair#*|}"
        [ -n "$root" ] || continue
        [ "$root" = "$target" ] && continue                 # already the mounted path
        # A root that IS a mount point holds volume data already — nothing to move.
        if docker exec -u 0 cognee sh -c "grep -q ' ${root} ' /proc/mounts" 2>/dev/null; then
            print_substep "  ${root} is a mount — nothing to preserve."
            continue
        fi
        count=$(docker exec -u 0 cognee sh -c "find '${root}' -mindepth 1 2>/dev/null | wc -l" 2>/dev/null | tr -d '[:space:]')
        [ -n "$count" ] || count=0
        if [ "$count" -eq 0 ]; then
            print_substep "  ${root}: empty — nothing to preserve."
            continue
        fi
        need=$(docker exec -u 0 cognee sh -c "du -sk '${root}' 2>/dev/null | cut -f1" 2>/dev/null | tr -d '[:space:]')
        free=$(docker exec -u 0 cognee sh -c "df -Pk '${target}' 2>/dev/null | awk 'NR==2{print \$4}'" 2>/dev/null | tr -d '[:space:]')
        [ -n "$need" ] || need=0; [ -n "$free" ] || free=0
        if [ "$need" -gt "$free" ]; then
            print_error "  ${root}: ${need} KiB to preserve but only ${free} KiB free on ${target} — refusing to recreate cognee (#2433)."
            print_error "  Free space on the cognee volumes, then re-run the upgrade; the live tree is still in the container until then."
            return 1
        fi
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would preserve ${count} entries (${need} KiB) from ${root} into ${target} (existing volume content set aside under _pre-2433/)"
            continue
        fi
        # Set aside what the volume already holds (pre-1.x stores, the original
        # documents), then copy the live tree in — the live tree is what cognee
        # has been serving; nothing is deleted. Safe to repeat (a retry after an
        # abort runs this again over a volume the first run already filled):
        # content identical to the live tree is recognised and left alone, and
        # anything else is set aside into a fresh per-run directory under
        # _pre-2433/, never onto a name an earlier run used.
        if ! _pc_out=$(docker exec -u 0 cognee sh -c "set -e; cd '${target}'; \
                _ts=\$(date -u +%Y%m%dT%H%M%SZ)-\$\$; \
                if command -v diff >/dev/null 2>&1 && diff -rq -x _pre-2433 '${root}' . >/dev/null 2>&1; then \
                    echo ALREADY-PRESERVED; exit 0; fi; \
                _n=0; for e in * .[!.]*; do [ -e \"\$e\" ] || continue; [ \"\$e\" = _pre-2433 ] && continue; _n=1; done; \
                if [ \"\$_n\" = 1 ]; then _d=_pre-2433/\$_ts; mkdir -p \"\$_d\"; \
                    for e in * .[!.]*; do [ -e \"\$e\" ] || continue; [ \"\$e\" = _pre-2433 ] && continue; mv \"\$e\" \"\$_d/\"; done; fi; \
                cp -a '${root}/.' '${target}/'" 2>&1); then
            print_error "  Copy ${root} → ${target} FAILED — refusing to recreate cognee (#2433). The live tree is still in the container."
            return 1
        fi
        case "$_pc_out" in *ALREADY-PRESERVED*)
            print_info "  ${target} already holds this tree from an earlier run — nothing moved or copied again." ;; esac
        after=$(docker exec -u 0 cognee sh -c "find '${target}' -mindepth 1 -not -path '${target}/_pre-2433*' 2>/dev/null | wc -l" 2>/dev/null | tr -d '[:space:]')
        [ -n "$after" ] || after=0
        if [ "$after" -lt "$count" ]; then
            print_error "  ${target} holds ${after} entries after the copy, expected ${count} — refusing to recreate cognee (#2433)."
            return 1
        fi
        print_substep "  Preserved ${count} entries (${need} KiB) from ${root} into ${target}."
        ((preserved++)) || true
    done
    if [ "$preserved" -gt 0 ]; then
        print_success "cognee storage preserved into the volumes (${preserved} tree(s)); the recreate will use them."
    else
        print_substep "cognee storage already on the volumes — nothing to preserve."
    fi
    return 0
}
# #2455: the cognee volumes belong to the user the cognee image runs as.
# Existing boxes hold root-owned volumes; the image since ga.1 runs as uid 1000,
# and on a ga.15 box the preserve step above is skipped entirely (its root
# container has no SYSTEM_ROOT_DIRECTORY) — so the recreate would start cognee
# on volumes it cannot write. The uid:gid is read from the image that is about
# to run, never assumed. Runs BEFORE restart_stack, fail-closed.
COGNEE_IMAGE="${COGNEE_IMAGE:-razzfazz-stack-cognee}"
align_cognee_volume_ownership() {
    # #2524: a step of the image change; --migrations-only changes no image.
    [ "${MIGRATIONS_ONLY:-false}" = true ] && { [ "${DRY_RUN:-false}" = true ] || print_substep "align_cognee_volume_ownership: skipped (--migrations-only)."; return 0; }
    print_step "Aligning cognee volume ownership with the image user (#2455)..."
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping."; return 0; }
    local image="$COGNEE_IMAGE"
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        print_substep "no ${image} image on this box — cognee is not installed, nothing to align."
        return 0
    fi
    local uid gid
    uid=$(docker run --rm --entrypoint id "$image" -u 2>/dev/null | tr -d '[:space:]')
    gid=$(docker run --rm --entrypoint id "$image" -g 2>/dev/null | tr -d '[:space:]')
    if ! printf '%s' "$uid" | grep -Eq '^[0-9]+$' || ! printf '%s' "$gid" | grep -Eq '^[0-9]+$'; then
        print_error "  Could not read the user of ${image} (got '${uid}:${gid}') — refusing to recreate cognee (#2455)."
        return 1
    fi
    if [ "$uid" = 0 ]; then
        print_substep "  ${image} runs as root — no ownership to align."
        return 0
    fi
    local short vol wrong
    for short in cognee-data cognee-data-storage; do
        vol=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E "_${short}\$" | head -n1 || true)
        if [ -z "$vol" ]; then
            print_substep "  no ${short} volume yet — a fresh one takes the image's owner."
            continue
        fi
        wrong=$(docker run --rm -u 0 --entrypoint sh -v "${vol}:/v" "$image" \
                  -c "find /v \\( ! -uid ${uid} -o ! -gid ${gid} \\) | wc -l" 2>/dev/null | tr -d '[:space:]')
        if ! printf '%s' "$wrong" | grep -Eq '^[0-9]+$'; then
            print_error "  Could not read the ownership of ${vol} — refusing to recreate cognee (#2455)."
            return 1
        fi
        if [ "$wrong" -eq 0 ]; then
            print_substep "  ${vol}: already owned by ${uid}:${gid}."
            continue
        fi
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would chown -R ${uid}:${gid} ${vol} (${wrong} entries owned by someone else)"
            continue
        fi
        if ! docker run --rm -u 0 --entrypoint sh -v "${vol}:/v" "$image" -c "chown -R ${uid}:${gid} /v" >/dev/null 2>&1; then
            print_error "  chown -R ${uid}:${gid} ${vol} FAILED — refusing to recreate cognee (#2455)."
            return 1
        fi
        wrong=$(docker run --rm -u 0 --entrypoint sh -v "${vol}:/v" "$image" \
                  -c "find /v \\( ! -uid ${uid} -o ! -gid ${gid} \\) | wc -l" 2>/dev/null | tr -d '[:space:]')
        if [ "${wrong:-1}" != 0 ]; then
            print_error "  ${vol} still holds ${wrong:-?} entries not owned by ${uid}:${gid} — refusing to recreate cognee (#2455)."
            return 1
        fi
        print_substep "  ${vol}: re-owned to ${uid}:${gid}."
    done
    return 0
}

# #2455: after the recreate, cognee's OWN user can write both storage roots.
# Runs as the container user on purpose (no -u 0) — that is the user that has
# to write. Returns 1 when it cannot; the count feeds the final verdict.
verify_cognee_storage_writable() {
    command -v docker >/dev/null 2>&1 || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x cognee >/dev/null || return 0
    if docker exec cognee sh -c 'set -e; for d in "$SYSTEM_ROOT_DIRECTORY" "$DATA_ROOT_DIRECTORY"; do [ -n "$d" ] || continue; f="$d/.rzfz-write-probe"; : > "$f"; rm -f "$f"; done' >/dev/null 2>&1; then
        print_substep "cognee can write its storage roots (#2455)."
        return 0
    fi
    print_error "cognee cannot write its storage roots as its own user (#2455) — every add/cognify will fail."
    print_info "  Fix: docker run --rm -u 0 --entrypoint sh -v <project>_cognee-data:/v ${COGNEE_IMAGE} -c 'chown -R \$(id -u cognee):\$(id -g cognee) /v' (same for _cognee-data-storage), then docker restart cognee."
    return 1
}
# PRESERVE_COGNEE-END

# ==============================================================================
# #2732: the Wazuh agent registry moves from the container layer into the volume
# ==============================================================================
# Before this fix wazuh-manager kept /var/ossec/etc (client.keys, the agent
# registry), queue (agent databases) and var/multigroups in its container layer;
# the compose now mounts them from subpaths of wazuh-manager-data. The recreate
# that applies the mounts would start the manager on empty directories, so every
# agent would re-enrol under a NEW ID. The upgrade that carries the fix copies
# the live trees into the volume first, through the running container (which has
# the volume at /var/ossec/data): no helper image, so it works air-gapped.
# client.keys is copied LAST, from the live file, and compared by sha256, so
# it is the freshest thing in the volume. Runs BEFORE restart_stack, fail-closed:
# a registry that cannot be copied safely aborts the upgrade with nothing
# recreated. Idempotent: a path that is already a mount is left alone.
# PRESERVE_WAZUH-BEGIN
_wazuh_registry_is_mount() {
    docker exec -u 0 wazuh-manager sh -c "grep -q ' $1 ' /proc/mounts" 2>/dev/null
}
preserve_wazuh_registry() {
    [ "${MIGRATIONS_ONLY:-false}" = true ] && { [ "${DRY_RUN:-false}" = true ] || print_substep "preserve_wazuh_registry: skipped (--migrations-only)."; return 0; }
    print_step "Preserving the Wazuh agent registry from the container layer (#2732)..."
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping."; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x wazuh-manager >/dev/null || {
        print_substep "wazuh-manager not running — nothing to preserve."; return 0; }
    if ! _wazuh_registry_is_mount /var/ossec/data; then
        print_error "  wazuh-manager has no wazuh-manager-data volume at /var/ossec/data — refusing to recreate it (#2732)."
        return 1
    fi
    local pair src sub copied=0
    for pair in "etc|etc" "queue|queue" "var/multigroups|var-multigroups"; do
        src="/var/ossec/${pair%%|*}"; sub="${pair#*|}"
        if _wazuh_registry_is_mount "$src"; then
            print_substep "  ${src} is a mount — nothing to preserve."
            continue
        fi
        if [ "${DRY_RUN:-false}" = true ]; then
            print_info "  Would copy ${src} into the wazuh-manager-data volume (${sub}/)"
            continue
        fi
        if ! docker exec -u 0 wazuh-manager sh -c "set -e; mkdir -p '/var/ossec/data/${sub}'; cp -a '${src}/.' '/var/ossec/data/${sub}/'" >/dev/null 2>&1; then
            print_error "  Copy ${src} → volume ${sub}/ FAILED — refusing to recreate wazuh-manager (#2732). The live registry is still in the container."
            return 1
        fi
        ((copied++)) || true
    done
    [ "$copied" -gt 0 ] || { print_substep "Wazuh agent registry already on the volume — nothing to preserve."; return 0; }
    # The freshest client.keys last (agents may enrol while the trees copy), then
    # a sha256 comparison (no cmp in the image): this file is what keeps the agent IDs.
    if ! docker exec -u 0 wazuh-manager sh -c "set -e; [ ! -e /var/ossec/etc/client.keys ] || cp -a /var/ossec/etc/client.keys /var/ossec/data/etc/client.keys" >/dev/null 2>&1 \
       || ! docker exec -u 0 wazuh-manager sh -c "[ ! -e /var/ossec/etc/client.keys ] || [ \"\$(sha256sum < /var/ossec/etc/client.keys)\" = \"\$(sha256sum < /var/ossec/data/etc/client.keys)\" ]" >/dev/null 2>&1; then
        print_error "  client.keys was not copied intact — refusing to recreate wazuh-manager (#2732)."
        return 1
    fi
    print_success "Wazuh agent registry preserved into wazuh-manager-data (${copied} tree(s)); the recreate will use it."
    return 0
}
# PRESERVE_WAZUH-END

# ==============================================================================
# #2403: the blueprints are re-applied, or the upgrade says they were not.
# ==============================================================================
# Delivery of every Authentik change is the `authentik-init` compose one-shot,
# which a failed sibling (a migrate-reconcile that died on #2401) silently gates
# in `Created`; a later targeted restart of server+worker never pulls it, so a
# box keeps its old provider rows while the tree moved on (0.208: marker 3.5
# against INIT_VERSION 4.9, no settings./search. providers, "Not Found" behind
# SSO). Provider rows are persistent state — nothing short of the init
# re-applies them. Contract, after restart_stack: (1) bring up the WHOLE
# project once more so every gated one-shot and chain is released, and list
# what is still in `created`; (2) compare the stored marker with the tree's
# INIT_VERSION and, when behind, run authentik-init DETACHED (an attached run
# dies with the operator's terminal and leaves a half-applied Authentik whose
# marker still reads "never ran", 0.208 again), wait on its exit code, re-read
# the marker; (3) a marker still behind, a stale start stamp (partial apply) or
# a service left in `created` is an upgrade ISSUE, never a silent success.
# VERIFY_AUTHENTIK_INIT-BEGIN
verify_authentik_init_marker() {
    print_step "Verifying the Authentik blueprints reached this box (#2403)..."
    command -v docker >/dev/null 2>&1 || { print_substep "docker unavailable — skipping."; return 0; }
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x authentik-server >/dev/null || {
        print_substep "authentik-server not running — skipping (the init cannot apply without it)."; return 1; }

    local tree_version stored started created_list issues=0
    tree_version=$(grep -oE '^INIT_VERSION="[^"]+"' "${SCRIPT_DIR}/core/init-authentik.sh" 2>/dev/null | head -1 | cut -d'"' -f2)
    if [ -z "$tree_version" ]; then
        print_warning "  core/init-authentik.sh declares no INIT_VERSION — cannot verify the marker."
        return 1
    fi

    # (1) the whole project, once more, so gated chains release; then the census.
    # --migrations-only (#2524): rzfz start has just done exactly that.
    if [ "$DRY_RUN" != "true" ] && [ "${MIGRATIONS_ONLY:-false}" != true ]; then
        docker compose up -d >/dev/null 2>&1 || print_warning "  compose up (whole project) exited non-zero — reading the census anyway."
    fi
    created_list=$(docker compose ps -a --format '{{.Name}} {{.State}}' 2>/dev/null | awk '$2=="created"{print $1}' | tr '\n' ' ')
    if [ -n "$created_list" ]; then
        print_error "  Services still in 'created' after the whole-project up: ${created_list}"
        print_error "  A one-shot left in 'created' never ran — its dependency failed; see 'docker compose ps -a' and the sibling's logs."
        issues=$((issues + 1))
    else
        print_substep "  Whole project up: nothing left in 'created'."
    fi

    # (2) the marker, read from the media volume through the running server.
    _ak_marker() { docker exec authentik-server cat /data/media/.authentik_initialized 2>/dev/null | tr -d '[:space:]'; }
    _ak_started() { docker exec authentik-server cat /data/media/.authentik_initialized.started 2>/dev/null | tr -d '[:space:]'; }
    stored=$(_ak_marker); [ -n "$stored" ] || stored="(none)"
    started=$(_ak_started)
    if [ "$stored" = "$tree_version" ] && [ -z "$started" ]; then
        print_substep "  Authentik init marker ${stored} == tree INIT_VERSION ${tree_version}."
    else
        if [ -n "$started" ]; then
            print_substep "  Authentik init marker ${stored}, tree ${tree_version}, start stamp ${started} present — a PREVIOUS apply did not complete (partial)."
        else
            print_substep "  Authentik init marker ${stored} != tree INIT_VERSION ${tree_version} — the blueprints did not reach this box."
        fi
        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would run authentik-init detached and wait for it, then re-read the marker."
        else
            print_substep "  Running authentik-init detached (docker compose up -d authentik-init) and waiting for its exit..."
            docker compose up -d --no-deps authentik-init >/dev/null 2>&1 || true
            local rc
            rc=$(timeout 900 docker wait authentik-init 2>/dev/null | tr -d '[:space:]'); [ -n "$rc" ] || rc="timeout"
            stored=$(_ak_marker); [ -n "$stored" ] || stored="(none)"
            started=$(_ak_started)
            if [ "$rc" = "0" ] && [ "$stored" = "$tree_version" ] && [ -z "$started" ]; then
                print_success "  authentik-init completed: marker ${stored} == tree ${tree_version}."
                # its consumers may have been gated on it — release them
                docker compose up -d >/dev/null 2>&1 || true
            else
                print_error "  authentik-init exit ${rc}; marker ${stored} vs tree ${tree_version}; start stamp '${started}' — the blueprints are NOT applied (#2403)."
                print_error "  Repair: docker compose logs authentik-init; fix the cause; setsid nohup docker compose up -d authentik-init; docker wait authentik-init; then re-check the marker."
                issues=$((issues + 1))
            fi
        fi
    fi
    return $issues
}
# VERIFY_AUTHENTIK_INIT-END

# ==============================================================================
# Dify Environment Sync
# ==============================================================================
sync_dify_env() {
    print_step "Synchronizing Dify environment..."

    if [ ! -f ".env.dify" ]; then
        print_substep "No .env.dify found — Dify not configured. Skipping."
        return 0
    fi

    if [ ! -f "config/.env.dify.example" ]; then
        print_substep "No .env.dify.example template. Skipping Dify sync."
        return 0
    fi

    local added=0
    local removed=0

    # Detect new keys in .env.dify.example not in .env.dify
    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        local key="${line%%=*}"
        # Validate key is a valid env var name
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

        if ! grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" ".env.dify" 2>/dev/null; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would add ${key} to .env.dify"
            else
                echo "$line" >> ".env.dify"
                print_substep "  Added new Dify variable: ${key}"
                ((added++)) || true
            fi
        fi
    done < "config/.env.dify.example"

    # Detect removed keys (in .env.dify but not in .env.dify.example)
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        local key="${line%%=*}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

        if ! grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "config/.env.dify.example" 2>/dev/null; then
            print_warning "  Dify variable ${key} exists in .env.dify but not in template (may be obsolete)"
            ((removed++)) || true
        fi
    done < ".env.dify"

    # Sync shared secrets from .env to .env.dify
    if [ "$DRY_RUN" != "true" ]; then
        if sync_dify_valkey_password ".env" ".env.dify"; then
            print_substep "  Synced VALKEY_PASSWORD → REDIS_PASSWORD + CELERY_BROKER_URL in .env.dify"
        fi

        # Sync plugin keys
        for key in PLUGIN_DAEMON_KEY PLUGIN_DIFY_INNER_API_KEY; do
            local env_val dify_val
            env_val=$(read_env_value ".env" "$key")
            dify_val=$(read_env_value ".env.dify" "$key")
            if [ -n "$env_val" ] && [ "$env_val" != "$dify_val" ]; then
                update_env_value ".env.dify" "$key" "$env_val"
                print_substep "  Synced ${key} from .env → .env.dify"
            fi
        done
    fi

    print_success "Dify env sync: ${added} added, ${removed} potentially obsolete."
}

# ==============================================================================
# Generate Missing Secrets (runs after env migration)
# ==============================================================================
# For each secret key, checks if the value in .env is empty. If empty AND the
# corresponding profile is enabled (or it's a core secret), generates it.
# Uses the same generation methods as razzfazz-init.sh.
# ==============================================================================

# Thin re-exports under the historical *_upgrade names so the 71 existing
# call sites stay byte-identical. The bodies are provided by lib.sh's
# generate_secret / generate_password / generate_hex_secret with identical
# semantics (openssl rand under the hood).
generate_secret_upgrade()     { generate_secret "$@"; }
generate_password_upgrade()   { generate_password "$@"; }
generate_hex_secret_upgrade() { generate_hex_secret "$@"; }

# Check if a profile is active in COMPOSE_PROFILES
profile_active() {
    local profile="$1"
    local profiles
    profiles=$(read_env_value ".env" "COMPOSE_PROFILES")
    echo ",$profiles," | grep -q ",$profile,"
}

# Returns true if the key is empty or unset in .env
secret_is_empty() {
    local key="$1"
    local val
    val=$(read_env_value ".env" "$key")
    [ -z "$val" ]
}

# Step 6b.2 (2026.11): OpenHands is no longer part of the stack. On a box that
# ran it, this switches it off for good: the `openhands` profile leaves
# COMPOSE_PROFILES, every OpenHands container is stopped and removed (the shared
# module, the personal agents launched from the Agent Manager, the sandboxes
# they started and the Docker guard that served them), its networks and images
# go, and the admin is told once, in plain words, where the files are. NO data
# volume is deleted: the workspace volumes stay until the admin removes them.
# The only volume removed is docker-guard-shared, which held a derived access
# URL for the guard and no user data. Idempotent: on a box with nothing of it
# left it prints nothing. The notice is printed once per box, recorded in
# .rzfz-state/openhands-retired next to the .env: a later upgrade that still
# finds a leftover (a network a running container held, say) removes it with
# a line of its own and does not repeat the notice. A network is removed only
# after every container still attached to it is disconnected: at this step the
# running Caddy, created from the previous compose, is still on
# oh-sandbox-shared, and `docker network rm` refuses a network in use.
# The Authentik application, provider and group go in apply-policy-bindings.py;
# the Agent Manager marks the personal agents stopped and refuses to start them.
# RETIRE_OPENHANDS-BEGIN
retire_openhands_module() {
    local env_file="${1:-.env}"
    # --migrations-only changes no container, image or host state; the env
    # migration still retires the keys. A later full upgrade does the rest.
    [ "${MIGRATIONS_ONLY:-false}" != true ] || return 0
    local profiles had_profile=false
    profiles=$(read_env_value "$env_file" COMPOSE_PROFILES 2>/dev/null || true)
    profiles=$(printf '%s' "$profiles" | tr -d '"')
    if printf '%s\n' "$profiles" | tr ',' '\n' | grep -qx 'openhands'; then
        had_profile=true
    fi
    local containers="" networks="" images="" volumes="" guard_volume=""
    if command -v docker >/dev/null 2>&1; then
        containers=$( {
            docker ps -a --format '{{.Names}}' 2>/dev/null \
                | grep -E '^(openhands|agent-docker-guard|agent-openhands-.+|oh-agent-server-.+|openhands-runtime-.+)$'
            docker ps -a --filter 'label=razzfazz.agent.type=openhands' --format '{{.Names}}' 2>/dev/null
            docker ps -a --filter 'label=razzfazz.docker-guard.owner' --format '{{.Names}}' 2>/dev/null
        } | awk 'NF && !seen[$0]++' || true)
        networks=$(docker network ls --format '{{.Name}}' 2>/dev/null \
            | grep -E '^(docker-guard|oh-sandbox-.+)$' || true)
        images=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
            | grep -E '^ghcr\.io/(openhands|all-hands-ai)/' || true)
        volumes=$(docker volume ls --format '{{.Name}}' 2>/dev/null \
            | grep -E '^(([a-z0-9_-]+_)?openhands-workspace|agent-openhands-.+)$' || true)
        guard_volume=$(docker volume ls --format '{{.Name}}' 2>/dev/null \
            | grep -x 'docker-guard-shared' || true)
    fi
    if [ "$had_profile" != true ] && [ -z "$containers" ] && [ -z "$networks" ] \
            && [ -z "$images" ] && [ -z "$guard_volume" ]; then
        return 0
    fi
    # The notice is about the module, its agents and their images. Once it was
    # printed on this box, or when only a network or the guard volume is left,
    # the leftovers go without it.
    local marker
    marker="$(dirname "$env_file")/.rzfz-state/openhands-retired"
    local notice=false
    if [ ! -f "$marker" ] && { [ "$had_profile" = true ] || [ -n "$containers" ] || [ -n "$images" ]; }; then
        notice=true
    fi

    if [ "$notice" = true ]; then
        print_step "Removing OpenHands (no longer part of the stack)..."
    else
        print_step "Removing what is left of OpenHands..."
    fi
    local c_list i_list v_list
    c_list=$(echo $containers)
    i_list=$(echo $images)
    v_list=$(echo $volumes)
    if [ "${DRY_RUN:-false}" = true ]; then
        [ "$had_profile" = true ] && print_substep "Would switch off the OpenHands module (COMPOSE_PROFILES)."
        [ -n "$c_list" ] && print_substep "Would stop and remove: $c_list"
        [ -n "$i_list" ] && print_substep "Would remove the images: $i_list"
        [ -n "$v_list" ] && print_substep "Would keep the data volumes: $v_list"
        return 0
    fi

    if [ "$had_profile" = true ]; then
        local new_profiles
        new_profiles=$(printf '%s\n' "$profiles" | tr ',' '\n' | grep -vx 'openhands' \
            | awk 'NF && !seen[$0]++' | paste -sd, || true)
        update_env_value "$env_file" COMPOSE_PROFILES "$new_profiles"
        print_substep "The OpenHands module is switched off (removed from COMPOSE_PROFILES)."
    fi
    local item
    while IFS= read -r item; do
        [ -n "$item" ] || continue
        docker rm -f "$item" >/dev/null 2>&1 \
            && print_substep "Removed container $item" \
            || print_warning "Could not remove container $item (remove it with: docker rm -f $item)"
    done <<< "$containers"
    local attached member
    while IFS= read -r item; do
        [ -n "$item" ] || continue
        attached=$(docker network inspect --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' \
            "$item" 2>/dev/null || true)
        while IFS= read -r member; do
            [ -n "$member" ] || continue
            docker network disconnect -f "$item" "$member" >/dev/null 2>&1 \
                || print_warning "Could not disconnect $member from network $item"
        done <<< "$attached"
        docker network rm "$item" >/dev/null 2>&1 \
            && print_substep "Removed network $item" \
            || print_warning "Could not remove network $item (remove it with: docker network rm $item)"
    done <<< "$networks"
    while IFS= read -r item; do
        [ -n "$item" ] || continue
        docker image rm "$item" >/dev/null 2>&1 \
            && print_substep "Removed image $item" \
            || print_warning "Could not remove image $item (remove it with: docker image rm $item)"
    done <<< "$images"
    [ -n "$guard_volume" ] && { docker volume rm "$guard_volume" >/dev/null 2>&1 || true; }

    [ "$notice" = true ] || return 0
    echo ""
    print_warning "OpenHands is no longer part of the stack and has been removed from this box."
    if [ -n "$volumes" ]; then
        print_info "  Its files were kept. They are in these Docker volumes:"
        while IFS= read -r item; do
            [ -n "$item" ] && print_info "    - $item"
        done <<< "$volumes"
        print_info "  Where a volume is on disk: docker volume inspect <volume> (the Mountpoint line)"
        print_info "  Copy what you need from there. When you no longer need a volume: docker volume rm <volume>"
        print_info "  These volumes are no longer part of the backup."
    else
        print_info "  No OpenHands data volume was found on this box."
    fi
    print_info "  Personal OpenHands agents stay listed in the Agent Manager, stopped, until their owner deletes them."
    if declare -F journal_event >/dev/null 2>&1; then
        journal_event "warning" "warn" "OpenHands removed from the stack; data volumes kept: $v_list" || true
    fi
    mkdir -p "$(dirname "$marker")" 2>/dev/null \
        && printf 'notice printed %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$marker" 2>/dev/null \
        || print_warning "Could not record that the OpenHands notice was shown ($marker); a later upgrade may show it again."
    return 0
}
# RETIRE_OPENHANDS-END

# #2500: the fleet hub's /v2 registry credential. LLM_HUB_REGISTRY_PASSWORD
# gates the manager's /api/hub-auth (FAIL-CLOSED while empty), which
# hub.<domain>'s /v2 forward_auths every docker login/pull through. Without it
# the first thin-worker join after an upgrade cannot authenticate to the hub —
# measured on 8.246, 2026-09-26 (2026.08-ga.15 → 2026.09-ga.3, part B): fixed
# by hand with `rzfz hub-credentials` (mints + restarts llm-manager), then the
# join succeeded. This mints the same pair on upgrade so that manual step is
# never required for the FIRST join. tests/unit/razzfazz-setup-cli/
# test_2500_upgrade_mints_hub_credentials.py runs this function directly.
#
# NOT a plain _gen_if_empty entry: that helper generates one independent
# random value per key, and this credential is really ONE password shared by
# two pairs — LLM_HUB_REGISTRY_USER/PASSWORD (the hub edge) and
# LLM_WORKER_REGISTRY_USER/PASSWORD (the CO-LOCATED worker-agent's own
# runner-image pulls through the host docker daemon, #1677). Generating each
# independently would mint two DIFFERENT passwords — worse than leaving both
# empty, because it would look provisioned while every worker-agent pull
# still 401s.
#
# No targeted container restart here, unlike `rzfz hub-credentials` itself:
# Step 8's restart_stack() (below, in the main upgrade flow) runs `docker
# compose up -d --force-recreate` — an UNCONDITIONAL recreate of every
# container, not merely an env-diff check — so llm-manager picks up the
# freshly-written LLM_HUB_REGISTRY_USER/PASSWORD regardless, the same
# guarantee every other secret generated here already relies on. (`rzfz
# post-install --refresh` has no such follow-up restart, which is why its
# own self-heal, ensure_hub_credentials_minted() in post-install.sh, calls
# hub-credentials.sh WITHOUT --no-restart — it needs the recreate done there
# and then.)
#
# #2500 fix round 1 (reviewer): this used to duplicate hub-credentials.sh's
# mint+mirror writes verbatim, AND only checked LLM_HUB_REGISTRY_PASSWORD —
# missing the partial state where a box has a hub password but an
# empty/stale worker pair (#559 predates #1677; reachable). Both defects are
# fixed by delegating to hub-credentials.sh's `--no-restart` mode: it is now
# the SINGLE place that mints, mirrors, AND repairs a partial pair, so this
# function's only job is deciding WHETHER to bother calling it (cheap local
# .env reads, no subprocess) — comparing the worker pair, not just the hub
# password being non-empty.
#
# Gated on the llm-manager profile (issue #2500: "when the llm-registry/
# manager trio is active") — a box that never runs it has no hub to
# authenticate against. Returns 1 (nothing to do) when the profile is off or
# the pair is already fully consistent — idempotent, and cheap to call
# unconditionally from generate_missing_secrets.
mint_hub_registry_credentials_if_missing() {
    local dry_run="${1:-false}"
    profile_active "llm-manager" || return 1
    local hub_user hub_pw worker_user worker_pw
    hub_user="$(read_env_value ".env" LLM_HUB_REGISTRY_USER)"
    hub_user="${hub_user:-fleet}"
    hub_pw="$(read_env_value ".env" LLM_HUB_REGISTRY_PASSWORD)"
    worker_user="$(read_env_value ".env" LLM_WORKER_REGISTRY_USER)"
    worker_pw="$(read_env_value ".env" LLM_WORKER_REGISTRY_PASSWORD)"
    # Fully provisioned AND mirrored already — nothing to do.
    if [ -n "${hub_pw}" ] && [ "${worker_user}" = "${hub_user}" ] && [ "${worker_pw}" = "${hub_pw}" ]; then
        return 1
    fi
    if [ "$dry_run" = "true" ]; then
        if [ -z "${hub_pw}" ]; then
            print_info "  Generating LLM_HUB_REGISTRY_PASSWORD (+ mirrored LLM_WORKER_REGISTRY_USER/PASSWORD, #2500)"
        else
            print_info "  Repairing LLM_WORKER_REGISTRY_USER/PASSWORD to mirror the existing hub credential (#2500)"
        fi
        return 0
    fi
    if [ -x "${SCRIPT_DIR}/cli/hub-credentials.sh" ]; then
        "${SCRIPT_DIR}/cli/hub-credentials.sh" --no-restart \
            && print_substep "  Hub registry credential provisioning applied (#2500)" \
            || print_warning "  Hub credential provisioning reported an issue — run 'rzfz hub-credentials' manually."
    else
        print_warning "  hub-credentials.sh not found — hub registry credentials NOT minted/repaired; run 'rzfz hub-credentials' manually."
    fi
    return 0
}

generate_missing_secrets() {
    local dry_run="${1:-false}"
    local action_verb="Generating"
    [ "$dry_run" = "true" ] && action_verb="Would generate"

    if [ "$dry_run" = "true" ]; then
        print_step "Checking for missing secrets..."
    else
        print_step "Generating missing secrets..."
    fi

    local generated=0

    # #2471: keys that ENCRYPT a module's stored data (core/config/profiles.yaml
    # `data_bound_secrets:`) are never generated over a database that holds data.
    # Read once. If the declaration cannot be read, a name rule stands in for
    # it — this pass fails closed, never open.
    local -A _data_bound=()
    local -a _data_bound_refused=()
    local _db_list="" _db_unknown=false _p _k
    if _db_list=$(PYTHONPATH="${SCRIPT_DIR}/core/common${PYTHONPATH:+:$PYTHONPATH}" \
            python3 -m razzfazz_common.data_bound_secrets all \
            --profiles-yaml "${SCRIPT_DIR}/core/config/profiles.yaml" 2>/dev/null); then
        while read -r _p _k; do
            [ -n "${_k:-}" ] && _data_bound["$_p:$_k"]=1
        done <<< "$_db_list"
    else
        _db_unknown=true
        print_warning "  #2471: cannot read the data-bound secret declarations — every *_ENCRYPTION_KEY / *_SECRET_KEY / *_AUTH_SECRET is treated as data-bound this run."
    fi

    # ── Secret generation table ──
    # Format: generate_if_missing KEY GENERATOR_CMD CONDITION
    # CONDITION: "core" (always), or a profile name
    #
    # Helper to check and generate
    _gen_if_empty() {
        local key="$1"
        local gen_cmd="$2"
        local condition="$3"  # "core" or profile name

        if ! secret_is_empty "$key"; then
            return
        fi

        # Check profile condition
        if [ "$condition" != "core" ]; then
            if ! profile_active "$condition"; then
                return
            fi
        fi

        local _bound=false
        if [ -n "${_data_bound[$condition:$key]:-}" ]; then
            _bound=true
        elif [ "$_db_unknown" = true ]; then
            case "$key" in *_ENCRYPTION_KEY|*_SECRET_KEY|*_AUTH_SECRET) _bound=true ;; esac
        fi
        if [ "$_bound" = true ]; then
            local _refusal="" _rc=0
            _refusal=$(PYTHONPATH="${SCRIPT_DIR}/core/common${PYTHONPATH:+:$PYTHONPATH}" \
                python3 -m razzfazz_common.data_bound_secrets check --profile "$condition" \
                --profiles-yaml "${SCRIPT_DIR}/core/config/profiles.yaml" --env .env 2>&1) || _rc=$?
            if [ "$_rc" -ne 0 ]; then
                print_error "  NOT generating ${key}: ${_refusal:-the data-bound check failed (exit ${_rc})}"
                _data_bound_refused+=("$key")
                return
            fi
        fi

        if [ "$dry_run" = "true" ]; then
            print_info "  ${action_verb} ${key}"
        else
            local value
            value=$(eval "$gen_cmd")
            update_env_value ".env" "$key" "$value"
            print_substep "  Generated ${key}"
        fi
        ((generated++)) || true
    }

    # ── Core secrets (always required) ──
    _gen_if_empty "WEBUI_SECRET_KEY"           'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "AUTHENTIK_SECRET_KEY"        'generate_secret_upgrade 42'          "core"
    _gen_if_empty "AUTHENTIK_BOOTSTRAP_TOKEN"   'generate_secret_upgrade 48'          "core"
    _gen_if_empty "POSTGRES_PASSWORD"           'generate_password_upgrade 24'        "core"
    _gen_if_empty "VALKEY_PASSWORD"             'generate_password_upgrade 24'        "core"
    _gen_if_empty "GPUSTACK_SECRET_KEY"         'generate_secret_upgrade 32'          "core"
    _gen_if_empty "KOMODO_PASSKEY"              'generate_password_upgrade 24'        "core"
    # #3309: Komodo's own cluster password. SAFE to mint on upgrade: the
    # postgres-komodo-reconcile one-shot moves the existing cluster to it (it
    # logs in with the previous password once) before ferretdb starts.
    _gen_if_empty "KOMODO_PG_PASSWORD"          'generate_password_upgrade 24'        "core"
    _gen_if_empty "ACME_EMAIL"                  'echo ""'                             "core"  # not a secret, skip

    # Core client secrets (Authentik proxy providers — always on)
    _gen_if_empty "CHAT_CLIENT_SECRET"          'generate_secret_upgrade 64'          "core"
    _gen_if_empty "ADMIN_CLIENT_SECRET"         'generate_secret_upgrade 64'          "core"
    _gen_if_empty "LLM_CLIENT_SECRET"           'generate_secret_upgrade 64'          "core"
    _gen_if_empty "BACKUP_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "LICENSES_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "SETUP_CLIENT_SECRET"         'generate_hex_secret_upgrade 32'      "core"
    _gen_if_empty "HELP_CLIENT_SECRET"          'generate_hex_secret_upgrade 32'      "core"
    # #386: existing boxes replace the public-mirror-known static value on
    # their next upgrade (INIT_VERSION bump re-applies the blueprint).
    _gen_if_empty "CONFIG_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "core"
    # #2507: the native-OIDC clients of Open WebUI and Gitea. init.sh seeds
    # them UNCONDITIONALLY (so flipping the switch later "just works"), but an
    # upgrade never did: on prod (2026.09-ga.3) both secrets were absent from
    # .env while ENABLE_OPENWEBUI_OIDC / ENABLE_GITEA_AUTHENTIK_OIDC were true,
    # so authentik-init skipped both provider blueprints on every run
    # ("...CLIENT_ID or SECRET empty — skipping"), /application/o/openwebui-oidc/
    # and /gitea-oidc/ answered 404, and OWUI showed an Authentik button that
    # led nowhere. "core", like init: the pair must exist before the switch is
    # flipped, whatever the profiles. The ids are fixed strings (init's values).
    _gen_if_empty "OPENWEBUI_OIDC_CLIENT_ID"    'echo openwebui_oidc_client'          "core"
    _gen_if_empty "OPENWEBUI_OIDC_CLIENT_SECRET" 'generate_hex_secret_upgrade 32'     "core"
    _gen_if_empty "GITEA_OIDC_CLIENT_ID"        'echo gitea_oidc_client'              "core"
    _gen_if_empty "GITEA_OIDC_CLIENT_SECRET"    'generate_hex_secret_upgrade 32'      "core"

    # Core infrastructure secrets
    # F10: BACKUP_ENCRYPTION_PASSWORD defaults to AUTHENTIK_BOOTSTRAP_PASSWORD
    # (the password printed on the sticker). If the stack dir is lost, the
    # operator can still decrypt backups using the well-known admin password.
    # Generating a fresh random value here would orphan the backups when the
    # .env is gone.
    _gen_if_empty "BACKUP_ENCRYPTION_PASSWORD"  'read_env_value .env AUTHENTIK_BOOTSTRAP_PASSWORD' "core"
    _gen_if_empty "SMTP_INTERNAL_PASSWORD"      'generate_password_upgrade 24'        "core"

    # #184/#168 rc1 (security M-1/M-2): mint these opt-in-module secrets on EVERY
    # upgrade (gated "core" = always), not just when their profile is active — so
    # a box that ENABLES observability / mac-llm AFTER upgrading never runs with an
    # empty (fail-open) secret. CLICKHOUSE_PASSWORD auths the ClickHouse `default`
    # user (network-reachable on the docker bridge, holds LLM prompt/response
    # spans); MAC_GATEWAY_MASTER_KEY is the LiteLLM Mac-gateway's only auth.
    _gen_if_empty "CLICKHOUSE_PASSWORD"         'generate_password_upgrade 24'        "core"
    _gen_if_empty "MAC_GATEWAY_MASTER_KEY"      'generate_password_upgrade 32'        "core"
    # postgres-exporter's dedicated pg_monitor role (#253 X5 S3). SAFE to mint
    # on upgrade for the same reason as LLM_MANAGER_DB_PASSWORD below:
    # postgres_exporter_user is a NEW role with no ${:-POSTGRES_PASSWORD}
    # superuser fallback in compose, and the init-db reconcile on this same
    # upgrade creates it with this value. Without it the exporter would keep
    # connecting as the cluster superuser.
    _gen_if_empty "POSTGRES_EXPORTER_PASSWORD"  'generate_password_upgrade 24'        "core"

    # LLM Manager (#254, profile llm-manager) — same opt-in-module
    # rationale as CLICKHOUSE/MAC above. SAFE to mint on upgrade (unlike the
    # commented-out core DB passwords below): llm_manager_user is a NEW
    # dedicated role with NO ${:-POSTGRES_PASSWORD} superuser fallback in
    # compose, and init-db reconcile creates it with this value on the same
    # upgrade — there is no pre-existing shared user whose stored password we
    # could contradict. LITELLM_INTERNAL_KEY is the router master key
    # (fail-closed if empty).
    _gen_if_empty "LLM_MANAGER_DB_PASSWORD"    'generate_password_upgrade 24'        "core"
    _gen_if_empty "LITELLM_INTERNAL_KEY"        'generate_hex_secret_upgrade 32'      "core"
    # Node→manager registration key (#254 P2-B1). Backfilled on upgrade so the
    # worker-agent can register; fail-closed (registration disabled) if left empty.
    _gen_if_empty "LLM_MANAGER_NODE_KEY"        'generate_hex_secret_upgrade 32'      "core"
    # #3301: the bearer key callers of the Ollama-protocol proxy must send.
    # Backfilled so an existing box's proxy is gated, not closed to everyone.
    _gen_if_empty "OLLAMA_PROXY_CLIENT_KEY"     'generate_hex_secret_upgrade 32'      "core"
    # LLM Manager UI forward-auth provider secret (#254 P2-P3). Backfilled so the
    # Authentik blueprint for llm-manager.<domain> can resolve it on upgrade.
    _gen_if_empty "LLM_MANAGER_UI_CLIENT_SECRET" 'generate_hex_secret_upgrade 32'     "core"
    _gen_if_empty "LLM_HUB_CLIENT_SECRET" 'generate_hex_secret_upgrade 32'     "core"
    # #3302: the model registry's writer identity. Safe to mint on upgrade: the
    # registry's htpasswd is derived from it at every start (llm-registry-auth),
    # so there is no stored copy to contradict.
    _gen_if_empty "LLM_REGISTRY_WRITE_PASSWORD" 'generate_hex_secret_upgrade 32' "core"

    # #2500: fleet hub /v2 registry credential (see
    # mint_hub_registry_credentials_if_missing above for why this is not a
    # plain _gen_if_empty entry).
    mint_hub_registry_credentials_if_missing "$dry_run" && ((generated++)) || true

    # Per-service DB passwords (core services):
    # intentionally NOT generated on upgrade — on fresh installs, init-db.sh
    # creates dedicated PostgreSQL users (authentik_user, openwebui_user, …)
    # and razzfazz-init.sh generates these passwords to match; on an upgrade
    # from pre-2026.05, those users don't exist yet and compose always falls
    # back to ${POSTGRES_USER}/${POSTGRES_PASSWORD} (see compose files:
    # ${AUTHENTIK_DB_PASSWORD:-${POSTGRES_PASSWORD}} pattern). Generating here
    # would install a password that doesn't match what postgres stored for
    # the shared 'docker' user, breaking DB auth for every core service.
    # Creating the per-service users is the job of the ga.6 DB-user migration
    # in run_data_migrations(); once that migration is extended to cover these
    # core services, re-enable the generators below.
    #_gen_if_empty "AUTHENTIK_DB_PASSWORD"        'generate_password_upgrade 24'       "core"
    #_gen_if_empty "OPENWEBUI_DB_PASSWORD"        'generate_password_upgrade 24'       "core"
    #_gen_if_empty "GPUSTACK_DB_PASSWORD"         'generate_password_upgrade 24'       "core"

    # Per-service admin passwords (core services)
    _gen_if_empty "GPUSTACK_ADMIN_PASSWORD"      'generate_password_upgrade 24'       "core"
    _gen_if_empty "KOMODO_INIT_ADMIN_PASSWORD"   'generate_password_upgrade 24'       "core"

    # ── Chat profile ──
    _gen_if_empty "PIPELINES_API_KEY"           'generate_secret_upgrade 32'          "chat"

    # ── Dify profile ──
    _gen_if_empty "DIFY_CLIENT_SECRET"          'generate_secret_upgrade 64'          "dify"
    _gen_if_empty "PLUGIN_DAEMON_KEY"           'generate_secret_upgrade 42'          "dify"
    _gen_if_empty "PLUGIN_DIFY_INNER_API_KEY"   'generate_secret_upgrade 42'          "dify"
    _gen_if_empty "SANDBOX_API_KEY"             'generate_hex_secret_upgrade 32'      "dify"
    # DIFY_DB_PASSWORD / DIFY_PLUGIN_DB_PASSWORD — NOT generated on upgrade
    # (same reason as AUTHENTIK_DB_PASSWORD above — compose falls back to
    # POSTGRES_PASSWORD; generating would break DB auth until the ga.6
    # per-service-user migration is extended).
    #_gen_if_empty "DIFY_DB_PASSWORD"            'generate_password_upgrade 24'        "dify"
    #_gen_if_empty "DIFY_PLUGIN_DB_PASSWORD"     'generate_password_upgrade 24'        "dify"
    _gen_if_empty "DIFY_ADMIN_PASSWORD"         'generate_password_upgrade 24'        "dify"

    # ── Gitea profile ──
    _gen_if_empty "GITEA_SECRET_KEY"            'generate_hex_secret_upgrade 32'      "gitea"
    _gen_if_empty "GITEA_INTERNAL_TOKEN"        'generate_secret_upgrade 64'          "gitea"
    _gen_if_empty "GITEA_CLIENT_SECRET"         'generate_hex_secret_upgrade 32'      "gitea"
    # GITEA_DB_PASSWORD — NOT generated on upgrade (same reason as
    # AUTHENTIK_DB_PASSWORD). compose uses ${GITEA_DB_PASSWORD:-${POSTGRES_PASSWORD}}.
    #_gen_if_empty "GITEA_DB_PASSWORD"           'generate_password_upgrade 24'        "gitea"

    # ── LightRAG profile ──
    _gen_if_empty "LIGHTRAG_API_KEY"            'generate_secret_upgrade 32'          "lightrag"
    _gen_if_empty "LIGHTRAG_TOKEN_SECRET"       'generate_secret_upgrade 32'          "lightrag"
    _gen_if_empty "LIGHTRAG_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "lightrag"

    # ── Cognee profile ──
    _gen_if_empty "COGNEE_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "cognee"
    _gen_if_empty "COGNEE_JWT_SECRET"           'generate_hex_secret_upgrade 32'      "cognee"
    _gen_if_empty "COGNEE_ADMIN_PASSWORD"       'generate_password_upgrade 24'        "cognee"
    _gen_if_empty "FALKORDB_PASSWORD"           'generate_secret_upgrade 32'          "cognee"

    # ── Docling profile ──
    _gen_if_empty "DOCLING_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "docling"

    # ── Stirling-PDF profile ──
    _gen_if_empty "STIRLING_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "stirling-pdf"

    # ── Crawl4AI profile ── (#191) forward_auth proxy-provider client_secret;
    # previously minted nowhere, so the provider applied with an empty secret.
    _gen_if_empty "CRAWL4AI_CLIENT_SECRET"      'generate_hex_secret_upgrade 32'      "crawl4ai"
    _gen_if_empty "SEARXNG_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "searxng"
    # (#191) startup auth guard: 0.0.0.0 bind + no token = sys.exit(1) crash-loop.
    # Also the Bearer Caddy injects upstream. Back-fill on every upgrade.
    _gen_if_empty "CRAWL4AI_API_TOKEN"          'generate_hex_secret_upgrade 32'      "crawl4ai"

    # ── Paperclip profile ──
    _gen_if_empty "PAPERCLIP_DB_PASSWORD"       'generate_hex_secret_upgrade 16'      "paperclip"
    _gen_if_empty "PAPERCLIP_BETTER_AUTH_SECRET" 'generate_hex_secret_upgrade 32'     "paperclip"
    _gen_if_empty "PAPERCLIP_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "paperclip"

    # ── Matrix profile ──
    _gen_if_empty "SYNAPSE_DB_PASSWORD"         'generate_hex_secret_upgrade 16'      "matrix"
    _gen_if_empty "SYNAPSE_REGISTRATION_SHARED_SECRET" 'generate_hex_secret_upgrade 32' "matrix"
    _gen_if_empty "SYNAPSE_MACAROON_SECRET_KEY" 'generate_hex_secret_upgrade 32'      "matrix"
    _gen_if_empty "SYNAPSE_FORM_SECRET"         'generate_hex_secret_upgrade 16'      "matrix"
    _gen_if_empty "SYNAPSE_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "matrix"
    _gen_if_empty "MATRIX_CLIENT_ID"            'generate_hex_secret_upgrade 16'      "matrix"
    _gen_if_empty "ELEMENT_WEB_CLIENT_SECRET"   'generate_hex_secret_upgrade 32'      "matrix"

    # ── Moltis profile ──
    _gen_if_empty "MOLTIS_PASSWORD"             'generate_hex_secret_upgrade 16'      "moltis"
    _gen_if_empty "MOLTIS_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "moltis"

    # ── Hermes profile ──
    _gen_if_empty "HERMES_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "hermes"

    # ── Paperless-ngx profile ──
    _gen_if_empty "PAPERLESS_SECRET_KEY"        'generate_secret_upgrade 64'          "paperless-ngx"
    _gen_if_empty "PAPERLESS_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "paperless-ngx"
    _gen_if_empty "PAPERLESS_DB_PASSWORD"       'generate_password_upgrade 24'        "paperless-ngx"

    # ── Vaultwarden profile ──
    # VAULTWARDEN_ADMIN_TOKEN is intentionally NOT backfilled — empty = /admin panel
    # disabled by default. Re-generating it here would silently RE-ENABLE the shared-secret
    # /admin backdoor on every upgrade (and clobber an operator's deliberate disable).
    # Operators who want /admin set it manually (preferably an Argon2 `vaultwarden hash`).
    _gen_if_empty "VAULTWARDEN_CLIENT_SECRET"   'generate_hex_secret_upgrade 32'      "vaultwarden"

    # ── Infisical profile ──
    _gen_if_empty "INFISICAL_ENCRYPTION_KEY"    'generate_hex_secret_upgrade 16'      "infisical"
    _gen_if_empty "INFISICAL_AUTH_SECRET"       'generate_secret_upgrade 42'          "infisical"
    _gen_if_empty "INFISICAL_CLIENT_SECRET"     'generate_hex_secret_upgrade 32'      "infisical"
    _gen_if_empty "INFISICAL_DB_PASSWORD"       'generate_password_upgrade 24'        "infisical"

    # ── Onyx profile ──
    _gen_if_empty "ONYX_SECRET"                 'generate_secret_upgrade 42'          "onyx"
    _gen_if_empty "ONYX_CLIENT_SECRET"          'generate_hex_secret_upgrade 32'      "onyx"
    _gen_if_empty "ONYX_DB_PASSWORD"            'generate_password_upgrade 24'        "onyx"

    # ── OpenUEM profile (#1075) ──
    _gen_if_empty "OPENUEM_CONSOLE_JWT_KEY"     'generate_hex_secret_upgrade 32'      "openuem"
    _gen_if_empty "OPENUEM_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "openuem"
    _gen_if_empty "OPENUEM_DB_PASSWORD"         'generate_password_upgrade 24'        "openuem"

    # ── Wazuh profile (#855) ──
    # _gen_if_empty only fires when the key is empty AND the profile is active,
    # so a box that never enables `wazuh` is untouched, and one that enables it
    # gets all five on the next `rzfz upgrade`.
    _gen_if_empty "WAZUH_INDEXER_PASSWORD"      'generate_password_upgrade 32'        "wazuh"
    _gen_if_empty "WAZUH_DASHBOARD_PASSWORD"    'generate_password_upgrade 32'        "wazuh"
    _gen_if_empty "WAZUH_API_PASSWORD"          'generate_password_upgrade 32'        "wazuh"
    _gen_if_empty "WAZUH_AUTHD_PASSWORD"        'generate_hex_secret_upgrade 32'      "wazuh"
    # rev-B blocker 4: the forward-auth ProxyProvider secret is a SECOND,
    # separate Authentik secret (Gitea pattern). Without it the outpost has no
    # provider to match wazuh.<domain> against and the vhost is 404 for
    # everyone, login redirect included.
    _gen_if_empty "WAZUH_CLIENT_SECRET"         'generate_hex_secret_upgrade 32'      "wazuh"
    _gen_if_empty "WAZUH_OIDC_CLIENT_SECRET"    'generate_hex_secret_upgrade 32'      "wazuh"

    # ── OpenBao profile (#2476) ──
    # The forward-auth ProxyProvider secret and OpenBao's own OIDC client
    # (blueprint 38-openbao.yaml; init-authentik deploys it only with all three).
    _gen_if_empty "OPENBAO_CLIENT_SECRET"       'generate_hex_secret_upgrade 32'      "openbao"
    _gen_if_empty "OPENBAO_OIDC_CLIENT_ID"      'generate_hex_secret_upgrade 16'      "openbao"
    _gen_if_empty "OPENBAO_OIDC_CLIENT_SECRET"  'generate_hex_secret_upgrade 32'      "openbao"

    # ── Coding Tools profile ──
    _gen_if_empty "CODING_TOOLS_CLIENT_SECRET"  'generate_hex_secret_upgrade 32'      "coding-tools"

    # ── Agent Manager profile ──
    _gen_if_empty "AGENTS_CLIENT_SECRET"        'generate_hex_secret_upgrade 32'      "agents"
    # C1 bypass fix (PR #84 re-review): "came through Caddy" proof secret shared
    # only Caddy↔agent-manager (X-Razzfazz-Proxy-Proof). Empty => the manager's
    # per-instance proxy fails CLOSED (refuses every agent-subdomain request), so
    # an upgraded agents-enabled box that never ran init MUST get it minted here.
    _gen_if_empty "MANAGER_PROXY_SECRET"        'generate_hex_secret_upgrade 32'      "agents"

    # ── MCP Manager profile (#36 / #61 NEW-3) ──
    # MCP_INTERNAL_TOKEN gates mcp-manager's /internal/* (agent-manager presents
    # it to fetch per-user MCP wiring at agent launch). Empty => /internal fails
    # closed (401) => the per-proxy bearer never reaches the user's agents. Only
    # init.sh minted these before; an UPGRADED mcp-enabled box never ran init's
    # secret regeneration, so they stayed empty. Mint-if-empty on upgrade too.
    # MCP_MANAGER_SECRET_KEY = base64(32 bytes) = a valid AES-256-GCM master key
    # (same `generate_secret 32` init uses; do NOT rotate an existing one — that
    # would orphan every already-encrypted credential, which secret_is_empty
    # guards against). The matching mint-if-absent self-heal also lives in
    # cli/post-install.sh ensure_mcp_manager_secret().
    _gen_if_empty "MCP_MANAGER_SECRET_KEY"      'generate_secret_upgrade 32'          "mcp"
    _gen_if_empty "MCP_MANAGER_DB_PASSWORD"     'generate_password_upgrade 24'        "mcp"
    _gen_if_empty "MCP_INTERNAL_TOKEN"          'generate_hex_secret_upgrade 32'      "mcp"
    # #2419: agent-manager (agents profile) presents the same token; an
    # agents-only box needs it before mcp is ever enabled. _gen_if_empty is
    # fill-if-empty, so the line above and this one mint at most once.
    _gen_if_empty "MCP_INTERNAL_TOKEN"          'generate_hex_secret_upgrade 32'      "agents"
    _gen_if_empty "MCP_CLIENT_SECRET"           'generate_hex_secret_upgrade 32'      "mcp"
    # #2086: mcp-manager's gateway <-> the docling-egress sidecar. Minted on
    # EVERY box ("core"), not only while docling is on: a module enabled later
    # from the portal never runs this, and both consumers fail closed without
    # it (the sidecar refuses to start, the gateway refuses the call).
    _gen_if_empty "DOCLING_RELAY_SECRET"        'generate_hex_secret_upgrade 32'      "core"

    # ── Monitor profile ──
    # (KOMODO_INIT_ADMIN_PASSWORD already handled as core above since Komodo is monitor profile)

    if [ $generated -eq 0 ]; then
        print_substep "All secrets are already populated."
    elif [ "$dry_run" = "true" ]; then
        print_info "${generated} secret(s) would be generated."
    else
        print_success "${generated} missing secret(s) generated."

        # Sync shared secrets to .env.dify if it exists
        if [ -f ".env.dify" ]; then
            sync_dify_valkey_password ".env" ".env.dify" || true
            for key in PLUGIN_DAEMON_KEY PLUGIN_DIFY_INNER_API_KEY SANDBOX_API_KEY; do
                local env_val
                env_val=$(read_env_value ".env" "$key")
                if [ -n "$env_val" ]; then
                    update_env_value ".env.dify" "$key" "$env_val"
                fi
            done
            local dify_db_user dify_db_pass
            dify_db_user=$(read_env_value ".env" "DIFY_DB_USER")
            dify_db_pass=$(read_env_value ".env" "DIFY_DB_PASSWORD")
            if [ -n "$dify_db_user" ] && [ -n "$dify_db_pass" ]; then
                update_env_value ".env.dify" "DB_USERNAME" "$dify_db_user"
                update_env_value ".env.dify" "DB_PASSWORD" "$dify_db_pass"
            fi
            # #952 rev-B (review F1): the DIFY_ADMIN_PASSWORD -> INIT_PASSWORD
            # copy is GONE. INIT_PASSWORD is only Dify's first-run setup gate
            # and Dify 1.14+ caps it at 30 chars; after #952 aligns
            # DIFY_ADMIN_PASSWORD to CONFIG_ADMIN_PASSWORD (which may be >30),
            # the old blind copy would make /console/api/init 422 permanently
            # on every upgraded box. The two values are deliberately decoupled
            # (same separation cli/init.sh enforces): keep an existing
            # INIT_PASSWORD untouched, and only seed a missing/empty one with a
            # fresh dedicated <=30-char token.
            local dify_init_pw
            dify_init_pw=$(read_env_value ".env.dify" "INIT_PASSWORD")
            if [ -z "$dify_init_pw" ]; then
                update_env_value ".env.dify" "INIT_PASSWORD" "$(generate_password 24)"
            fi
        fi
    fi

    if [ "${#_data_bound_refused[@]}" -gt 0 ]; then
        print_warning "${#_data_bound_refused[@]} data-bound secret(s) left EMPTY on purpose (#2471): ${_data_bound_refused[*]} — their module stays down until the original key is back in .env or its data is reset (rzfz module reset-data <profile> --i-understand-data-loss)."
    fi
}

# ==============================================================================
# Build & Pull Docker Images
# ==============================================================================
# #2402: does the package's image set, now loaded, cover what THIS tree runs?
# The question init already asks (#2006 part 2). The upgrade skipped it and let
# RAZZFAZZ_NETWORK_MODE=online send a complete package to a registry — fatal
# behind an air gap. The mode says whether a FALLBACK download is allowed, not
# whether to download what the package has. rc 0 covered, 1 not covered, 2 cannot
# tell (never "covered"). Sets _PKG_COVER_LINE.
upgrade_package_covers_tree() {
    local out line blocking profiles rc=0
    _PKG_COVER_LINE=""
    out="$(python3 "${SCRIPT_DIR}/core/config/app/services/expected_images.py" --stack-root "$SCRIPT_DIR" \
            --verify --active --quiet 2>/dev/null)" || rc=$?
    line="$(printf '%s\n' "$out" | grep -E '^verify-images \(active set\): [0-9]+ expected, [0-9]+ present, [0-9]+ missing\.$' || true)"
    [ -n "$line" ] || return 2
    _PKG_COVER_LINE="${line#verify-images (active set): }"
    # #2778: ask the provenance question EVEN when an image is missing. It used to
    # stop at the first gap, so an operator who fixed one missing image only then
    # learned about the stale custom images behind it. Name both in one run.
    local covered=0
    if [ "$rc" -ne 0 ] || ! printf '%s' "$line" | grep -q ', 0 missing\.$'; then
        covered=1
    fi
    profiles="${COMPOSE_PROFILES:-$(read_env_value .env COMPOSE_PROFILES)}"
    blocking="$(razzfazz_custom_images_not_from_this_tree "$profiles" "$(razzfazz_package_manifest_state_path)")"
    if [ -n "$blocking" ]; then
        _PKG_COVER_LINE="${_PKG_COVER_LINE%.}; not the package's build: $(printf '%s\n' "$blocking" | awk -F'\t' 'NF{printf "%s [%s] ", $1, $3}')"
        return 1
    fi
    return "$covered"
}

build_and_pull() {
    # #2524: --migrations-only builds and pulls nothing; the tree's images are the box's.
    if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
        print_substep "Build and pull: skipped (--migrations-only)."
        return 0
    fi
    print_step "Updating Docker images..."

    # rc6.10 fix: REQUIRES_BUILD/_PULL are populated from migrations/env-changes.json's
    # `needs_build`/`needs_pull` flags by migrate_env(). A release that ships new
    # source for a locally-built image (Caddy, start-portal, setup, help, licenses,
    # backup, dify-web, llm-gpustack-vulkan, …) WITHOUT bumping the
    # migration manifest will silently keep stale code in containers. Caught
    # 2026-05-08 on 0.91 (M028-S05 upgrade) where start-portal kept old app.py
    # because no migration entry mentioned it. Heuristic: if HEAD actually moved
    # during this upgrade, force a rebuild + repull. Build cache hits make the
    # cost trivial when nothing changed; the cost of skipping when something
    # DID change is silent code drift.
    # #431: the heuristic must NOT fire on the --package path — the package
    # carries the target commit's images (custom builds included) and
    # code_update_package just docker-loaded them (SKIP_PULL=true was set for
    # exactly that reason). Forcing a rebuild+repull there added a long build
    # to an upgrade whose entire purpose was to avoid building; verify-images
    # stays the completeness gate for the loaded set.
    if [ -z "${PACKAGE_FILE:-}" ] \
       && [ -n "${INSTALLED_COMMIT:-}" ] && [ -n "${TARGET_COMMIT:-}" ] \
       && [ "${INSTALLED_COMMIT}" != "${TARGET_COMMIT}" ] \
       && [ "${INSTALLED_COMMIT}" != "unknown" ] \
       && [ "${TARGET_COMMIT}" != "unknown" ]; then
        if [ "${REQUIRES_BUILD:-false}" != true ]; then
            print_substep "Commit moved (${INSTALLED_COMMIT} → ${TARGET_COMMIT}); forcing image rebuild even though no manifest entry requested it."
            REQUIRES_BUILD=true
            REQUIRES_PULL=true
        fi
    fi

    # Locally built images this box runs but lacks (a pruned custom image on a
    # same-version retry): the manifest range says "no build", so ask. A bare
    # pull cannot fetch them; the build step below makes them.
    local _missing_built=""
    if ! razzfazz_is_offline && [ -z "${PACKAGE_FILE:-}" ]; then
        _missing_built=$("${SCRIPT_DIR}/rzfz" verify-images --missing --built --active 2>/dev/null) || _missing_built=""
        # #2566: a build only lost its local tag (a prune) while the hub tag
        # kept the image: put the name back instead of rebuilding.
        local _r _still=""
        while IFS= read -r _r; do
            [ -n "$_r" ] || continue
            if razzfazz_retag_from_hub "$_r"; then
                print_substep "${_r}: present only under the hub tag — re-tagged, not rebuilt."
            else
                _still="${_still}${_r}"$'\n'
            fi
        done <<< "$_missing_built"
        _missing_built="${_still%$'\n'}"
        if [ -n "$_missing_built" ] && [ "${REQUIRES_BUILD:-true}" != true ]; then
            print_substep "Locally built image(s) missing on this box ($(printf '%s' "$_missing_built" | tr '\n' ' ')) — adding the build step."
            REQUIRES_BUILD=true
        fi
    fi

    # #2402: a package that covers this tree is neither built from nor pulled for.
    local _cover_rc=1
    if [ -n "${PACKAGE_FILE:-}" ] && [ "${FORCE_BUILD:-false}" != true ]; then
        upgrade_package_covers_tree && _cover_rc=0 || _cover_rc=$?
        if [ "$_cover_rc" -ne 0 ]; then
            print_substep "The package does not cover this tree (${_PKG_COVER_LINE:-cannot tell}) — building/pulling what is missing (network mode $(razzfazz_network_mode), #2402)."
            # A box that may build does: the range's manifest flag says what the
            # release asked for, not what this package left uncovered. Without this
            # the line above was false and the pre-restart check refused (or an old
            # image restarted) on a box that could have built it.
            razzfazz_is_offline || REQUIRES_BUILD=true
        fi
    fi

    if [ "$_cover_rc" -eq 0 ]; then
        print_substep "The package carries every image this tree runs (${_PKG_COVER_LINE}), each custom image the package's build — no build, no pull, whatever the network mode (#2402)."
        SKIP_PULL=true
    elif razzfazz_is_offline; then
        # #184 WS2b: an air-gapped box obtains every image from the offline package
        # (loaded by code_update_package). A `docker compose build` here would try
        # to pull base images + apt/pip/npm → firewall trip. Skip it; verify-images
        # (in restart_stack) is the hard check that the loaded set is complete.
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping image build — images come from the offline package."
        # #2757: a code-only package supplies no image. Say which custom images the
        # box keeps from an earlier build and what would fix it; never imply the
        # package covers them.
        case "${_PKG_COVER_LINE:-}" in
            *"not the package's build:"*)
                print_warning "These custom images are NOT this tree's build and the package does not carry them: ${_PKG_COVER_LINE#*not the package\'s build: }— they keep their old build. This needs a package with images (an offline package (Enterprise)) or an online rebuild." ;;
        esac
    elif [ "${REQUIRES_BUILD:-true}" = true ] || [ "${FORCE_BUILD:-false}" = true ]; then
        print_substep "Building custom images..."
        # #185: a single transient network blip during build (e.g. apt-get
        # 'Could not resolve deb.debian.org' / pip connection reset — common on
        # WiFi boxes like 0.208) used to fail the build and ROLL BACK the whole
        # upgrade. Retry the build a few times before giving up; BuildKit reuses
        # completed layers so retries are cheap.
        # #2820: that retry now lives in the shared wrapper (razzfazz_image_retry
        # inside razzfazz_compose_build): up to 3 tries, but ONLY for a transient
        # upstream error. The old loop here retried every failure three times,
        # 40 s of sleep in front of a Dockerfile error that cannot change.
        local _build_ok=false
        # #184 WS2a: strip the no-build overlay so the build: contexts are present
        # for this ONE intended build (ensure_nobuild_overlay composed it above,
        # before build_and_pull, so restart_stack's `up` stays build-proof).
        local _build_cf; _build_cf="$(compose_file_for_build)"
        if COMPOSE_FILE="$_build_cf" razzfazz_compose_build --parallel 2>&1; then
            _build_ok=true
            # #2006 part 2: the upgrade's build is the one that makes an old
            # box current — record it, or the next init treats these images
            # as adopted from the cache.
            razzfazz_record_custom_image_builds "" built
        fi
        if [ "$_build_ok" != true ]; then
            print_error "Docker build failed (a transient upstream error is retried up to 3 times; see the attempt lines above)."
            # #2177: name the one cause the operator cannot see from the build
            # output. An air-gapped box upgrading from a stick fails here every
            # time — the gates above skip build and pull when the mode is
            # offline, and nothing sets the mode, so they never fire. Measured
            # on 0.175 (journey D, #2126): exit 1, rolled back, .env restored,
            # and no indication anywhere that the mode was the reason.
            #
            # Both readings are printed on purpose. Saying only "you are
            # air-gapped" would be a guess, and a wrong guess here sends someone
            # chasing a network problem instead of a real build error.
            if [ -n "${PACKAGE_FILE:-}" ] && ! razzfazz_is_offline; then
                print_error "  This upgrade was given a package (--package) and the box's network mode is '$(razzfazz_network_mode)'."
                print_error "  The build above tried to reach a registry. If this box has no internet, that is why:"
                print_error "    re-run with '--network-mode offline', or set it once with 'rzfz setup --network-mode --mode offline'."
                print_error "  If the box IS online and the package is only a cache, this is a real build error — see the output above."
            fi
            return 1
        fi
        print_substep "Custom images built."
    else
        print_substep "No rebuild required for this upgrade."
    fi

    if [ "${SKIP_PULL:-false}" = true ]; then
        print_substep "Skipping image pull (images loaded from package)."
    elif razzfazz_is_offline; then
        # #184 WS2b: never reach a registry on an air-gapped box.
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping image pull — images come from the offline package."
    elif [ "${REQUIRES_PULL:-true}" = true ] || [ "${FORCE_BUILD:-false}" = true ]; then
        print_substep "Pulling pre-built images..."
        PULL_RAN=true
        # #2035: `--ignore-pull-failures` stays — one unreachable image must not
        # abort the pull of the others — but its exit code is no longer thrown
        # away, and success is no longer announced regardless. Whether a missing
        # image STOPS the upgrade is decided by the pre-restart image check
        # (Step 7b), which now runs on this path too; here we only stop lying.
        if docker compose pull --ignore-pull-failures 2>&1; then
            print_substep "Pre-built images updated."
        else
            PULL_HAD_FAILURES=true
            print_warning "Some images failed to pull — the pre-restart image check decides whether the stack may restart (#2035)."
        fi
    else
        # The manifest range says "no pull" — on a retry at the same version it
        # always does. Ask the box what it actually lacks. Only reached after
        # the confirmation: build_and_pull runs past the --check exit and the
        # prompt, and pulling changes the image store.
        local _missing="" _missing_rc=0
        _missing=$("${SCRIPT_DIR}/rzfz" verify-images --missing --active) || _missing_rc=$?
        if [ "$_missing_rc" -ne 0 ]; then
            # Unknown, not "nothing missing": say so, so the log shows why no
            # pull happened. The pre-restart check still decides.
            print_warning "Could not ask which images this box is missing (exit ${_missing_rc}); pulling nothing extra — the pre-restart image check decides."
        else
            local _ref _to_pull=""
            while IFS= read -r _ref; do
                [ -n "$_ref" ] || continue
                if razzfazz_retag_from_hub "$_ref"; then
                    print_substep "${_ref}: present only under the hub tag — re-tagged, not pulled."
                    continue
                fi
                if printf '%s\n' "$_missing_built" | grep -qxF -- "$_ref"; then
                    print_substep "${_ref}: built locally, rebuilt by the build step — not pulled."
                    continue
                fi
                _to_pull="${_to_pull}${_ref}"$'\n'
            done <<< "$_missing"
            if [ -n "$_to_pull" ]; then
                print_substep "No pull was scheduled, but $(printf '%s' "$_to_pull" | grep -c .) image(s) this box runs are missing — pulling exactly those."
                PULL_RAN=true
                while IFS= read -r _ref; do
                    [ -n "$_ref" ] || continue
                    docker pull "$_ref" 2>&1 || PULL_HAD_FAILURES=true
                done <<< "$_to_pull"
            else
                print_substep "No pull required for this upgrade."
            fi
        fi
    fi

    if [ "${PULL_HAD_FAILURES:-false}" = true ]; then
        print_warning "Docker images updated with pull failures (see above)."
    else
        print_success "Docker images updated."
    fi
}

# ==============================================================================
# Blueprint Sync — DISABLED for upgrades
# ==============================================================================
# Blueprints are declarative and would overwrite any manual changes made
# in the Authentik Admin UI (branding, flows, providers, groups, roles, etc.).
# They are only applied during first-time initialization (init-authentik.sh).
#
# For upgrade-time changes to Authentik objects, use run_data_migrations()
# with targeted Django ORM updates via 'docker exec authentik-worker'.
# ==============================================================================

# ==============================================================================
# Data Migrations (version-gated, runs in authentik-worker)
# ==============================================================================
# Targeted DB changes that must NOT be done via blueprint re-application,
# because blueprints are declarative and would overwrite manual UI changes.
# Each migration checks the installed version and only runs if applicable.
# ==============================================================================
run_data_migrations() {
    print_step "Running data migrations..."

    local migrations_run=0

    # --- Migration: 2026-03.GA — Set access_token_validity on all proxy providers ---
    if version_lt "$INSTALLED_VERSION" "2026-03.GA" 2>/dev/null || \
       version_lt "$INSTALLED_VERSION" "2026.03-GA" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would set access_token_validity=hours=24 on all proxy providers"
            ((migrations_run++)) || true
        else
            print_substep "Setting access_token_validity=hours=24 on proxy providers..."

            # Wait for authentik-worker to be healthy and ready
            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
                if [ $((retries % 6)) -eq 0 ]; then
                    print_substep "  Waiting for authentik-worker... (${worker_health}, $((retries * 5))s)"
                fi
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker did not become healthy. Skipping token validity migration."
                print_info "  Run manually: rzfz upgrade --check (shows pending migrations)"
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-token-validity.py
import os, sys, time, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from django.db import OperationalError, connection
from authentik.providers.proxy.models import ProxyProvider


def _save_with_deadlock_retry(obj, **kwargs):
    # Saving a ProxyProvider fires Authentik's outpost permission rebuild
    # (outpost_related_post_save -> build_user_permissions ->
    # remove_all_perms_from_managed_role -> DELETE on guardian_rolemodelpermission).
    # Running this migration right after a MAJOR-version Authentik restart, that
    # same rebuild is happening concurrently in the worker, so a transient Postgres
    # deadlock on guardian_rolemodelpermission is possible (observed on the
    # 2026.04-ga.1 -> 2026.07 upgrade — it aborted the whole run under set -e).
    # Retry the save a few times on a transient deadlock / serialization failure.
    for attempt in range(6):
        try:
            obj.save(**kwargs)
            return
        except OperationalError as exc:
            msg = str(exc).lower()
            if "deadlock" not in msg and "could not serialize" not in msg:
                raise
            connection.close()  # drop the aborted transaction/connection
            time.sleep(1.5 * (attempt + 1))
    # Last attempt: let it raise if it still deadlocks so the caller sees it.
    obj.save(**kwargs)


updated = 0
for provider in ProxyProvider.objects.all():
    if provider.access_token_validity != "hours=24":
        old_val = provider.access_token_validity
        provider.access_token_validity = "hours=24"
        _save_with_deadlock_retry(provider, update_fields=["access_token_validity"])
        print(f"  Updated {provider.name}: {old_val} -> hours=24")
        updated += 1
    else:
        print(f"  {provider.name}: already hours=24")

print(f"\nDone: {updated} provider(s) updated.")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-token-validity.py authentik-worker:/tmp/razzfazz-migration-token-validity.py 2>/dev/null; then
                    # Non-fatal (`if PIPELINE; then`): this migration saves EVERY proxy
                    # provider and each save fires Authentik's outpost permission
                    # rebuild — which can deadlock on guardian_rolemodelpermission
                    # against the worker's own post-restart reconcile (the Python
                    # above retries; this branch is defence-in-depth). Under
                    # `set -eo pipefail` an un-guarded `python ... | while` pipeline
                    # would abort the ENTIRE upgrade on that deadlock (observed on the
                    # 2026.04-ga.1 -> 2026.07 run — verify + provisioning never ran).
                    # The other Authentik migrations below already tolerate it via
                    # their `python ... && rm` (`&&`-short-circuit) form.
                    if docker exec authentik-worker python /tmp/razzfazz-migration-token-validity.py 2>&1 | while IFS= read -r line; do
                        print_substep "$line"
                    done; then
                        ((migrations_run++)) || true
                    else
                        print_warning "Token-validity migration reported an error (non-fatal) — some proxy providers may keep their prior access-token validity. Re-run later: rzfz upgrade"
                    fi
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-token-validity.py 2>/dev/null || true
                else
                    print_warning "Could not reach authentik-worker for token validity migration."
                fi
                rm -f /tmp/razzfazz-migration-token-validity.py
            fi
        fi
    fi

    # --- Migration: 2026.04-rc2 — Create cognee_db if cognee profile is enabled ---
    if version_lt "$INSTALLED_VERSION" "2026.04-rc2" 2>/dev/null; then
        local cognee_db
        cognee_db=$(read_env_value .env COGNEE_DB)

        if [ -n "$cognee_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "cognee"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$cognee_db' with pgvector + uuid-ossp extensions"
                ((migrations_run++)) || true
            else
                print_substep "Creating Cognee database '$cognee_db'..."
                local pg_user
                pg_user=$(read_env_value .env POSTGRES_USER)
                # CREATE DATABASE is idempotent-safe via IF NOT EXISTS pattern
                docker exec postgres psql -U "${pg_user:-docker}" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$cognee_db'" 2>/dev/null | grep -q "exists" || {
                    docker exec postgres psql -U "${pg_user:-docker}" -d postgres \
                        -c "CREATE DATABASE \"$cognee_db\";" 2>/dev/null
                    docker exec postgres psql -U "${pg_user:-docker}" -d "$cognee_db" \
                        -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null
                    print_substep "Database '$cognee_db' created with extensions."
                }
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Add future migrations here, gated by version_lt checks ---

    # --- Migration: 2026.04-ga.2 — Restructure Authentik app groups ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.2" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would restructure Authentik app groups (1 Use / 3 Development & APIs / 4 razzfazz.ai Admin & Tools)"
            ((migrations_run++)) || true
        else
            print_substep "Restructuring Authentik application groups..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping app group migration."
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-app-groups.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application

GROUP_MAP = {
    # 1 Use
    "chat":                "1 Use",
    "workflow-automation": "1 Use",
    "lightrag":            "1 Use",
    "llm-management":      "1 Use",
    "stirling-pdf":        "1 Use",
    # 3 Development & APIs
    "gitea":               "3 Development & APIs",
    "docling":             "3 Development & APIs",
    "cognee":              "3 Development & APIs",
    # 4 razzfazz.ai Admin & Tools
    "administration":      "4 razzfazz.ai Admin & Tools",
    "backup":              "4 razzfazz.ai Admin & Tools",
    "help":                "4 razzfazz.ai Admin & Tools",
    "setup":               "4 razzfazz.ai Admin & Tools",
    "licenses":            "4 razzfazz.ai Admin & Tools",
}

updated = 0
for slug, new_group in GROUP_MAP.items():
    try:
        app = Application.objects.get(slug=slug)
        if app.meta_launch_url != new_group:  # reuse field name workaround
            pass
        if getattr(app, 'group', None) != new_group:
            old = getattr(app, 'group', '(none)')
            app.group = new_group
            app.save(update_fields=["group"])
            print(f"  {slug}: '{old}' -> '{new_group}'")
            updated += 1
        else:
            print(f"  {slug}: already '{new_group}'")
    except Application.DoesNotExist:
        print(f"  {slug}: not found, skipping")

print(f"\nDone: {updated} app(s) updated.")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-app-groups.py authentik-worker:/tmp/razzfazz-migration-app-groups.py 2>/dev/null; then
                    docker exec authentik-worker \
                        python /tmp/razzfazz-migration-app-groups.py 2>/dev/null && \
                        print_success "App group migration applied." || \
                        print_warning "App group migration failed — run manually if needed."
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-app-groups.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-app-groups.py
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.3 — Create paperclip_db and synapse_db for existing installs ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.3" 2>/dev/null; then

        local pg_user
        pg_user=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass
        pg_pass=$(read_env_value .env POSTGRES_PASSWORD)

        # paperclip_db — standard UTF-8
        local paperclip_db
        paperclip_db=$(read_env_value .env PAPERCLIP_DB)
        if [ -n "$paperclip_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "paperclip"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create paperclip_db if missing"
            else
                PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$paperclip_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                        -c "CREATE DATABASE \"$paperclip_db\";" 2>/dev/null
                    print_substep "Database '$paperclip_db' created."
                }
            fi
            ((migrations_run++)) || true
        fi

        # synapse_db — MUST use C locale (non-negotiable for Matrix)
        local synapse_db
        synapse_db=$(read_env_value .env SYNAPSE_DB)
        if [ -n "$synapse_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "matrix"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create synapse_db with C locale if missing"
            else
                PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$synapse_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass" docker exec postgres psql -U "$pg_user" -d postgres \
                        -c "CREATE DATABASE \"$synapse_db\" ENCODING 'UTF8' LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0;" 2>/dev/null
                    print_substep "Database '$synapse_db' created (C locale for Matrix)."
                }
            fi
            ((migrations_run++)) || true
        fi
    fi

    # --- Migration: 2026.04-ga.4 — Add agentic AI apps to Authentik (hermes, moltis, paperclip, element-web) ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.4" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would create Hermes/Moltis/Paperclip/Element Web Authentik providers and apps"
            ((migrations_run++)) || true
        else
            print_substep "Adding agentic AI apps to Authentik portal..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping agentic AI app migration."
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-agentic-apps.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application

GROUP = "2 Agentic AI"
SLUGS = ["hermes-agent", "moltis", "paperclip", "element-web"]

updated = 0
for slug in SLUGS:
    try:
        app = Application.objects.get(slug=slug)
        if getattr(app, 'group', None) != GROUP:
            app.group = GROUP
            app.save(update_fields=["group"])
            print(f"  {slug}: group set to '{GROUP}'")
            updated += 1
        else:
            print(f"  {slug}: already in '{GROUP}'")
    except Application.DoesNotExist:
        print(f"  {slug}: not found, skipping (will be created by blueprint on fresh install)")

print(f"\nDone: {updated} app(s) updated.")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-agentic-apps.py authentik-worker:/tmp/razzfazz-migration-agentic-apps.py 2>/dev/null; then
                    docker exec authentik-worker python /tmp/razzfazz-migration-agentic-apps.py 2>/dev/null && \
                        print_success "Agentic AI app group migration applied." || \
                        print_warning "Agentic AI app group migration failed — run manually if needed."
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-agentic-apps.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-agentic-apps.py
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.5 — Persona-driven app group taxonomy rename (M007) ---
    # Renames all existing Authentik app groups to the new 5-group persona structure:
    #   "1 Use"/"1 Productivity"    → "1 Workspace" (v5, #1157)
    #   "2 Agentic AI"              → "2 AI Assistants"
    #   "3 Development & APIs"      → "3 Development"
    #   "4 razzfazz.ai Admin & Tools" → "5 Administration"
    # Creates "4 Knowledge Engines" for LightRAG and Cognee if not already set.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.5" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would rename Authentik app groups to persona-driven 5-group taxonomy"
            ((migrations_run++)) || true
        else
            print_substep "Renaming Authentik app groups to persona-driven taxonomy..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping app group taxonomy migration."
            else
                cat <<'MIGRATION_EOF' > /tmp/razzfazz-migration-group-taxonomy.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application

# Map old group names → new group names
GROUP_RENAME = {
    # pre-2026.08 names AND the 2026.08 names both map straight to v5 (#1157)
    "1 Use":                       "1 Workspace",
    "1 Productivity":              "1 Workspace",
    "2 Agentic AI":                "6 Automation & Agents",
    "2 AI Assistants":             "6 Automation & Agents",
    "3 Development & APIs":        "5 Development",
    "3 Development":               "5 Development",
    "4 razzfazz.ai Admin & Tools": "8 Administration",
    "5 Administration":            "8 Administration",
    "4 Knowledge Engines":         "3 Knowledge & Search",
    "1 LLM Inference":             "7 LLM Infrastructure",
}

# Explicit overrides for apps that move between groups in the v5 taxonomy
# (only applied if the app exists; skipped silently if not found)
SLUG_OVERRIDES = {
    # -> 1 Workspace
    "chat":             "1 Workspace",
    "element-web":      "1 Workspace",
    "paperclip":        "1 Workspace",
    "hermes-agent":     "1 Workspace",
    "moltis":           "1 Workspace",
    # -> 2 Documents
    "stirling-pdf":     "2 Documents",
    "docling":          "2 Documents",
    "paperless-ngx":    "2 Documents",
    # -> 3 Knowledge & Search
    "cognee":           "3 Knowledge & Search",
    "lightrag":         "3 Knowledge & Search",
    "onyx":             "3 Knowledge & Search",
    "ai-search":        "3 Knowledge & Search",
    "crawl4ai":         "3 Knowledge & Search",
    "searxng":          "3 Knowledge & Search",
    # -> 4 Security & Secrets
    "vaultwarden":      "4 Security & Secrets",
    "infisical":        "4 Security & Secrets",
    # -> 5 Development
    "gitea":            "5 Development",
    "coding-tools":     "5 Development",
    # -> 6 Automation & Agents
    "workflow-automation": "6 Automation & Agents",
    "my-agents":        "6 Automation & Agents",
    "agents":           "6 Automation & Agents",
    "mcp":              "6 Automation & Agents",
    # -> 7 LLM Infrastructure
    "llm-manager":      "7 LLM Infrastructure",
    "fleet-hub":        "7 LLM Infrastructure",
    "llm-management":   "7 LLM Infrastructure",
    "observability":    "7 LLM Infrastructure",
    # -> 8 Administration
    "administration":   "8 Administration",
    "backup":           "8 Administration",
    "help":             "8 Administration",
    "setup":            "8 Administration",
    "licenses":         "8 Administration",
    "config":           "8 Administration",
    "openuem":          "8 Administration",
}

# #1157: GPUStack drops its old marketing name in the same pass.
try:
    _app = Application.objects.get(slug="llm-management")
    if _app.name != "GPUStack":
        _app.name = "GPUStack"
        _app.save(update_fields=["name"])
        print("  llm-management: renamed to 'GPUStack'")
except Application.DoesNotExist:
    pass


updated = 0

# Step 1: apply slug-level overrides (highest priority)
for slug, new_group in SLUG_OVERRIDES.items():
    try:
        app = Application.objects.get(slug=slug)
        old = getattr(app, 'group', '(none)')
        if old != new_group:
            app.group = new_group
            app.save(update_fields=["group"])
            print(f"  {slug}: '{old}' -> '{new_group}'")
            updated += 1
        else:
            print(f"  {slug}: already '{new_group}'")
    except Application.DoesNotExist:
        print(f"  {slug}: not installed, skipping")

# Step 2: rename any remaining apps still using old group names (catch-all)
for app in Application.objects.all():
    old = getattr(app, 'group', '') or ''
    new = GROUP_RENAME.get(old)
    if new and app.slug not in SLUG_OVERRIDES:
        app.group = new
        app.save(update_fields=["group"])
        print(f"  {app.slug}: '{old}' -> '{new}' (catch-all rename)")
        updated += 1

print(f"\nDone: {updated} app(s) updated.")

# Step 3: app name renames
NAME_RENAMES = {"onyx": "Search", "hermes-agent": "Hermes"}
for slug, new_name in NAME_RENAMES.items():
    try:
        app = Application.objects.get(slug=slug)
        if app.name != new_name:
            print(f"  {slug}: name '{app.name}' -> '{new_name}'")
            app.name = new_name
            app.save(update_fields=["name"])
    except Application.DoesNotExist:
        pass

# Step 4: remove synapse portal entry (OIDC provider stays)
try:
    Application.objects.get(slug="synapse").delete()
    print("  synapse: portal entry deleted (OIDC provider preserved)")
except Application.DoesNotExist:
    print("  synapse: already removed")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-group-taxonomy.py authentik-worker:/tmp/razzfazz-migration-group-taxonomy.py 2>/dev/null; then
                    docker exec authentik-worker \
                        python /tmp/razzfazz-migration-group-taxonomy.py 2>/dev/null && \
                        print_success "App group taxonomy migration applied." || \
                        print_warning "App group taxonomy migration failed — run manually if needed."
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-group-taxonomy.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-group-taxonomy.py
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.5 — Create M007 databases for existing installs ---
    # paperless_db, vaultwarden_db, infisical_db, onyx_db were added in ga.5 (M007).
    # Installations upgrading from before ga.5 need these databases created.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.5" 2>/dev/null; then

        local pg_user_m007
        pg_user_m007=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_m007
        pg_pass_m007=$(read_env_value .env POSTGRES_PASSWORD)

        # paperless_db — standard UTF-8
        local paperless_db
        paperless_db=$(read_env_value .env PAPERLESS_DB)
        if [ -n "$paperless_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "paperless-ngx"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$paperless_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$paperless_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$paperless_db\";" 2>/dev/null
                    print_substep "Database '$paperless_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi

        # vaultwarden_db — standard UTF-8
        local vaultwarden_db
        vaultwarden_db=$(read_env_value .env VAULTWARDEN_DB)
        if [ -n "$vaultwarden_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "vaultwarden"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$vaultwarden_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$vaultwarden_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$vaultwarden_db\";" 2>/dev/null
                    print_substep "Database '$vaultwarden_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi

        # infisical_db — standard UTF-8
        local infisical_db
        infisical_db=$(read_env_value .env INFISICAL_DB)
        if [ -n "$infisical_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "infisical"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$infisical_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$infisical_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$infisical_db\";" 2>/dev/null
                    print_substep "Database '$infisical_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi

        # onyx_db — standard UTF-8
        local onyx_db
        onyx_db=$(read_env_value .env ONYX_DB)
        if [ -n "$onyx_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "onyx"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$onyx_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$onyx_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m007" docker exec postgres psql -U "$pg_user_m007" -d postgres \
                        -c "CREATE DATABASE \"$onyx_db\";" 2>/dev/null
                    print_substep "Database '$onyx_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.6 — Create agent_manager_db for M011 Agent Manager ---
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then

        local pg_user_m011
        pg_user_m011=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_m011
        pg_pass_m011=$(read_env_value .env POSTGRES_PASSWORD)

        local agent_manager_db
        agent_manager_db=$(read_env_value .env AGENT_MANAGER_DB)
        if [ -n "$agent_manager_db" ] && echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "agents"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create database '$agent_manager_db' if missing"
                ((migrations_run++)) || true
            else
                PGPASSWORD="$pg_pass_m011" docker exec postgres psql -U "$pg_user_m011" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$agent_manager_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_m011" docker exec postgres psql -U "$pg_user_m011" -d postgres \
                        -c "CREATE DATABASE \"$agent_manager_db\";" 2>/dev/null
                    print_substep "Database '$agent_manager_db' created."
                }
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.04-ga.6 — Create per-service PostgreSQL users (M012 F-009) ---
    # M012 introduced per-service DB users. On fresh installs, init-db.sh creates them.
    # During upgrade, existing installations only have the shared postgres superuser.
    # This migration creates per-service users and grants them ownership of their databases.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then

        local pg_user_perservice
        pg_user_perservice=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_perservice
        pg_pass_perservice=$(read_env_value .env POSTGRES_PASSWORD)

        # Map: env_var_db env_var_password service_user profile_name
        # Only services that have per-service DB passwords in .env are included.
        local -a db_user_migrations=(
            "PAPERCLIP_DB:PAPERCLIP_DB_PASSWORD:paperclip_user:paperclip"
            "SYNAPSE_DB:SYNAPSE_DB_PASSWORD:synapse_user:matrix"
        )

        for entry in "${db_user_migrations[@]}"; do
            local db_var password_var svc_user profile_name
            db_var=$(echo "$entry" | cut -d: -f1)
            password_var=$(echo "$entry" | cut -d: -f2)
            svc_user=$(echo "$entry" | cut -d: -f3)
            profile_name=$(echo "$entry" | cut -d: -f4)

            # Use read_env_value (pipefail-safe) — raw `grep | cut` returns 1
            # when the key is absent from .env and, as a bare `var=$(...)`
            # assignment, propagates that non-zero under `set -eo pipefail`,
            # killing the whole upgrade (incl. `--check`) before the dry-run
            # `exit 0`. Absent keys are the common case here: a pre-2026.04-ga.6
            # box that never enabled the paperclip/matrix profiles has no
            # PAPERCLIP_DB / SYNAPSE_DB line at all. Same trap the module-
            # provider block below already guards against (see its comment).
            local db_name svc_password
            db_name=$(read_env_value .env "$db_var")
            svc_password=$(read_env_value .env "$password_var")

            if [ -n "$db_name" ] && [ -n "$svc_password" ] && \
               echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "$profile_name"; then
                if [ "$DRY_RUN" = "true" ]; then
                    print_info "  Would create PostgreSQL user '$svc_user' for database '$db_name'"
                    ((migrations_run++)) || true
                else
                    # Create user if not exists, grant privileges on database
                    # #2746: SCRAM verifier on stdin, never the plaintext in argv or DDL
                    local svc_verifier
                    svc_verifier=$(RZFZ_ROLE_PW="$svc_password" scram_sha256_verifier RZFZ_ROLE_PW) || svc_verifier=""
                    [ -n "$svc_verifier" ] && printf '%s\n' "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$svc_user') THEN CREATE ROLE \"$svc_user\" WITH LOGIN PASSWORD '$svc_verifier'; END IF; END \$\$; GRANT ALL PRIVILEGES ON DATABASE \"$db_name\" TO \"$svc_user\";" \
                        | PGPASSWORD="$pg_pass_perservice" docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pg_user_perservice" -d postgres -f - >/dev/null 2>&1 && \
                        print_substep "PostgreSQL user '$svc_user' created/verified for '$db_name'." || \
                        print_warning "Failed to create PostgreSQL user '$svc_user' for '$db_name'."
                    ((migrations_run++)) || true
                fi
            fi
        done
    fi

    # --- Migration: 2026.04-ga.6 — Authentik 2025.10→2026.2 session invalidation ---
    # Old JWT session cookies crash Authentik 2026.2 due to removed User_user_permissions model.
    # Warn the operator to clear browser cookies after upgrade.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then
        print_warning "IMPORTANT: After this upgrade, all users must clear their browser cookies"
        print_warning "for auth.${MAIN_DOMAIN} (or clear all site data). Old Authentik sessions"
        print_warning "are incompatible with the new version and will cause 'Server Error'."
        ((migrations_run++)) || true
    fi

    # --- Migration: 2026.04-ga.6 — Create Authentik providers + apps for new modules ---
    # On fresh installs, blueprints 12-24 create these automatically.
    # Existing installs upgrading from pre-ga.6 need them created via Django ORM.
    if version_lt "$INSTALLED_VERSION" "2026.04-ga.6" 2>/dev/null; then

        if [ "$DRY_RUN" = "true" ]; then
            print_info "  Would create Authentik providers and apps for: docling, stirling-pdf, hermes-agent,"
            print_info "    moltis, paperclip, element-web, paperless-ngx, infisical, onyx,"
            print_info "    coding-tools (ProxyProvider), synapse, vaultwarden (OAuth2Provider)"
            ((migrations_run++)) || true
        else
            print_substep "Creating Authentik providers and apps for new modules..."

            local retries=0
            local worker_ready=false
            while [ $retries -lt 60 ]; do
                local worker_health
                worker_health=$(docker inspect --format='{{.State.Health.Status}}' authentik-worker 2>/dev/null || echo "missing")
                if [ "$worker_health" = "healthy" ]; then
                    worker_ready=true
                    break
                fi
                sleep 5
                retries=$((retries + 1))
                if [ $((retries % 6)) -eq 0 ]; then
                    print_substep "  Waiting for authentik-worker... (${worker_health}, $((retries * 5))s)"
                fi
            done

            if [ "$worker_ready" != true ]; then
                print_warning "authentik-worker not healthy. Skipping new module Authentik migration."
            else
                # Read secrets and domains from .env for the Python script
                local main_domain hermes_domain moltis_domain paperclip_domain
                local matrix_domain element_web_domain paperless_domain vaultwarden_domain
                local infisical_domain onyx_domain coding_tools_domain
                local docling_client_secret stirling_client_secret hermes_client_secret
                local moltis_client_secret paperclip_client_secret element_web_client_secret
                local paperless_client_secret infisical_client_secret onyx_client_secret
                local coding_tools_client_secret
                local synapse_client_secret matrix_client_id vaultwarden_client_secret

                # Use read_env_value (pipefail-safe) — raw `grep | cut`
                # kills the script via set -eo pipefail when a key is
                # absent from .env, which is the common upgrade-from-older
                # case (e.g. DOCLING_CLIENT_SECRET / STIRLING_CLIENT_SECRET
                # don't exist in a pre-ga.5 stack).
                main_domain=$(read_env_value .env MAIN_DOMAIN)
                hermes_domain=$(read_env_value .env HERMES_DOMAIN)
                moltis_domain=$(read_env_value .env MOLTIS_DOMAIN)
                paperclip_domain=$(read_env_value .env PAPERCLIP_DOMAIN)
                matrix_domain=$(read_env_value .env MATRIX_DOMAIN)
                element_web_domain=$(read_env_value .env ELEMENT_WEB_DOMAIN)
                paperless_domain=$(read_env_value .env PAPERLESS_DOMAIN)
                vaultwarden_domain=$(read_env_value .env VAULTWARDEN_DOMAIN)
                infisical_domain=$(read_env_value .env INFISICAL_DOMAIN)
                onyx_domain=$(read_env_value .env ONYX_DOMAIN)
                coding_tools_domain=$(read_env_value .env CODING_TOOLS_DOMAIN)

                docling_client_secret=$(read_env_value .env DOCLING_CLIENT_SECRET)
                stirling_client_secret=$(read_env_value .env STIRLING_CLIENT_SECRET)
                hermes_client_secret=$(read_env_value .env HERMES_CLIENT_SECRET)
                moltis_client_secret=$(read_env_value .env MOLTIS_CLIENT_SECRET)
                paperclip_client_secret=$(read_env_value .env PAPERCLIP_CLIENT_SECRET)
                element_web_client_secret=$(read_env_value .env ELEMENT_WEB_CLIENT_SECRET)
                paperless_client_secret=$(read_env_value .env PAPERLESS_CLIENT_SECRET)
                infisical_client_secret=$(read_env_value .env INFISICAL_CLIENT_SECRET)
                onyx_client_secret=$(read_env_value .env ONYX_CLIENT_SECRET)
                coding_tools_client_secret=$(read_env_value .env CODING_TOOLS_CLIENT_SECRET)
                synapse_client_secret=$(read_env_value .env SYNAPSE_CLIENT_SECRET)
                matrix_client_id=$(read_env_value .env MATRIX_CLIENT_ID)
                vaultwarden_client_secret=$(read_env_value .env VAULTWARDEN_CLIENT_SECRET)

                # Resolve domain defaults (matching .env.example patterns)
                : "${hermes_domain:=hermes.${main_domain}}"
                : "${moltis_domain:=moltis.${main_domain}}"
                : "${paperclip_domain:=paperclip.${main_domain}}"
                : "${matrix_domain:=matrix.${main_domain}}"
                : "${element_web_domain:=element.${main_domain}}"
                : "${paperless_domain:=paperless.${main_domain}}"
                : "${vaultwarden_domain:=vault.${main_domain}}"
                : "${infisical_domain:=infisical.${main_domain}}"
                : "${onyx_domain:=onyx.${main_domain}}"
                : "${coding_tools_domain:=coding.${main_domain}}"

                cat <<MIGRATION_EOF > /tmp/razzfazz-migration-new-modules.py
import os, sys, django
sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application, Group
from authentik.providers.proxy.models import ProxyProvider
from authentik.providers.oauth2.models import OAuth2Provider
from authentik.outposts.models import Outpost
from authentik.flows.models import Flow
from authentik.policies.models import PolicyBinding
from authentik.crypto.models import CertificateKeyPair
from authentik.providers.oauth2.models import ScopeMapping

# Lookup shared references
auth_flow_explicit = Flow.objects.get(slug="default-provider-authorization-explicit-consent")
auth_flow_implicit = Flow.objects.get(slug="default-provider-authorization-implicit-consent")
invalidation_flow = Flow.objects.get(slug="default-provider-invalidation-flow")

# Scope mappings for proxy providers
proxy_mappings = list(ScopeMapping.objects.filter(name__in=[
    "authentik default OAuth Mapping: OpenID 'email'",
    "authentik default OAuth Mapping: OpenID 'profile'",
    "authentik default OAuth Mapping: Proxy outpost",
]))

# Scope mappings for OIDC providers
oidc_mappings = list(ScopeMapping.objects.filter(name__in=[
    "authentik default OAuth Mapping: OpenID 'email'",
    "authentik default OAuth Mapping: OpenID 'profile'",
    "authentik default OAuth Mapping: OpenID 'openid'",
]))

# Signing key for OIDC providers
signing_key = CertificateKeyPair.objects.filter(name="authentik Self-signed Certificate").first()

created = 0
skipped = 0

def ensure_proxy_provider(name, client_id, client_secret, external_host):
    """Create a forward-auth proxy provider if it does not exist."""
    global created, skipped
    if not client_secret:
        print(f"  SKIP {name}: no client_secret in .env")
        skipped += 1
        return None
    provider, was_created = ProxyProvider.objects.get_or_create(
        name=name,
        defaults={
            "client_id": client_id,
            "client_secret": client_secret,
            "authorization_flow": auth_flow_explicit,
            "invalidation_flow": invalidation_flow,
            "external_host": external_host,
            "mode": "forward_single",
            "intercept_header_auth": True,
            "access_token_validity": "hours=24",
        },
    )
    if was_created:
        provider.property_mappings.set(proxy_mappings)
        print(f"  CREATED provider: {name}")
        created += 1
    else:
        print(f"  EXISTS  provider: {name}")
    return provider

def ensure_oauth2_provider(name, client_id, client_secret, redirect_uris, consent="explicit"):
    """Create an OAuth2/OIDC provider if it does not exist."""
    global created, skipped
    if not client_secret or not client_id:
        print(f"  SKIP {name}: missing client_id or client_secret in .env")
        skipped += 1
        return None
    flow = auth_flow_explicit if consent == "explicit" else auth_flow_implicit
    defaults = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_type": "confidential",
        "authorization_flow": flow,
        "invalidation_flow": invalidation_flow,
        "redirect_uris": redirect_uris,
        "access_code_validity": "minutes=1",
        "access_token_validity": "hours=1",
        "refresh_token_validity": "days=30",
        "include_claims_in_id_token": True,
    }
    if signing_key:
        defaults["signing_key"] = signing_key
    provider, was_created = OAuth2Provider.objects.get_or_create(
        name=name,
        defaults=defaults,
    )
    if was_created:
        provider.property_mappings.set(oidc_mappings)
        print(f"  CREATED provider: {name}")
        created += 1
    else:
        print(f"  EXISTS  provider: {name}")
    return provider

def ensure_app(slug, name, group, provider, description="", publisher="", icon="", launch_url=""):
    """Create an Authentik application if it does not exist."""
    global created
    app, was_created = Application.objects.get_or_create(
        slug=slug,
        defaults={
            "name": name,
            "group": group,
            "provider": provider,
            "meta_description": description,
            "meta_publisher": publisher,
            "meta_launch_url": launch_url,
            "open_in_new_tab": True,
            "policy_engine_mode": "any",
        },
    )
    if was_created:
        if icon:
            app.icon = icon
            app.save(update_fields=["icon"])
        print(f"  CREATED app: {slug} ({name})")
        created += 1
    else:
        print(f"  EXISTS  app: {slug}")
    return app

def ensure_group(name):
    """Create an Authentik group if it does not exist."""
    global created
    group, was_created = Group.objects.get_or_create(name=name)
    if was_created:
        print(f"  CREATED group: {name}")
        created += 1
    else:
        print(f"  EXISTS  group: {name}")
    return group

def ensure_binding(app_slug, group_name, order=0):
    """Create a policy binding for app -> group if it does not exist."""
    try:
        app = Application.objects.get(slug=app_slug)
        group = Group.objects.get(name=group_name)
        existing = PolicyBinding.objects.filter(target_id=app.pk, group=group).first()
        if not existing:
            PolicyBinding.objects.create(target_id=app.pk, group=group, enabled=True, order=order)
            print(f"  CREATED binding: {app_slug} -> {group_name}")
        else:
            print(f"  EXISTS  binding: {app_slug} -> {group_name}")
    except (Application.DoesNotExist, Group.DoesNotExist) as e:
        print(f"  SKIP binding {app_slug} -> {group_name}: {e}")

print("=== Creating ProxyProvider + Application pairs ===")

# 1. Docling
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Docling",
    "docling_app_client_id",
    "${docling_client_secret}",
    "https://docling.${main_domain}",
)
if p:
    ensure_app("docling", "Docling Converter", "3 Development", p,
        description="Document conversion and understanding API with Gradio UI",
        publisher="IBM / Docling Project",
        icon="/media/razzfazz-ai_docling_icon.png")

# 2. Stirling-PDF
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Stirling-PDF",
    "stirling_pdf_app_client_id",
    "${stirling_client_secret}",
    "https://pdf.${main_domain}",
)
if p:
    ensure_app("stirling-pdf", "PDF Tools", "1 Productivity", p,
        description="PDF manipulation and processing suite",
        publisher="Stirling-PDF",
        icon="/media/razzfazz-ai_pdf_icon.png")

# 3. Hermes Agent
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Hermes Agent",
    "hermes_agent_app_client_id",
    "${hermes_client_secret}",
    "https://${hermes_domain}",
)
if p:
    ensure_app("hermes-agent", "Hermes", "2 AI Assistants", p,
        description="Self-improving AI agent with persistent memory, 30+ tools, and MCP client",
        publisher="Nous Research",
        icon="/media/razzfazz-ai_hermes_icon.png")

# 4. Moltis
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Moltis",
    "moltis_app_client_id",
    "${moltis_client_secret}",
    "https://${moltis_domain}",
)
if p:
    ensure_app("moltis", "Moltis", "2 AI Assistants", p,
        description="Secure persistent personal agent server",
        publisher="moltis-org",
        icon="/media/razzfazz-ai_moltis_icon.png")

# 5. Paperclip
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Paperclip",
    "paperclip_app_client_id",
    "${paperclip_client_secret}",
    "https://${paperclip_domain}",
)
if p:
    ensure_app("paperclip", "Paperclip", "2 AI Assistants", p,
        description="Open-source AI company orchestration",
        publisher="paperclipai",
        icon="/media/razzfazz-ai_paperclip_icon.png")

# 6. Element Web (Matrix browser client — forward auth on matrix domain)
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Element Web",
    "element_web_app_client_id",
    "${element_web_client_secret}",
    "https://${matrix_domain}",
)
if p:
    ensure_app("element-web", "Matrix", "1 Productivity", p,
        description="Matrix browser client — encrypted messaging, rooms, and direct messages",
        publisher="Element HQ",
        icon="/media/razzfazz-ai_matrix_icon.png")

# 7. Paperless-ngx (also creates its group)
ensure_group("razzfazz.ai Paperless Users")
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Paperless-ngx",
    "paperless_app_client_id",
    "${paperless_client_secret}",
    "https://${paperless_domain}",
)
if p:
    ensure_app("paperless-ngx", "Documents", "1 Productivity", p,
        description="Scan, archive, and search your physical documents with OCR",
        publisher="paperless-ngx Project",
        icon="/media/razzfazz-ai_documents_icon.png")

# 8. Infisical
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Infisical",
    "infisical_app_client_id",
    "${infisical_client_secret}",
    "https://${infisical_domain}",
)
if p:
    ensure_app("infisical", "Secrets Manager", "5 Administration", p,
        description="Self-hosted secrets management for developers and AI coding agents",
        publisher="Infisical, Inc.",
        icon="/media/razzfazz-ai_secrets_icon.png")

# 9. Onyx (AI Search)
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Onyx",
    "onyx_app_client_id",
    "${onyx_client_secret}",
    "https://${onyx_domain}",
)
if p:
    ensure_app("onyx", "Search", "1 Productivity", p,
        description="AI-powered enterprise search across Gitea repos and documents",
        publisher="Onyx",
        icon="/media/razzfazz-ai_search_icon.png")

# 11. Coding Tools
p = ensure_proxy_provider(
    "Caddy Forward Auth Provider for Coding Tools",
    "coding_tools_app_client_id",
    "${coding_tools_client_secret}",
    "https://${coding_tools_domain}",
)
if p:
    ensure_app("coding-tools", "Coding Tools", "3 Development", p,
        description="AI coding agents — gsd-pi and opencode, pre-configured with Gitea",
        publisher="razzfazz.ai",
        icon="/media/razzfazz-ai_coding_icon.png")

print("\n=== Creating OAuth2/OIDC Provider + Application pairs ===")

# 12. Synapse (Matrix OIDC — implicit consent, no portal tile)
p = ensure_oauth2_provider(
    "Synapse Matrix OIDC Provider",
    "${matrix_client_id}",
    "${synapse_client_secret}",
    "https://${matrix_domain}/_synapse/client/oidc/callback",
    consent="implicit",
)
if p:
    ensure_app("synapse", "Matrix Synapse (OIDC)", "", p,
        description="Matrix homeserver OIDC authentication provider (internal)")

# 13. Vaultwarden (OIDC — explicit consent, portal tile)
p = ensure_oauth2_provider(
    "Vaultwarden OIDC Provider",
    "vaultwarden_oidc_client",
    "${vaultwarden_client_secret}",
    "https://${vaultwarden_domain}/identity/connect/oidc-signin",
    consent="explicit",
)
if p:
    ensure_app("vaultwarden", "Password Manager", "1 Productivity", p,
        description="Self-hosted Bitwarden-compatible password manager",
        publisher="Vaultwarden / dani-garcia",
        icon="/media/razzfazz-ai_vault_icon.png")

print("\n=== Adding all new proxy providers to embedded outpost ===")
outpost = Outpost.objects.filter(name="authentik Embedded Outpost").first()
if outpost:
    for provider in ProxyProvider.objects.all():
        if provider not in outpost.providers.all():
            outpost.providers.add(provider)
            print(f"  Added to outpost: {provider.name}")
        else:
            print(f"  Already in outpost: {provider.name}")
    outpost.save()
else:
    print("  WARNING: Embedded outpost not found — proxy providers will not work!")

print("\n=== Creating policy bindings for new apps ===")

# Docling — Super Admins only
ensure_binding("docling", "razzfazz.ai Super Admins", 0)

# Stirling-PDF — Super Admins only
ensure_binding("stirling-pdf", "razzfazz.ai Super Admins", 0)

# Paperclip — Super Admins only
ensure_binding("paperclip", "razzfazz.ai Super Admins", 0)

# Moltis — Super Admins only
ensure_binding("moltis", "razzfazz.ai Super Admins", 0)

# Hermes Agent — Super Admins only
ensure_binding("hermes-agent", "razzfazz.ai Super Admins", 0)

# Element Web — Super Admins only
ensure_binding("element-web", "razzfazz.ai Super Admins", 0)

# Paperless-ngx — Paperless Users + Super Admins
ensure_binding("paperless-ngx", "razzfazz.ai Paperless Users", 0)
ensure_binding("paperless-ngx", "razzfazz.ai Super Admins", 1)

# Vaultwarden — Super Admins only
ensure_binding("vaultwarden", "razzfazz.ai Super Admins", 0)

# Infisical — Super Admins only
ensure_binding("infisical", "razzfazz.ai Super Admins", 0)

# Onyx — Super Admins only
ensure_binding("onyx", "razzfazz.ai Super Admins", 0)

# Coding Tools — Super Admins only
ensure_binding("coding-tools", "razzfazz.ai Super Admins", 0)

print(f"\nDone: {created} object(s) created, {skipped} skipped (missing secrets).")
MIGRATION_EOF

                if docker cp /tmp/razzfazz-migration-new-modules.py authentik-worker:/tmp/razzfazz-migration-new-modules.py 2>/dev/null; then
                    # F-silent-halt: swallow pipefail here so the explicit
                    # exit_code check below handles failure — without this,
                    # an error inside the Python migration kills the whole
                    # upgrade script via set -eo pipefail and skips the rest.
                    # The child's status, read INSIDE the group: after
                    # `{ … } || true` PIPESTATUS describes `true` (always 0).
                    local exit_code=0
                    { docker exec authentik-worker \
                        python /tmp/razzfazz-migration-new-modules.py 2>&1 | while IFS= read -r line; do
                        print_substep "$line"
                    done; exit_code=${PIPESTATUS[0]}; } || true
                    docker exec authentik-worker rm -f /tmp/razzfazz-migration-new-modules.py 2>/dev/null || true
                    rm -f /tmp/razzfazz-migration-new-modules.py
                    if [ "${exit_code:-0}" -eq 0 ]; then
                        print_success "New module Authentik migration applied."
                    else
                        print_warning "New module Authentik migration had errors — check output above."
                    fi
                else
                    print_warning "Could not reach authentik-worker for new module migration."
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: 2026.05-rc5 — Create agent_manager_db for existing installs ---
    # On fresh installs, init-db.sh creates this. Pre-rc5 installs that enable
    # the `agents` profile need it created here, otherwise agent-manager
    # crash-loops with `database "agent_manager_db" does not exist`.
    if version_lt "$INSTALLED_VERSION" "2026.05-rc5" 2>/dev/null; then

        local pg_user_am
        pg_user_am=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2 || echo "docker")
        local pg_pass_am
        pg_pass_am=$(read_env_value .env POSTGRES_PASSWORD)

        local agent_manager_db
        agent_manager_db=$(read_env_value .env AGENT_MANAGER_DB)
        agent_manager_db="${agent_manager_db:-agent_manager_db}"

        if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "agents"; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would create agent_manager_db if missing"
            else
                PGPASSWORD="$pg_pass_am" docker exec postgres psql -U "$pg_user_am" -d postgres \
                    -c "SELECT 'exists' FROM pg_database WHERE datname='$agent_manager_db'" 2>/dev/null | grep -q "exists" || {
                    PGPASSWORD="$pg_pass_am" docker exec postgres psql -U "$pg_user_am" -d postgres \
                        -c "CREATE DATABASE \"$agent_manager_db\";" 2>/dev/null
                    print_substep "Database '$agent_manager_db' created (M020 agent-manager)."
                }
            fi
            ((migrations_run++)) || true
        fi
    fi

    # --- Migration: Dify 1.15 — one-shot plugin-auto-upgrade backfill (#26) ---
    # Dify 1.15.0 makes plugin auto-upgrade PER-CATEGORY. Upstream requires a
    # one-shot `flask backfill-plugin-auto-upgrade` AFTER `flask db upgrade`
    # (dify-api runs the latter automatically on start via MIGRATION_ENABLED);
    # skipping it silently breaks previously-configured plugin auto-upgrade
    # settings on existing installs. run_data_migrations() runs AFTER
    # restart_stack, so dify-api is already up applying the 24 new 1.15 alembic
    # revisions. We trigger the backfill exactly on the crossing into 1.15 —
    # the new DIFY_VERSION is >= 1.15.0 while the pre-upgrade one was < 1.15.0
    # (tag-name independent; keys off the Dify version transition, not
    # INSTALLED_VERSION) — and only when the dify profile is active. dify-api
    # has no compose healthcheck and the migrations take time, so we retry the
    # backfill (idempotent-ish) until the migrated schema is ready.
    if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "dify"; then
        local dify_new dify_old
        dify_new=$(read_env_value .env DIFY_VERSION 2>/dev/null || echo "")
        dify_new="${dify_new:-1.15.0}"
        dify_old=$(read_env_value "${UPGRADE_FROM_ENV_FILE:-.env.pre-upgrade-backup}" DIFY_VERSION 2>/dev/null || echo "")
        # Reached >= 1.15.0 now, AND wasn't already there before (empty backup
        # => can't prove we were on 1.15 => run once; harmless, idempotent-ish).
        if ! version_lt "$dify_new" "1.15.0" 2>/dev/null \
           && { [ -z "$dify_old" ] || version_lt "$dify_old" "1.15.0" 2>/dev/null; }; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would run 'flask backfill-plugin-auto-upgrade' in dify-api (Dify ${dify_old:-?} -> ${dify_new} crossing)"
                ((migrations_run++)) || true
            else
                print_substep "Dify 1.15: backfilling per-category plugin auto-upgrade settings..."
                local bf_retries=0 bf_ok=false
                while [ $bf_retries -lt 30 ]; do
                    if docker compose exec -T dify-api flask backfill-plugin-auto-upgrade >/dev/null 2>&1; then
                        bf_ok=true
                        break
                    fi
                    sleep 10
                    bf_retries=$((bf_retries + 1))
                    if [ $((bf_retries % 3)) -eq 0 ]; then
                        print_substep "  Waiting for dify-api 1.15 migrations to finish... ($((bf_retries * 10))s)"
                    fi
                done
                if [ "$bf_ok" = true ]; then
                    print_success "Dify plugin auto-upgrade backfill complete."
                else
                    print_warning "Dify plugin auto-upgrade backfill did not complete after retries."
                    print_info "  Run manually once dify-api is up: docker compose exec -T dify-api flask backfill-plugin-auto-upgrade"
                    journal_event "warning" "warn" "Dify 1.15 backfill-plugin-auto-upgrade did not complete; run manually (#26)" 2>/dev/null || true
                fi
                ((migrations_run++)) || true
            fi
        fi
    fi

    # --- Migration: Open WebUI 0.10.x -> 0.11.x — reset stale oauth
    # PersistentConfig on upgrade (#881; native "Login via Authentik" 500) ---
    # HYPOTHESIS, not yet confirmed by a live traceback — evidenced by
    # .gsd/reports/2026.09-owui-0.11-dify-1.16.1-impact-2026-08-05.md
    # §1.1.3/§1.3 ("the PersistentConfig trap") and the observed
    # discriminator (an upgraded box 500s on login, a fresh 0.11.0 box is
    # fine). OWUI persists every `oauth.*` PersistentConfig key into the
    # Postgres `config` table after first boot, and a persisted DB value
    # OVERRIDES the compose env on every later boot (verified directly
    # against upstream backend/open_webui/models/config.py at BOTH v0.10.2
    # and v0.11.0: `config` is a per-key table — `key text primary key,
    # value json` — one row per dotted PersistentConfig path, e.g.
    # `oauth.oidc.enable`; PersistentConfig falls back to the env default
    # only when its row is absent). 0.11.0 apparently cannot cleanly load a
    # stale 0.10-era persisted oauth.* row -> 500 on the OIDC login route.
    # Fix: delete the persisted oauth.* rows so PersistentConfig re-seeds
    # cleanly from env (ENABLE_OAUTH/OAUTH_*/OPENID_* — the canonical source
    # for this stack; SSO is Authentik-blueprint-driven) on next boot.
    # Idempotent (no-op once the rows are gone) and safe: worst case it
    # resets oauth to exactly what a fresh box already has. Keys off the OWUI
    # version transition — same pattern as the Dify 1.15 backfill above, not
    # INSTALLED_VERSION — so it fires only on a 0.10.x -> 0.11.x crossing: a
    # no-op on a 0.11.x -> 0.11.x restart/patch upgrade, and it never runs on
    # a fresh install (run_data_migrations is upgrade-only, never called from
    # init.sh).
    if echo "${COMPOSE_PROFILES:-}" | tr ',' '\n' | grep -qx "chat"; then
        local owui_new owui_old
        owui_new=$(read_env_value .env OPENWEBUI_VERSION 2>/dev/null || echo "")
        owui_new="${owui_new:-0.11.0}"
        owui_old=$(read_env_value "${UPGRADE_FROM_ENV_FILE:-.env.pre-upgrade-backup}" OPENWEBUI_VERSION 2>/dev/null || echo "")
        # Reached >= 0.11.0 now, AND wasn't already there before (empty
        # backup => can't prove we were on 0.11 => run once; idempotent, so
        # harmless if it re-fires).
        if ! version_lt "$owui_new" "0.11.0" 2>/dev/null \
           && { [ -z "$owui_old" ] || version_lt "$owui_old" "0.11.0" 2>/dev/null; }; then
            if [ "$DRY_RUN" = "true" ]; then
                print_info "  Would reset stale OWUI oauth PersistentConfig (OpenWebUI ${owui_old:-?} -> ${owui_new} crossing, #881)"
                ((migrations_run++)) || true
            else
                print_substep "OWUI 0.11: resetting stale oauth PersistentConfig (#881)..."
                reconcile_owui_oauth_persistentconfig
                ((migrations_run++)) || true
            fi
        fi
    fi

    if [ $migrations_run -eq 0 ]; then
        print_substep "No data migrations needed for this version range."
    else
        print_success "Data migrations complete: ${migrations_run} migration(s) applied."
    fi
}

# ==============================================================================
# Authentik hop chain (F12)
# ==============================================================================
# Authentik upgrades one release series at a time and refuses to start on data
# that skipped one ("Major version skips are not allowed"; the refusal comes
# before the migration, so the database stays recoverable). The stops below are
# the releases the chain passes through, in order; a box is hopped through every
# stop whose series lies strictly between the data's series and the target's.
#   2025.10 -> 2026.8.5 : 2025.12.4, 2026.2.6, 2026.5.7
#   2026.2.x -> 2026.8.5: 2026.5.7
#   2026.5.x -> 2026.8.5: (none, one step)
# The first stop has a second reason: 2025.12 introduced a schema migration
# (0056_user_roles) that depends on a prior migration shipped in 2025.12.x, so
# 2025.10.x -> 2026.2.x crashes Django with
#   FieldError: Cannot resolve keyword 'group_id'
# Data older than 2025.12 therefore always passes 2025.12.4, whatever the target.
# That hop also migrates the authentik-media volume layout: 2025.10 wrote files
# at the volume root (mounted /media), 2025.12+ expects /data/media/<files>.
# ==============================================================================
AUTHENTIK_HOP_STOPS=(2025.12.4 2026.2.6 2026.5.7)

# 2026.5.7 -> 202605; empty for anything that is not YYYY.M[.x]
_authentik_series() {
    local v="${1:-}"
    [[ "$v" =~ ^([0-9]{4})\.([0-9]{1,2})(\.|$) ]] || { echo ""; return 0; }
    echo $(( BASH_REMATCH[1] * 100 + 10#${BASH_REMATCH[2]} ))
}

# authentik_hop_chain <data-version> [<target-version>] -> the versions to pass
# through before the target, one per line, in order.
authentik_hop_chain() {
    local cur="${1:-}" tgt="${2:-}" cs ts s ss
    cs="$(_authentik_series "$cur")"
    [ -n "$cs" ] || return 0
    ts="$(_authentik_series "$tgt")"
    [ -n "$ts" ] || ts=999999
    for s in "${AUTHENTIK_HOP_STOPS[@]}"; do
        ss="$(_authentik_series "$s")"
        [ "$ss" -gt "$cs" ] || continue
        if [ "$ss" -lt "$ts" ] || { [ "$ss" -eq 202512 ] && [ "$cs" -lt 202512 ]; }; then
            echo "$s"
        fi
    done
}

# The line that says why authentik-server or -worker is not starting.
# The cause comes first: a crashing 2026.x server logs the Python RuntimeError
# ("Major version skips are not allowed") mid-run and then a multi-line Rust
# error trailer ("0: Errors encountered: [") in colour, so the last line that
# merely matches "Error" is the trailer, not the cause (observed on a live upgrade,
# 2026-10). One run is ~100 lines, hence a tail that spans a few restarts.
authentik_crash_reason() {
    local c logs line
    for c in authentik-server authentik-worker; do
        logs="$(docker logs --tail 400 "$c" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' || true)"
        line="$(printf '%s\n' "$logs" | grep -E 'RuntimeError:|not allowed|FATAL|OperationalError' | tail -1 || true)"
        [ -n "$line" ] || line="$(printf '%s\n' "$logs" | grep -E 'Error|refus' \
            | grep -vE 'Errors encountered|^[[:space:]]*[0-9]+: ' | tail -1 || true)"
        [ -n "$line" ] && { echo "${c}: ${line}"; return 0; }
    done
    return 0
}

# authentik_run_hop <version>: recreate server+worker on <version> (env override,
# no .env edit) and wait for it to become healthy. A crash names its log line.
authentik_run_hop() {
    local hop="$1"
    print_substep "Starting Authentik ${hop} intermediate hop..."
    AUTHENTIK_VERSION="$hop" docker compose up -d --force-recreate \
        authentik-server authentik-worker 2>&1

    # Wait for the hop to become healthy (Django migrations run on start).
    print_substep "Waiting for ${hop} migrations to complete..."
    local max_wait="${AUTHENTIK_HOP_MAX_WAIT:-420}" waited=0
    while [ $waited -lt $max_wait ]; do
        local health status
        health=$(docker inspect authentik-server --format '{{.State.Health.Status}}' 2>/dev/null || echo "starting")
        status=$(docker inspect authentik-server --format '{{.State.Status}}' 2>/dev/null || echo "")
        if [ "$status" = "restarting" ] || [ "$status" = "exited" ]; then
            print_error "authentik-server failed during the ${hop} hop (status: $status)."
            print_error "Reason: $(authentik_crash_reason)"
            docker logs --tail 50 authentik-server 2>&1 || true
            return 1
        fi
        if [ "$health" = "healthy" ]; then
            break
        fi
        sleep 5
        waited=$((waited + 5))
        if [ $((waited % 30)) -eq 0 ]; then
            print_substep "  ... still waiting (${waited}s / ${max_wait}s, health=${health})"
        fi
    done

    if [ $waited -ge $max_wait ]; then
        print_error "Authentik ${hop} hop did not become healthy within ${max_wait}s."
        docker logs --tail 50 authentik-server 2>&1 || true
        return 1
    fi
    print_success "Authentik ${hop} hop complete."
}

authentik_intermediate_hop() {
    print_substep "F12: checking whether Authentik two-hop upgrade is needed..."
    local current_version=""
    local raw_image
    if [ -n "${1:-}" ]; then
        # #2524 (--migrations-only): the DATA's Authentik version, named by the
        # caller. After a restore the running container says nothing about the
        # schema of the database it was restored under.
        current_version="$1"
        print_substep "F12: Authentik version of the data (from the source .env): '${current_version}'"
    else
        raw_image=$(docker inspect authentik-server --format '{{.Config.Image}}' 2>/dev/null || true)
        if [ -n "$raw_image" ]; then
            current_version="${raw_image##*:}"
        fi
        print_substep "F12: detected current Authentik image: '${raw_image:-<none>}' (version: '${current_version:-<none>}')"
    fi

    if [ -z "$current_version" ] || [ "$current_version" = "<no value>" ]; then
        print_substep "F12: no installed Authentik detected — skipping hop."
        return 0
    fi

    local target_version hop_list
    target_version="$(read_env_value .env AUTHENTIK_VERSION 2>/dev/null || true)"
    hop_list="$(authentik_hop_chain "$current_version" "$target_version")"
    if [ -z "$hop_list" ]; then
        print_substep "F12: Authentik ${current_version} -> ${target_version:-<target>}: no intermediate hop needed."
        return 0
    fi
    print_step "Running the Authentik hop chain: ${current_version} → $(echo $hop_list | sed 's/ / → /g') → ${target_version:-<target>}"

    # The volume layout is only older than 2025.12 data's concern.
    local cs_now
    cs_now="$(_authentik_series "$current_version")"
    if [ "$cs_now" -lt 202512 ]; then
    # Migrate volume layout. Authentik changed the layout twice across the
    # 2025.10 → 2026.2 jump:
    #   pre-2025.12:  /data/<file>           (root of volume)
    #   2025.12 hop:  /data/media/<file>     (Django MEDIA_ROOT moved into a subdir)
    #   2026.2:       /data/media/public/<file>  (signed-URL serving requires the
    #                 public/ subdir; Application meta_icon and brand assets all
    #                 live here)
    # We do BOTH hops in one pass: anything at the volume root → /data/media/,
    # then anything under /data/media/<file> (excluding subdirs we expect like
    # public/, application-icons/, cdrom/, floppy/) → /data/media/public/.
    # Idempotent: skips entries that already exist at the destination. Caught
    # during prod 2026.05 cutover where the second hop was missing — Authentik
    # login page logo, flow background, app library tile icons all 404'd.
    local vol_name
    vol_name=$(docker volume ls --format '{{.Name}}' | grep -E '_authentik-media$' | head -1)
    if [ -n "$vol_name" ]; then
        print_substep "Migrating authentik-media volume layout (root → /data/media → /data/media/public)..."
        docker run --rm -v "${vol_name}:/data" alpine:3.21 sh -c '
            # Hop 1: volume root → /data/media/
            mkdir -p /data/media
            for entry in /data/*; do
                [ -e "$entry" ] || continue
                name=$(basename "$entry")
                [ "$name" = "media" ] && continue
                if [ ! -e "/data/media/$name" ]; then
                    mv "$entry" "/data/media/"
                fi
            done
            # Hop 2: /data/media/* (loose files only) → /data/media/public/
            mkdir -p /data/media/public
            for entry in /data/media/*; do
                [ -e "$entry" ] || continue
                name=$(basename "$entry")
                # Skip directories that Authentik expects at /data/media/<dirname>
                # (public/ is the destination; application-icons/ used by some
                # blueprints; cdrom + floppy are leftover dev artifacts).
                case "$name" in
                    public|application-icons|cdrom|floppy) continue ;;
                esac
                # Only move regular files (not directories — those are intentional)
                if [ -f "$entry" ] && [ ! -e "/data/media/public/$name" ]; then
                    mv "$entry" "/data/media/public/"
                fi
            done
            # A1 D10: the access-group snapshot directory, owned by the
            # worker uid (as authentik-media-migrator does it). Idempotent.
            mkdir -p /data/.razzfazz
            chown 1000:1000 /data/.razzfazz
            chmod 700 /data/.razzfazz
        ' >/dev/null 2>&1 || print_warning "Volume layout migration produced warnings (continuing)."
    fi

    fi

    local hop
    for hop in $hop_list; do
        authentik_run_hop "$hop" || return 1
    done
    print_success "Authentik hop chain complete. Proceeding to target version."
}

# After the restart: is Authentik in a restart loop? Waits (bounded) for
# authentik-server to turn healthy; a restarting/exited server, or an unhealthy
# one that has already restarted twice, is a crash loop. It is named with the
# log line that says why, and the caller makes it the upgrade's verdict.
# Returns 0 healthy / absent / still starting without restarts, 1 crash loop.
authentik_crashloop_check() {
    docker inspect authentik-server >/dev/null 2>&1 || return 0
    local max_wait="${AUTHENTIK_CRASHLOOP_WAIT:-180}" waited=0 status health restarts
    while :; do
        status=$(docker inspect authentik-server --format '{{.State.Status}}' 2>/dev/null || echo "")
        health=$(docker inspect authentik-server --format '{{.State.Health.Status}}' 2>/dev/null || echo "")
        restarts=$(docker inspect authentik-server --format '{{.RestartCount}}' 2>/dev/null || echo 0)
        case "$restarts" in ''|*[!0-9]*) restarts=0 ;; esac
        [ "$health" = "healthy" ] && return 0
        if [ "$status" = "restarting" ] || [ "$status" = "exited" ] || [ "$restarts" -ge 2 ]; then
            break
        fi
        [ "$waited" -lt "$max_wait" ] || return 0
        sleep 5
        waited=$((waited + 5))
    done
    print_error "authentik-server is in a restart loop (status: ${status:-?}, restarts: ${restarts}): single sign-on is down."
    print_error "Reason: $(authentik_crash_reason)"
    print_info "Authentik refuses to skip a release series; set AUTHENTIK_VERSION to the next step and run 'rzfz upgrade --migrations-only', or read: docker logs authentik-server"
    return 1
}

# ==============================================================================
# Restart Stack
# ==============================================================================
# A stack we stopped for the build (--stop-stack-for-build) must not stay down
# on ANY exit path. Where the stack is stopped, STACK_STOPPED_FOR_BUILD is set
# and STACK_STOPPED_IDS holds the ids of the containers that were RUNNING just
# before (`compose ps -q --status running`, same project/COMPOSE_FILE/profiles
# as the stop). The marker is cleared at the Step 8 call site. What comes back
# here is exactly those containers, unchanged (`docker start <ids>`): not
# `compose start`, which resolves services from the NEW tree and fails on a
# service the release adds; never a recreate on the new tree, so "nothing was
# restarted" stays true on the aborts. Called on the build-failure path and from
# the EXIT trap. Its output goes to the run log and the terminal, never through
# the run-log tee, which a Ctrl-C has already killed.
STACK_STOPPED_FOR_BUILD=false
STACK_STOPPED_IDS=""
restore_stack_stopped_for_build() {
    [ "${STACK_STOPPED_FOR_BUILD:-false}" = true ] || return 0
    STACK_STOPPED_FOR_BUILD=false
    local _log="${RZFZ_RUN_LOG:-/dev/null}" _ids _id _ok=true _cmd
    _ids="${STACK_STOPPED_IDS:-}"
    for _id in $_ids; do [[ "$_id" =~ ^[0-9a-f]+$ ]] || _ok=false; done
    _say() { { printf '[!] %s\n' "$*" >/dev/tty; } 2>/dev/null || printf '[!] %s\n' "$*" >&2 2>/dev/null || true
             printf '[!] %s\n' "$*" >>"$_log" 2>/dev/null || true; }
    if [ -z "$_ids" ] || [ "$_ok" != true ]; then
        _say "The stack was stopped for the build but its container ids are missing or unreadable (stored: '${_ids}'). Nothing was recreated."
        _say "List this stack's stopped containers with 'docker compose ps -a --status exited -q' and start them with 'docker start <id> <id> ...' (log: ${_log})."
        return 0
    fi
    _cmd="docker start $(echo $_ids)"
    _say "The upgrade stopped before its restart step; starting the containers that were stopped for the build."
    if ! docker start $_ids >>"$_log" 2>&1; then
        _say "'${_cmd}' failed; run it by hand (details in ${_log})."
    fi
    return 0
}

restart_stack() {
    # #2578: the Synapse config is rendered from a template at init/prepare
    # time only; without this an upgrade leaves the box on the old homeserver
    # config (here: without the case-variant login refusal). The full path
    # below recreates every container right after, so only the render is
    # needed; --migrations-only starts the stack as it is and recreates
    # synapse itself when the render changed.
    if [ "${DRY_RUN:-false}" != true ]; then
        if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
            matrix_refresh_config "$(pwd)" || print_warning "matrix: the Synapse config was not refreshed - run: rzfz post-install --refresh"
        else
            matrix_refresh_config "$(pwd)" --no-recreate || print_warning "matrix: the Synapse config was not refreshed - run: rzfz post-install --refresh"
        fi
    fi
    # #2524: --migrations-only recreates nothing on a new tree; it starts the
    # stack as it is, for the migrations that need it, after the Authentik
    # two-hop check against the DATA's Authentik version (the source .env's).
    if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
        local _hop_rc=0
        if [ -n "${MIGRATIONS_FROM_ENV:-}" ] && [ -n "${OLD_AUTHENTIK_VERSION:-}" ]; then
            authentik_intermediate_hop "$OLD_AUTHENTIK_VERSION" || _hop_rc=$?
        else
            authentik_intermediate_hop || _hop_rc=$?
        fi
        if [ "$_hop_rc" -ne 0 ]; then
            print_error "The Authentik two-hop failed — the stack was not started on the migrated data."
            exit 1
        fi
        print_step "Starting the stack for the migrations that need it (rzfz start)..."
        "${SCRIPT_DIR}/rzfz" start || { print_error "rzfz start failed — the post-restart migrations did not run. Re-run this command once the stack starts."; exit 1; }
        return 0
    fi
    print_step "Restarting stack with new configuration..."

    # #1595: the same property applies to EVERY file bind source, not only the
    # one below — and an upgrade is the moment a previous `up` has already had
    # the chance to materialise them as directories. Undo that first; only
    # EMPTY directories inside the project are touched.
    repair_empty_dir_bind_sources || true

    # #1080: certs/caddy-ca.pem is bind-mounted by openwebui/gitea/vaultwarden.
    # The upgrade path never runs ensure_oidc_ca_superset, so on a box where the
    # file does not exist yet (fresh-volume re-provisioning), the first `up`
    # below would make Docker materialise the mount target as a root-owned
    # DIRECTORY — permanently breaking the #152 CA-superset step. Seed a
    # placeholder FILE so that can never happen; the real bundle is rebuilt by
    # init / post-install / --refresh. An existing file is left untouched (it
    # may already carry the live internal CA — overwriting here would regress
    # TLS_MODE=internal boxes until the next --refresh).
    if [ ! -e certs/caddy-ca.pem ]; then
        mkdir -p certs 2>/dev/null || true
        printf '# razzfazz.ai OIDC CA bundle placeholder (#1080) — rebuilt by ensure_oidc_ca_superset (#152) on init/post-install/--refresh\n' > certs/caddy-ca.pem 2>/dev/null \
            || print_warning "could not seed certs/caddy-ca.pem placeholder (#1080) — the first 'up' may create it as a directory."
    fi
    # #2559: caddy bind-mounts the DIRECTORY data/worker-bundle-generated; seeded
    # here as the operator's, or the first `up` makes it root's and post-install
    # can never write the Mac worker bundle into it.
    mkdir -p data/worker-bundle-generated 2>/dev/null \
        || print_warning "could not create data/worker-bundle-generated (#2559) — the first 'up' creates it root-owned."

    # #125 P1: refresh the box-local Enterprise-docs overlay from the freshly
    # checked-out tree BEFORE recreating razzfazz-help. On an internal box this
    # mirrors docs/enterprise/ into overlay/enterprise/docs/; on a public/Codeberg
    # box docs/enterprise/ is absent → no content sync → the overlay (populated
    # out-of-band by a USB bake / gated fetch — P1.5/P2) is left untouched, so the
    # Enterprise docs are retained across the upgrade. Best-effort.
    if [ -x "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" ]; then
        print_substep "Refreshing box-local Enterprise-docs overlay for the Help-UI (#125)..."
        "${SCRIPT_DIR}/scripts/sync-enterprise-overlay.sh" 2>&1 || print_warning "Enterprise-docs overlay sync failed (Help-UI may serve community-only)."
    fi

    # F12: two-hop across the 2025.12 migration boundary
    authentik_intermediate_hop

    # rc6.7 #48: stop GPUStack-spawned runner pods before recreating the
    # gpustack server. They were spawned via the docker socket so compose
    # has no record of them; without this they'd survive the recreate and
    # double-pin VRAM/RAM until manually rm'd.
    stop_orphan_gpustack_runners

    # --remove-orphans: M018 / S06.6 renamed the gpustack-experimental and
    # model-sync-experimental services to gpustack and model-sync. Without
    # this flag, the old containers (which compose no longer recognizes
    # under the new service names) cause "container name in use" errors on
    # the next docker compose up. Same applies to any future profile/service
    # rename — since #252 that cleanup is remove_compose_orphans_safe (the
    # raw flag would also delete socket-provisioned user agents).
    #
    # NO --build here (#184 WS2a — UNIVERSAL, all modes): the runtime path must
    # NEVER build. This was `up -d --build …` (the #1 offline breaker: it forces
    # a base-image pull + apt/pip/npm on every upgrade restart, tripping the
    # firewall on proxied/air-gapped boxes, and emitting "can't pull razzfazz-*"
    # noise on online ones). A NEW custom-build service that a release adds is now
    # built EXPLICITLY by build_and_pull (upgrade) / prebuild_all_custom_images
    # (post-install) BEFORE this restart; every build service also carries
    # `pull_policy: never`, so a missing image FAILS CLEAR (surfaced by
    # `rzfz verify-images`) instead of silently building. The historical
    # cognee-frontend regression (culturehack-001, 2026-05-21) is covered by that
    # explicit pre-build, not by an implicit `up --build`.
    # #156: guarantee the governance checksum DB is a regular FILE before compose
    # up. razzfazz-config bind-mounts .checksums.db as a single file. On a box whose
    # .gitignore did NOT ignore it (older public-curated Codeberg exports, ga..ga.4)
    # the pre-checkout `git stash --include-untracked` stashes it away; Docker then
    # recreates the bind-mount source as a DIRECTORY and razzfazz-config crash-loops
    # on sqlite (`unable to open database file`). The ga.5 curated-.gitignore fix
    # only protects go-forward (the stash runs from the SOURCE box's .gitignore);
    # this belt runs from the TARGET script's restart_stack, so it also fixes the
    # TRANSITION from any older buggy export. Coerce a stray dir / absent path back
    # to an empty file (sqlite re-inits the schema) — idempotent. See #156.
    if [ -d "${SCRIPT_DIR}/.checksums.db" ]; then
        rm -rf "${SCRIPT_DIR}/.checksums.db"
        print_substep "  #156: replaced a stray .checksums.db DIRECTORY with an empty file (razzfazz-config sqlite)."
    fi
    [ -e "${SCRIPT_DIR}/.checksums.db" ] || : > "${SCRIPT_DIR}/.checksums.db"

    # #153: a pre-existing-unhealthy peripheral (e.g. an onyx-vespa that was
    # already red BEFORE the upgrade) makes `compose up` exit rc=1. Under
    # `set -eo pipefail` that aborted the ENTIRE upgrade right here — before the
    # RAZZFAZZ_VERSION sync (so the box kept reporting the OLD version in the
    # Config UI) and before the post-upgrade self-heal `--refresh` ever ran
    # (observed on prod 8.246 ga.2→ga.3, 2026-07). Make it NON-FATAL: capture the
    # rc, report which services are not up/healthy, and continue. The health-wait
    # below + the final verification/diagnose gate still surface a genuine
    # regression, but one sick peripheral no longer blocks the version bump +
    # self-heal for the rest of the stack.
    local _compose_rc=0
    # #252: NO --remove-orphans — socket-provisioned agents carry the
    # compose project label without a service record and are one compose
    # behavior change away from being deleted as "orphans".
    # remove_compose_orphans_safe (below) covers the flag's real purpose
    # (renamed-service leftovers, M018 class) while skipping
    # razzfazz.managed=true.
    # review #683: the sweep must run BEFORE up too — the M018 case is a
    # container_name COLLISION after a service rename (our services pin
    # explicit container_name:), so an after-up-only sweep left the new
    # container missing until the next upgrade. After-up call stays as a
    # second net for containers up itself replaces.
    remove_compose_orphans_safe || true
    docker compose up -d --force-recreate 2>&1 || _compose_rc=$?
    remove_compose_orphans_safe || true
    if [ "$_compose_rc" -ne 0 ]; then
        print_warning "compose up exited rc=${_compose_rc} — not every container (re)started/healthy. Continuing (#153)."
        docker compose ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null \
            | awk '$2!="running" || $3=="unhealthy" || $3=="starting"' \
            | sed 's/^/    not-ready: /' || true
    else
        print_substep "Containers recreated."
    fi

    # Wait for containers to become healthy
    print_substep "Waiting for services to become healthy..."
    local max_wait=300
    local waited=0
    local interval=10

    while [ $waited -lt $max_wait ]; do
        local unhealthy
        unhealthy=$(docker compose ps --format '{{.Name}} {{.Health}}' 2>/dev/null | \
            grep -cE 'starting|unhealthy' || true)
        local total
        total=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l)

        if [ "$unhealthy" -eq 0 ] && [ "$total" -gt 0 ]; then
            break
        fi

        sleep $interval
        waited=$((waited + interval))
        print_substep "  Waiting... (${waited}s / ${max_wait}s, ${unhealthy} services still starting)"
    done

    if [ $waited -ge $max_wait ]; then
        print_warning "Some services did not become healthy within ${max_wait}s."
    fi

    # Wait for Authentik init if running
    if docker ps --format '{{.Names}}' 2>/dev/null | grep "authentik-init" >/dev/null; then
        print_substep "Waiting for Authentik initialization..."
        timeout 300 docker wait authentik-init 2>/dev/null || true
    fi

    print_success "Stack restarted."
}

# #2400: a box that serves models on GPUStack gains the LLM Manager trio in this
# upgrade. Hand the weights over NOW — after the restart created the manager's
# volume, before post-install --refresh can schedule a download. Zero bytes on
# one filesystem (hard links); a copy is allowed only up to 90 % of free space.
adopt_gpustack_weights_if_migrating() {
    # upgrade.sh does not export COMPOSE_PROFILES; the box's truth is .env.
    local profiles="${COMPOSE_PROFILES:-}"
    [ -n "$profiles" ] || profiles=$(read_env_value .env COMPOSE_PROFILES 2>/dev/null || true)
    case ",${profiles}," in *,llm-manager,*) : ;; *) return 0 ;; esac
    docker volume ls -q 2>/dev/null | grep -qE '_gpustack-data$' || return 0
    docker volume ls -q 2>/dev/null | grep -qE '_llm-node-models$' || return 0
    local docker_root free_kb="" limit_args=() err_file rc=0 need="" need_gb
    docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
    [ -n "$docker_root" ] && [ -d "$docker_root" ] || docker_root=/var/lib/docker
    free_kb=$(df -Pk "$docker_root" 2>/dev/null | awk 'NR==2{print $4}') || free_kb=""
    case "$free_kb" in
        ''|*[!0-9]*)
            free_kb=""
            print_warning "Free space on '$docker_root' could not be read — the weights will be linked where possible, but never copied." >&2
            limit_args=(--link-only) ;;
        *)  local max_bytes=$(( free_kb * 1024 * 9 / 10 ))
            # 0 means "unlimited" to the planner; a full disk must mean "no copy".
            if [ "$max_bytes" -ge 1 ]; then limit_args=(--max-copy-bytes "$max_bytes"); else limit_args=(--link-only); fi ;;
    esac
    print_step "Handing GPUStack's weights to the LLM Manager..."
    err_file=$(mktemp 2>/dev/null || echo "")
    if [ -n "$err_file" ]; then
        # stderr streams LIVE (the pin check prints one "hashing …" line per
        # weight — minutes on a full volume must not look like a hang) and is
        # captured for the "would COPY" figure below. `wait $!` lets tee
        # finish writing the file before it is read — which needs the brace
        # group: bash sets `$!` to a process substitution only when the shell
        # itself applies the redirection. On an external command it is applied
        # in the child, `$!` never names the tee, and on a loaded host sed read
        # an empty file ("an unknown amount of data"; CI run 2257).
        { "$SCRIPT_DIR/cli/adopt-models.sh" "${limit_args[@]}"; } 2> >(tee "$err_file" >&2) || rc=$?
        wait $! 2>/dev/null || true
        need=$(sed -n 's/.*would COPY \([0-9][0-9]*\) bytes.*/\1/p' "$err_file" | head -1)
        rm -f "$err_file"
    else
        "$SCRIPT_DIR/cli/adopt-models.sh" "${limit_args[@]}" || rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
        print_substep "Weights adopted — nothing the manager already holds will be downloaded again."
    else
        if [ -n "$need" ]; then
            need_gb=$(( (need + 1073741823) / 1073741824 ))
            need_gb="about ${need_gb} GB"
        else
            need_gb="an unknown amount of data"
        fi
        print_warning "The weight adoption did not complete (exit ${rc}). Free ${need_gb} of disk space, then run: rzfz adopt-models — or do nothing, and the automatic 'post-install --refresh' that follows this upgrade will download the weights instead (${need_gb})." >&2
    fi
    return 0
}

# GPUSTACK_CPU_HANDOVER-BEGIN
# #2813: a CPU box that still runs GPUStack moves
# to the LLM Manager's own engines.
#
# 2026.07 shipped CPU boxes on GPUStack v0.7.1-cpu (`llm-cpu`; folded into
# `llm-legacy` + HARDWARE=cpu by Step 5b). Left there, the manager fronts GPUStack
# and the standard set is deployed INTO GPUStack's single ~8 GiB worker, where the
# kept 8B embedding starves the reranker and the class-default embedder (R4). A
# fresh CPU box never runs GPUStack, so an upgraded one must end the same way.
#
# Three moves, in this order, so the box never ends with NOTHING serving:
#   1. begin  (after the restart and the weight adoption): list the GPUStack-only
#      models, drop `llm-legacy` from COMPOSE_PROFILES and leave a marker. The
#      GPUStack container keeps running — it is just no longer a profile.
#   2. `post-install --refresh` (unchanged) now takes the manager arm and deploys
#      the standard set — chat, the box's pinned embedding (#2727 keep-rule),
#      reranker — on the manager's engines. The manager's admission control
#      refuses what does not fit; that refusal is reported, not hidden.
#   3. finish (after the refresh): wait, bounded, until the manager serves
#      the kept models; only then stop and remove GPUStack. On timeout or failure
#      the profile is put back and the refresh re-run, so GPUStack keeps serving
#      (federated, the documented 2026.09 state).
# AMD and NVIDIA boxes never enter this path (HARDWARE=cpu only); they keep
# GPUStack 0.7.1 as the federated optional backend.
GPUSTACK_CPU_HANDOVER_MARKER=".gpustack-cpu-handover.pending"

_handover_load_lib() {
    declare -F _llmm_ready_report >/dev/null 2>&1 && return 0
    # shellcheck source=/dev/null
    . "${SCRIPT_DIR}/cli/lib-llm-manager-deploy.sh" 2>/dev/null
}

# GPUStack model names that the standard-set catalogue does not carry.
_handover_custom_models() {
    local held="$1"
    HELD="$held" YAML_PATH="${SCRIPT_DIR}/core/llm/standard-models.yaml" python3 -c '
import os, sys
try:
    import yaml
    cat = set((yaml.safe_load(open(os.environ["YAML_PATH"])) or {}).get("models") or {})
except Exception:
    sys.exit(3)
for line in os.environ["HELD"].splitlines():
    name = line.split("\t")[0].strip()
    if name and name not in cat:
        print(name)
' 2>/dev/null
}

# The aliases the box must keep serving: chat, embedding (the pinned one on an
# existing box) and reranker, through the one hardware rule.
_handover_kept_models() {
    ENVF="${SCRIPT_DIR}/.env" YAML_PATH="${SCRIPT_DIR}/core/llm/standard-models.yaml" python3 -c '
import os, sys
sys.path.insert(0, os.path.dirname(os.environ["YAML_PATH"]))
import yaml, hardware_catalog as hc
spec = yaml.safe_load(open(os.environ["YAML_PATH"])) or {}
d = hc.defaults_for(spec, "cpu", os.environ["ENVF"])
seen = []
for role in ("chat", "embedding", "reranker"):
    v = d.get(role)
    if v and v not in seen:
        seen.append(v)
print("\n".join(seen))
' 2>/dev/null
}

cpu_gpustack_handover_begin() {
    [ "${DRY_RUN:-false}" != true ] && [ "${MIGRATIONS_ONLY:-false}" != true ] || return 0
    local env_file="${SCRIPT_DIR}/.env" marker="${SCRIPT_DIR}/${GPUSTACK_CPU_HANDOVER_MARKER}"
    [ -f "$env_file" ] || return 0
    local hw profiles
    hw=$(read_env_value "$env_file" HARDWARE 2>/dev/null || true)
    profiles=$(read_env_value "$env_file" COMPOSE_PROFILES 2>/dev/null || true)
    [ "$hw" = "cpu" ] || return 0
    case ",${profiles}," in *,llm-legacy,*) : ;; *) return 0 ;; esac
    case ",${profiles}," in *,llm-manager,*) : ;; *) return 0 ;; esac
    print_step "Step 8b: this CPU box moves from GPUStack to the LLM Manager"
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -x llm-manager >/dev/null \
       || ! docker ps --format '{{.Names}}' 2>/dev/null | grep -x llm-worker-agent >/dev/null; then
        print_warning "The LLM Manager or its worker agent is not running — GPUStack stays the backend (federated). Fix the manager, then re-run: rzfz upgrade"
        return 0
    fi
    local held="" custom=""
    if _handover_load_lib; then
        held=$(_llmm_gpustack_models 2>/dev/null) || held=""
        [ -n "$held" ] && custom=$(_handover_custom_models "$held" | tr '\n' ',' | sed 's/,$//')
    fi
    if [ -n "$custom" ]; then
        print_warning "GPUStack-only models go out of service (the LLM Manager serves the standard set): ${custom//,/, }"
        print_info    "  Their weights stay in the gpustack-data volume — it is never deleted. Re-deploy any of them from the LLM Manager console."
    fi
    local kept="" tok
    local _ifs="$IFS"; IFS=','
    # shellcheck disable=SC2086
    for tok in $profiles; do
        [ -n "$tok" ] && [ "$tok" != "llm-legacy" ] && kept="${kept:+$kept,}$tok"
    done
    IFS="$_ifs"
    { printf 'profiles=%s\n' "$profiles"; printf 'custom=%s\n' "$custom"; } > "$marker"
    update_env_value "$env_file" "COMPOSE_PROFILES" "$kept"
    print_substep "COMPOSE_PROFILES: ${profiles} → ${kept} (llm-legacy leaves; GPUStack keeps running until the manager serves)"
    return 0
}

cpu_gpustack_handover_finish() {
    local marker="${SCRIPT_DIR}/${GPUSTACK_CPU_HANDOVER_MARKER}"
    [ -f "$marker" ] || return 0
    [ "${DRY_RUN:-false}" != true ] || return 0
    local env_file="${SCRIPT_DIR}/.env" orig custom names want budget poll start report ready pending unplaceable failed ok=false
    orig=$(sed -n 's/^profiles=//p' "$marker" | head -1)
    custom=$(sed -n 's/^custom=//p' "$marker" | head -1)
    print_step "Step 11b: GPUStack retires once the LLM Manager serves"
    names=""
    if _handover_load_lib; then names=$(_handover_kept_models) || names=""; fi
    if [ -z "$names" ]; then
        print_warning "Could not work out which models the manager has to serve — GPUStack is NOT removed."
    else
        want=$(printf '%s\n' "$names" | grep -c .)
        budget="${LLM_CPU_HANDOVER_READY_TIMEOUT:-1800}"; case "$budget" in ''|*[!0-9]*) budget=1800 ;; esac
        poll="${LLM_CPU_HANDOVER_READY_POLL:-10}"; case "$poll" in ''|*[!0-9.]*) poll=10 ;; esac
        print_info "Waiting up to ${budget}s for the manager to serve: $(printf '%s' "$names" | tr '\n' ' ')"
        start=$SECONDS
        while :; do
            report=$(_llmm_admin_api GET /api/deployments 2>/dev/null | _llmm_ready_report "$names") || report=""
            ready=$(printf '%s\n' "$report" | sed -n 1p)
            pending=$(printf '%s\n' "$report" | sed -n 2p)
            unplaceable=$(printf '%s\n' "$report" | sed -n 3p)
            failed=$(printf '%s\n' "$report" | sed -n 4p)
            if [ "${ready:-0}" = "$want" ]; then ok=true; break; fi
            if _llmm_all_pending_are_terminal "$pending" "$unplaceable" "$failed" 2>/dev/null; then break; fi
            [ $((SECONDS - start)) -lt "$budget" ] || break
            sleep "$poll"
        done
    fi
    if [ "$ok" = true ]; then
        docker stop gpustack model-sync >/dev/null 2>&1 || true
        docker rm -f gpustack model-sync >/dev/null 2>&1 || true
        stop_orphan_gpustack_runners || true
        if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -x gpustack >/dev/null; then
            print_warning "The gpustack container could not be removed — remove it by hand: docker rm -f gpustack"
        else
            print_success "The LLM Manager serves the kept models; GPUStack is stopped and removed."
        fi
        [ -n "$custom" ] && print_info "  Out of service: ${custom//,/, } — their weights stay in the gpustack-data volume (never deleted)."
        rm -f "$marker"
        return 0
    fi
    # Not ready: GPUStack must keep serving. Put the profile back and let the
    # refresh federate it again (the documented 2026.09 state).
    print_warning "The LLM Manager is not serving the kept models (waiting: ${pending:-unknown}) — GPUStack is NOT removed and keeps serving."
    [ -n "$orig" ] && update_env_value "$env_file" "COMPOSE_PROFILES" "$orig"
    print_substep "COMPOSE_PROFILES restored: ${orig}"
    rm -f "$marker"
    if [ -x "${SCRIPT_DIR}/rzfz" ]; then
        "${SCRIPT_DIR}/rzfz" post-install --refresh || print_warning "The re-federating refresh reported issues — run: rzfz post-install --refresh"
    fi
    print_info "  Fix the manager (rzfz logs llm-manager), then re-run: rzfz upgrade — the handover is attempted again."
    return 0
}
# GPUSTACK_CPU_HANDOVER-END

# #2393: engines launched before 2026.11 sit on razzfazz-stack_default, which
# has egress. Move each one onto the internal llm-engines network WITHOUT a
# restart: `docker network connect`/`disconnect` hot-plug an interface into a
# running container (llama-server listens on 0.0.0.0, so it answers on the new
# one at once). Order: connect, verify, and disconnect the old network only
# once the engine AND every running caller (router, manager, agent) are seen on
# llm-engines. Any failure leaves the engine on its old network, serving, and
# WARNs. Idempotent: an engine already on llm-engines only is left alone. A box
# whose .env names another engine network keeps its engines where they are.
# Never fails the upgrade.
#
# Rollback record: every move is written to .env.engine-networks.pre-upgrade-
# backup, next to .env.pre-upgrade-backup, BEFORE the old network is dropped
# (no record, no disconnect). Its first line names the upgrade it belongs to
# (`from=<RAZZFAZZ_VERSION>@<RAZZFAZZ_COMMIT>` of .env.pre-upgrade-backup). A
# record under any other header is wiped first. Only a re-run with
# --skip-backup keeps the same header (pre_upgrade_backup rewrites the backup,
# and with it the key, on every normal run) and so appends to it.
# rollback() replays it through _rollback_engine_networks.
migrate_master_engines_network() {
    local new=llm-engines old=razzfazz-stack_default want engines c nets err
    local fmt='{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
    local callers_ok=1 unverified="" left=0 moved=0
    local state=.env.engine-networks.pre-upgrade-backup key run errf
    key="$(read_env_value .env.pre-upgrade-backup RAZZFAZZ_VERSION 2>/dev/null || true)@$(read_env_value .env.pre-upgrade-backup RAZZFAZZ_COMMIT 2>/dev/null || true)"
    if [ "$(head -1 "$state" 2>/dev/null || true)" != "from=${key}" ]; then
        rm -f "$state" 2>/dev/null || true
    fi
    want=$(read_env_value .env LLM_WORKER_ENGINE_NETWORK 2>/dev/null || true)
    if [ -n "$want" ] && [ "$want" != "$new" ]; then
        print_info "LLM_WORKER_ENGINE_NETWORK=${want} — the LLM engines stay on the network this box names (#2393)."
        return 0
    fi
    # stderr apart from the name list: a CLI warning on success is not an engine
    errf=$(mktemp 2>/dev/null) || errf=/dev/null
    if ! engines=$(docker ps -a --filter label=rzfz.role=llm-engine --format '{{.Names}}' 2>"$errf"); then
        err=$(cat "$errf" 2>/dev/null || true)
        [ "$errf" = /dev/null ] || rm -f "$errf" 2>/dev/null || true
        print_warning "Could not list the LLM engines (${err:-no output}) — engines started before this upgrade keep internet access until the next upgrade (#2393)." >&2
        return 0
    fi
    [ "$errf" = /dev/null ] || rm -f "$errf" 2>/dev/null || true
    [ -n "$engines" ] || return 0
    print_step "Moving the LLM engines onto the internal ${new} network (#2393)..."
    if ! err=$(docker network inspect "$new" 2>&1 >/dev/null); then
        print_warning "Network ${new} is missing (${err:-no output}) — the engines stay on ${old}, serving, with internet access. Re-run 'rzfz upgrade' once the stack is up (#2393)." >&2
        return 0
    fi
    # The callers must already be on llm-engines, or an engine that loses the
    # old network becomes unreachable. A caller that does not exist or is not
    # running is recreated from the compose file, which puts it there.
    for c in llm-manager-router llm-manager llm-worker-agent; do
        # absent → skip; any other inspect error → unverified (never "not running")
        if ! run=$(docker inspect -f '{{.State.Running}}' "$c" 2>&1); then
            case "$run" in *"No such"*) continue ;; esac
            callers_ok=0; unverified="${unverified} ${c}"; continue
        fi
        [ "$run" = true ] || continue
        if ! nets=$(docker inspect -f "$fmt" "$c" 2>/dev/null); then
            callers_ok=0; unverified="${unverified} ${c}"; continue
        fi
        case " $nets " in *" $new "*) ;; *) callers_ok=0; unverified="${unverified} ${c}" ;; esac
    done
    if [ "$callers_ok" -ne 1 ]; then
        print_warning "Not on ${new} yet:${unverified} — the engines keep ${old} as well, so they stay reachable (and keep internet access). Re-run 'rzfz upgrade' to finish the move (#2393)." >&2
    fi
    for c in $engines; do
        if ! nets=$(docker inspect -f "$fmt" "$c" 2>&1); then
            print_warning "Engine ${c}: cannot read its networks (${nets}) — left as it is (#2393)." >&2
            left=$((left + 1)); continue
        fi
        case " $nets " in
            *" $new "*) ;;
            *)
                if ! err=$(docker network connect "$new" "$c" 2>&1); then
                    print_warning "Engine ${c}: connect to ${new} failed (${err}) — left on ${nets% }, serving (#2393)." >&2
                    left=$((left + 1)); continue
                fi
                if ! nets=$(docker inspect -f "$fmt" "$c" 2>&1); then
                    print_warning "Engine ${c}: connected, but its networks cannot be read back (${nets}) — the old network stays (#2393)." >&2
                    left=$((left + 1)); continue
                fi
                case " $nets " in *" $new "*) ;; *)
                    print_warning "Engine ${c}: the connect reported success but ${new} is not attached (${nets% }) — left as it is (#2393)." >&2
                    left=$((left + 1)); continue ;;
                esac ;;
        esac
        case " $nets " in *" $old "*) ;; *) continue ;; esac
        if [ "$callers_ok" -ne 1 ]; then left=$((left + 1)); continue; fi
        if ! { [ -f "$state" ] || printf 'from=%s\n' "$key" > "$state"; } 2>/dev/null \
           || ! printf '%s\t%s\n' "$c" "$old" 2>/dev/null >> "$state"; then
            print_warning "Engine ${c}: the move could not be recorded in ${state} — it keeps ${old} as well, so a rollback still reaches it (#2393)." >&2
            left=$((left + 1)); continue
        fi
        if ! err=$(docker network disconnect "$old" "$c" 2>&1); then
            print_warning "Engine ${c}: disconnect from ${old} failed (${err}) — it serves on both networks and keeps internet access (#2393)." >&2
            left=$((left + 1)); continue
        fi
        moved=$((moved + 1))
    done
    [ "$moved" -eq 0 ] || print_substep "${moved} engine(s) moved onto ${new} — no restart, no internet access."
    [ "$left" -eq 0 ] || print_warning "${left} engine(s) still on ${old} — see above; they keep serving (#2393)." >&2
    return 0
}

# #2393: the rollback half of migrate_master_engines_network. A rollback brings
# back a router and a manager that are NOT on llm-engines, so every engine on
# llm-engines must be back on the network the restored tree uses, or it is
# unreachable. Two sources of work:
#   record — the moves the migration recorded (only the record of the upgrade
#            being undone: its from= line matches the restored .env);
#   sweep  — EVERY engine container on llm-engines, whether or not the record
#            names it: engines launched after the upgrade exist only there.
#            Their target is the restored .env's LLM_WORKER_ENGINE_NETWORK (empty:
#            the restored tree's compose default). A tree whose target IS
#            llm-engines (a rollback within 2026.11) needs no sweep.
# Phases:
#   connect — before the recreate: attach each engine to its target network
#             (additive: safe while the current router runs, and it holds even
#             if the recreate fails).
#   finish  — after the recreate: once the engine is on its target AND every
#             running caller is there too, detach llm-engines, so the box is as
#             before the upgrade (the next upgrade moves the engine again).
# An engine that cannot be restored WARNs with the manual command, the summary
# claims nothing, and the record stays. A removed engine is skipped. Record
# fields that are not docker names are skipped with a WARN. Never fails the
# rollback.
_rollback_engine_networks() {
    local phase="$1" state=.env.engine-networks.pre-upgrade-backup new=llm-engines
    local fmt='{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
    local re='^[A-Za-z0-9][A-Za-z0-9_.-]*$'
    local key header c net origin nets err run tgt names errf work="" line sorted
    local caller callers_checked=0 callers_ok=1 unverified="" warned_callers=0 failed=0 restored=0
    if [ -f "$state" ]; then
        key="$(read_env_value .env RAZZFAZZ_VERSION 2>/dev/null || true)@$(read_env_value .env RAZZFAZZ_COMMIT 2>/dev/null || true)"
        header=$(head -1 "$state" 2>/dev/null || true)
        if [ "$header" = "from=${key}" ]; then
            while IFS= read -r line; do
                [ -n "$line" ] && work="${work}${line}"$'\t'"record"$'\n'
            done < <(tail -n +2 "$state" 2>/dev/null)
        elif [ "$phase" = connect ]; then
            print_info "The engine network record belongs to another upgrade (${header#from=}) — only engines stranded on ${new} are restored (#2393)."
        fi
    fi
    # Every step below tolerates failure: this runs inside rollback() under
    # `set -eo pipefail`, and an abort here would stop the rollback itself.
    tgt=$(read_env_value .env LLM_WORKER_ENGINE_NETWORK 2>/dev/null || true)
    if [ -z "$tgt" ] && [ -f modules/llm/node-agent/compose.yml ]; then
        tgt=$(sed -n 's/.*LLM_WORKER_ENGINE_NETWORK:-\([^}]*\)}.*/\1/p' modules/llm/node-agent/compose.yml 2>/dev/null | head -1 || true)
    fi
    # a tree without the variable or without the file (2026.08) is pre-D20:
    # its engines lived on the stack network
    [ -n "$tgt" ] || tgt=razzfazz-stack_default
    if [ "$tgt" != "$new" ]; then
        errf=$(mktemp 2>/dev/null) || errf=/dev/null
        if names=$(docker ps -a --filter label=rzfz.role=llm-engine --format '{{.Names}}' 2>"$errf"); then
            for c in $names; do work="${work}${c}"$'\t'"${tgt}"$'\t'"sweep"$'\n'; done
        else
            print_warning "Could not list the LLM engines ($(cat "$errf" 2>/dev/null)) — an engine launched after the upgrade may sit on ${new} only; check with: docker ps --filter label=rzfz.role=llm-engine, then docker network connect ${tgt} <engine> (#2393)" >&2
            failed=1
        fi
        [ "$errf" = /dev/null ] || rm -f "$errf" 2>/dev/null || true
    fi
    # one entry per (engine, network); a recorded entry wins over a swept one
    if sorted=$(printf '%s' "$work" | sort -s -t $'\t' -k1,2 -u 2>/dev/null); then
        work=$sorted
    else   # duplicates are harmless: every step below is idempotent
        print_warning "Could not de-duplicate the engine list — restoring it as it is (#2393)." >&2
    fi
    while IFS=$'\t' read -r -u 3 c net origin; do
        [ -n "$c$net" ] || continue
        if ! [[ "$c" =~ $re ]] || ! [[ "$net" =~ $re ]]; then
            [ "$phase" = finish ] || print_warning "Skipped a record line that names no container/network (${c:0:40} / ${net:0:40}) — check ${state} by hand (#2393)" >&2
            failed=1; continue
        fi
        if ! nets=$(docker inspect -f "$fmt" -- "$c" 2>&1); then
            case "$nets" in *"No such"*) continue ;; esac   # engine gone since: nothing to restore
            print_warning "Engine ${c}: cannot read its networks (${nets}) — restore by hand: docker network connect ${net} ${c} (#2393)" >&2
            failed=1; continue
        fi
        if [ "$origin" = sweep ]; then
            case " $nets " in *" $new "*) ;; *) continue ;; esac   # not on llm-engines: not ours
        fi
        if [ "$phase" = connect ]; then
            case " $nets " in *" $net "*) continue ;; esac
            if ! err=$(docker network connect -- "$net" "$c" 2>&1); then
                print_warning "Engine ${c}: reconnect to ${net} failed (${err}) — restore by hand: docker network connect ${net} ${c} (#2393)" >&2
                failed=1
            fi
            continue
        fi
        case " $nets " in
            *" $net "*) ;;
            *)  print_warning "Engine ${c}: not back on ${net} — the rolled-back router cannot reach it. Restore by hand: docker network connect ${net} ${c} (#2393)" >&2
                failed=1; continue ;;
        esac
        case " $nets " in *" $new "*) ;; *) restored=$((restored + 1)); continue ;; esac
        # every running caller must be on this network before llm-engines goes
        if [ "$callers_checked" != "$net" ]; then
            callers_checked=$net callers_ok=1 unverified=""
            for caller in llm-manager-router llm-manager llm-worker-agent; do
                if ! run=$(docker inspect -f '{{.State.Running}}' -- "$caller" 2>&1); then
                    case "$run" in *"No such"*) continue ;; esac
                    callers_ok=0; unverified="${unverified} ${caller}"; continue
                fi
                [ "$run" = true ] || continue
                if ! err=$(docker inspect -f "$fmt" -- "$caller" 2>/dev/null); then
                    callers_ok=0; unverified="${unverified} ${caller}"; continue
                fi
                case " $err " in *" $net "*) ;; *) callers_ok=0; unverified="${unverified} ${caller}" ;; esac
            done
        fi
        if [ "$callers_ok" -ne 1 ]; then
            [ "$warned_callers" = 1 ] || print_warning "Not verified on ${net}:${unverified} — the engines keep ${new} as well (#2393)." >&2
            warned_callers=1; failed=1; continue
        fi
        if ! err=$(docker network disconnect -- "$new" "$c" 2>&1); then
            print_warning "Engine ${c}: still on ${new} as well (${err}) — reachable; detach later with: docker network disconnect ${new} ${c} (#2393)" >&2
            failed=1; continue
        fi
        restored=$((restored + 1))
    done 3<<< "$work"
    [ "$phase" = finish ] || return 0
    if [ "$failed" -eq 0 ]; then
        rm -f "$state" 2>/dev/null || true
        [ "$restored" -eq 0 ] || print_substep "${restored} engine(s) back on their pre-upgrade network (#2393)."
    else
        print_warning "Not every LLM engine could be restored — see the lines above; the record stays in ${state} (#2393)." >&2
    fi
    return 0
}

# ==============================================================================
# Post-Upgrade Verification
# ==============================================================================
# #148: After an Authentik version bump, sessions serialized by the OLD version
# fail to deserialize against the new model registry → LookupError 500 on every
# /application/o/authorize ("Server Error"). Flushing the session tables
# (everyone re-logs-in — harmless, no data loss) prevents the whole class. Gated
# on an actual AUTHENTIK_VERSION change so steady-state upgrades don't log users
# out for nothing. Per-table truncate so a missing table never blocks the rest.
flush_authentik_sessions_on_version_change() {
    local new_ver
    new_ver="$(read_env_value ".env" "AUTHENTIK_VERSION")"
    if [ -z "${OLD_AUTHENTIK_VERSION:-}" ] || [ "${OLD_AUTHENTIK_VERSION}" = "${new_ver}" ]; then
        return 0
    fi
    print_step "Authentik ${OLD_AUTHENTIK_VERSION} → ${new_ver}: flushing stale sessions (#148)..."
    journal_event "remediation" "info" "authentik ${OLD_AUTHENTIK_VERSION}->${new_ver}: flush sessions"
    local pg_user pg_pass auth_db i
    pg_user="$(read_env_value ".env" "POSTGRES_USER")"; [ -z "$pg_user" ] && pg_user="docker"
    pg_pass="$(read_env_value ".env" "POSTGRES_PASSWORD")"
    auth_db="$(read_env_value ".env" "AUTHENTIK_DB")"; [ -z "$auth_db" ] && auth_db="authentik_db"
    for i in $(seq 1 30); do
        docker exec postgres pg_isready -U "$pg_user" >/dev/null 2>&1 && break
        sleep 2
    done
    local t ok=true
    for t in authentik_core_session authentik_core_authenticatedsession \
             authentik_providers_proxy_proxysession authentik_providers_saml_samlsession django_session; do
        PGPASSWORD="$pg_pass" docker exec -e PGPASSWORD postgres psql -U "$pg_user" -d "$auth_db" \
            -c "TRUNCATE ${t} CASCADE;" >/dev/null 2>&1 || ok=false
    done
    if [ "$ok" = true ]; then
        print_success "Authentik sessions flushed (users re-login on next visit)."
        journal_event "remediation" "ok" "authentik sessions flushed"
    else
        print_warning "Some Authentik session tables could not be flushed (non-fatal)."
        journal_event "remediation" "warn" "authentik session flush partial"
    fi
}

# #145/#147: reconcile the embedded-outpost provider bindings from the HOST after
# restart. init-authentik.sh attempts this from inside the bare-alpine init
# container, which fails on a restricted-network box (runtime apk can't fetch
# docker-cli) → outpost unbound → gated apps 500/404 (the MEGAS/Chris case). The
# host ALWAYS has docker, so reconcile here too; idempotent (the bindings script
# no-ops when a provider is already attached). The #143-B diagnose-gate then
# confirms attached==total.
reconcile_outpost_bindings_hostside() {
    [ -f "core/Authentik/apply-policy-bindings.py" ] || return 0
    docker ps --format '{{.Names}}' | grep -x authentik-worker >/dev/null || return 0
    # authentik-worker needs its Django ORM ready; restart_stack already waited
    # for stack health, but give the worker a short settle window.
    local i
    for i in $(seq 1 24); do
        docker exec authentik-worker python -c "import django" >/dev/null 2>&1 && break
        sleep 5
    done

    if ! docker cp core/Authentik/apply-policy-bindings.py authentik-worker:/tmp/apply_policy_bindings.py 2>/dev/null; then
        print_warning "Could not stage apply-policy-bindings.py in authentik-worker — outpost reconcile skipped."
        journal_event "remediation" "warn" "host-side outpost reconcile: docker cp failed"
        return 0
    fi

    print_step "Reconciling Authentik outpost bindings from host (#145)..."
    journal_event "remediation" "info" "host-side outpost binding reconcile"

    # #175: a single apply-policy-bindings.py pass can lose the race against
    # Authentik's own async outpost_controller task — the script's OWN
    # "verify-and-reattach" loop (core/Authentik/apply-policy-bindings.py)
    # documents this exact drift. Depending on restart luck for that race to
    # resolve is what forced several full stack restarts on the Care
    # Solutions big-bang upgrade before every outpost/provider binding
    # stuck. Poll the actual bound-state — reusing apply-policy-bindings.py's
    # own Outpost/ProxyProvider ORM query via _outpost_bindings_green rather
    # than a second client — and re-invoke the script until it reports
    # green. Bounded: a persistently broken box still ends the upgrade with
    # a WARN (the #143-B diagnose-gate flags it downstream), never an abort
    # — this is a reconcile step, not a hard gate.
    local max_attempts=5 attempt=0 green=false
    while [ "$attempt" -lt "$max_attempts" ]; do
        attempt=$((attempt + 1))
        # #1465: the script skips apps of inactive profiles only when told which are active.
        # #2507: and whether the native-OIDC applications are expected.
        docker exec -e COMPOSE_PROFILES="$(read_env_value ".env" "COMPOSE_PROFILES" 2>/dev/null || true)" \
            -e ENABLE_OPENWEBUI_OIDC="$(read_env_value ".env" "ENABLE_OPENWEBUI_OIDC" 2>/dev/null || true)" \
            -e ENABLE_GITEA_AUTHENTIK_OIDC="$(read_env_value ".env" "ENABLE_GITEA_AUTHENTIK_OIDC" 2>/dev/null || true)" \
            -e RAZZFAZZ_REQUIRE_ADMIN_MFA="$(read_env_value ".env" "RAZZFAZZ_REQUIRE_ADMIN_MFA" 2>/dev/null || true)" \
            -e RAZZFAZZ_ACCESS_GROUP="$(read_env_value ".env" "RAZZFAZZ_ACCESS_GROUP" 2>/dev/null || true)" \
            authentik-worker python /tmp/apply_policy_bindings.py >/dev/null 2>&1 || true
        if _outpost_bindings_green; then
            green=true
            break
        fi
        if [ "$attempt" -lt "$max_attempts" ]; then
            print_substep "Outpost bindings not fully attached yet (attempt ${attempt}/${max_attempts}) — retrying..."
            sleep 10
        fi
    done

    if [ "$green" = true ]; then
        print_success "Outpost bindings reconciled (host-side, attempt ${attempt}/${max_attempts})."
        journal_event "remediation" "ok" "outpost bindings reconciled (host, attempt=${attempt})"
    else
        print_warning "Outpost bindings still not fully attached after ${max_attempts} attempts — the diagnose-gate will flag if still unbound."
        journal_event "remediation" "warn" "host-side outpost reconcile: not green after ${max_attempts} attempts"
    fi
}

# #175: "green" means exactly what apply-policy-bindings.py itself means by
# "attached" — every ProxyProvider present on the embedded outpost. Reusing
# its own model query (rather than standing up a second Authentik client)
# keeps this check from ever disagreeing with what the apply script did.
# Guards the `docker exec` substitution explicitly (bare `x=$(cmd)` combined
# with `local` would swallow a non-zero exit under this file's `set -eo
# pipefail`, #158/#258 precedent).
_outpost_bindings_green() {
    local state=""
    # #1465: the model imports below need a configured Django — without
    # DJANGO_SETTINGS_MODULE + django.setup() the snippet died with
    # ImproperlyConfigured (rc 1, stderr discarded), `state` stayed empty and
    # the reconcile was NEVER green: every box ran all attempts and ended in
    # the WARN (0.91, 2026-09-05: with setup the same query says 32/32 green).
    state="$(docker exec authentik-worker python -c '
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()
from authentik.outposts.models import Outpost
from authentik.providers.proxy.models import ProxyProvider
outpost = Outpost.objects.filter(name="authentik Embedded Outpost").first()
if not outpost:
    print("no-outpost")
else:
    total = ProxyProvider.objects.count()
    attached = outpost.providers.count()
    print("green" if attached >= total else f"red {attached}/{total}")
' 2>/dev/null)" || return 1
    [ "$state" = "green" ]
}

# #539 follow-up (PR #540 review R1; confirmed live on an already-installed
# customer box 2026-08-21): the auto_prune disable ran ONLY in post-install,
# which an upgrade never re-runs — so every already-installed monitor-profile
# box kept Komodo's nightly `docker image prune -a -f` (00:00 UTC), deleting
# disabled modules' custom images; unrecoverable on an offline box. Reconcile
# it on every upgrade. Best-effort: a warning beats a failed upgrade, and the
# helper re-verifies the flag server-side, so the next upgrade re-checks.
# #659: every NEW employee's first SSO login 403'd — the ga.1 signup
# hardening closed ENABLE_OAUTH_SIGNUP along with the native form, and the
# init-time derivation never re-runs on upgrades (the #649 SSO-flag repair
# can even flip ENABLE_OPENWEBUI_OIDC to true in THIS upgrade, so this must
# run after migrate_env). Conditional, hence a reconcile and not a
# change_default: signup stays closed on non-SSO boxes where that is the
# correct posture. Safe on SSO boxes: the OAuth callback is reachable only
# AFTER an Authentik forward_auth login + the IdP's domain binding.
# #908 follow-up — post-upgrade OWUI retrieval-defaults reconcile.
#
# Live on 0.91 (2026-09-02): container env ENABLE_RETRIEVAL_QUERY_GENERATION=false
# was correct, but the DB row task.query.retrieval.enable stayed true — the
# defaults push (merged 01.09) landed AFTER the box's last full post-install,
# and `rzfz upgrade` reaches that push only through `post-install --refresh`,
# a whole provisioning run that can die or skip earlier (a failed dify init,
# a manager hiccup) without ever getting to OWUI. Every box whose last full
# post-install predates the push kept the LLM query rewrite ON, and every
# "fix" so far was a per-box hotfix of the same gap.
#
# This step is the push alone, run AFTER the stack is verified healthy:
# idempotent GET -> merge -> POST of _owui_retrieval_defaults (the ONE table,
# scripts/lib-owui.sh) + read-back verdict. No model deploy. Chat-gated.
# Best-effort for the upgrade (never fails it), but LOUD: when it cannot run
# the operator sees a WARN naming the reason, instead of a silently-skipped
# --refresh step. Skipped in --check (no real upgrade).
reconcile_owui_retrieval_defaults_after_upgrade() {
    local env_file="${1:-.env}" profiles
    profiles=$(read_env_value "$env_file" COMPOSE_PROFILES 2>/dev/null) || profiles=""
    echo "$profiles" | tr ',' '\n' | grep -qx "chat" || return 0
    print_step "Step 10b: Re-asserting Open WebUI retrieval defaults (#908)..."
    if ENV_FILE="$env_file" owui_reconcile_retrieval_defaults; then
        return 0
    fi
    print_warning "OWUI retrieval-defaults reconcile could NOT run (see above) — the LLM query rewrite and chunking defaults may still be at their pre-#908 values on this box."
    print_info    "  Re-run once chat + llm-manager are up:  rzfz post-install --refresh"
    return 0
}

# #2507: the SSO keys the fresh path DERIVES in init.sh, re-derived on every
# upgrade — BEFORE restart_stack, whose unconditional --force-recreate then
# carries them into openwebui/gitea without a second recreate. Prod needed both
# by hand on 2026-09-26: GITEA_OAUTH_AUTO_REGISTER=true appended to .env, and a
# COMPOSE_FILE overlay giving openwebui empty Google keys + OAUTH_AUTO_REDIRECT.
#   * derive_owui_sso_env (scripts/lib.sh): OWUI_GOOGLE_CLIENT_ID/SECRET and
#     OWUI_OAUTH_AUTO_REDIRECT from ENABLE_OPENWEBUI_OIDC.
#   * GITEA_OAUTH_AUTO_REGISTER: raised to true when Google OAuth or Gitea's
#     Authentik OIDC is on — never LOWERED (an operator may have opened it on
#     a box without either), the same one-way shape as reconcile_owui_oauth_signup.
reconcile_sso_env_derivations() {
    local env_file="${1:-.env}" google gitea_oidc auto_register
    [ -f "$env_file" ] || return 0
    print_substep "Deriving SSO login keys (#2507)..."
    derive_owui_sso_env "$env_file"
    # Compose reads the process environment before .env (#1486 class): keep any
    # exported copy of the derived keys equal to the file for the recreate below.
    reexport_owui_sso_env "$env_file"
    google=$(read_env_value "$env_file" ENABLE_GOOGLE_OAUTH 2>/dev/null || true)
    gitea_oidc=$(read_env_value "$env_file" ENABLE_GITEA_AUTHENTIK_OIDC 2>/dev/null || true)
    auto_register=$(read_env_value "$env_file" GITEA_OAUTH_AUTO_REGISTER 2>/dev/null || true)
    if { [ "$google" = "true" ] || [ "$gitea_oidc" = "true" ]; } && [ "$auto_register" != "true" ]; then
        update_env_value "$env_file" "GITEA_OAUTH_AUTO_REGISTER" "true"
        print_substep "  GITEA_OAUTH_AUTO_REGISTER=true (Google=${google:-false}, Gitea-OIDC=${gitea_oidc:-false})"
    fi
    return 0
}

reconcile_owui_oauth_signup() {
    local env_file="${1:-.env}"
    local google oidc signup role changed=false
    google=$(read_env_value "$env_file" ENABLE_GOOGLE_OAUTH 2>/dev/null || true)
    oidc=$(read_env_value "$env_file" ENABLE_OPENWEBUI_OIDC 2>/dev/null || true)
    [ "$google" = "true" ] || [ "$oidc" = "true" ] || return 0
    signup=$(read_env_value "$env_file" ENABLE_OAUTH_SIGNUP 2>/dev/null || true)
    role=$(read_env_value "$env_file" OWUI_DEFAULT_USER_ROLE 2>/dev/null || true)
    if [ "$signup" != "true" ]; then
        update_env_value "$env_file" "ENABLE_OAUTH_SIGNUP" "true"
        changed=true
    fi
    if [ -z "$role" ]; then
        update_env_value "$env_file" "OWUI_DEFAULT_USER_ROLE" "user"
        changed=true
    fi
    if [ "$changed" = true ]; then
        # The .env write alone is INERT on an already-initialised box. This
        # repo's own model of OWUI is that a persisted config row overrides
        # compose env on every later boot (modules/chat/compose.yml; the whole
        # #881 writeup above). DEFAULT_USER_ROLE is a PersistentConfig under
        # `ui.default_user_role` — NOT under `oauth.*` — so the #881 reset
        # (`DELETE ... WHERE key = 'oauth' OR key LIKE 'oauth.%'`) never
        # cleared it, and neither does anything else. On any box where OWUI had
        # persisted that row, the new employee still landed as `pending` and
        # still got the 403 this fix claims to repair, while the upgrade
        # printed "default role 'user' (first-login 403 repair)" — a false
        # success (#427 honest-signals). Same trap catches
        # ENABLE_OAUTH_SIGNUP (`oauth.enable_signup`) on a box already on
        # 0.11.x, because the oauth.* delete is keyed to the one-time
        # 0.10 -> 0.11 crossing.
        _owui_drop_persisted_signup_overrides
        print_substep "#659: SSO box — OAuth signup enabled + default role 'user' (first-login 403 repair); recreating openwebui."
        docker compose up -d --force-recreate openwebui >/dev/null 2>&1 || true
    fi
}

# Remove the two persisted OWUI config entries that would otherwise override
# what reconcile_owui_oauth_signup just wrote to .env. Best-effort and
# non-fatal throughout (an upgrade must not fail here), and deliberately
# NARROW: only `ui.default_user_role` and `oauth.enable_signup`, never a
# blanket wipe of the `ui` subtree — an operator's other UI settings are not
# ours to reset. Handles both schemas, the same detection
# reconcile_owui_oauth_persistentconfig uses:
#   * 0.10.x/0.11.x — one row per dotted key (`key`, `value` json);
#   * <=0.9.x legacy — a single row whose `data` json holds the whole tree.
_owui_drop_persisted_signup_overrides() {
    command -v docker >/dev/null 2>&1 || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x postgres >/dev/null || return 0

    local pg_user owui_db data_col value_col
    pg_user=$(grep -m1 '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2 || echo "docker")
    pg_user="${pg_user:-docker}"
    owui_db=$(read_env_value .env OPENWEBUI_DB 2>/dev/null || true)
    owui_db="${owui_db:-openwebui_db}"

    data_col=$(docker exec postgres psql -U "$pg_user" -d "$owui_db" -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='config' AND column_name='data';" \
        2>/dev/null | tr -d '[:space:]')
    value_col=$(docker exec postgres psql -U "$pg_user" -d "$owui_db" -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='config' AND column_name='value';" \
        2>/dev/null | tr -d '[:space:]')

    if [ "$value_col" = "json" ] || [ "$value_col" = "jsonb" ]; then
        # Flattened leaves, plus the nested-subtree spelling of the same two
        # settings if this box carries `ui` / `oauth` as whole-blob rows.
        if docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pg_user" -d "$owui_db" <<'SQL' >/dev/null 2>&1
DELETE FROM config WHERE key IN ('ui.default_user_role', 'oauth.enable_signup');
UPDATE config SET value = (value::jsonb #- '{default_user_role}')::json
 WHERE key = 'ui' AND value::jsonb ? 'default_user_role';
UPDATE config SET value = (value::jsonb #- '{enable_signup}')::json
 WHERE key = 'oauth' AND value::jsonb ? 'enable_signup';
SQL
        then
            print_substep "OWUI persisted ui.default_user_role / oauth.enable_signup cleared — the .env values now take effect on recreate (#659)."
        else
            print_warning "#659: could not clear the persisted OWUI ui.default_user_role / oauth.enable_signup rows — the .env change may be overridden on next boot; check Admin → Settings if first-login 403s persist."
        fi
    elif [ "$data_col" = "json" ] || [ "$data_col" = "jsonb" ]; then
        if docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pg_user" -d "$owui_db" \
                <<< "UPDATE config SET data = (data::jsonb #- '{ui,default_user_role}' #- '{oauth,enable_signup}')::${data_col} WHERE data::jsonb #> '{ui}' ? 'default_user_role' OR data::jsonb #> '{oauth}' ? 'enable_signup';" \
                >/dev/null 2>&1; then
            print_substep "OWUI persisted ui.default_user_role / oauth.enable_signup cleared (legacy config.data) (#659)."
        else
            print_warning "#659: could not clear the persisted OWUI defaults from config.data — the .env change may be overridden on next boot."
        fi
    else
        print_warning "#659: OWUI config table schema not recognised — cannot tell whether a persisted ui.default_user_role still overrides the .env value."
    fi
    return 0
}

# #881: OWUI native OIDC login 500 on a 0.10.x -> 0.11.x upgrade — the
# "PersistentConfig trap" (HYPOTHESIS, unconfirmed by a live traceback; see
# run_data_migrations()'s 0.11-crossing block above for the full writeup).
# Mirrors core/config/app/services/apply_manager.py::_reconcile_openwebui_domain
# (schema-detect the config json column, THEN feed the mutating SQL on
# stdin — psql `:'var'`/literal interpolation only happens for SQL read from
# stdin or a file, never for `-c`) and post-install.sh's own schema-detect
# pattern for the SAME `config` table (_owui_fix_persisted_gpustack_key,
# ~L4326). Best-effort / non-fatal throughout: a failed reconcile must not
# fail the upgrade — worst case OWUI keeps 500ing until a manual retry.
reconcile_owui_oauth_persistentconfig() {
    local pg_user pg_pass owui_db
    pg_user=$(grep -m1 '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2 || echo "docker")
    pg_pass=$(read_env_value .env POSTGRES_PASSWORD)
    owui_db=$(read_env_value .env OPENWEBUI_DB)
    owui_db="${owui_db:-openwebui_db}"

    # OWUI's `config` table schema (verified against upstream
    # backend/open_webui/models/config.py at v0.10.2 AND v0.11.0 — identical
    # between the two, so this detection is schema-safe across the crossing):
    #   • <=0.9.x legacy peewee schema — single row, `data` json/jsonb column
    #     holding the WHOLE config tree nested (oauth is a top-level key).
    #   • 0.10.x/0.11.x current schema — one row PER dotted config key:
    #     `key text primary key, value json`; every oauth setting is its own
    #     row keyed `oauth` or `oauth.<path>` (e.g. `oauth.oidc.enable`,
    #     `oauth.google.client_id`).
    local data_col value_col
    data_col=$(docker exec postgres psql -U "$pg_user" -d "$owui_db" -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='config' AND column_name='data';" \
        2>/dev/null | tr -d '[:space:]')
    value_col=$(docker exec postgres psql -U "$pg_user" -d "$owui_db" -tAc \
        "SELECT data_type FROM information_schema.columns WHERE table_name='config' AND column_name='value';" \
        2>/dev/null | tr -d '[:space:]')

    if [ "$value_col" = "json" ] || [ "$value_col" = "jsonb" ]; then
        # Per-key schema: drop the oauth row + every flattened oauth.* leaf.
        # Targets ONLY oauth keys — no blanket wipe of the config table.
        if docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pg_user" -d "$owui_db" \
                <<< "DELETE FROM config WHERE key = 'oauth' OR key LIKE 'oauth.%';" \
                > /dev/null 2>&1; then
            print_substep "OWUI persisted oauth.* config rows removed (config.value) — will re-seed from env on next boot (#881)."
        else
            print_warning "OWUI oauth reconcile: DELETE returned non-zero (non-fatal, #881)."
        fi
    elif [ "$data_col" = "json" ] || [ "$data_col" = "jsonb" ]; then
        # Legacy single-row schema: strip the top-level 'oauth' key only, and
        # only from rows that actually carry it (WHERE ... ? 'oauth') —
        # idempotent, and no other config is touched.
        if docker exec -i postgres psql -v ON_ERROR_STOP=1 -U "$pg_user" -d "$owui_db" \
                <<< "UPDATE config SET data = (data::jsonb - 'oauth')::${data_col} WHERE data::jsonb ? 'oauth';" \
                > /dev/null 2>&1; then
            print_substep "OWUI persisted oauth subtree removed (config.data) — will re-seed from env on next boot (#881)."
        else
            print_warning "OWUI oauth reconcile: UPDATE returned non-zero (non-fatal, #881)."
        fi
    else
        print_warning "OWUI oauth reconcile: no config json column found (neither 'data' nor 'value') — skipping (#881)."
        return 0
    fi

    # restart_stack already brought openwebui up earlier in THIS upgrade — it
    # loaded the stale oauth rows at that boot. Force a recreate so it
    # re-reads config fresh with the oauth rows gone, same as the
    # domain/RAG/signup reconciles elsewhere in this file. Best-effort: a
    # failed recreate just means the fix lands on OWUI's next normal restart
    # instead of instantly.
    docker compose up -d --force-recreate openwebui >/dev/null 2>&1 || true
}

reconcile_komodo_auto_prune() {
    local env_file="${SCRIPT_DIR}/.env" profiles
    profiles="$(read_env_value "$env_file" COMPOSE_PROFILES)"
    printf '%s' "$profiles" | grep -qw "monitor" || return 0
    print_step "Komodo: disabling nightly image auto-prune..."
    local komodo_out="" komodo_rc=0
    local KOMODO_INIT_ADMIN_USERNAME KOMODO_INIT_ADMIN_PASSWORD KOMODO_PORT KOMODO_API_KEY KOMODO_API_SECRET POSTGRES_USER POSTGRES_PASSWORD
    KOMODO_INIT_ADMIN_USERNAME="$(read_env_value "$env_file" KOMODO_INIT_ADMIN_USERNAME)"
    KOMODO_INIT_ADMIN_PASSWORD="$(read_env_value "$env_file" KOMODO_INIT_ADMIN_PASSWORD)"
    KOMODO_PORT="$(read_env_value "$env_file" KOMODO_PORT)"
    POSTGRES_USER="$(read_env_value "$env_file" POSTGRES_USER)"
    POSTGRES_PASSWORD="$(read_env_value "$env_file" POSTGRES_PASSWORD)"
    # #2566: the stack's own API key, so a password changed in the Komodo UI
    # no longer strands this step. Minted while the .env password still works.
    razzfazz_komodo_ensure_api_key "$env_file" 2>&1 | while IFS= read -r line; do print_substep "$line"; done || true
    KOMODO_API_KEY="$(read_env_value "$env_file" KOMODO_API_KEY)"
    KOMODO_API_SECRET="$(read_env_value "$env_file" KOMODO_API_SECRET)"
    komodo_out="$(razzfazz_komodo_disable_auto_prune 2>&1)" || komodo_rc=$?
    [ -n "$komodo_out" ] && printf '%s\n' "$komodo_out" \
        | while IFS= read -r line; do print_substep "$line"; done
    if [ "$komodo_rc" -eq 0 ]; then
        print_success "Komodo: auto_prune disabled (disabled modules' custom images are safe)."
        journal_event "remediation" "ok" "komodo auto_prune disabled"
    else
        # #2566: an error, not a warning — `docker image prune -a` runs tonight.
        print_error "Komodo: auto_prune could NOT be turned off — its nightly 'docker image prune -a' will delete images this stack needs. 'rzfz status' shows the live setting; turn it off in Komodo (Servers → Local → Auto Prune) or re-run 'rzfz post-install --refresh'."
        journal_event "remediation" "fail" "komodo auto_prune reconcile failed"
    fi
}

# #142 (lightweight upgrade observability): when an upgrade hits a problem,
# auto-produce a diagnostic log bundle and ask the operator to email it to
# support. Reuses razzfazz-logs.sh --standalone (#141, container-independent —
# works even when the stack is down) + the structured journal (#143).
# Best-effort + once-per-run: log collection must never mask the real failure.
_UPGRADE_LOGZIP_DONE=""
collect_failure_logzip() {
    local reason="${1:-upgrade-failure}"
    [ -n "$_UPGRADE_LOGZIP_DONE" ] && return 0
    _UPGRADE_LOGZIP_DONE=1
    local zip=""
    if [ -x "${SCRIPT_DIR}/rzfz" ]; then
        zip="$("${SCRIPT_DIR}/rzfz" logs take --standalone "$reason" 2>/dev/null | tail -1)"
    fi
    echo ""
    print_warning "A diagnostic log bundle was created for this upgrade:"
    if [ -n "$zip" ] && [ -f "$zip" ]; then
        echo "    $zip  ($(du -h "$zip" 2>/dev/null | cut -f1))"
    else
        echo "    (automatic collection unavailable — run: rzfz logs take --standalone)"
    fi
    echo "    Please email this file to your razzfazz.ai support contact so the"
    echo "    upgrade outcome can be diagnosed."
    echo ""
}

# #1463: one line per container that logged a critical pattern in its last 100
# lines: `<name>|<hits>|<state>|critical|benign`. "critical" iff the container
# is Restarting, unhealthy, or Exited with a non-zero code — the same pattern
# in a healthy container is not an upgrade issue.
_critical_log_offenders() {
    local line name status hits verdict
    docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | while IFS='|' read -r name status; do
        [ -n "$name" ] || continue
        hits=$(docker logs --tail=100 "$name" 2>&1 | grep -ciE 'FATAL|panic:|Traceback \(most recent' || true)
        [ "${hits:-0}" -gt 0 ] || continue
        case "$status" in
            *Restarting*|*unhealthy*|"Exited (0)"*) verdict=benign ;;
            Exited*) verdict=critical ;;
            *) verdict=benign ;;
        esac
        case "$status" in *Restarting*|*unhealthy*) verdict=critical ;; esac
        printf '%s|%s|%s|%s\n' "$name" "$hits" "$status" "$verdict"
    done
}

# #3265: the first half, before code_update_git resolves its target (see there).
prune_dev_tags_before_fetch() {
    [ "$(razzfazz_channel)" = public ] && [ "${DRY_RUN:-false}" != true ] || return 0
    local _pr
    if ! _pr=$(razzfazz_prune_dev_history "$SCRIPT_DIR" tags); then
        print_error "Could not remove the development tags from this checkout. Nothing was fetched."
        exit 1
    fi
    [ "$_pr" = "removed 0 development ref(s)" ] \
        || print_substep "Public channel: ${_pr} from git.razzfazz.ai (the public tags are fetched next)"
    return 0
}

# #3265 (operator 2026-11-16): after a successful upgrade, a public-channel box
# keeps no development history from git.razzfazz.ai: every ref whose commit is
# not public (no PUBLIC_EXPORT_OF stamp) is deleted, reflogs expired, then
# gc --prune=now (razzfazz_prune_dev_history). A stash pins development
# commits; each one is saved as a patch first (the operator's own edits).
# A checkout that IS a development commit (an offline-package upgrade of a box
# that was on the internal channel) is all development history: its .git goes;
# such a box upgrades by package, or online after a fresh clone of the public
# mirror. Only after a verified upgrade (a rollback still needs the old commit).
_save_stash_patches() {
    local dir="${HOME}/.razzfazz/stash-patches" ts ref
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    while IFS= read -r ref; do
        [ -n "$ref" ] || continue
        mkdir -p "$dir" && chmod 700 "$dir"
        git -C "$SCRIPT_DIR" stash show -p --include-untracked "$ref" > "$dir/${ts}-${ref//[^0-9]/}.patch" 2>/dev/null \
            || git -C "$SCRIPT_DIR" stash show -p "$ref" > "$dir/${ts}-${ref//[^0-9]/}.patch" 2>/dev/null || true
        print_substep "  stash ${ref} saved as ${dir}/${ts}-${ref//[^0-9]/}.patch"
    done < <(git -C "$SCRIPT_DIR" stash list --format=%gd 2>/dev/null)
}

prune_dev_history_after_upgrade() {
    [ "$(razzfazz_channel)" = public ] || return 0
    [ -d "${SCRIPT_DIR}/.git" ] || return 0
    local said out rc=0 line
    said=$(razzfazz_prune_dev_history "$SCRIPT_DIR" check) && return 0
    if [ "${DRY_RUN:-false}" = true ]; then
        print_info "[check] WOULD remove the development history from this checkout (${said})."
        return 0
    fi
    print_step "Removing development history (public channel: nothing from git.razzfazz.ai stays on this box)..."
    _save_stash_patches
    if ! razzfazz_public_commit "$SCRIPT_DIR" HEAD; then
        rm -rf "${SCRIPT_DIR}/.git"
        print_warning "This checkout was a development commit (${said}): its git history was removed."
        print_info "Upgrades: rzfz upgrade --package <file>, or online after a fresh clone of the public mirror (${RAZZFAZZ_PUBLIC_REMOTE})."
        return 0
    fi
    out=$(razzfazz_prune_dev_history "$SCRIPT_DIR" all) || rc=$?
    while IFS= read -r line; do [ -n "$line" ] && print_substep "$line"; done <<< "$out"
    if [ "$rc" -ne 0 ]; then
        print_warning "The development history could not be removed completely (rc ${rc}). The next rzfz upgrade tries again; rzfz status shows it."
    fi
    return 0
}

verify_upgrade() {
    print_step "Verifying upgrade..."

    local issues=0

    # #143 A: explicit installed->target version event (the .env still holds the
    # pre-update RAZZFAZZ_VERSION at this point; it's rewritten below).
    journal_event "versions" "info" \
        "installed=$(read_env_value ".env" "RAZZFAZZ_VERSION") target=${TARGET_VERSION:-?} commit=${TARGET_COMMIT:-?}"

    # Check for restart loops
    local restarting
    restarting=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | \
        grep -c "Restarting" || true)
    if [ "$restarting" -gt 0 ]; then
        print_error "Restart loops detected in ${restarting} container(s):"
        docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep "Restarting"
        ((issues++)) || true
    fi

    # Check for critical log errors — #1463: judged PER CONTAINER, paired with
    # its state. A traceback in a container that is Restarting / unhealthy /
    # Exited(≠0) is an issue; the same line in a healthy container is
    # information (searxng logs engine tracebacks by design — that ended every
    # upgrade on a searxng box "with issues" + a support-bundle prompt).
    local _off critical_containers="" benign_containers="" critical_hits=0 benign_hits=0
    while IFS='|' read -r _cname _ccount _cstate _ccrit; do
        [ -n "$_cname" ] || continue
        if [ "$_ccrit" = "critical" ]; then
            critical_containers="${critical_containers}${_cname}(${_ccount}, ${_cstate}) "
            critical_hits=$((critical_hits + _ccount))
        else
            benign_containers="${benign_containers}${_cname}(${_ccount}) "
            benign_hits=$((benign_hits + _ccount))
        fi
    done < <(_critical_log_offenders)
    if [ "$critical_hits" -gt 0 ]; then
        print_warning "Found ${critical_hits} critical log entries in sick container(s): ${critical_containers}— check their logs."
        ((issues++)) || true
    fi
    if [ "$benign_hits" -gt 0 ]; then
        print_info "${benign_hits} log traceback(s) in healthy container(s): ${benign_containers}— not counted as an issue (#1463)."
    fi

    # Check expected containers are running
    local expected_count
    expected_count=$(docker compose config --services 2>/dev/null | wc -l)
    local running_count
    running_count=$(docker compose ps --status running --format '{{.Name}}' 2>/dev/null | wc -l)
    print_substep "Running: ${running_count} / ${expected_count} services"

    # #2873: the FINAL health gate. A compose up that aborted (tolerated above,
    # #153, so the later steps still run) leaves services in Created; the version
    # stamp must not say "done" over a stack that is down. On a failed gate the
    # stamp and RAZZFAZZ_UPGRADE_IN_PROGRESS stay as they are (the honest "run
    # unfinished" signal, #1593) and the script exits non-zero at the end.
    if razzfazz_final_health_gate; then
        # #2359: the development commit this tree was cut from. On a public-mirror
        # box it comes from the export's PUBLIC_EXPORT_OF stamp (the mirror sha in
        # TARGET_COMMIT exists in no internal history); without a stamp it equals
        # TARGET_COMMIT — passed as the fallback so an offline package, whose rsync
        # leaves .git at the PRE-upgrade commit (#272), never reads a stale HEAD.
        # Written BEFORE the version stamp: the stamp and the in-progress marker
        # below must stay adjacent (#1593, guarded by test_1587).
        if _source_commit="$(razzfazz_source_commit "$SCRIPT_DIR" "$TARGET_COMMIT")"; then
            update_env_value ".env" "RAZZFAZZ_SOURCE_COMMIT" "$_source_commit"
        fi
        # Update version tracking in .env
        update_env_value ".env" "RAZZFAZZ_VERSION" "$TARGET_VERSION"
        update_env_value ".env" "RAZZFAZZ_COMMIT" "$TARGET_COMMIT"
        # #1593: the run is complete — stamp and marker are written together, so
        # the pair can never say "finished" and "in progress" at the same time.
        update_env_value ".env" "RAZZFAZZ_UPGRADE_IN_PROGRESS" ""
        print_substep "Version tracking updated: ${TARGET_VERSION} (${TARGET_COMMIT})"
    else
        UPGRADE_HEALTH_GATE_FAILED=true
        issues=$((issues + 1))
        print_error "Version tracking NOT updated: still ${TARGET_VERSION:-?} in progress (RAZZFAZZ_UPGRADE_IN_PROGRESS kept)."
    fi

    # STACK_HOST_PATH (rc5+): set if missing or stale. Required for the
    # razzfazz-config bind-mount to match the host path the docker daemon
    # sees — see core/compose.yml razzfazz-config comments. Idempotent:
    # only writes when the value differs from the current $(realpath .).
    local current_host_path
    current_host_path=$(read_env_value ".env" "STACK_HOST_PATH")
    local actual_host_path
    actual_host_path=$(realpath .)
    if [ "$current_host_path" != "$actual_host_path" ]; then
        update_env_value ".env" "STACK_HOST_PATH" "$actual_host_path"
        print_substep "Stack host path updated: ${actual_host_path}"
    fi


    # Governance checksum snapshot (#22: runs on the host now).
    if RAZZFAZZ_STACK_ROOT="$SCRIPT_DIR" python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" \
            --checksum-take "Post-upgrade to v${TARGET_VERSION}" 2>/dev/null; then
        print_substep "Governance checksum snapshot created."
    else
        print_warning "Could not create checksum snapshot."
    fi

    # BSB-06 — deploy-time per-app group-binding lint. Upgrades can ship
    # binding-script typos that pass blueprint apply but break SSO at
    # customer login (97f7e2c0 was caught by a customer post-release).
    # Read-only; counts as a verify "issue" if the lint reports a
    # mismatch — surfaces in the verify-issue summary so the operator
    # sees it on the same screen as restart loops / FATAL log entries.
    if [ -x "${SCRIPT_DIR}/scripts/post-install-group-lint.sh" ]; then
        print_substep "BSB-06 group-binding lint..."
        if ! "${SCRIPT_DIR}/scripts/post-install-group-lint.sh"; then
            print_warning "Group-binding lint reported issues — review the output above."
            ((issues++)) || true
        fi
    fi

    # #143 B: post-upgrade diagnose-gate. Consistency asserts (outpost provider
    # bindings — the MEGAS/Chris 500/404 class; container health) + reuse of the
    # read-only razzfazz-post-install.sh --verify. Writes each finding to the
    # structured journal (inherits the open $RAZZFAZZ_JOURNAL_JSON) and adds its
    # problem count to the verify issues. `|| diag_problems=$?` keeps set -e
    # from aborting on a non-zero (= problems-found) exit.
    if [ -x "${SCRIPT_DIR}/scripts/upgrade-diagnose.sh" ]; then
        local diag_problems=0
        "${SCRIPT_DIR}/scripts/upgrade-diagnose.sh" || diag_problems=$?
        if [ "${diag_problems:-0}" -gt 0 ]; then
            print_warning "Diagnose-gate flagged ${diag_problems} consistency problem(s) — see journal."
            issues=$((issues + diag_problems))
        fi
    fi

    # #2403: a marker still behind the tree or a service left in `created` is
    # an upgrade issue, counted here so the final verdict cannot say "passed".
    if [ "${AUTHENTIK_INIT_ISSUES:-0}" -gt 0 ]; then
        print_warning "Authentik blueprint delivery flagged ${AUTHENTIK_INIT_ISSUES} issue(s) (#2403) — see the step above."
        issues=$((issues + AUTHENTIK_INIT_ISSUES))
    fi

    if [ "${AUTHENTIK_CRASHLOOP:-0}" -gt 0 ]; then
        print_warning "authentik-server is in a restart loop — see the step above."
        issues=$((issues + 1))
    fi

    # #2455: cognee that cannot write its storage is an upgrade issue.
    if [ "${COGNEE_STORAGE_ISSUES:-0}" -gt 0 ]; then
        print_warning "cognee storage is not writable by cognee (#2455) — see the step above."
        issues=$((issues + COGNEE_STORAGE_ISSUES))
    fi

    if [ $issues -eq 0 ]; then
        print_success "Upgrade verification passed."
    else
        print_warning "Upgrade completed with ${issues} issue(s). Review logs."
        # #142 (lightweight): bundle logs + ask the operator to email them.
        collect_failure_logzip "upgrade-verify-issues"
    fi

    return $issues
}

# ==============================================================================
# Rollback
# ==============================================================================
# #2178: what a rollback must put back, recorded BEFORE the code moves.
#
# The state file is the rollback's contract with the upgrade that wrote it:
#   METHOD            package|git — decided once, at the start. A package box
#                     never gets a git checkout from the rollback.
#   FROM              <RAZZFAZZ_VERSION>@<RAZZFAZZ_COMMIT> of the pre-upgrade
#                     .env. A rollback whose restored .env names another run
#                     refuses the record instead of applying a foreign snapshot.
#   PRE_HEAD, PRE_VERSION_FILE, PRE_RAZZFAZZ_VERSION — the three markers.
#   PRE_DIRTY, PRE_TRACKED_DIFF — how many tracked files differed from HEAD, and
#                     a hash of that whole diff (set AND content, not a count);
#                     the file list is in pre-upgrade-modified.txt.
#   STASH             the git arm's auto-stash commit, re-applied after the
#                     forced checkout (code_update_git keeps it, #1590).
#   SNAPSHOT, REPLACED, NEW_FILES — the package arm's tar of the files it
#                     replaces, their pre-upgrade sha256 manifest, and the list
#                     of files it adds. NEW_FILES is written LAST: while it is
#                     empty, the package copy never started.
ROLLBACK_STATE="backups/state/upgrade-rollback.env"

_rollback_state_dir() {
    dirname "$ROLLBACK_STATE"
}

# _rollback_state_set <key> <value> — replace one key (temp file + rename).
_rollback_state_set() {
    local key="$1" val="$2" state="${ROLLBACK_STATE:-}" tmp
    [ -n "$state" ] && [ -f "$state" ] || return 1
    tmp="${state}.tmp"
    grep -v "^${key}=" "$state" > "$tmp" || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv -f "$tmp" "$state"
}

# Hash of every tracked difference from HEAD, staged or not, content included.
_rollback_tracked_diff_sha() {
    { git diff HEAD --binary 2>/dev/null || true; } | sha256sum | cut -d' ' -f1
}

record_rollback_state() {
    local d; d=$(_rollback_state_dir)
    mkdir -p "$d"
    # #2178: the previous upgrade's snapshot goes when the next upgrade
    # starts — not after a successful verification, because the snapshot is
    # what lets an operator roll back an upgrade that "passed" but is unwanted.
    rm -f "$d/pre-package-code.tar" "$d/pre-package-new-files.txt" \
          "$d/pre-package-replaced.sha256" "$d/pre-upgrade-modified.txt"
    # The arm comes from the script's own --package parse (PACKAGE_FILE is
    # initialised at top level), never from an inherited variable.
    local method=git
    [ -z "${PACKAGE_FILE:-}" ] || method=package
    local pre_dirty="" pre_diff=""
    if [ -d .git ]; then
        git diff HEAD --name-only 2>/dev/null > "$d/pre-upgrade-modified.txt" || true
        pre_dirty=$(wc -l < "$d/pre-upgrade-modified.txt" | tr -d ' ')
        pre_diff=$(_rollback_tracked_diff_sha)
    fi
    {
        printf 'METHOD=%s\n' "$method"
        printf 'FROM=%s@%s\n' "$(read_env_value .env RAZZFAZZ_VERSION)" "$(read_env_value .env RAZZFAZZ_COMMIT)"
        printf 'PRE_HEAD=%s\n' "$(git rev-parse HEAD 2>/dev/null || true)"
        printf 'PRE_VERSION_FILE=%s\n' "$(tr -d '[:space:]' < VERSION 2>/dev/null || true)"
        printf 'PRE_RAZZFAZZ_VERSION=%s\n' "$(read_env_value .env RAZZFAZZ_VERSION)"
        printf 'PRE_DIRTY=%s\n' "$pre_dirty"
        printf 'PRE_TRACKED_DIFF=%s\n' "$pre_diff"
        printf 'STASH=\nSNAPSHOT=\nREPLACED=\nNEW_FILES=\n'
    } > "$ROLLBACK_STATE"
}

# #2178: the package path moves no git ref — it copies files over the tree. Keep
# the current copy of every file the package will REPLACE, and the list of files
# it will ADD, so a rollback can undo exactly that (works with or without .git).
# What it skips is PACKAGE_RSYNC_EXCLUDES, the list the copy itself uses.
snapshot_files_the_package_replaces() {
    local pkg_dir="$1" prune_plan="${2:-}" f d
    d=$(_rollback_state_dir)
    local snap="$d/pre-package-code.tar" added="$d/pre-package-new-files.txt" sums="$d/pre-package-replaced.sha256"
    local -a keep=()
    mkdir -p "$d"
    [ -f "$ROLLBACK_STATE" ] || record_rollback_state   # before our files: it clears them
    : > "$added"
    : > "$sums"
    while IFS= read -r -d '' f; do
        f="${f#./}"
        _package_path_is_excluded "$f" && continue
        if [ -e "${SCRIPT_DIR}/${f}" ] || [ -L "${SCRIPT_DIR}/${f}" ]; then
            keep+=("$f")
        else
            printf '%s\n' "$f" >> "$added"
        fi
    done < <(cd "$pkg_dir" && find . \( -type f -o -type l \) -print0)
    # #2818: what the release prune will remove is kept too, so a rollback restores it.
    if [ -n "$prune_plan" ] && [ -s "$prune_plan" ]; then
        while IFS= read -r f; do
            { [ -e "${SCRIPT_DIR}/${f}" ] || [ -L "${SCRIPT_DIR}/${f}" ]; } && keep+=("$f")
        done < "$prune_plan"
    fi
    rm -f "$snap"
    if [ "${#keep[@]}" -gt 0 ]; then
        tar -C "$SCRIPT_DIR" -cf "$snap" -- "${keep[@]}"
        (cd "$SCRIPT_DIR" && sha256sum -- "${keep[@]}") > "$sums"
        _rollback_state_set SNAPSHOT "$snap"
    fi
    _rollback_state_set REPLACED "$sums"
    _rollback_state_set NEW_FILES "$added"     # last: the snapshot is complete
}

# A manual checkout the rollback may print: it saves every local edit first
# (tracked, staged, untracked) in a named stash, so following it can never
# destroy an edit; then a plain checkout; then, optionally, the auto-stash.
_rollback_safe_checkout_cmd() {
    local commit="$1" apply="${2:-}" ts="${3:-$(date +%Y%m%d%H%M%S)}"
    printf "git stash push --include-untracked -m 'pre-manual-rollback %s' && git checkout %s%s" \
        "$ts" "$(printf '%q' "$commit")" "${apply:+ && git stash apply $(printf '%q' "$apply")}"
}

# The upgrade's own auto-stash (code_update_git: "razzfazz-upgrade: auto-stash
# before upgrade to …") made on top of <base>: the newest such entry whose
# first parent is <base>. Prints its sha, or nothing.
_rollback_upgrade_autostash() {
    local base="$1" sha subj
    [ -n "$base" ] || return 0
    while IFS=$'\t' read -r sha subj; do
        case "$subj" in *"razzfazz-upgrade: auto-stash"*) ;; *) continue ;; esac
        if [ "$(git rev-parse -q --verify "${sha}^1" 2>/dev/null)" = "$base" ]; then
            printf '%s\n' "$sha"
            return 0
        fi
    done < <(git stash list --format='%H%x09%gs' 2>/dev/null)
    return 0
}

# 0 when the working tree already holds <stash>: its tracked tree, and each of
# its untracked files byte for byte.
_rollback_stash_is_applied() {
    local stash="$1" p
    git diff --quiet "$stash" -- 2>/dev/null || return 1
    if git rev-parse -q --verify "${stash}^3" >/dev/null 2>&1; then
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            [ "$(git hash-object -- "$p" 2>/dev/null)" = "$(git rev-parse -q --verify "${stash}^3:${p}" 2>/dev/null)" ] || return 1
        done < <(git -c core.quotePath=false ls-tree -r --name-only "${stash}^3" 2>/dev/null)
    fi
    return 0
}

# #2178: the record must belong to the upgrade whose .env backup is about to be
# restored. Asked BEFORE anything is restored, so a refusal leaves the live .env
# (and everything else) as it is.
_rollback_record_matches_backup() {
    local backup="$1" from now
    [ -f "$ROLLBACK_STATE" ] || return 0
    from=$(read_env_value "$ROLLBACK_STATE" FROM)
    [ -n "$from" ] || return 0
    now="$(read_env_value "$backup" RAZZFAZZ_VERSION)@$(read_env_value "$backup" RAZZFAZZ_COMMIT)"
    [ "$from" = "$now" ] && return 0
    print_error "The rollback record ${ROLLBACK_STATE} belongs to an upgrade from ${from}, but ${backup} names ${now}. Nothing was restored: the live .env and the code are as they were."
    print_info "Put the .env the upgrade started from in ${backup} (it is written only when the backup step ran), then re-run 'rzfz upgrade --rollback'."
    return 1
}

# Never overwrite a live env file without keeping it: <file>.pre-rollback-<ts>,
# mode 0600 (it holds every secret the upgrade minted). 1 = could not keep it.
_rollback_keep_live_env() {
    local f="$1" ts="$2" kept
    [ -f "$f" ] || return 0
    kept="${f}.pre-rollback-${ts}"
    [ ! -e "$kept" ] || kept="${kept}-$$"
    # A new file under umask 077 is created 0600 (cp keeps at most the source mode).
    if ! ( umask 077 && cp "$f" "$kept" ); then
        print_error "Could not keep a copy of the live ${f} as ${kept}; it was not overwritten."
        return 1
    fi
    print_substep "Kept the live ${f} as ${kept} (mode 0600) before restoring the pre-upgrade copy."
}

# Prints (one per line) every path whose current bytes a forced checkout of
# <pre_head> would destroy and that exist nowhere else: not in HEAD (then it is
# no edit), not in <pre_head> (the checkout writes the same bytes back), not in
# <stash> (tracked part or ^3, its untracked part). A deletion loses nothing.
_rollback_edits_found_nowhere_else() {
    local pre_head="$1" stash="$2" p cur
    {
        git -c core.quotePath=false diff HEAD --name-only 2>/dev/null || true
        # untracked files the checkout would overwrite: tracked in pre_head, not in the index
        LC_ALL=C comm -23 \
            <(git -c core.quotePath=false ls-tree -r --name-only "$pre_head" 2>/dev/null | LC_ALL=C sort) \
            <(git -c core.quotePath=false ls-files 2>/dev/null | LC_ALL=C sort)
    } | LC_ALL=C sort -u | while IFS= read -r p; do
        [ -n "$p" ] || continue
        [ -e "$p" ] || [ -L "$p" ] || continue
        cur=$(git hash-object -- "$p" 2>/dev/null) || { printf '%s\n' "$p"; continue; }
        [ "$cur" = "$(git rev-parse -q --verify "${pre_head}:${p}" 2>/dev/null)" ] && continue
        if [ -n "$stash" ]; then
            [ "$cur" = "$(git rev-parse -q --verify "${stash}:${p}" 2>/dev/null)" ] && continue
            [ "$cur" = "$(git rev-parse -q --verify "${stash}^3:${p}" 2>/dev/null)" ] && continue
        fi
        printf '%s\n' "$p"
    done
}

# The git arm: forced checkout of the pre-upgrade commit, then the auto-stash
# re-applied. Refuses — tree untouched — when the checkout would destroy edits
# that exist nowhere else, including when the recorded stash is gone.
_rollback_git_restore() {
    local pre_head="$1" stash="$2" risk p kept ts
    ts=$(date +%Y%m%d%H%M%S)
    if [ -n "$stash" ] && ! git cat-file -e "${stash}^{commit}" 2>/dev/null; then
        print_error "The pre-upgrade auto-stash ${stash} no longer exists (popped or dropped, then pruned): the pre-upgrade local changes cannot be re-applied. The code was not touched."
        risk=$(_rollback_edits_found_nowhere_else "$pre_head" "")
        if [ -n "$risk" ]; then
            print_info "A checkout would destroy these working-tree edits, which exist nowhere else: $(printf '%s\n' "$risk" | tr '\n' ' ')"
        fi
        print_info "These pre-upgrade local changes are gone with the stash, so this rollback can no longer end as 'complete' — a re-run refuses again. Finish by hand:"
        print_info "Manual restore (saves every local edit first): $(_rollback_safe_checkout_cmd "$pre_head" "" "$ts")"
        print_info "Then rebuild and recreate the stack yourself (docker compose build && docker compose up -d --force-recreate); your current edits are in the stash 'pre-manual-rollback ${ts}' (git stash list)."
        return 1
    fi
    risk=$(_rollback_edits_found_nowhere_else "$pre_head" "$stash")
    if [ -n "$risk" ]; then
        print_error "The working tree holds edits that exist nowhere else (not in the pre-upgrade commit, the new one or the auto-stash); a checkout would destroy them. The code was not touched:"
        printf '%s\n' "$risk" | sed 's/^/    /'
        local list="" q
        while IFS= read -r q; do list+="$(printf '%q' "$q") "; done <<< "$risk"
        print_info "Manual restore: tar -cf $(printf '%q' "${SCRIPT_DIR}/backups/rollback-kept-${ts}.tar") -- ${list}&& rm -f -- ${list}&& ./rzfz upgrade --rollback"
        return 1
    fi
    # An untracked file (or symlink) where the pre-upgrade commit has a
    # DIRECTORY: the forced checkout would replace it. Set it aside instead.
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        if [ -L "$p" ] || { [ -e "$p" ] && [ ! -d "$p" ]; }; then
            git ls-files --error-unmatch -- "$p" >/dev/null 2>&1 && continue   # tracked: HEAD or the check above holds it
            kept="${p}.pre-rollback-${ts}"
            mv -f -- "$p" "$kept"
            print_warning "Set aside ${p} as ${kept}: the pre-upgrade commit has a directory there."
        fi
    done < <(git -c core.quotePath=false ls-tree -r -d --name-only "$pre_head" 2>/dev/null)
    print_substep "Checking out the pre-upgrade commit: ${pre_head:0:9}"
    if ! git checkout -q --force "$pre_head" 2>&1; then
        print_error "Could not check out the pre-upgrade commit ${pre_head}."
        print_info "Manual restore (saves every local edit first): $(_rollback_safe_checkout_cmd "$pre_head" "$stash" "$ts")"
        return 1
    fi
    [ -n "$stash" ] || return 0
    # Untracked files the stash carries (#1590: certs/cert.pem, key.pem) that
    # are back in the tree make `git stash apply` refuse ("already exists") —
    # on a second rollback, after the operator applied or popped the stash, or
    # after code_update_git popped it on a failure arm. A forced checkout never
    # removes untracked files. An identical copy goes (the apply writes the
    # same bytes back); a differing one is set aside under a named backup.
    if git rev-parse -q --verify "${stash}^3" >/dev/null 2>&1; then
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            [ -e "$p" ] || [ -L "$p" ] || continue
            if [ "$(git hash-object -- "$p" 2>/dev/null)" = "$(git rev-parse -q --verify "${stash}^3:${p}" 2>/dev/null)" ]; then
                rm -f -- "$p"
            else
                kept="${p}.pre-rollback-${ts}"
                mv -f -- "$p" "$kept"
                print_warning "Set aside ${p} as ${kept}: it differs from the pre-upgrade copy in the auto-stash, which is restored in its place."
            fi
        done < <(git -c core.quotePath=false ls-tree -r --name-only "${stash}^3" 2>/dev/null)
    fi
    # apply, not pop: the stash survives whatever happens next.
    print_substep "Re-applying the pre-upgrade local changes (stash ${stash:0:9})..."
    if ! git stash apply -q "$stash" 2>&1; then
        print_error "Could not re-apply the pre-upgrade local changes from stash ${stash}. The stash is kept."
        print_info "Manual restore: re-run './rzfz upgrade --rollback' (it sets aside untracked files in the way), or (saves every local edit first): $(_rollback_safe_checkout_cmd "$pre_head" "$stash" "$ts")"
        return 1
    fi
    return 0
}

# restore_code_for_rollback → 0 restored (or nothing to restore) / 1 could not;
# on 1 it has printed what failed and the manual command.
#
# The arm is decided fail-closed from the record: any package evidence
# (NEW_FILES/SNAPSHOT/REPLACED, or METHOD=package) takes the package arm; a git
# checkout needs METHOD exactly "git", no package evidence, a restored .env
# that does not say offline-package, a PRE_HEAD and a .git. Any
# other METHOD (unknown, empty, "Package", …) leaves the code as it is.
restore_code_for_rollback() {
    local method="" pre_head="" snap="" added="" replaced="" stash="" f rc=0
    if [ -f "$ROLLBACK_STATE" ]; then
        method=$(read_env_value "$ROLLBACK_STATE" METHOD)
        pre_head=$(read_env_value "$ROLLBACK_STATE" PRE_HEAD)
        snap=$(read_env_value "$ROLLBACK_STATE" SNAPSHOT)
        replaced=$(read_env_value "$ROLLBACK_STATE" REPLACED)
        added=$(read_env_value "$ROLLBACK_STATE" NEW_FILES)
        stash=$(read_env_value "$ROLLBACK_STATE" STASH)
        # (FROM was checked by rollback() before anything was restored.)

        if [ "$method" = "package" ] || [ -n "$added" ] || [ -n "$snap" ] || [ -n "$replaced" ]; then
            # A package box never gets a git checkout from here.
            if [ -z "$added" ]; then
                print_substep "Nothing to restore: the package upgrade stopped before changing code (#2178)."
                return 0
            fi
            if [ -n "$snap" ]; then
                if [ ! -f "$snap" ] || ! tar -xf "$snap" -C "$SCRIPT_DIR"; then
                    print_error "Could not restore the files the package replaced from ${snap}."
                    print_info "Manual restore: tar -xf $(printf '%q' "${SCRIPT_DIR}/${snap}") -C $(printf '%q' "$SCRIPT_DIR")"
                    rc=1
                fi
            fi
            if [ ! -f "$added" ]; then
                print_error "The list of files the package added (${added}) is missing; they were not removed."
                rc=1
            else
                while IFS= read -r f; do
                    [ -n "$f" ] || continue
                    if [ -e "${SCRIPT_DIR}/${f}" ] || [ -L "${SCRIPT_DIR}/${f}" ]; then
                        rm -f -- "${SCRIPT_DIR}/${f}" 2>/dev/null || true
                        if [ -e "${SCRIPT_DIR}/${f}" ] || [ -L "${SCRIPT_DIR}/${f}" ]; then
                            print_error "Could not remove ${f}, which the package added."
                            print_info "Manual restore: rm -f -- $(printf '%q' "${SCRIPT_DIR}/${f}")"
                            rc=1
                        fi
                    fi
                done < "$added"
            fi
            [ "$rc" -eq 0 ] && print_substep "Restored the files the package replaced and removed the ones it added (#2178)."
            return "$rc"
        fi

        if [ "$method" != "git" ]; then
            print_warning "The rollback record names upgrade method '${method}', which is neither git nor package: the code is left as it is (#2178)."
            return 0
        fi
        if [ "$(read_env_value .env RAZZFAZZ_UPGRADE_METHOD)" = "offline-package" ]; then
            print_error "The rollback record says git, but the restored .env says this box ran from a package, where HEAD is not the running release. The code was not touched."
            print_info "Manual restore, only if this upgrade really was a git checkout (saves every local edit first): $(_rollback_safe_checkout_cmd "$pre_head" "$stash")"
            return 1
        fi
        if [ -z "$pre_head" ] || [ ! -d .git ]; then
            print_error "The rollback record names a git upgrade but holds no pre-upgrade commit, or this tree has no .git. The code was not touched."
            return 1
        fi
        _rollback_git_restore "$pre_head" "$stash"
        return $?
    fi

    # No record (an upgrade run by an upgrader without #2178): the old
    # mechanism — except on a box installed from a package, where HEAD is not
    # the running release and a checkout would split the tree.
    if [ "$(read_env_value .env RAZZFAZZ_UPGRADE_METHOD)" = "offline-package" ]; then
        print_warning "No rollback record, and this box runs from a package: the code is left as it is (#2178)."
        return 0
    fi
    local old_commit old_sha="" auto=""
    old_commit=$(read_env_value ".env" "RAZZFAZZ_COMMIT")
    if [ -n "$old_commit" ] && [ "$old_commit" != "unknown" ] && [ -d ".git" ]; then
        old_sha=$(git rev-parse -q --verify "${old_commit}^{commit}" 2>/dev/null || true)
        auto=$(_rollback_upgrade_autostash "$old_sha")
        print_substep "Checking out previous commit: ${old_commit}"
        # A plain checkout: it refuses rather than overwrite a local edit.
        if ! git checkout "$old_commit" 2>&1; then
            print_error "Could not check out ${old_commit}: local edits are in the way (git named them above). Manual rollback needed."
            print_info "Manual restore (saves every local edit first): $(_rollback_safe_checkout_cmd "$old_commit" "$auto")"
            print_info "Your current edits are then in the stash 'pre-manual-rollback …' (git stash list); re-run 'rzfz upgrade --rollback' afterwards."
            return 1
        fi
        # The first upgrade into 2026.11 writes no record, but its auto-stash
        # holds the operator's pre-upgrade changes. Without a record it is not
        # applied automatically — and the rollback is not complete without it.
        if [ -n "$auto" ] && ! _rollback_stash_is_applied "$auto"; then
            print_error "The code is back at ${old_commit}, but the upgrade's auto-stash ${auto} (your pre-upgrade local changes) is not applied. Without a rollback record it is not applied automatically."
            print_info "Apply it, then re-run the rollback: git stash apply ${auto} && ./rzfz upgrade --rollback"
            return 1
        fi
    fi
    return 0
}

# #2178: the three markers and the tree, read AFTER the restore.
# → 0 agree / 2 disagree (prints `marker|expected|actual` lines).
verify_rollback_markers() {
    local want_v="" want_env="" want_head="" want_diff="" method="" added="" sums="" d
    local got_v got_env got_head got_diff bad=0
    d=$(_rollback_state_dir)
    if [ -f "$ROLLBACK_STATE" ]; then
        want_v=$(read_env_value "$ROLLBACK_STATE" PRE_VERSION_FILE)
        want_env=$(read_env_value "$ROLLBACK_STATE" PRE_RAZZFAZZ_VERSION)
        want_head=$(read_env_value "$ROLLBACK_STATE" PRE_HEAD)
        want_diff=$(read_env_value "$ROLLBACK_STATE" PRE_TRACKED_DIFF)
        method=$(read_env_value "$ROLLBACK_STATE" METHOD)
        added=$(read_env_value "$ROLLBACK_STATE" NEW_FILES)
        sums=$(read_env_value "$ROLLBACK_STATE" REPLACED)
    fi
    got_env=$(read_env_value .env RAZZFAZZ_VERSION)
    got_v=$(tr -d '[:space:]' < VERSION 2>/dev/null || true)
    [ -n "$want_env" ] || want_env="$got_env"
    [ -n "$want_v" ] || want_v="$want_env"          # no state: VERSION must match the restored .env
    [ "$got_v" = "$want_v" ] || { echo "  VERSION|${want_v}|${got_v}"; bad=1; }
    [ "$got_env" = "$want_env" ] || { echo "  .env RAZZFAZZ_VERSION|${want_env}|${got_env}"; bad=1; }
    if [ -d .git ]; then
        got_head=$(git rev-parse HEAD 2>/dev/null || true)
        [ -z "$want_head" ] || [ "$got_head" = "$want_head" ] || { echo "  git HEAD|${want_head}|${got_head}"; bad=1; }
        if [ -n "$want_diff" ]; then
            # The same files, with the same content, as before the upgrade.
            got_diff=$(_rollback_tracked_diff_sha)
            if [ "$got_diff" != "$want_diff" ]; then
                local now_list; now_list=$(git diff HEAD --name-only 2>/dev/null || true)
                echo "  modified tracked files|$(grep -c . "$d/pre-upgrade-modified.txt" 2>/dev/null || true) files, diff ${want_diff:0:12}|$(printf '%s' "$now_list" | grep -c . || true) files, diff ${got_diff:0:12}"
                if [ -f "$d/pre-upgrade-modified.txt" ]; then
                    LC_ALL=C comm -23 <(LC_ALL=C sort "$d/pre-upgrade-modified.txt") <(printf '%s\n' "$now_list" | LC_ALL=C sort) | head -n 10 | sed 's/^/    modified before, not now: /'
                    LC_ALL=C comm -13 <(LC_ALL=C sort "$d/pre-upgrade-modified.txt") <(printf '%s\n' "$now_list" | grep . | LC_ALL=C sort) | head -n 10 | sed 's/^/    modified now, not before: /'
                fi
                bad=1
            fi
        elif [ ! -f "$ROLLBACK_STATE" ] \
             && [ "$(read_env_value .env RAZZFAZZ_UPGRADE_METHOD)" != "offline-package" ]; then
            # No record: a git box must be clean. A package box's tracked files
            # differ from HEAD by design; VERSION against .env is its marker.
            local got_dirty auto
            auto=$(_rollback_upgrade_autostash "$(git rev-parse -q --verify HEAD 2>/dev/null || true)")
            if [ -n "$auto" ]; then
                # The upgrade's own auto-stash is what the tree held before it.
                # restore_code_for_rollback checks it only when RAZZFAZZ_COMMIT is
                # usable; with it unknown or empty this is the only check.
                _rollback_stash_is_applied "$auto" \
                    || { echo "  auto-stash ${auto:0:12}|applied|not applied (the tree differs from it)"; bad=1; }
            else
                got_dirty=$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')
                [ "$got_dirty" = 0 ] || { echo "  modified tracked files|0|${got_dirty}"; bad=1; }
            fi
        fi
    fi
    # The package arm, with or without .git: the replaced files hold their
    # pre-upgrade content again.
    if [ "$method" = "package" ] && [ -n "$added" ]; then
        if [ -n "$sums" ] && [ ! -f "$sums" ]; then
            echo "  files the package replaced|manifest ${sums}|missing — their content cannot be checked"
            bad=1
        elif [ -n "$sums" ] && [ -s "$sums" ]; then
            local abs_sums failed
            case "$sums" in /*) abs_sums="$sums" ;; *) abs_sums="$(pwd)/$sums" ;; esac
            failed=$(cd "$SCRIPT_DIR" && sha256sum -c -- "$abs_sums" 2>/dev/null | grep -v ': OK$' || true)
            if [ -n "$failed" ]; then
                echo "  files the package replaced|pre-upgrade content|$(printf '%s\n' "$failed" | grep -c .) differ"
                printf '%s\n' "$failed" | head -n 10 | sed 's/^/    /'
                bad=1
            fi
        fi
        # (A file the package added and the restore could not remove already
        # failed the restore — it re-checks each one after its rm.)
    fi
    [ "$bad" -eq 0 ] && return 0
    return 2
}

# #2536 item 30: the idempotent post-install self-heal, shared by the full
# upgrade, `--update` and `--rollback`. ga.4's D2 (OIDC client secrets), D4
# (embedding scale-out) and K1 rely on it after EVERY code or image change;
# --update and --rollback exited before it. Best-effort; never fails the caller.
# $1 names the path for the messages: upgrade (default), update or rollback.
# #2280: the bundle in a continuation of an upgrader that did not handle it
# (MODEL_BUNDLE_CROSSING, set after the parse). The code upgrade is done by
# now; the bundle is either handed to the provisioning refresh, which verifies
# and stages it before its model deploy (APPLIANCE_MODEL_BUNDLE), or — refused
# — skipped with a loud warning and the command that applies the right one.
model_bundle_after_crossing() {
    if [ "${MODEL_BUNDLE_CROSSING:-false}" != true ]; then
        if [ -n "${MODEL_BUNDLE_FILE:-}" ] && [ "${CONTINUE_AFTER_REEXEC:-false}" = true ] \
           && [ "${_UPGRADE_MODEL_BUNDLE_HANDLED:-}" = 1 ]; then
            print_substep "model bundle: handled by the previous half"
        fi
        return 0
    fi
    if [ -z "${MODEL_BUNDLE_SKIP_REASON:-}" ]; then
        export APPLIANCE_MODEL_BUNDLE="$MODEL_BUNDLE_FILE"
        [ -z "${MODEL_BUNDLE_SHA256:-}" ] || export MODEL_BUNDLE_SHA256
        print_substep "The previous release's upgrader did not stage the model bundle; the provisioning refresh verifies and stages it before its model deploy: ${MODEL_BUNDLE_FILE} (#2280)"
        return 0
    fi
    local cls
    cls="$(razzfazz_box_hardware_class 2>/dev/null || true)"
    print_warning "════════════════════════════════════════════════════════════════════"
    print_warning "MODEL BUNDLE NOT APPLIED — the upgrade itself completed without it (#2280)."
    print_warning "  Refused: $(printf '%s' "$MODEL_BUNDLE_SKIP_REASON" | tr '\n' ' ')"
    print_warning "  The previous release's upgrader had already backed up and changed this box"
    print_warning "  before the new upgrader could check the bundle, so the box was NOT left"
    print_warning "  half-upgraded; it has no models from this bundle."
    print_warning "  Apply this box's bundle now (class ${cls:-<class>}):"
    print_warning "    APPLIANCE_MODEL_BUNDLE=/path/to/razzfazz-${TARGET_VERSION:-<version>}-models-${cls:-<class>}.tar.gz rzfz post-install --refresh"
    print_warning "  Next crossing from 2026.09: unpack the new upgrader first (tar -xzf <base> ./cli ./scripts ./rzfz,"
    print_warning "  then ./rzfz upgrade …) — it refuses a wrong bundle before anything changes"
    print_warning "  (docs: offline install, \"Base package and model bundle\")."
    print_warning "════════════════════════════════════════════════════════════════════"
    return 0
}

upgrade_post_install_refresh() {
    local _what="${1:-upgrade}"
    [ "${DRY_RUN:-false}" != "true" ] || return 0
    [ -x "${SCRIPT_DIR}/rzfz" ] || return 0
    # A rollback runs the RESTORED tree's rzfz; a release older than ga.1 has no
    # `post-install --refresh`. Detect the support rather than parse versions.
    # Capture the whole help text, then match: a `| grep -q` pipeline under pipefail
    # races (grep exits on the first match, the writer dies of SIGPIPE, status 141).
    if [ "$_what" = "rollback" ]; then
        local _help=""
        _help=$(timeout 30 "${SCRIPT_DIR}/rzfz" post-install --help 2>/dev/null </dev/null) || true
        case "$_help" in
            *--refresh*) ;;
            *) print_info "The restored release has no 'post-install --refresh'; the provisioning self-heal is skipped."
               return 0 ;;
        esac
    fi
    print_step "Applying post-${_what} provisioning self-heal (rzfz post-install --refresh)..."
    if "${SCRIPT_DIR}/rzfz" post-install --refresh; then
        print_success "Post-${_what} provisioning self-heal applied."
    else
        print_warning "Post-${_what} provisioning self-heal reported issues (non-fatal)."
        print_info "  Re-run manually: rzfz post-install --refresh"
    fi
    echo ""
    return 0
}

# ==============================================================================
# #3293 - engines follow the release: runner build and engine hardening
# ==============================================================================
# An engine container keeps the image and settings it was created with; the new
# worker agent's runner pin and least-privilege settings reach it only when it
# is started again, and nothing restarts a serving engine. Measured on an
# upgraded box: engines from the previous release ran its runner as root, with
# every capability and a writable root, through two upgrades. So the upgrade
# ends by switching them (cli/relaunch-engines.sh: the LLM Manager's runner
# switch, health-gated, the old runner back for an engine that does not come
# up). After the refresh, which recreates the agent with the GPU group ids and
# publishes the runner images the switch pulls. Never fails the upgrade; the
# result is UPGRADE_ENGINE_RELAUNCH (done|failed|skipped) for the summary.
UPGRADE_ENGINE_RELAUNCH=""
upgrade_relaunch_engines() {
    [ "${DRY_RUN:-false}" != "true" ] || return 0
    local script="${SCRIPT_DIR}/cli/relaunch-engines.sh"
    [ -f "$script" ] || return 0
    if [ "${NO_ENGINE_RELAUNCH:-false}" = true ]; then
        UPGRADE_ENGINE_RELAUNCH=skipped
        print_info "Engine relaunch skipped (--no-engine-relaunch). Engines that are not on this release's runner or not hardened:"
        bash "$script" --check || true
        print_info "  Relaunch them when it suits: rzfz relaunch-engines"
        return 0
    fi
    print_step "Moving the LLM engines onto this release's runner and engine settings..."
    if bash "$script" --from-upgrade; then
        UPGRADE_ENGINE_RELAUNCH=done
    else
        UPGRADE_ENGINE_RELAUNCH=failed
        print_warning "Not every engine is on this release's runner and settings (see above). The models keep serving. Re-run when the cause is fixed: rzfz relaunch-engines"
    fi
    echo ""
    return 0
}

# ==============================================================================
# #2977 - rotate the Valkey password once, for every consumer together
# ==============================================================================
# docling-serve wrote the RQ Redis URL, password included, into its log at every
# worker start (2026.09 up to 2026.11-rc6); the password is therefore in docker
# logs, log bundles and wherever container logs are shipped. The log line is gone
# (the module compose), the collectors mask URL credentials, and this step
# replaces the password itself: cli/rotate-secret.sh --rotate VALKEY_PASSWORD
# (valkey requirepass, .env, the Dify copies in .env.dify, then the consumers
# whose environment changed). Once per rotation generation:
# VALKEY_PASSWORD_ROTATION in .env says which generation the current password
# belongs to (scripts/lib.sh valkey_rotation_due), so a re-run does not rotate
# again. A failed rotation is not silent: UPGRADE_VALKEY_ROTATION_FAILED turns the
# end of the run into "Upgrade INCOMPLETE" (exit 7), and the final check
# (valkey_rotation_final_gate) names every running container that still holds
# the old password, agent instances included.
UPGRADE_VALKEY_ROTATION_FAILED=false
UPGRADE_VALKEY_OLD_PASSWORD=""

rotate_valkey_password_once() {
    valkey_rotation_due ".env" || return 0
    if [ "${DRY_RUN:-false}" = true ]; then
        print_info "Would rotate VALKEY_PASSWORD once (generation ${VALKEY_ROTATION_GENERATION}) and restart every consumer: it was written to container logs by an earlier release."
        return 0
    fi
    [ "${MIGRATIONS_ONLY:-false}" != true ] || return 0
    local old
    old=$(read_env_value ".env" VALKEY_PASSWORD)
    if [ -z "$old" ]; then
        # A box that had none was just given a fresh one (generate_missing_secrets):
        # it never appeared in a log line.
        update_env_value ".env" VALKEY_PASSWORD_ROTATION "$VALKEY_ROTATION_GENERATION"
        return 0
    fi
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -x valkey >/dev/null; then
        print_warning "Valkey is not running: its password was NOT rotated. The next upgrade (or rzfz rotate-secret --rotate VALKEY_PASSWORD) does it."
        return 0
    fi
    print_step "Rotating the Valkey password (an earlier release wrote it to container logs)..."
    UPGRADE_VALKEY_OLD_PASSWORD="$old"
    if bash "${SCRIPT_DIR}/cli/rotate-secret.sh" --rotate VALKEY_PASSWORD --yes; then
        print_success "Valkey password rotated; its consumers were restarted with the new one."
    else
        UPGRADE_VALKEY_ROTATION_FAILED=true
        print_error "The Valkey password rotation did not complete cleanly (see above)."
        print_info "  Fix the cause and re-run: rzfz upgrade   (a rotation that failed before it changed anything is retried)"
    fi
    return 0
}

# The check that outlives the rotation: after the post-install refresh has
# re-keyed the agent instances, no running container may hold the old password
# and valkey must refuse it. The old value is this run's, or - on a re-run, when
# the marker says "already rotated" - the one rotate-secret kept in
# .env.pre-rotate-bak. 0 clean (or nothing to check), 1 stale.
valkey_rotation_final_gate() {
    [ "${DRY_RUN:-false}" != true ] || return 0
    [ "${MIGRATIONS_ONLY:-false}" != true ] || return 0
    local old="$UPGRADE_VALKEY_OLD_PASSWORD" cur bak
    cur=$(read_env_value ".env" VALKEY_PASSWORD)
    if [ -z "$old" ] && [ -f ".env.pre-rotate-bak" ]; then
        bak=$(read_env_value ".env.pre-rotate-bak" VALKEY_PASSWORD)
        [ -z "$bak" ] || [ "$bak" = "$cur" ] || old="$bak"
    fi
    [ -n "$old" ] && [ "$old" != "$cur" ] || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x valkey >/dev/null || return 0
    print_step "Checking that nothing still uses the old Valkey password..."
    if valkey_rotation_stale_check "$old"; then
        print_success "The old Valkey password is refused and no running container carries it."
        return 0
    fi
    return 1
}

# #2629 (reopened): the images that migrate their data but carry no .env pin
# (STATEFUL_HARDCODED_IMAGES) arrive with the restored compose files. The .env
# guard in rollback() never sees them, so the recreate started the previous
# release's tag over data the newer one had migrated — an older pgvector over
# an updated extension, an older Synapse over a newer schema. The same rule as
# the forward path (hardcoded_image_sync_decision) is applied to the RESTORED
# tree before anything is recreated: where a container of the image runs a
# newer tag than the restored compose file names, that line keeps the running
# tag, and the image is named. Read from every container, stopped ones
# included (enabling the module again would recreate it at the compose tag).
# No container of the image means no data a newer version touched, and a
# digest-only reference has no tag to keep; both roll back unchanged. Only
# compose*.yml/.yaml is read — the stack's own naming; the vendored upstream
# Dify docker-compose*.yaml files are not part of its compose chain.
# Prints "<image> (<kept>, the restored release has <restored>)" per kept
# image, comma-separated, on stdout.
_rollback_keep_migrated_compose_images() {
    local _rows _img _re _line _t _run _f _have _kept=""
    command -v docker >/dev/null 2>&1 || return 0
    _rows=$(docker ps -a --format '{{.Image}}' 2>/dev/null) || return 0
    for _img in $STATEFUL_HARDCODED_IMAGES; do
        _run=""
        while IFS= read -r _line; do
            case "$_line" in "$_img":*|*/"$_img":*) ;; *) continue ;; esac
            _t="${_line##*"$_img":}"
            case "$_t" in ""|*@*|*/*) continue ;; esac
            if [ -z "$_run" ] || [ "$(_pin_order "$_t" "$_run")" = gt ]; then _run="$_t"; fi
        done <<< "$_rows"
        [ -n "$_run" ] || continue
        _re="${_img//./\\.}"
        while IFS= read -r _f; do
            [ -n "$_f" ] || continue
            _have=$(grep -m1 -oE "image: *${_re}:[^ \"']+" "$_f" || true)
            _have="${_have##*:}"
            [ -n "$_have" ] && [ "$_have" != "$_run" ] || continue
            [ "$(hardcoded_image_sync_decision "$_img" "$_run" "$_have" 2>/dev/null)" = keep ] || continue
            sed -i -E "s|(image: *${_re}:)[^ \"']+|\1${_run}|g" "$_f"
            case ", ${_kept}, " in *", ${_img} ("*) ;; *)
                _kept="${_kept:+${_kept}, }${_img} (${_run}, the restored release has ${_have})" ;;
            esac
        done < <(find . -path ./.git -prune -o \
                     \( -name 'compose*.yml' -o -name 'compose*.yaml' \) -type f \
                     -exec grep -lE "image: *${_re}:" {} + 2>/dev/null)
    done
    printf '%s' "$_kept"
}

rollback() {
    print_step "Rolling back last upgrade..."

    if [ ! -f ".env.pre-upgrade-backup" ]; then
        print_error "No .env.pre-upgrade-backup found. Cannot roll back."
        exit 1
    fi

    # #2178: the record is checked BEFORE anything is restored, and the live
    # env files are kept before they are overwritten — a refused or failed
    # rollback never costs the only copy of the running .env.
    if ! _rollback_record_matches_backup ".env.pre-upgrade-backup"; then
        print_error "Rollback INCOMPLETE — nothing was restored (see above). The stack was NOT rebuilt or recreated. (#2178)"
        return 2
    fi
    local _rollback_ts
    _rollback_ts=$(date +%Y%m%d%H%M%S)
    if ! _rollback_keep_live_env ".env" "$_rollback_ts"; then
        print_error "Rollback INCOMPLETE — nothing was restored. (#2178)"
        return 2
    fi

    # #2629: a service that migrates its own data is never rolled BACK over
    # data a newer version migrated. For each stateful pin the live value (or
    # the running image) newer than the backup's is kept and named; everything
    # else rolls back. The way back for those is the pre-upgrade backup.
    local _rb_keep="" _rb_names="" _e _pin _have _bak _run _cur
    for _e in $STATEFUL_VERSION_PINS; do
        _pin="${_e%%:*}"
        _bak=$(read_env_value ".env.pre-upgrade-backup" "$_pin")
        [ -n "$_bak" ] || continue
        _have=$(read_env_value ".env" "$_pin")
        _run=$(_stateful_pin_running "$_pin")
        _cur=""
        [ -n "$_have" ] && [ "$(_pin_order "$_have" "$_bak")" = gt ] && _cur="$_have"
        if [ -n "$_run" ] && [ "$(_pin_order "$_run" "$_bak")" = gt ]; then
            if [ -z "$_cur" ] || [ "$(_pin_order "$_run" "$_cur")" = gt ]; then _cur="$_run"; fi
        fi
        [ -n "$_cur" ] || continue
        _rb_keep="${_rb_keep}${_pin}=${_cur}"$'\n'
        _rb_names="${_rb_names:+${_rb_names}, }${_pin} (${_cur}, backup has ${_bak})"
    done

    # Restore .env files
    cp -f ".env.pre-upgrade-backup" ".env"
    print_substep "Restored .env from backup"
    if [ -n "$_rb_keep" ]; then
        local _kv
        while IFS= read -r _kv; do
            [ -n "$_kv" ] || continue
            update_env_value ".env" "${_kv%%=*}" "${_kv#*=}"
        done <<< "$_rb_keep"
        print_warning "NOT rolled back (their data was migrated forward; a downgrade is not supported): ${_rb_names}. To go back for these, restore the pre-upgrade backup: rzfz backup restore <backup-file>"
    fi

    if [ -f ".env.dify.pre-upgrade-backup" ]; then
        if ! _rollback_keep_live_env ".env.dify" "$_rollback_ts"; then
            print_error "Rollback INCOMPLETE — .env was restored, .env.dify was not. (#2178)"
            return 2
        fi
        cp -f ".env.dify.pre-upgrade-backup" ".env.dify"
        print_substep "Restored .env.dify from backup"
    fi

    # #2178: restore the code the upgrade changed, then read the markers back.
    # A restore that fails is the same verdict as markers that disagree:
    # INCOMPLETE, exit 2, nothing rebuilt or recreated.
    if ! restore_code_for_rollback; then
        print_error "Rollback INCOMPLETE — the pre-upgrade code could not be restored (see above for what failed and the manual command)."
        print_info "Your .env files have been restored. The stack was NOT rebuilt or recreated. (#2178)"
        print_info "To restore volumes, use: rzfz backup restore <backup-file>"
        return 2
    fi
    local _rollback_mismatch
    if ! _rollback_mismatch=$(verify_rollback_markers); then
        print_error "Rollback INCOMPLETE — the box is not the release it was before the upgrade. Marker | expected | found:"
        printf '%s\n' "$_rollback_mismatch"
        print_info "The stack was NOT rebuilt or recreated over a split tree. Restore the code by hand (the pre-upgrade package, or save your local edits with 'git stash push --include-untracked' and check out the pre-upgrade commit), then re-run 'rzfz upgrade --rollback'. (#2178)"
        return 2
    fi

    # #2629: the restored compose files never lower an image that migrated its data.
    local _rb_images
    _rb_images=$(_rollback_keep_migrated_compose_images)
    if [ -n "$_rb_images" ]; then
        print_warning "NOT rolled back (their data was migrated forward; a downgrade is not supported): ${_rb_images}. Their compose files keep the running tag. To go back for these, restore the pre-upgrade backup: rzfz backup restore <backup-file>"
    fi

    # #2393: engines on llm-engines go back on the network the restored tree uses
    # BEFORE anything is recreated (additive; holds if the recreate fails). After
    # the checkout, so an empty .env value resolves to the restored compose default.
    _rollback_engine_networks connect

    # Rebuild and restart
    if razzfazz_is_offline; then
        # #184 WS2b: on an air-gapped box the rolled-back code's images are already
        # present (built/loaded at the prior install) — do NOT build or pull (that
        # would trip the firewall). Recreate from the local images.
        print_substep "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): rolling back with existing local images (no build/pull)."
    else
        print_substep "Rebuilding images with rolled-back code..."
        # #184 WS2a: strip the no-build overlay so the build: contexts are present.
        # #2659: only a build that succeeded is recorded as built; a failed one
        # records nothing as built (empty list) and refreshes the verdicts only.
        if COMPOSE_FILE="$(compose_file_for_build)" razzfazz_compose_build --parallel 2>&1; then
            razzfazz_record_custom_image_builds "" built   # #2006 part 2: the rolled-back tree's build
        else
            print_warning "Some builds failed."
            razzfazz_record_custom_image_builds "" built "" ""
        fi
        docker compose pull --ignore-pull-failures 2>&1 || true
    fi
    docker compose up -d --force-recreate 2>&1

    # #2393: the rolled-back router and manager are up; detach llm-engines.
    _rollback_engine_networks finish

    if [ "${ROLLBACK_SKIP_REFRESH:-}" = "true" ]; then
        # The automatic recovery after a failed build_and_pull: the self-heal would
        # repeat the build/pull work that just failed and bury the real error.
        print_info "Self-heal skipped after a failed build; once the cause is fixed run: rzfz upgrade (or rzfz post-install --refresh)."
    else
        upgrade_post_install_refresh rollback   # #2536 item 30
    fi

    print_success "Rollback complete."
    print_warning "A rollback restores the code and .env, not data: database schema and data changes made by the newer release are NOT reverted."
    print_info "To go back including data, restore the pre-upgrade backup: rzfz backup restore <backup-file>   (list them: rzfz backup list)"
}

# ==============================================================================
# Post-Upgrade Acceptance Probes (--with-acceptance, M032-S06)
# ==============================================================================
# Wait up to RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT seconds (default 300) for
# every container to leave its starting/restarting/unhealthy state, then
# invoke `rzfz test --ci-mode --acceptance all` (plus
# `--include-disabled` if the operator passed the long-form flag). Returns 0
# on probe success, 4 on probe failure (exit code 4 = probes-failed,
# distinct from 1 = upgrade-failed). Mirrors the helper in razzfazz-init.sh.
run_acceptance_probes() {
    print_step "Running post-upgrade acceptance probes..."

    # #1326: a package / public-export box ships no tests/; refuse before the
    # health wait and the venv bootstrap, so nothing is written to the box.
    if [ ! -f "$SCRIPT_DIR/tests/requirements.txt" ] || [ ! -d "$SCRIPT_DIR/tests/acceptance" ]; then
        print_error "This delivery does not ship the acceptance tests (tests/); --with-acceptance needs a development checkout."
        return 4
    fi

    local timeout="${RAZZFAZZ_ACCEPTANCE_HEALTHZ_TIMEOUT:-300}"
    local interval=10
    local elapsed=0
    local unhealthy

    while [ "$elapsed" -lt "$timeout" ]; do
        unhealthy=$(docker compose ps --format '{{.Name}}\t{{.Status}}' 2>/dev/null \
            | grep -cE 'starting|unhealthy|restarting' || true)
        unhealthy="${unhealthy:-0}"
        if [ "$unhealthy" -eq 0 ]; then
            break
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done

    if [ "$elapsed" -ge "$timeout" ]; then
        print_warning "Some containers did not reach a healthy state within ${timeout}s."
        print_info "Running probes anyway — they will report which container is unhealthy."
    fi

    local wrapper="$SCRIPT_DIR/cli/test.sh"
    if [ ! -x "$wrapper" ]; then
        print_error "Test runner not found at $wrapper — cannot run acceptance probes."
        return 4
    fi

    "$wrapper" --bootstrap-only >/dev/null 2>&1 || {
        print_error "Test venv bootstrap failed — cannot run acceptance probes."
        return 4
    }

    local probe_args=(--ci-mode --acceptance)
    if [ "$WITH_ACCEPTANCE_INCLUDE_DISABLED" = true ]; then
        probe_args+=(--include-disabled)
    fi
    probe_args+=(all)

    local rc=0
    "$wrapper" "${probe_args[@]}" || rc=$?
    if [ "$rc" -eq 0 ]; then
        print_success "All acceptance probes passed."
        return 0
    fi
    if [ "$rc" -eq 5 ]; then
        print_error "No acceptance probes were collected — see M032 S02."
    else
        print_error "One or more acceptance probes failed (probe-suite RC=$rc)."
    fi
    print_info "Report path: $SCRIPT_DIR/tests/results/latest/acceptance-report.md"
    return 4
}

# ==============================================================================
# Container Update Engine (--update)
# ==============================================================================
# Applies vendor-curated container version bumps from the manifest without
# changing stack code, configuration, or features.

MANIFESTS_DIR="${SCRIPT_DIR}/config/manifests"
MANIFEST_FILE="${MANIFESTS_DIR}/versions.json"

update_download_manifest() {
    local manifest_url
    # #372: path via argv, not interpolated into the program.
    manifest_url=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get('manifest_url', ''))
" "${MANIFEST_FILE}" 2>/dev/null)

    if [ -z "$manifest_url" ]; then
        print_error "No manifest_url found in ${MANIFEST_FILE}."
        return 1
    fi

    local tmpfile="${MANIFESTS_DIR}/.versions.json.tmp"
    local checksum_url="${manifest_url}.sha256"
    local tmpsha="${MANIFESTS_DIR}/.versions.json.sha256.tmp"
    rm -f "$tmpfile" "$tmpsha"

    # #660: on internal-channel boxes the raw manifest URL sits behind the
    # Authentik proxy — an anonymous curl gets 302 → login-flow → 200 with
    # an HTML page, so the HTTP happy path can never verify (the #646 guard
    # then refuses, correctly but unhelpfully). Fetch through the box's git
    # credentials instead: same PAT, same trust anchor as the code itself.
    # Falls back to the HTTP path (public boxes; git hiccups) below.
    if [ "$(razzfazz_channel)" = "internal" ] \
       && git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        print_substep "Fetching manifest via git credentials (internal channel, #660)..."
        # #2690: a PAT entered for this run if one is held; never a prompt.
        # No credential means the HTTP fallback below.
        if razzfazz_git_origin -C "$SCRIPT_DIR" fetch --quiet origin main 2>/dev/null \
           && git -C "$SCRIPT_DIR" show origin/main:config/manifests/versions.json > "$tmpfile" 2>/dev/null; then
            git -C "$SCRIPT_DIR" show origin/main:config/manifests/versions.json.sha256 > "$tmpsha" 2>/dev/null \
                || rm -f "$tmpsha"
        else
            rm -f "$tmpfile"
            print_warning "git fetch of the manifest failed — falling back to HTTP (${manifest_url})."
        fi
    fi

    if [ ! -s "$tmpfile" ]; then
        print_substep "Downloading manifest from ${manifest_url}..."
        if ! curl -fsSL -o "$tmpfile" "$manifest_url"; then
            print_error "Failed to download manifest."
            return 1
        fi
        curl -fsSL -o "$tmpsha" "$checksum_url" 2>/dev/null || rm -f "$tmpsha"
    fi

    # Verify. #373: verification is MANDATORY — an attacker who can serve a
    # malicious manifest can equally 404 the .sha256, so a warn-and-proceed
    # here is no control at all. Missing/unfetchable checksum → refuse; only
    # an explicit operator --force proceeds. One shared verify path for both
    # transports (#660).
    if [ -s "$tmpsha" ]; then
        local expected
        expected=$(awk '{print $1}' "$tmpsha")
        rm -f "$tmpsha"
        if ! printf '%s' "$expected" | grep -qE '^[0-9a-f]{64}$'; then
            # A captive portal / error page served as .sha256 must not be
            # compared as if it were a hash (#373).
            print_error "Checksum file is not a sha256 hash (got: ${expected:0:40})."
            rm -f "$tmpfile"
            return 1
        fi
        local actual
        actual=$(sha256sum "$tmpfile" | awk '{print $1}')
        if [ "$expected" != "$actual" ]; then
            print_error "Checksum mismatch!"
            print_error "  Expected: ${expected}"
            print_error "  Actual:   ${actual}"
            rm -f "$tmpfile"
            return 1
        fi
        print_success "Checksum verified."
    elif [ "$FORCE" = true ]; then
        print_warning "No checksum file at ${checksum_url} — proceeding UNVERIFIED because --force was passed."
        print_warning "The manifest decides which container images this box pulls and runs."
    else
        print_error "No checksum file at ${checksum_url} — refusing the unverified manifest (#373)."
        print_error "The manifest drives which container images this box pulls; without its"
        print_error ".sha256 it cannot be distinguished from an attacker-served file."
        print_info  "Operator override (knowingly skip verification): re-run with --force."
        rm -f "$tmpfile"
        return 1
    fi

    mv -f "$tmpfile" "${MANIFESTS_DIR}/versions-remote.json"
    print_success "Remote manifest saved."
}

update_compute_plan() {
    # Reads the manifest and compares against current .env / compose versions.
    # Outputs the update plan as JSON to stdout.
    # Args: $1 = manifest file path
    local manifest_path="${1:-${MANIFESTS_DIR}/versions-remote.json}"

    python3 << 'PYEOF'
import json, os, re, sys

manifest_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MANIFEST_PATH", "")
env_path = os.path.join(os.environ.get("SCRIPT_DIR", "."), ".env")

# Load manifest
with open(manifest_path) as f:
    manifest = json.load(f)

# Load .env
env = {}
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

# Load enabled profiles
enabled_profiles = set()
for p in env.get('COMPOSE_PROFILES', '').split(','):
    p = p.strip()
    if p:
        enabled_profiles.add(p)
# core is always enabled
enabled_profiles.add('core')

def strip_prefix(v):
    return v.lstrip('v')

def parse_semver(v):
    """Parse version into (major, minor, patch) tuple, ignoring suffixes."""
    v = v.lstrip('v')
    # Remove non-numeric suffix after last number group
    parts = re.split(r'[.\-]', v)
    nums = []
    for p in parts:
        m = re.match(r'^(\d+)', p)
        if m:
            nums.append(int(m.group(1)))
        else:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])

def is_within_compat(current, new, compat):
    if compat == 'frozen':
        return False
    cur = parse_semver(current)
    nw = parse_semver(new)
    if compat == 'patch':
        return cur[0] == nw[0] and cur[1] == nw[1] and nw[2] >= cur[2]
    if compat == 'minor':
        return cur[0] == nw[0] and (nw[1] > cur[1] or (nw[1] == cur[1] and nw[2] >= cur[2]))
    if compat == 'major':
        # publish-manifest.sh (#374) accepts {patch, minor, major, frozen}, but
        # this function only knew patch/minor and returned False for anything
        # else — so `major` on an env-controlled image was a SILENT no-op
        # ("version X outside major range"), which reads as "nothing to do"
        # rather than "this script does not understand your manifest".
        # `major` = any FORWARD move is allowed; a manifest that regresses a
        # version is still refused, so a running box is never downgraded.
        return nw >= cur
    return False

plan = {"updates": [], "skipped": [], "frozen": [], "up_to_date": []}

# ENV-controlled images
for key, entry in manifest.get("images", {}).items():
    profile = entry.get("profile", "")
    if profile not in enabled_profiles:
        plan["skipped"].append({"key": key, "reason": f"profile '{profile}' not enabled"})
        continue

    env_var = entry.get("env_var", "")
    current = env.get(env_var, "")
    manifest_ver = entry.get("current", "")
    compat = entry.get("compatibility", "patch")

    if not current:
        plan["skipped"].append({"key": key, "reason": f"env var {env_var} not set"})
        continue

    if strip_prefix(current) == strip_prefix(manifest_ver):
        plan["up_to_date"].append({"key": key, "version": current})
        continue

    if compat == "frozen":
        plan["frozen"].append({"key": key, "current": current, "manifest": manifest_ver})
        continue

    if is_within_compat(current, manifest_ver, compat):
        plan["updates"].append({
            "key": key,
            "type": "env",
            "image": entry["image"],
            "env_var": env_var,
            "from": current,
            "to": manifest_ver,
            "compat": compat,
            "profile": profile,
        })
    else:
        plan["skipped"].append({
            "key": key,
            "reason": f"version {manifest_ver} outside {compat} range from {current}",
        })

# Hardcoded images
for key, entry in manifest.get("hardcoded", {}).items():
    profile = entry.get("profile", "")
    if profile not in enabled_profiles:
        plan["skipped"].append({"key": key, "reason": f"profile '{profile}' not enabled"})
        continue

    compat = entry.get("compatibility", "patch")
    manifest_ver = entry.get("current", "")
    compose_file = entry.get("compose_file", "")

    if compat == "frozen":
        plan["frozen"].append({"key": key, "current": "(hardcoded)", "manifest": manifest_ver})
        continue

    # Every entry passes ONE gate. This loop short-circuited on `frozen` and
    # then appended UNGATED, so an unknown or mistyped compatibility token
    # silently meant "always update" here — including for postgres-vanilla,
    # where a major bump is data-destroying — while the SAME token on an
    # env-controlled image meant "never update". A hardcoded image's current
    # version lives in a compose file, not in .env, so there is no version to
    # compare here; what can and must be checked is that the token is one this
    # script understands.
    if compat not in ("patch", "minor", "major"):
        plan["skipped"].append({
            "key": key,
            "reason": f"unknown compatibility token '{compat}' (expected patch|minor|major|frozen) — refusing to plan an update",
        })
        continue

    plan["updates"].append({
        "key": key,
        "type": "hardcoded",
        "image": entry["image"],
        "compose_file": compose_file,
        "from": "(check compose)",
        "to": manifest_ver,
        "compat": compat,
        "profile": profile,
    })

# CVE alerts
plan["cve_alerts"] = manifest.get("cve_alerts", [])
plan["manifest_version"] = manifest.get("stack_version", "unknown")
plan["published"] = manifest.get("published", "unknown")

json.dump(plan, sys.stdout, indent=2)
PYEOF
}

update_display_plan() {
    local plan_json="$1"

    local update_count
    update_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['updates']))")
    local frozen_count
    frozen_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['frozen']))")
    local uptodate_count
    uptodate_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['up_to_date']))")
    local manifest_ver
    manifest_ver=$(echo "$plan_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['manifest_version'])")
    local published
    published=$(echo "$plan_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['published'])")

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║              Container Update Plan                               ║${NC}"
    echo -e "${CYAN}╠══════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${CYAN}║${NC} Manifest: ${manifest_ver}  Published: ${published}"
    echo -e "${CYAN}║${NC} Updates: ${update_count}  Up-to-date: ${uptodate_count}  Frozen: ${frozen_count}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    if [ "$update_count" -gt 0 ]; then
        echo -e "${BLUE}Updates to apply:${NC}"
        echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for u in plan['updates']:
    arrow = u['from'] + ' → ' + u['to']
    print(f\"  {u['key']:30s} {arrow:40s} [{u['compat']}] ({u['profile']})\")
"
        echo ""
    fi

    if [ "$frozen_count" -gt 0 ]; then
        echo -e "${YELLOW}Frozen (not updated):${NC}"
        echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for f in plan['frozen']:
    print(f\"  {f['key']:30s} {f['current']:20s} (frozen)\")
"
        echo ""
    fi

    # CVE alerts
    local cve_count
    cve_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('cve_alerts',[])))")
    if [ "$cve_count" -gt 0 ]; then
        echo -e "${RED}CVE Alerts:${NC}"
        echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for c in plan.get('cve_alerts', []):
    print(f\"  ⚠ {c['cve']} ({c['severity']}) — {c['image']} fixed in {c['fixed_in']}\")
"
        echo ""
    fi
}

update_apply() {
    local plan_json="$1"

    local update_count
    update_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['updates']))")

    if [ "$update_count" -eq 0 ]; then
        print_success "All container images are up to date. Nothing to do."
        return 0
    fi

    # Save current manifest as previous for rollback
    if [ -f "${MANIFESTS_DIR}/applied.json" ]; then
        cp -f "${MANIFESTS_DIR}/applied.json" "${MANIFESTS_DIR}/previous.json"
        print_substep "Previous manifest saved for rollback."
    fi

    # Apply env-var updates
    print_step "Applying version updates..."
    echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for u in plan['updates']:
    if u['type'] == 'env':
        print(f\"{u['env_var']}={u['to']}\")
" | while IFS='=' read -r key value; do
        # #2629: the same direction rule as the upgrade's pin syncs — a stateful
        # service is never lowered over data a newer version migrated.
        [ "$(pin_sync_decision "$key" "$(read_env_value ".env" "$key")" "$value")" = sync ] || continue
        update_env_value ".env" "$key" "$value"
        print_substep "  ${key}=${value}"
    done

    # Apply hardcoded updates (compose file edits)
    echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
for u in plan['updates']:
    if u['type'] == 'hardcoded':
        print(f\"{u['image']}|{u['to']}|{u['compose_file']}\")
" | while IFS='|' read -r image new_ver compose_file; do
        if [ -f "$compose_file" ]; then
            # #2629: a service that migrates its data is never lowered.
            local _have_tag
            _have_tag=$(grep -oE "image: *${image}:[^ ]*" "$compose_file" | head -1 | sed -E 's/.*://')
            [ "$(hardcoded_image_sync_decision "$image" "$_have_tag" "$new_ver")" = sync ] || continue
            # Replace image:OLD_TAG with image:NEW_TAG
            sed -i -E "s|(image: *${image}:)[^ ]*|\1${new_ver}|g" "$compose_file"
            print_substep "  ${compose_file}: ${image}:${new_ver}"
        fi
    done

    # Collect affected profiles for targeted restart
    local profiles
    profiles=$(echo "$plan_json" | python3 -c "
import sys, json
plan = json.load(sys.stdin)
profiles = set()
for u in plan['updates']:
    profiles.add(u['profile'])
print(','.join(sorted(profiles)))
")

    # Pull updated images
    if razzfazz_is_offline; then
        # #184 WS2b: an air-gapped box gets image updates from the offline package,
        # never from a registry. Skip the pull; the load happened at package apply.
        print_step "OFFLINE (RAZZFAZZ_NETWORK_MODE=offline): skipping image pull — updates come from the offline package."
    else
        print_step "Pulling updated images..."
        docker compose pull --ignore-pull-failures 2>&1 || print_warning "Some image pulls failed."
    fi

    # Restart affected services
    print_step "Restarting affected services..."
    docker compose up -d --force-recreate 2>&1

    # Save applied manifest
    local remote_manifest="${MANIFESTS_DIR}/versions-remote.json"
    if [ -f "$remote_manifest" ]; then
        cp -f "$remote_manifest" "${MANIFESTS_DIR}/applied.json"
    fi

    print_success "Container updates applied successfully."
    print_info "Affected profiles: ${profiles}"
}

update_rollback() {
    if [ ! -f "${MANIFESTS_DIR}/previous.json" ]; then
        print_error "No previous manifest found. Cannot rollback."
        return 1
    fi

    print_step "Rolling back to previous container versions..."

    # Compute plan from previous manifest (treating it as the target)
    local plan_json
    MANIFEST_PATH="${MANIFESTS_DIR}/previous.json" \
    plan_json=$(SCRIPT_DIR="$SCRIPT_DIR" update_compute_plan "${MANIFESTS_DIR}/previous.json")

    update_display_plan "$plan_json"
    update_apply "$plan_json"

    # Swap manifests
    mv -f "${MANIFESTS_DIR}/previous.json" "${MANIFESTS_DIR}/applied.json"
    print_success "Rollback complete."
}

run_update() {
    local do_check="$1"
    local manifest_file="$2"
    local do_rollback="$3"

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║                 rzfz.ai Container Update                         ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Handle rollback
    if [ "$do_rollback" = "true" ]; then
        # Plain call, then the status: `|| return` would switch set -e off inside
        # update_rollback and a failed recreate would be recorded as applied.
        update_rollback
        local _rc=$?
        [ "$_rc" -eq 0 ] || return "$_rc"
        upgrade_post_install_refresh update   # #2536 item 30
        return 0
    fi

    # Download or use local manifest
    if [ -n "$manifest_file" ]; then
        if [ ! -f "$manifest_file" ]; then
            print_error "Manifest file not found: ${manifest_file}"
            return 1
        fi
        cp -f "$manifest_file" "${MANIFESTS_DIR}/versions-remote.json"
        print_success "Using local manifest: ${manifest_file}"
    else
        update_download_manifest || return 1
    fi

    # Compute update plan
    print_step "Computing update plan..."
    local plan_json
    plan_json=$(SCRIPT_DIR="$SCRIPT_DIR" MANIFEST_PATH="${MANIFESTS_DIR}/versions-remote.json" \
        update_compute_plan "${MANIFESTS_DIR}/versions-remote.json")

    # Display plan
    update_display_plan "$plan_json"

    # Check mode — stop here
    if [ "$do_check" = "true" ]; then
        print_info "Dry run complete. No changes were made."
        print_info "Run without --check to apply updates."
        return 0
    fi

    local update_count
    update_count=$(echo "$plan_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['updates']))")

    if [ "$update_count" -eq 0 ]; then
        print_success "All container images are up to date."
        return 0
    fi

    # Confirm
    if [ "$FORCE" != true ]; then
        read -p "Apply ${update_count} container update(s)? [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy] ]]; then
            print_warning "Update aborted by user."
            return 0
        fi
    fi

    update_apply "$plan_json"
    local _rc=$?
    [ "$_rc" -eq 0 ] || return "$_rc"
    upgrade_post_install_refresh update   # #2536 item 30
    return 0
}

# ==============================================================================
# --migrations-only (#2524)
# ==============================================================================
# Data restored from an older release (a staging box refreshed from a production
# backup) needs the stack-level migrations of <from> -> this tree: the
# env-changes.json blocks, the DB-role/ownership reconcile, the Step 5b profile
# and compose-chain migrations, the env reconcile of Step 6b/6c, the Authentik
# two-hop check against the DATA's Authentik version, and the post-restart data
# migrations. Everything else of an upgrade is skipped: no code update, no image
# build or pull, no host tunables or hardening, no pre-upgrade backup.
#
# The "from" version replaces INSTALLED_VERSION, which every range-gated step
# reads. --from-env names the source's .env (the restored production one): its
# AUTHENTIK_VERSION is the data's Authentik version (session flush, two-hop), its
# DIFY_VERSION/OPENWEBUI_VERSION decide the Dify/OWUI crossing migrations. Without
# it those fall back to the conservative answer (no flush; the crossings run once).
migrations_only_known_release() {   # $1 = version; 0 when this tree records it
    local v="$1"
    [ -d "${SCRIPT_DIR}/releases/${v}" ] && return 0
    python3 - "${SCRIPT_DIR}/config/migrations/env-changes.json" "$v" <<'PYEOF' 2>/dev/null && return 0
import json, sys
m = json.load(open(sys.argv[1]))
sys.exit(0 if any(e.get("version") == sys.argv[2] for e in m.get("versions", [])) else 1)
PYEOF
    command -v git >/dev/null 2>&1 && [ -d "${SCRIPT_DIR}/.git" ] \
        && git -C "${SCRIPT_DIR}" rev-parse -q --verify "refs/tags/${v}" >/dev/null 2>&1 && return 0
    return 1
}

validate_migrations_only_request() {
    local bad=""
    [ -z "$TARGET_TAG" ]   || bad="${bad} --target"
    [ -z "$PACKAGE_FILE" ] || bad="${bad} --package"
    [ -z "$EXPECT_SHA256" ] || bad="${bad} --expect-sha256"
    [ -z "${CONFIG_NETWORK_MODE:-}" ] || bad="${bad} --network-mode"
    [ "$DO_UPDATE" != true ]   || bad="${bad} --update"
    [ "$DO_ROLLBACK" != true ] || bad="${bad} --rollback"
    [ "$CONTINUE_AFTER_REEXEC" != true ] || bad="${bad} --_continue"
    [ "$PREVIEW_ONLY" != true ] || bad="${bad} --_preview"
    [ "$STOP_STACK_FOR_BUILD" != true ] || bad="${bad} --stop-stack-for-build"
    [ "$WITH_ACCEPTANCE" != true ] || bad="${bad} --with-acceptance"
    [ "${ALLOW_DOWNGRADE:-false}" != true ] || bad="${bad} --allow-downgrade"
    [ "$SKIP_PULL" != true ]   || bad="${bad} --skip-pull"
    [ "$SKIP_BACKUP" != true ] || bad="${bad} --skip-backup"
    [ "$SKIP_VERIFY" != true ] || bad="${bad} --skip-verify"
    [ "$SKIP_HOST_UPDATES" != true ] || bad="${bad} --skip-host-updates"
    [ -z "$UPDATE_FILE" ]      || bad="${bad} --file"
    [ "${#DEFERRED_OPTS[@]}" -eq 0 ] || bad="${bad} ${DEFERRED_OPTS[*]}"
    if [ -n "$bad" ]; then
        print_error "--migrations-only runs on the tree as it is; it does not combine with:${bad}. Nothing was changed."
        return 1
    fi
    case "$MIGRATIONS_FROM" in
        ""|unknown|0.0.0)
            print_error "--migrations-only needs --from <version>: the release the data comes from (e.g. the backup's RAZZFAZZ_VERSION). Nothing was changed."
            return 1 ;;
    esac
    if [ "$DRY_RUN" != "true" ] && [ "$FORCE" != true ] && [ ! -t 0 ]; then
        print_error "--migrations-only without a terminal needs --force (it writes .env and starts the stack). Nothing was changed."
        return 1
    fi
    if [ -n "$MIGRATIONS_FROM_ENV" ] && { [ ! -f "$MIGRATIONS_FROM_ENV" ] || [ ! -r "$MIGRATIONS_FROM_ENV" ]; }; then
        print_error "--from-env '${MIGRATIONS_FROM_ENV}' is not a readable file. Nothing was changed."
        return 1
    fi
    detect_target_version
    if [ "$TARGET_VERSION" = unknown ] || [ -z "$TARGET_VERSION" ]; then
        print_error "This tree has no VERSION file, so the migration range cannot be named. Nothing was changed."
        return 1
    fi
    local rc=0
    compare_versions "$MIGRATIONS_FROM" "$TARGET_VERSION" || rc=$?
    case "$rc" in
        0|1) ;;
        2)  print_error "--from ${MIGRATIONS_FROM} is NEWER than this tree (${TARGET_VERSION}): its data may carry migrations this tree does not know. Nothing was changed."
            return 1 ;;
        *)  print_error "--from '${MIGRATIONS_FROM}' is not a version this tree can order (expected e.g. 2026.09-ga.5). Nothing was changed."
            return 1 ;;
    esac
    if ! migrations_only_known_release "$MIGRATIONS_FROM"; then
        print_warning "--from ${MIGRATIONS_FROM}: no release notes, manifest block or tag of that name in this tree — the range is applied as given."
    fi
    INSTALLED_VERSION="$MIGRATIONS_FROM"
    INSTALLED_COMMIT="unknown"
    INSTALLED_VERSION_SYNTHETIC=false
    INSTALLED_DIRECTION_VERSION=""
    UPGRADE_FROM_ENV_FILE="${MIGRATIONS_FROM_ENV:-/dev/null}"
    # Health verification and the post-install self-heal are the caller's
    # (the staging driver runs post-install --refresh and rzfz status next).
    SKIP_VERIFY=true
    return 0
}

# ==============================================================================
# Main
# ==============================================================================

# #3: library-only sourcing hook. When set, return here so that test harnesses
# (and any other caller) can `source` this script to obtain its functions
# (e.g. check_git_credentials) WITHOUT running the upgrade body. All function
# definitions live above this point; nothing below is a function definition.
if [ "${RAZZFAZZ_UPGRADE_LIB_ONLY:-}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

# Defaults
TARGET_TAG=""
PACKAGE_FILE=""
# #2137: set only while code_update_package runs the backup; never inherited.
_UPGRADE_PKG_DIR_PENDING=""
# #2280: the model bundle's verified extraction, beside it; same EXIT cleanup.
_UPGRADE_BUNDLE_DIR_PENDING=""
# #2177: empty means "leave the box's mode alone". Deliberately NOT derived
# from --package: the operator rule is package-first on ANY box, so a package is
# not evidence of an air gap.
CONFIG_NETWORK_MODE=""
# #781: out-of-band transported SHA-256 for `--package`. Empty = not supplied.
EXPECT_SHA256=""
# #2280: one hardware class's model bundle, applied after the --package code/base.
MODEL_BUNDLE_FILE=""
MODEL_BUNDLE_SHA256=""
DRY_RUN="false"
SKIP_BACKUP=false
STOP_STACK_FOR_BUILD=false
SKIP_VERIFY=false
# #3293: --no-engine-relaunch — leave the LLM engines on the runner and container
# settings they were created with (upgrade_relaunch_engines says what is left).
NO_ENGINE_RELAUNCH=false
# #2873: set by verify_upgrade's final health gate.
UPGRADE_HEALTH_GATE_FAILED=false
SKIP_HOST_UPDATES=false   # F-RC5-5: bypass install_kernel_stability_tunables fatal-on-fail
SKIP_PULL=false
FORCE=false
DO_ROLLBACK=false
STASHED=false
DO_UPDATE=false
UPDATE_FILE=""
CONTINUE_AFTER_REEXEC=false
# #2138: --_preview — this run previews for another upgrader (see
# preview_with_target_tree); it is a check whatever else was passed.
PREVIEW_ONLY=false
# M032-S06: opt-in post-upgrade acceptance probe hook. Same semantics as
# razzfazz-init.sh's --with-acceptance: wait-for-healthy → run probes →
# probe failure surfaces as exit 4 (probes-failed, distinct from 1).
WITH_ACCEPTANCE=false
WITH_ACCEPTANCE_INCLUDE_DISABLED=false
# #2524: --migrations-only --from <version> — the stack-level migration steps of
# <version> -> this tree, on the tree and stack as they are (no code update, no
# image pull or build, no host changes). For data restored from an older release.
MIGRATIONS_ONLY=false
MIGRATIONS_FROM=""
MIGRATIONS_FROM_ENV=""
UPGRADE_FROM_ENV_FILE=""
# #2260/#2215: the argv exactly as typed. The re-exec hands ALL of it to the new
# script, so a flag this script does not know, or one it reads only after the
# re-exec (--network-mode, --stop-stack-for-build, --with-acceptance), is not
# lost on the way.
UPGRADE_ORIG_ARGS=("$@")
DEFERRED_OPTS=()
DEFERRED_PKG_HINT=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --_continue)
            # Internal flag: script was re-exec'd after code update
            CONTINUE_AFTER_REEXEC=true
            restore_reexec_state
            shift
            ;;
        --_preview)
            # Internal flag (#2138): the preview-only contract. An installed
            # upgrader runs this script with it, from a staged copy, to answer
            # its --check. Checked right after the parse.
            PREVIEW_ONLY=true
            shift
            ;;
        --update)
            DO_UPDATE=true
            shift
            ;;
        --file)
            UPDATE_FILE="$2"
            shift 2
            ;;
        --target)
            TARGET_TAG="$2"
            shift 2
            ;;
        --package)
            PACKAGE_FILE="$2"
            shift 2
            ;;
        --network-mode)
            CONFIG_NETWORK_MODE="$2"
            shift 2
            ;;
        --expect-sha256)
            # #781: the out-of-band transported hash. The operator reads it
            # from the release notes or the fleet channel — NOT from the stick.
            # This is the authenticity check that works today, with no key
            # management at all; the detached-signature form is the other half
            # and needs no flag (the sidecar is found beside the archive).
            EXPECT_SHA256="$2"
            shift 2
            ;;
        --model-bundle)
            # #2280: one hardware class's models, applied after the --package
            # code/base. The re-exec hands it on with the rest of the argv (#2260).
            MODEL_BUNDLE_FILE="$2"
            shift 2
            ;;
        --model-bundle-sha256)
            MODEL_BUNDLE_SHA256="$2"
            shift 2
            ;;
        --check|--dry-run)
            DRY_RUN="true"
            shift
            ;;
        --skip-backup)
            SKIP_BACKUP=true
            shift
            ;;
        --stop-stack-for-build)
            # #693: free the RAM for the build phase on small boxes — the
            # stack is stopped right before build_and_pull and comes back
            # via the regular restart_stack step. Deliberate opt-in: the
            # default path keeps the stack up (downtime-minimizing design).
            STOP_STACK_FOR_BUILD=true
            shift
            ;;
        --skip-verify)
            SKIP_VERIFY=true
            shift
            ;;
        --no-engine-relaunch)
            NO_ENGINE_RELAUNCH=true
            shift
            ;;
        --skip-host-updates)
            # F-RC5-5: skip the fatal-on-fail kernel-stability-tunables install
            SKIP_HOST_UPDATES=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --allow-downgrade)
            # #1587: deliberately NOT folded into --force. --force skips
            # confirmations; this one crosses a rule the migrations cannot
            # reverse, so it has to be asked for by name.
            ALLOW_DOWNGRADE=true
            shift
            ;;
        --rollback)
            DO_ROLLBACK=true
            shift
            ;;
        --migrations-only)
            MIGRATIONS_ONLY=true
            shift
            ;;
        --from)
            [ $# -gt 1 ] || { print_error "--from needs a version (e.g. 2026.09-ga.5)."; exit 1; }
            MIGRATIONS_FROM="$2"
            shift 2
            ;;
        --from-env)
            [ $# -gt 1 ] || { print_error "--from-env needs a file."; exit 1; }
            MIGRATIONS_FROM_ENV="$2"
            shift 2
            ;;
        --with-acceptance)
            # M032-S06: post-upgrade acceptance probes. Tests only currently-
            # enabled profiles; probe failure → exit 4 (probes-failed).
            WITH_ACCEPTANCE=true
            shift
            ;;
        --with-acceptance-include-disabled)
            # M032-S06: as above, plus cycle each disabled profile up→probe→down.
            WITH_ACCEPTANCE=true
            WITH_ACCEPTANCE_INCLUDE_DISABLED=true
            shift
            ;;
        --status)
            # rc5.1+: read-only assessment via razzfazz-status.sh.
            # Exec'd directly so the operator's flags pass through and exit
            # codes match the dedicated tool.
            shift
            exec "${SCRIPT_DIR}/rzfz" status "$@"
            ;;
        -h|--help)
            show_help
            ;;
        --*)
            # #2215: an option this upgrader does not know may be one the
            # TARGET release introduces. Defer it: validate_deferred_options
            # decides after the parse, the target's upgrader is asked before the
            # tree moves, and the re-exec hands it on in UPGRADE_ORIG_ARGS.
            DEFERRED_OPTS+=("$1")
            if [ $# -gt 1 ] && [[ "$2" != -* ]]; then
                # a mistyped --package swallows the archive path: remember it
                # for the refusal's "did you mean" line
                case "$2" in *.tar.gz|*.tgz) DEFERRED_PKG_HINT="$2" ;; esac
                shift 2
            else
                shift
            fi
            ;;
        *)
            print_error "Unknown option: $1"
            echo "Use --help for usage information."
            exit 1
            ;;
    esac
done
# #2137: "the operator already answered" is only ever handed over by this
# script's own re-exec; a fresh run never takes it from the environment.
[ "$CONTINUE_AFTER_REEXEC" = true ] || unset _UPGRADE_CONFIRMED
[ "$CONTINUE_AFTER_REEXEC" = true ] || unset _UPGRADE_IMAGE_PREFLIGHT
[ "$CONTINUE_AFTER_REEXEC" = true ] || unset _UPGRADE_BACKUP_PAUSED
# PREVIEW_CONTRACT-BEGIN
# #2138: --_preview is a check, and only ever a hand-off from another upgrader.
if [ "$PREVIEW_ONLY" = true ]; then
    DRY_RUN="true"
    if [ "$CONTINUE_AFTER_REEXEC" != true ] || [ "$DO_ROLLBACK" = true ] || [ "$DO_UPDATE" = true ]; then
        print_error "--_preview is internal: another upgrader's preview hand-off (with --_continue), never with --rollback or --update. Nothing was changed."
        exit 1
    fi
fi
# PREVIEW_CONTRACT-END
# CHECK_READ_ONLY-BEGIN
# #2138: a check writes nothing to the box but its own .upgrade.log. No
# bytecode caches from its python, and no journal: the journal's per-run files
# would pile up in backups/logs, and .upgrade.log already records the check
# (under the rotation rule). An inherited journal is not appended to either.
if [ "$DRY_RUN" = "true" ]; then
    export PYTHONDONTWRITEBYTECODE=1
    unset RAZZFAZZ_JOURNAL_JSON RAZZFAZZ_JOURNAL_HUMAN
fi
# CHECK_READ_ONLY-END
validate_deferred_options
refuse_undeliverable_deferred_options
# #2613: never migrate a test fixture left live by a killed test-suite run.
refuse_leftover_env_test_backups "$SCRIPT_DIR" "rzfz upgrade" || exit 1

# #781: --expect-sha256 only means something for --package. Accepting it
# silently on a git-path upgrade would drop an EXPLICIT request to verify —
# the same silent-skip failure the check itself refuses for a malformed hash.
if [ -n "$EXPECT_SHA256" ] && [ -z "$PACKAGE_FILE" ]; then
    print_error "--expect-sha256 applies to --package only."
    print_info  "A git-path upgrade authenticates through the remote and its"
    print_info  "credentials, not through a hash of a downloaded archive."
    exit 1
fi
# MIGRATIONS_ONLY_VALIDATE-BEGIN
if [ "$MIGRATIONS_ONLY" = true ]; then
    validate_migrations_only_request || exit 1
elif [ -n "$MIGRATIONS_FROM" ] || [ -n "$MIGRATIONS_FROM_ENV" ]; then
    print_error "--from and --from-env belong to --migrations-only. Nothing was changed."
    exit 1
fi
# MIGRATIONS_ONLY_VALIDATE-END


# #2280: a model bundle rides on a --package upgrade, and a bundle of another
# hardware class is refused HERE — before the backup, the stash or the package.
# The check reads only the bundle's leading records (PACKAGE_INFO), not its
# weights; the full verification runs in code_update_package, before any change.
if [ -n "$MODEL_BUNDLE_SHA256" ] && [ -z "$MODEL_BUNDLE_FILE" ]; then
    print_error "--model-bundle-sha256 applies to --model-bundle only."
    exit 1
fi
# #2280 crossing: in a continuation whose pre-change half ran in an upgrader
# that did not handle the bundle (a release before 2026.11 — it deferred the
# flag, then took the backup, applied the package and loaded the images), the
# box is ALREADY changed. Refusing here would stop it half-upgraded under a
# false "Nothing was changed". The upgrade finishes without the bundle and says
# why (model_bundle_after_crossing, before the provisioning refresh).
MODEL_BUNDLE_CROSSING=false
MODEL_BUNDLE_SKIP_REASON=""
if [ -n "$MODEL_BUNDLE_FILE" ] && [ "$CONTINUE_AFTER_REEXEC" = true ]; then
    if [ "${_UPGRADE_MODEL_BUNDLE_HANDLED:-}" != 1 ]; then
        MODEL_BUNDLE_CROSSING=true
        if [ -z "$PACKAGE_FILE" ]; then
            MODEL_BUNDLE_SKIP_REASON="--model-bundle applies with --package only"
        elif [ ! -f "$MODEL_BUNDLE_FILE" ]; then
            MODEL_BUNDLE_SKIP_REASON="model bundle not found: ${MODEL_BUNDLE_FILE}"
        else
            MODEL_BUNDLE_SKIP_REASON="$(razzfazz_model_bundle_check "$MODEL_BUNDLE_FILE" "$(read_env_value "${SCRIPT_DIR}/.env" HARDWARE)" 2>&1)" \
                && MODEL_BUNDLE_SKIP_REASON=""
        fi
    fi
elif [ -n "$MODEL_BUNDLE_FILE" ]; then
    if [ -z "$PACKAGE_FILE" ]; then
        print_error "--model-bundle applies with --package only (#2280)."
        exit 1
    fi
    if [ ! -f "$MODEL_BUNDLE_FILE" ]; then
        print_error "Model bundle not found: ${MODEL_BUNDLE_FILE}"
        exit 1
    fi
    if ! razzfazz_model_bundle_check "$MODEL_BUNDLE_FILE" "$(read_env_value "${SCRIPT_DIR}/.env" HARDWARE)"; then
        print_error "Model bundle refused (see above). Nothing was changed."
        exit 1
    fi
fi

# Initialize upgrade log. #2260: the re-exec'd continuation APPENDS — the
# first half of the same upgrade (verify, backup, apply) is already in it.
if [ "$CONTINUE_AFTER_REEXEC" = true ]; then
    echo "=== continued after re-exec: $(date -Iseconds) ===" >> "$UPGRADE_LOG"
else
    # #2137: a fresh run no longer throws the previous log away. The log of a
    # run that changed the box ("=== changes begin", written once the backup
    # verdict lets the run go on) — or of an older upgrader, which does not
    # write the marker — moves to .upgrade.log.prev. Only the log of a run of
    # this format that changed nothing (--check, declined, stopped) is replaced,
    # so .prev keeps the last upgrade that did something however many
    # --check or declined runs follow it.
    if [ -f "$UPGRADE_LOG" ] \
       && { ! grep -qx 'Log-format: 2' "$UPGRADE_LOG" || grep -q '^=== changes begin' "$UPGRADE_LOG"; }; then
        mv -f "$UPGRADE_LOG" "${UPGRADE_LOG}.prev"
    fi
    echo "=== rzfz.ai Upgrade Log ===" > "$UPGRADE_LOG"
    echo "Log-format: 2" >> "$UPGRADE_LOG"
    echo "Started: $(date -Iseconds)" >> "$UPGRADE_LOG"
fi
echo "Args: $0 ${UPGRADE_ORIG_ARGS[*]}" >> "$UPGRADE_LOG"

# #143 (A+B): open the structured upgrade journal. Gate on "not already active"
# rather than "not a re-exec continuation": when upgrading FROM a version whose
# razzfazz-upgrade.sh predates the journal (the real field case, e.g. ga.7→rc1),
# the fresh process is the OLD script (no journal_init) and only the re-exec'd
# NEW script can open it — so the continuation MUST init when RAZZFAZZ_JOURNAL_JSON
# is still unset. In the new→new case the fresh process inits, exports the var
# (survives `exec`), and the continuation sees it set and keeps the same journal.
# The EXIT trap is registered unconditionally so both finalize (no-op if inactive).
if [ -z "${RAZZFAZZ_JOURNAL_JSON:-}" ] && [ "$DRY_RUN" != "true" ]; then
    __RZ_RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
    journal_init "${SCRIPT_DIR}/backups/logs" "$__RZ_RUN_ID" \
        "installed=${INSTALLED_VERSION:-unknown}" "dry_run=${DRY_RUN:-false}"
fi
# #142 (lightweight): on a non-zero exit of a REAL upgrade run (guarded by
# _REAL_UPGRADE_STARTED so --check/--help/--rollback don't trigger it), also
# bundle logs + ask the operator to email them. The once-guard inside
# collect_failure_logzip dedupes with the verify-issues path.
trap '__rc=$?; trap "" PIPE; upgrade_resume_backup_schedule || true; restore_stack_stopped_for_build || true; [ -z "${_UPGRADE_PKG_DIR_PENDING:-}" ] || rm -rf "$_UPGRADE_PKG_DIR_PENDING"; [ -z "${_UPGRADE_BUNDLE_DIR_PENDING:-}" ] || rm -rf "$_UPGRADE_BUNDLE_DIR_PENDING"; [ -z "${_UPGRADE_IMG_STAGE_PENDING:-}" ] || rm -rf "$_UPGRADE_IMG_STAGE_PENDING"; journal_finalize "$([ "$__rc" -eq 0 ] && echo success || echo failure)"; { [ "$__rc" -ne 0 ] && [ -n "${_REAL_UPGRADE_STARTED:-}" ] && collect_failure_logzip "upgrade-aborted-rc${__rc}"; } || true' EXIT

print_banner

# F-RC5-5 (rc5.1): prime sudo timestamp early so install_kernel_stability_
# tunables (Step 5c) and any other sudo-needing steps don't fail mid-flow.
# Skipped in dry-run, --skip-host-updates, --update, --rollback, and
# --_continue (the re-execed second invocation inherits the parent's sudo
# timestamp via TTY).
if [ "$DRY_RUN" != "true" ] \
   && [ "$SKIP_HOST_UPDATES" != true ] \
   && [ "$DO_UPDATE" != true ] \
   && [ "$DO_ROLLBACK" != true ] \
   && [ "$CONTINUE_AFTER_REEXEC" != true ] \
   && [ "$MIGRATIONS_ONLY" != true ]; then
    if ! sudo -n true 2>/dev/null; then
        # No cached sudo. If we have no controlling tty (nohup, disowned SSH,
        # CI), `sudo -v` would block on a password prompt that nobody can
        # answer — fail fast with an actionable message instead of hanging.
        if [ ! -t 0 ] || [ ! -t 1 ]; then
            echo ""
            print_error "Non-interactive run detected and sudo password is not cached."
            print_info  "This upgrade installs system files (kernel-stability sysctl tunables)"
            print_info  "and would prompt for a password. Either:"
            print_info  "  • re-run with --skip-host-updates (bypasses sysctl + host hardening), or"
            print_info  "  • prime sudo first:  sudo -v   (then immediately re-run the upgrade)"
            exit 1
        fi
        echo ""
        echo "This upgrade installs system files (kernel-stability sysctl tunables)."
        echo "Please enter your sudo password to prime the timestamp:"
        if ! sudo -v; then
            echo ""
            print_error "sudo authentication failed."
            print_info "Re-run with --skip-host-updates to bypass host-side changes."
            exit 1
        fi
    fi
fi

# Handle --update mode (container version updates only)
if [ "$DO_UPDATE" = true ]; then
    run_update "$DRY_RUN" "$UPDATE_FILE" "$DO_ROLLBACK"
    exit $?
fi

# Handle --update --rollback (redirect to update rollback)
if [ "$DO_ROLLBACK" = true ] && [ -n "$UPDATE_FILE" ]; then
    DO_UPDATE=true
    run_update "false" "" "true"
    exit $?
fi

# Handle rollback
if [ "$DO_ROLLBACK" = true ]; then
    rollback
    exit $?
fi

# #142 (lightweight): a real upgrade body is about to run — arm the EXIT-trap
# log-bundle-on-failure (skipped for dry-run / --check). --update / --rollback
# already returned above, so reaching here means an actual upgrade.
[ "$DRY_RUN" != "true" ] && _REAL_UPGRADE_STARTED=1

# If this is a re-exec'd continuation, skip pre-flight / backup / code update
if [ "$CONTINUE_AFTER_REEXEC" = true ]; then
    print_step "Continuing upgrade from re-exec'd script (v${TARGET_VERSION})..."
elif [ "$MIGRATIONS_ONLY" = true ]; then
    # #2524: no pre-flight against a remote, no backup, no code update.
    print_step "Migrations only: v${INSTALLED_VERSION} -> v${TARGET_VERSION}, on this tree and stack as they are (no code update, no image pull or build, no host changes)."
    [ -z "$MIGRATIONS_FROM_ENV" ] || print_substep "Source .env for the app versions of the data: ${MIGRATIONS_FROM_ENV}"
    echo "Migrations only: from=${INSTALLED_VERSION} to=${TARGET_VERSION}" >> "$UPGRADE_LOG"
    # Asked before the first write (migrate_env). Nothing is built, pulled or
    # checked out, so the summary says what DOES happen.
    if [ "$DRY_RUN" != "true" ] && [ "$FORCE" != true ]; then
        echo ""
        echo -e "${YELLOW}Migrations only: v${INSTALLED_VERSION} -> v${TARGET_VERSION} on this tree${NC}"
        echo "  Writes .env/.env.dify (the range's env changes, missing secrets), reconciles the DB"
        echo "  roles, starts the stack (rzfz start) and runs the data migrations of the range."
        echo "  No code update, no image build or pull, no backup, no host changes."
        read -r -p "Run the migrations? [y/N] " _mo_answer || _mo_answer=""
        if [[ ! "$_mo_answer" =~ ^[Yy] ]]; then
            print_info "Not run. Nothing was changed."
            exit 0
        fi
    fi
    # The late upgrade prompt (Step 6) is the code-update arm's; this run is answered.
    _UPGRADE_CONFIRMED=true
    # The DB role/ownership reconcile (Step 4b0-4c) asks the running postgres and
    # skips without it; say so here rather than only in its own line.
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -x postgres >/dev/null; then
        print_warning "postgres is not running: the DB role and ownership reconcile is skipped. For it, start postgres first (docker compose up -d postgres) and re-run."
    fi
else
    # Step 1: Pre-flight checks
    preflight_checks

    # #2137: the git arm knows its target now — ask before anything changes.
    if [ -z "$PACKAGE_FILE" ]; then
        confirm_upgrade_before_changes "${TARGET_TAG:-}" || exit 0
        # Would the pre-restart check refuse this target? Decided before the backup.
        upgrade_image_preflight_git_before_backup || true
    fi

    # Step 2: Pre-upgrade backup and the rollback record (#2178), after the
    # confirmation. The package arm asks inside code_update_package, once the
    # package is verified, and runs this there (#2137).
    if [ -z "$PACKAGE_FILE" ] || [ "$DRY_RUN" = "true" ]; then
        backup_and_record_before_changes
    fi

    # Step 3: Code update
    if [ -n "$PACKAGE_FILE" ]; then
        code_update_package
        # #2260: re-exec like the git arm, so this hop runs the TARGET's
        # migrations (Step 5b.2 #2308, the #2222 hand-off, every later fix).
        if [ "$DRY_RUN" != "true" ]; then
            reexec_if_updated
        fi
    elif [ "$DRY_RUN" != "true" ]; then
        code_update_git
        # #272: online git upgrade → clear any offline-package provenance marker
        # so `rzfz status` resumes the git tag-drift check for this box.
        update_env_value ".env" "RAZZFAZZ_UPGRADE_METHOD" "git"
        # Re-exec the new version of this script so all migration logic
        # and post-upgrade steps use the target version's code.
        reexec_if_updated
        # If reexec returns (shouldn't normally), continue in-place
    else
        # F1: Dry run with --target — read the VERSION file at the target ref
        # without touching the working tree. Falls back to current VERSION file
        # only when --target is not supplied.
        # #1466: a branch target previews the REMOTE tip (as of the last fetch).
        # #1337: a branch that origin knows but this box has never fetched used
        # to fall through to "using current code as target" — and then the dry
        # run compares the installed version with ITSELF, so the answer is
        # structurally always "No .env migrations needed". Measured on 0.79:
        # the target ref carried 2026.09-rc1 with 135 env_changes, `--check`
        # reported none. Fetch the ref instead of quietly answering a different
        # question. (A fetch touches only remote-tracking refs — the dry run
        # still changes nothing on the box.)
        if [ -n "$TARGET_TAG" ] && _pb=$(razzfazz_upgrade_target_branch "$TARGET_TAG") \
           && ! git rev-parse -q --verify "refs/remotes/origin/${_pb}" >/dev/null 2>&1; then
            razzfazz_upgrade_fetch_branch "$_pb" >/dev/null 2>&1 || true
        fi
        if [ -n "$TARGET_TAG" ] && _pb=$(razzfazz_upgrade_target_branch "$TARGET_TAG") \
           && git rev-parse -q --verify "refs/remotes/origin/${_pb}" >/dev/null 2>&1; then
            # #2138: a ref without VERSION is unreadable, not fatal under
            # set -e (a bare failing `git show` in the assignment exited 128).
            TARGET_VERSION=$({ git show "origin/${_pb}:VERSION" 2>/dev/null || true; } | tr -d '[:space:]')
            TARGET_COMMIT=$(git rev-list -1 "origin/${_pb}" --abbrev-commit 2>/dev/null)
            [ -n "$TARGET_VERSION" ] || { print_warning "Preview unavailable for a cross-version jump: origin/${_pb} carries no readable VERSION, so the jump cannot be named. Nothing was changed."; exit 3; }
            print_info "Dry run: previewing upgrade to ${TARGET_VERSION} (branch ${_pb} @ ${TARGET_COMMIT}, as of the last fetch)"
        elif [ -n "$TARGET_TAG" ] && git rev-parse -q --verify "refs/tags/${TARGET_TAG}" >/dev/null 2>&1; then
            TARGET_VERSION=$({ git show "${TARGET_TAG}:VERSION" 2>/dev/null || true; } | tr -d '[:space:]')
            TARGET_COMMIT=$(git rev-list -1 "${TARGET_TAG}" --abbrev-commit 2>/dev/null)
            if [ -z "$TARGET_VERSION" ]; then
                print_warning "Preview unavailable for a cross-version jump: tag ${TARGET_TAG} carries no readable VERSION, so the jump cannot be named. Nothing was changed."
                exit 3
            fi
            print_info "Dry run: previewing upgrade to ${TARGET_VERSION} (commit: ${TARGET_COMMIT})"
        elif [ -n "$TARGET_TAG" ] && [[ "$TARGET_TAG" != -* ]] \
             && git rev-parse -q --verify "${TARGET_TAG}^{commit}" >/dev/null 2>&1; then
            # #2138: a commit or a local branch — read like a tag, not
            # answered by the installed code.
            TARGET_VERSION=$({ git show "${TARGET_TAG}:VERSION" 2>/dev/null || true; } | tr -d '[:space:]')
            TARGET_COMMIT=$(git rev-list -1 "${TARGET_TAG}" --abbrev-commit 2>/dev/null)
            if [ -z "$TARGET_VERSION" ]; then
                print_warning "Preview unavailable for a cross-version jump: ${TARGET_TAG} carries no readable VERSION, so the jump cannot be named. Nothing was changed."
                exit 3
            fi
            print_info "Dry run: previewing upgrade to ${TARGET_VERSION} (${TARGET_TAG} @ ${TARGET_COMMIT})"
        else
            detect_target_version
            if [ -n "$TARGET_TAG" ]; then
                # #1337: the operator ASKED about a jump and is getting an
                # answer about standing still. That is not a footnote — with
                # installed == target the block selection cannot report
                # anything, so "No .env migrations needed" below would be a
                # free pass that says nothing about the jump.
                print_warning "Dry run: could not read VERSION at target '${TARGET_TAG}' (not a known tag, and not a branch on origin)."
                print_warning "  Falling back to the CURRENT code (${TARGET_VERSION}) — this preview compares the box with itself and therefore cannot show migrations for that jump. (#1337)"
                print_info    "  Fetch the ref first, or pass a tag that exists locally: git fetch --tags --force origin"
            else
                print_info "Dry run: no --target given; using the current checkout as target (${TARGET_VERSION})"
            fi
        fi
    fi
fi

# TARGET_PREVIEW-BEGIN
# #2138: a git-arm --check of another release hands the preview to that
# release's upgrader (the package arm does it inside code_update_package).
if [ "$DRY_RUN" = "true" ] && [ -z "$PACKAGE_FILE" ] && [ "$CONTINUE_AFTER_REEXEC" != true ] \
   && [ "$MIGRATIONS_ONLY" != true ]; then
    maybe_preview_with_target_ref
fi
# TARGET_PREVIEW-END

# Step 3b: #2543 — the files the code update wrote get git's modes back when a
# restrictive umask (CIS 027/077) made them unreadable to other users. Runs in
# the re-exec'd continuation too: there the checkout was done by the OLD
# script, which had no `umask 022` — so this is what heals the first hop.
if [ "$DRY_RUN" != "true" ] && [ -z "$PACKAGE_FILE" ] && [ "$MIGRATIONS_ONLY" != true ]; then
    print_substep "Checking the modes of the files the code update wrote (#2543)..."
    repair_repo_file_modes "$INSTALLED_COMMIT" HEAD \
        || print_warning "Some repo files stay unreadable to other users — see above (#2543)."
fi

# Step 3c: the image check, for a continuation whose first half did not settle it.
upgrade_image_gate_in_continuation

# Step 4: Environment migration
# #148: capture the pre-migration Authentik version so we can flush stale
# sessions if this upgrade bumps Authentik (sessions serialized by the old
# version fail to deserialize against the new model registry → LookupError 500
# on /application/o/authorize, the "Server Error" the field hit).
OLD_AUTHENTIK_VERSION="$(read_env_value ".env" "AUTHENTIK_VERSION")"
# #2524: with --migrations-only the data's Authentik version is the source's.
if [ "$MIGRATIONS_ONLY" = true ] && [ -n "$MIGRATIONS_FROM_ENV" ]; then
    OLD_AUTHENTIK_VERSION="$(read_env_value "$MIGRATIONS_FROM_ENV" "AUTHENTIK_VERSION")"
fi
migrate_env

# Step 4a: Reconcile stale *_VERSION pins against the shipped manifest (#177).
# Runs every upgrade, version-independent, right after migrate_env — catches
# any *_VERSION drift that no change_default rule (or the #169 sync inside
# migrate_env) ever covered, using config/manifests/versions.json as authority.
reconcile_version_pins_from_manifest

# Step 4b0: #2401 — the roles the (just-migrated) *_DB_USER keys point at must
# exist and authenticate BEFORE any container is recreated; otherwise abort.
if ! ensure_service_db_roles; then
    print_error "Upgrade aborted: per-service DB roles are not in place (#2401). Nothing was restarted."
    exit 1
fi

# Step 4b: Heal empty per-service *_DB_USER values (#184). Runs every upgrade,
# version-independent, right after migrate_env so any keys migrate_env added are
# already present.
reconcile_service_db_users

# Step 4c: Reassign per-service DB *table ownership* (#154). Must run after
# reconcile_service_db_users (which may have just healed *_DB_USER) and while
# postgres is up (pre-restart). No-op on healthy + docker-superuser boxes.
# #2524: every object kind in every schema, not only public tables.
reconcile_service_db_ownership || print_warning "DB-ownership reconcile incomplete: a service whose objects stay mis-owned fails its next migration (see the lines above)."

# Step 4d: #1059 P1.3 — the thin node's own half of the HARD rename
# (llm-node-agent -> llm-worker-agent; #1059-old-identity).
# Runs AFTER migrate_env, because the
# function refuses to start the new container against an env file that still
# spells the keys LLM_NODE_* (the #319 allow-list would drop the command key
# and the worker would leave the fleet with a 401).
#
# RENAME-OR-REFUSE, per the spec: a non-zero return means either nothing was
# touched or the old container was put back, and the upgrade ABORTS here rather
# than continuing with a node whose identity is half-moved. A silent skip is
# exactly the "stranded un-renamed worker" the design calls out as the risk.
# On a full box with no thin node the function reports "nothing to rename" and
# returns 0 — it is a no-op, not a skipped step.
print_step "Thin-node agent rename (#1059)..."
if ! rename_thin_node_agent; then
    print_error "Upgrade aborted: the thin-node agent rename did not complete."
    print_error "The node's state is described above; no further upgrade steps ran."
    exit 1
fi

# Step 5: Dify environment sync
sync_dify_env

# Step 5b: M018 Phase 6 / S06.6 — collapse the old llm-box / llm-experimental
# / llm-cpu profiles into the unified `llm` profile + HARDWARE selector.
# Idempotent (skips on stacks already on `llm`); only fires once per host.
#
# Defensive design (after 8.93/0.78 rc5 upgrade post-mortem 2026-04-28):
# always emit a step header BEFORE entering the function and an explicit
# "no migration needed" reason on early-return. Previous silent-return paths
# made it impossible for an operator (or a debug session post-fact) to
# distinguish "function never called" from "function called and saw nothing
# to do." If you ever see no Step 5b output at all in the upgrade log, that
# now strictly means the line below didn't execute — the function itself is
# guaranteed-noisy.
migrate_llm_profiles() {
    print_step "Step 5b: LLM profile migration (M018 Phase 6 / S06.6)"

    local env_file="${SCRIPT_DIR}/.env"
    if [ ! -f "$env_file" ]; then
        print_substep "No .env file at $env_file — skipping."
        return 0
    fi
    # `|| true` so a no-match doesn't propagate exit 1 through the pipeline
    # under `set -eo pipefail`, killing the script silently before the
    # explicit empty-check below can fire.
    local current_profiles
    current_profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ -z "$current_profiles" ]; then
        print_substep "COMPOSE_PROFILES is empty/missing in .env — skipping."
        return 0
    fi
    print_substep "Current COMPOSE_PROFILES: $current_profiles"

    # Detect any old profile name. Capture-then-grep instead of `if echo|tr|grep -q;`
    # — that pipeline pattern is fragile under `set -o pipefail` because grep -q
    # exits early and can SIGPIPE the upstream commands, flipping the if-condition
    # to false even when the pattern matched. Doing the comma-split into a variable
    # first removes the pipefail/grep-q interaction entirely.
    local profiles_lines
    profiles_lines=$(printf '%s\n' "$current_profiles" | tr ',' '\n')
    local old_profile=""
    # #1447 part b: `llm-cpu` is a RETIRED token now, so a box carrying it
    # enters this migration whether or not it came from 2026.04. It is listed
    # below in the same `elif` chain; the chain order is unchanged.
    if printf '%s\n' "$profiles_lines" | grep -Fxq "llm-experimental"; then
        old_profile="llm-experimental"
    elif printf '%s\n' "$profiles_lines" | grep -Fxq "llm-box"; then
        old_profile="llm-box"
    elif printf '%s\n' "$profiles_lines" | grep -Fxq "llm-cpu"; then
        old_profile="llm-cpu"
    fi

    if [ -z "$old_profile" ]; then
        print_substep "No legacy LLM profile (llm-experimental/llm-box/llm-cpu) in COMPOSE_PROFILES — already migrated or fresh install. No-op."
        return 0
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_step "Would migrate LLM profile: $old_profile → llm (M018 Phase 6 / S06.6)"
    else
        print_step "Migrating LLM profile: $old_profile → llm (M018 Phase 6 / S06.6)"
    fi

    # M029-S04: target profile depends on operator's intent (preserved
    # via the original profile name they had in 2026.04-ga) AND hardware:
    #
    #   llm-experimental → `llm-legacy`  (see below)
    #   llm-cpu          → `llm-legacy`  (#1447 part b: folded, HARDWARE=cpu)
    #   llm-box          → `llm-legacy`  (AMD operators land on the stable v0.7.1 default)
    #
    # #1447 (cutover C7a): `llm-experimental` used to become `llm` — the operator
    # had opted into v2.1.x, so we kept them there. v2.1.x is REMOVED, so that
    # target no longer exists and the migration would have written a profile no
    # compose file defines: docker compose accepts it in silence and starts
    # nothing, which is the worst of the three outcomes. They land on the stable
    # 0.7.1 line like every other AMD box, and Step 5b.2 then puts the LLM
    # Manager in front of it (#1446).
    #
    # NVIDIA isn't in the old-name set (rc4 added it under `llm` directly, so
    # NVIDIA upgraders were already on `llm` and don't enter this function).
    local target_profile target_hw
    case "$old_profile" in
        llm-experimental)
            target_profile="llm-legacy"
            target_hw="amd"
            ;;
        llm-cpu)
            # #1447 part b: the CPU line is `llm-legacy` + HARDWARE=cpu now.
            # The hardware carries the difference, the profile does not.
            target_profile="llm-legacy"
            target_hw="cpu"
            ;;
        llm-box)
            target_profile="llm-legacy"
            target_hw="amd"
            ;;
    esac
    print_substep "Target profile (M029-S04): $old_profile → $target_profile (hw=$target_hw)"

    # Replace old profile name with target_profile, then dedupe.
    local new_profiles
    new_profiles=$(echo "$current_profiles" | tr ',' '\n' \
        | sed "s/^${old_profile}\$/${target_profile}/" \
        | awk 'NF && !seen[$0]++' | paste -sd,)

    # COMPOSE_FILE picks the device overlay, and since #1448 the merged
    # GPUStack 0.7.1 service (`llm-legacy`) is what consumes one — it carries no
    # device wiring of its own — on any hardware line since #1447 part b. We default to
    # `compose.yml` (not empty) because
    # docker compose 2.40+ chokes on `COMPOSE_FILE=` (empty value), reading
    # the project root as a file: `read /path/to/stack: is a directory`,
    # aborting build/up. Found during M029-S04 big-bang test on 8.93.
    local target_compose_file="compose.yml"
    if [ "$target_profile" = "llm-legacy" ]; then
        target_compose_file="compose.yml:modules/llm/compose.devices.${target_hw}.yml"
    fi

    local existing_hw existing_cf
    existing_hw=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- || true)
    existing_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- || true)

    # PRESERVE OPERATOR-ADDED OVERLAYS in COMPOSE_FILE. This function only
    # manages the `compose.yml` + `modules/llm/compose.devices.*.yml` slots
    # (and the pre-2026.07 top-level `llm/compose.devices.*.yml` form, which
    # the 2026.07 Layout-A reorg moved under `modules/` — see the `case`
    # below: old-path entries are treated as managed so they get migrated to
    # the `modules/llm/...` form rather than re-appended as a stale overlay);
    # any
    # other overlay (e.g. operator's `docker-compose.kernel-compat.yml` or a
    # custom `docker-compose.override.yml`) gets re-appended after the
    # managed slots. Caught during the prod 2026.05 cutover where the
    # operator's apparmor compat overlay was silently clobbered, recreating
    # postgres+valkey with the broken security profile and breaking the 2-hop
    # Authentik migration. See .gsd/reports/fleet-test-2026-05-09-prod.md.
    if [ -n "${existing_cf:-}" ]; then
        local extra_overlays=""
        local _entry _saved_ifs
        _saved_ifs="$IFS"
        IFS=':'
        # shellcheck disable=SC2086
        for _entry in $existing_cf; do
            case "$_entry" in
                ""|"compose.yml"\
                |"llm/compose.devices.amd.yml"|"llm/compose.devices.cpu.yml"|"llm/compose.devices.nvidia.yml"\
                |"modules/llm/compose.devices.amd.yml"|"modules/llm/compose.devices.cpu.yml"|"modules/llm/compose.devices.nvidia.yml")
                    : # managed — skip (old top-level + new modules/ form both migrate to target)
                    ;;
                *)
                    extra_overlays="${extra_overlays}:${_entry}"
                    ;;
            esac
        done
        IFS="$_saved_ifs"
        if [ -n "$extra_overlays" ]; then
            target_compose_file="${target_compose_file}${extra_overlays}"
            print_substep "Preserving operator-added COMPOSE_FILE overlay(s):${extra_overlays}"
        fi
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_substep "Would set COMPOSE_PROFILES: $current_profiles → $new_profiles"
        print_substep "Would set HARDWARE: ${existing_hw:-<unset>} → $target_hw"
        if [ -n "$target_compose_file" ]; then
            print_substep "Would set COMPOSE_FILE: ${existing_cf:-<unset>} → $target_compose_file"
        else
            print_substep "Would CLEAR COMPOSE_FILE (was: ${existing_cf:-<unset>}; legacy/cpu profiles don't use it)"
        fi
        print_info "M029-S04: 2026.04-ga AMD installs land on the STABLE v0.7.1 path (\`llm-legacy\`)."
        print_info "  To opt into v2.1.x EXPERIMENTAL, run:"
        print_info "    Configuration Portal → Modules → LLM Runtime, OR"
        print_info "    edit .env: COMPOSE_PROFILES=...,llm,... + HARDWARE=$target_hw + COMPOSE_FILE=compose.yml:modules/llm/compose.devices.$target_hw.yml"
        return 0
    fi

    update_env_value "$env_file" "COMPOSE_PROFILES" "$new_profiles"
    print_substep "COMPOSE_PROFILES: $current_profiles → $new_profiles"

    if [ "$existing_hw" != "$target_hw" ]; then
        update_env_value "$env_file" "HARDWARE" "$target_hw"
        print_substep "HARDWARE: ${existing_hw:-<unset>} → $target_hw"
    fi

    if [ -n "$target_compose_file" ] && [ "$existing_cf" != "$target_compose_file" ]; then
        update_env_value "$env_file" "COMPOSE_FILE" "$target_compose_file"
        print_substep "COMPOSE_FILE: ${existing_cf:-<unset>} → $target_compose_file"
    elif [ -z "$target_compose_file" ] && [ -n "$existing_cf" ]; then
        # Legacy / CPU profiles don't use COMPOSE_FILE — clearing prevents
        # the v2.x device overlay from being applied to a v0.7.1 deploy.
        update_env_value "$env_file" "COMPOSE_FILE" ""
        print_substep "COMPOSE_FILE: $existing_cf → (cleared; legacy profiles don't use device overlays)"
    fi

    # Verify .env actually reflects the migration. update_env_value is
    # supposed to write through, but sed on a 30-line subset of a 600-line
    # operator-edited .env can fail silently if the file has unusual line
    # endings or missing newlines. Surface that BEFORE it bites later.
    local verify_profiles verify_hw verify_cf
    verify_profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    verify_hw=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    verify_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    local verify_ok=true
    if [ "$verify_profiles" != "$new_profiles" ]; then
        print_warning "VERIFY FAIL: COMPOSE_PROFILES did not update — got '$verify_profiles', expected '$new_profiles'"
        verify_ok=false
    fi
    if [ "$verify_hw" != "$target_hw" ]; then
        print_warning "VERIFY FAIL: HARDWARE did not update — got '$verify_hw', expected '$target_hw'"
        verify_ok=false
    fi
    if [ "$verify_cf" != "$target_compose_file" ]; then
        print_warning "VERIFY FAIL: COMPOSE_FILE did not update — got '$verify_cf', expected '$target_compose_file'"
        verify_ok=false
    fi
    if [ "$verify_ok" = true ]; then
        print_substep "Verified: .env now reflects the \`${target_profile}\` profile state."
    fi

    # #1720: this used to tell the operator that if "the new \`llm\` path"
    # does not work they should edit .env and fall back to \`llm-legacy\`.
    # Both halves stopped being true at the cutover (#1447): there is no
    # \`llm\` profile any more, and \`llm-legacy\` is not a fallback — it is
    # where this very function has just put them. Advice that names a profile
    # the release removed sends an operator looking for something that cannot
    # be found, at the moment they are least able to afford it.
    if [ "$target_profile" = "llm-legacy" ]; then
        print_info "  This box now runs the frozen GPUStack 0.7.1 stack (\`llm-legacy\`)."
        print_info "  Its models are served by \`gpustack\`; \`rzfz status\` shows the state."
    fi

    # rc6.6 (M023-S05.3): tear down retired-profile containers BEFORE
    # restart_stack runs `docker compose up -d`. Three services used to share
    # `container_name: gpustack` — #1447 left exactly one (`gpustack-legacy`),
    # but a box being upgraded still HAS the other two running under that name
    # from before, and compose will not remove them: they are no longer defined
    # at all, and `--remove-orphans` only reaches containers of THIS project's
    # current file set. So the teardown below stays, and stays explicit.
    # `docker compose up --remove-orphans` does NOT clean these up because
    # the legacy services are still defined in compose.yml — they're just
    # in a profile that's no longer active, which compose treats as
    # "inactive" rather than "orphaned". The result on box-004 / 8.93
    # during the rc6.5 fleet test was:
    #   `Error response from daemon: Conflict. The container name "/gpustack"
    #    is already in use by container "<old-id>"`
    # which aborted restart_stack mid-flow.
    #
    # Stop+remove only the LEGACY services (the ones whose profile is no
    # longer in COMPOSE_PROFILES). The new `gpustack` / `model-sync` from
    # the active `llm` profile will be created cleanly by restart_stack.
    case "$old_profile" in
        llm-cpu)
            # #1720: named the retired \`llm\` profile. What actually takes the
            # container_name slot after the cutover is \`llm-legacy\` — which is
            # where `target_profile` has just put this box.
            print_substep "Stopping + removing legacy llm-cpu containers (gpustack-cpu, model-sync-cpu) so the \`${target_profile}\` profile can take their container_name slot..."
            docker stop gpustack-cpu model-sync-cpu 2>/dev/null || true
            docker rm -f gpustack-cpu model-sync-cpu 2>/dev/null || true
            # The container_name `gpustack` / `model-sync` is what the
            # next compose up needs free; if the bare names are still
            # held by the legacy containers under their container_name,
            # rm by name too.
            docker rm -f gpustack model-sync 2>/dev/null || true
            ;;
        llm-box|llm-experimental)
            print_substep "Stopping + removing legacy llm-box/llm-experimental containers..."
            # These older profiles used container_name=gpustack-experimental
            # / model-sync-experimental in M018-pre-S06.6 layouts. Match
            # both the legacy and shared names defensively.
            docker rm -f gpustack-experimental model-sync-experimental \
                          gpustack-legacy model-sync-legacy \
                          gpustack model-sync 2>/dev/null || true
            ;;
    esac
}
migrate_llm_profiles

# Step 5b.1 (#946/#1448): migrate an EXISTING NVIDIA box off the retired `llm`
# (GPUStack v2.1.x) profile onto `llm-legacy` + HARDWARE=nvidia (GPUStack 0.7.1
# + custom CUDA llama.cpp, sm_120). migrate_llm_profiles() above deliberately
# never touches NVIDIA (rc4 put NVIDIA straight on `llm`), so WITHOUT this every
# existing NVIDIA customer stays on the dead-end v2.1.x runtime after
# `rzfz upgrade` — the exact opposite of #946. Idempotent: fires only on
# HARDWARE=nvidia + an active exact `llm` token; a box already on llm-legacy is
# a no-op (Step 5b.2 below handles the llm-cuda→llm-legacy rename).
migrate_nvidia_llm_to_llm_legacy() {
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || return 0
    local hw profiles
    hw=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    [ "$hw" = "nvidia" ] || return 0
    # exact-token match for `llm` (NOT llm-legacy/llm-cpu)
    printf '%s\n' "$profiles" | tr ',' '\n' | grep -Fxq "llm" || return 0

    print_step "Step 5b.1 (#946/#1448): NVIDIA llm (v2.1.x) → llm-legacy + HARDWARE=nvidia (0.7.1 + custom CUDA)"

    # Rewrite COMPOSE_PROFILES llm→llm-legacy (dedupe). #1448: the merged 0.7.1
    # module is NOT self-contained any more — it takes its image and its device
    # wiring from modules/llm/compose.devices.nvidia.yml, so COMPOSE_FILE KEEPS
    # that overlay (pre-#1448 this migration dropped it). Operator-added
    # overlays are preserved, same as migrate_llm_profiles.
    local new_profiles existing_cf target_cf="compose.yml:modules/llm/compose.devices.nvidia.yml"
    new_profiles=$(printf '%s\n' "$profiles" | tr ',' '\n' \
        | sed 's/^llm$/llm-legacy/' | awk 'NF && !seen[$0]++' | paste -sd,)
    existing_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ -n "$existing_cf" ]; then
        local extra="" _e _ifs; _ifs="$IFS"; IFS=':'
        # shellcheck disable=SC2086
        for _e in $existing_cf; do
            case "$_e" in
                ""|"compose.yml"\
                |"llm/compose.devices.nvidia.yml"|"modules/llm/compose.devices.nvidia.yml"\
                |"llm/compose.devices.amd.yml"|"modules/llm/compose.devices.amd.yml"\
                |"llm/compose.devices.cpu.yml"|"modules/llm/compose.devices.cpu.yml") : ;;
                *) extra="${extra}:${_e}" ;;
            esac
        done
        IFS="$_ifs"
        if [ -n "$extra" ]; then target_cf="${target_cf}${extra}"; print_substep "Preserving operator overlay(s):${extra}"; fi
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_substep "Would set COMPOSE_PROFILES: $profiles → $new_profiles"
        print_substep "Would set COMPOSE_FILE: ${existing_cf:-<unset>} → $target_cf"
        print_substep "Would RESET gpustack_db (v2.x→0.7.x alembic incompatible) + clear schema-line guard + swap gpustack container"
        return 0
    fi

    update_env_value "$env_file" "COMPOSE_PROFILES" "$new_profiles"
    print_substep "COMPOSE_PROFILES: $profiles → $new_profiles"
    if [ "$existing_cf" != "$target_cf" ]; then
        update_env_value "$env_file" "COMPOSE_FILE" "$target_cf"
        print_substep "COMPOSE_FILE: ${existing_cf:-<unset>} → $target_cf"
    fi

    # gpustack v2.x and 0.7.1 have incompatible alembic schemas — 0.7.1 CANNOT
    # start against a v2.x-migrated gpustack_db (the #278 guard FATAL-refuses).
    # Reset the DB + clear the schema-line guard so the 0.7.1 service (v0.7)
    # initialises cleanly. The pre-upgrade backup (Step 1) is the recovery point;
    # the model registry re-populates on the next 'rzfz post-install' model deploy.
    print_warning "Resetting gpustack_db — the GPUStack model registry is wiped (v2.x→0.7.x); recover from the pre-upgrade backup if needed. Models re-deploy via 'rzfz post-install'."
    # Clear the schema-line guard file FIRST, via the still-running v2.x gpustack
    # container (offline-safe: no helper image pull). It lives in the gpustack-data
    # volume at /var/lib/gpustack/.razzfazz-schema-line and would otherwise say 'v2'.
    docker exec gpustack rm -f /var/lib/gpustack/.razzfazz-schema-line 2>/dev/null || true
    local pg_user pg_pw
    pg_user=$(grep '^POSTGRES_USER=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true); pg_user="${pg_user:-docker}"
    pg_pw=$(grep '^POSTGRES_PASSWORD=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -x postgres >/dev/null; then
        local _drop_rc=0 _create_rc=0
        { PGPASSWORD="$pg_pw" docker exec -e PGPASSWORD postgres psql -U "$pg_user" -d postgres -c 'DROP DATABASE IF EXISTS gpustack_db;' 2>&1 | sed 's/^/    /'; _drop_rc=${PIPESTATUS[0]}; } || true
        { PGPASSWORD="$pg_pw" docker exec -e PGPASSWORD postgres psql -U "$pg_user" -d postgres -c "CREATE DATABASE gpustack_db OWNER \"$pg_user\";" 2>&1 | sed 's/^/    /'; _create_rc=${PIPESTATUS[0]}; } || true
        if [ "$_drop_rc" -eq 0 ] && [ "$_create_rc" -eq 0 ]; then
            print_substep "gpustack_db reset (DROP+CREATE) — gpustack-legacy will initialise it fresh."
        else
            print_warning "gpustack_db NOT reset (psql DROP exit ${_drop_rc}, CREATE exit ${_create_rc}). gpustack-legacy will FATAL-refuse (#278) until you DROP+CREATE gpustack_db manually."
        fi
    else
        print_warning "postgres not running — gpustack_db NOT reset. gpustack-legacy will FATAL-refuse (#278) until you DROP+CREATE gpustack_db manually."
    fi

    # Container-swap: free the shared container_name slots so gpustack-legacy /
    # model-sync-legacy take them on the next restart_stack `up` (mirrors the
    # migrate_llm_profiles teardown).
    docker rm -f gpustack model-sync 2>/dev/null || true
    print_substep "Removed old v2.x gpustack/model-sync containers; gpustack-legacy takes over on restart."
}
migrate_nvidia_llm_to_llm_legacy

# Step 5b.2 (#1448 / cutover C8, decision D4): `llm-cuda` and `llm-legacy` were
# two profiles for ONE module — GPUStack 0.7.1, differing only in image and
# device wiring, which is what the HARDWARE device overlay already models. C8
# merged them; this migration is what an existing box needs:
#
#   1. COMPOSE_PROFILES: the exact token `llm-cuda` → `llm-legacy` (dedup,
#      token-exact — never a substring rewrite, `llm-cuda-foo` stays put).
#   2. HARDWARE=nvidia — an llm-cuda box may never have had the key written
#      (pre-#946 installs) and the merged service picks its overlay by it.
#   3. COMPOSE_FILE: the merged service carries NO devices of its own, so the
#      overlay MUST be in the chain. Pre-C8 both 0.7.1 profiles ran on a bare
#      `compose.yml`, so this repair is needed on AMD llm-legacy boxes too —
#      that is the `llm-legacy` branch below, which fires without any profile
#      rewrite at all.
#
# NO database reset: 0.7.1 → 0.7.1 is the same alembic line ("v0.7" in the #278
# schema-line guard), unlike the v2.x→0.7.1 hop in Step 5b.1. The container is
# recreated by restart_stack; the model registry survives.
#
# COMPOSE_FILE-preserving: operator-added overlays are kept, only a stale
# device overlay for a DIFFERENT hardware is dropped (the 2026-06 prod incident
# — see migrate_llm_profiles :5561-5579 — was exactly a clobbered COMPOSE_FILE).
migrate_llm_cuda_into_llm_legacy() {
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || return 0
    local profiles hw profiles_lines
    profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    [ -n "$profiles" ] || return 0
    profiles_lines=$(printf '%s\n' "$profiles" | tr ',' '\n')
    hw=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)

    local new_profiles="$profiles" rename=false
    if printf '%s\n' "$profiles_lines" | grep -Fxq "llm-cuda"; then
        rename=true
        new_profiles=$(printf '%s\n' "$profiles" | tr ',' '\n' \
            | sed 's/^llm-cuda$/llm-legacy/' | awk 'NF && !seen[$0]++' | paste -sd,)
        # An llm-cuda box IS an NVIDIA box by construction (#946) — the profile
        # existed for no other hardware. Record it so the overlay below and the
        # container's own HARDWARE guard agree.
        hw="nvidia"
    elif printf '%s\n' "$profiles_lines" | grep -Fxq "llm-legacy"; then
        # AMD (or an already-renamed NVIDIA) box: no profile change, but the
        # overlay repair below still applies.
        hw="${hw:-amd}"
    else
        return 0
    fi

    # The overlay the merged service needs on this box.
    local want_overlay="modules/llm/compose.devices.${hw}.yml"
    local existing_cf target_cf="compose.yml:${want_overlay}"
    existing_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ -n "$existing_cf" ]; then
        local extra="" _e _ifs; _ifs="$IFS"; IFS=':'
        # shellcheck disable=SC2086
        for _e in $existing_cf; do
            case "$_e" in
                ""|"compose.yml"\
                |"llm/compose.devices.nvidia.yml"|"modules/llm/compose.devices.nvidia.yml"\
                |"llm/compose.devices.amd.yml"|"modules/llm/compose.devices.amd.yml"\
                |"llm/compose.devices.cpu.yml"|"modules/llm/compose.devices.cpu.yml") : ;;
                *) extra="${extra}:${_e}" ;;
            esac
        done
        IFS="$_ifs"
        if [ -n "$extra" ]; then target_cf="${target_cf}${extra}"; fi
    fi

    if [ "$rename" != true ] && [ "$existing_cf" = "$target_cf" ]; then
        return 0   # nothing to do — already merged and wired
    fi

    print_step "Step 5b.2 (#1448): GPUStack 0.7.1 module — one profile, HARDWARE=${hw} picks the device overlay"
    if [ "$DRY_RUN" = "true" ]; then
        [ "$rename" = true ] && print_substep "Would set COMPOSE_PROFILES: $profiles → $new_profiles"
        print_substep "Would set HARDWARE=${hw}"
        print_substep "Would set COMPOSE_FILE: ${existing_cf:-<unset>} → $target_cf"
        return 0
    fi
    if [ "$rename" = true ]; then
        update_env_value "$env_file" "COMPOSE_PROFILES" "$new_profiles"
        print_substep "COMPOSE_PROFILES: $profiles → $new_profiles"
    fi
    update_env_value "$env_file" "HARDWARE" "$hw"
    if [ "$existing_cf" != "$target_cf" ]; then
        update_env_value "$env_file" "COMPOSE_FILE" "$target_cf"
        print_substep "COMPOSE_FILE: ${existing_cf:-<unset>} → $target_cf"
        print_substep "The merged gpustack-legacy service takes its image + device wiring from ${want_overlay}; without it in the chain it refuses to start (#1448)."
    fi
}
migrate_llm_cuda_into_llm_legacy

# Step 5b.2 (#270): retag the two locally built GPUStack images off the RETIRED
# private SEQIS GitLab registry name onto the neutral bare local name.
#
# Operator ruling 2026-09-04: „die gitlab registry ist komplett outdated und
# gehört entfernt, sie ist seit Monaten nicht im Einsatz, wir liefern immer alle
# images mit, auch schon immer." modules/llm/compose.yml now names
# `razzfazz-gpustack:vulkan` / `:cuda`.
#
# WHY THIS IS LOAD-BEARING: on an EXISTING box the image exists locally under
# the OLD name only, and compose.no-build.yml sets `build: !reset null` +
# `pull_policy: never`. Without this step `docker compose up -d` looks for the
# NEW name, does not find it, may neither build nor pull it — and the LLM
# backend fails to start. On an offline/package box `rzfz verify-images`
# (Step 7b, below) would flag it missing and REFUSE the restart outright. A
# `docker tag` is instant: no rebuild (~6 min on Strix Halo), no download
# (there is nowhere to download it from), and it works air-gapped.
#
# Runs BEFORE the --check exit, so `rzfz upgrade --check` previews it without
# mutating anything, and long before Step 7/7b so the new name exists by the
# time the build/verify/restart phases look for it.
#
# The OLD tag is deliberately NOT deleted in this release: `rzfz upgrade
# --rollback` restores the previous code, whose compose files still name the
# old image. Dropping the old tag is a follow-up once no supported rollback
# target references it any more.
#
# Idempotent: a box already carrying the new name is a no-op, and a box that
# never ran llm-legacy/llm-cuda (neither image present) skips cleanly. Every
# `docker` call is guarded — a missing daemon or missing binary must not abort
# the upgrade (script runs under `set -eo pipefail`).
# Tests: tests/unit/scripts/test_270_gpustack_image_retag.py (extracts this
# function verbatim and drives it against a shadowed `docker`).
migrate_gpustack_image_rename() {
    print_step "Step 5b.2 (#270): GPUStack-Images auf den neutralen lokalen Namen umbenennen"
    local _old_repo="registry.gitlab.com/razzfazz.ai/razzfazz-ai-service-stack/gpustack"
    local _tag _old _new _acted=false
    for _tag in vulkan cuda; do
        _old="${_old_repo}:${_tag}"
        _new="razzfazz-gpustack:${_tag}"
        # rev-C (review, 0.79 Runde 5): die Meldung muss den Zustand nennen, den
        # sie vorgefunden hat. „bereits vorhanden" allein deckt zwei Fälle ab,
        # die verschieden sind: derselbe Image-Stand unter beiden Namen (fertig
        # umbenannt) oder ein NEUER Name, der auf ein anderes Image zeigt (dann
        # wäre ein blindes Umtaggen ein Rückschritt). Vergleiche die Image-ID.
        local _new_id _old_id
        _new_id="$(docker image inspect --format '{{.Id}}' "$_new" 2>/dev/null || true)"
        _old_id="$(docker image inspect --format '{{.Id}}' "$_old" 2>/dev/null || true)"
        if [ -n "$_new_id" ]; then
            if [ -z "$_old_id" ]; then
                print_substep "  $_new vorhanden, altes Tag nicht mehr da — bereits umbenannt, nichts zu tun."
            elif [ "$_new_id" = "$_old_id" ]; then
                print_substep "  $_new zeigt bereits auf dasselbe Image wie das alte Tag — bereits umbenannt, behalten."
            else
                print_substep "  $_new vorhanden und NEUER als das alte Tag (verschiedene Image-IDs) — unverändert gelassen."
            fi
            continue
        fi
        if [ -z "$_old_id" ]; then
            print_substep "  Weder $_new noch das alte Image vorhanden (:${_tag}) — übersprungen."
            continue
        fi
        if [ "${DRY_RUN:-false}" = "true" ]; then
            print_substep "  Würde umbenennen (docker tag): altes :${_tag}-Image → $_new"
            continue
        fi
        if docker tag "$_old" "$_new" 2>/dev/null; then
            print_substep "  Umbenannt (docker tag): altes :${_tag}-Image → $_new (altes Tag bleibt für --rollback erhalten)."
            _acted=true
        else
            print_warning "  docker tag → $_new fehlgeschlagen — das Image wird beim Build-Schritt neu gebaut (online) bzw. muss aus dem Offline-Paket geladen werden."
        fi
    done
    if [ "$_acted" = true ]; then
        journal_event "migration" "info" "#270: GPUStack-Images auf razzfazz-gpustack:* umgetaggt (altes Tag bleibt erhalten)" || true
    fi
    return 0
}
migrate_gpustack_image_rename

# Step 5b.0.5: F-RC5-1 / rc6.9 — install GPUSTACK_*_HOST_BIND defaults based
# on GPUSTACK_MODE for boxes that were initialised pre-rc6.9. Pre-fix the
# compose worker-port default was 0.0.0.0 unconditionally, so standalone
# boxes had ports 10150-10151 bound on the LAN. Both gpustack-legacy and
# gpustack v2.x share the same compose default, so this fix harmonises
# hardening across both runtime profiles.
migrate_gpustack_bind() {
    print_step "Step 5b.0.5: GPUStack host-bind hardening (F-RC5-1 / rc6.9)"
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || { print_substep "No .env — skipping."; return 0; }
    local mode existing_host existing_worker
    mode=$(grep '^GPUSTACK_MODE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    existing_host=$(grep '^GPUSTACK_HOST_BIND=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    existing_worker=$(grep '^GPUSTACK_WORKER_HOST_BIND=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    local target_host target_worker
    case "${mode:-standalone}" in
        master)            target_host="0.0.0.0";   target_worker="0.0.0.0" ;;
        worker)            target_host="127.0.0.1"; target_worker="0.0.0.0" ;;
        standalone|*)      target_host="127.0.0.1"; target_worker="127.0.0.1" ;;
    esac
    print_substep "GPUSTACK_MODE=${mode:-standalone} → HOST_BIND=$target_host, WORKER_HOST_BIND=$target_worker"
    if [ "$DRY_RUN" = "true" ]; then
        print_substep "  Would set GPUSTACK_HOST_BIND=$target_host (was: ${existing_host:-<unset>})"
        print_substep "  Would set GPUSTACK_WORKER_HOST_BIND=$target_worker (was: ${existing_worker:-<unset>})"
        return 0
    fi
    [ "$existing_host" != "$target_host" ] && update_env_value "$env_file" "GPUSTACK_HOST_BIND" "$target_host"
    [ "$existing_worker" != "$target_worker" ] && update_env_value "$env_file" "GPUSTACK_WORKER_HOST_BIND" "$target_worker"
    print_substep "  Updated."
}
migrate_gpustack_bind

# Step 5b.0.5b: COMPOSE_FILE Layout-A path migration (2026.07 reorg / #26).
# The 2026.07 Layout-A reorg moved every per-module dir under `modules/`, so
# the LLM device overlay moved from `llm/compose.devices.<HW>.yml` to
# `modules/llm/compose.devices.<HW>.yml`. EXISTING boxes carry the OLD
# top-level path in their LIVE .env's COMPOSE_FILE — after the code update
# `docker compose` can't find `llm/compose.devices.amd.yml` and refuses to
# compose the whole stack. This migration rewrites ONLY the device-overlay
# slot in place (`:llm/compose.devices.` → `:modules/llm/compose.devices.`),
# preserving the rest of the chain (compose.yml, operator-added overlays,
# ordering). Idempotent — already-migrated values are left untouched. Runs
# BEFORE migrate_compose_file_chain so the chain self-heal sees the new path.
# Regression-tested in tests/test-compose-file-modules-migration.sh (extracts
# the function verbatim between the markers below).
# >>> LAYOUT_A_COMPOSE_FILE_MIGRATION_BEGIN
migrate_compose_file_to_modules_layout() {
    local env_file="$1"
    [ -f "$env_file" ] || return 0
    local current
    current=$(read_env_value "$env_file" COMPOSE_FILE)
    [ -n "$current" ] || return 0
    # Only the pre-2026.07 top-level overlay slot is rewritten. The `:` anchor
    # avoids touching an already-migrated `:modules/llm/...` entry and avoids
    # matching unrelated substrings. There is exactly one device-overlay slot
    # per chain, but `//` keeps it robust if a box somehow carries two.
    case "$current" in
        *":llm/compose.devices."*)
            # NB: bash ${var//pat/rep} treats `/` inside `pat` as the
            # pattern/replacement delimiter, so we can't pattern-match a path
            # with embedded slashes there. Use sed with a `#` delimiter, and a
            # literal-dot-tolerant pattern (the `.` are fine to match literally).
            local migrated
            migrated=$(printf '%s' "$current" | sed 's#:llm/compose\.devices\.#:modules/llm/compose.devices.#g')
            update_env_value "$env_file" "COMPOSE_FILE" "$migrated"
            print_substep "  Migrated COMPOSE_FILE LLM overlay to modules/ layout (2026.07 #26):"
            print_substep "    $current → $migrated"
            ;;
        *)
            : # already on modules/ layout, or no llm overlay — no-op
            ;;
    esac
}
# <<< LAYOUT_A_COMPOSE_FILE_MIGRATION_END
migrate_compose_file_to_modules_layout "${SCRIPT_DIR}/.env"

# Step 5b.0.6: COMPOSE_FILE chain self-heal — match HARDWARE to the right
# per-device overlay. Pre-fix, boxes installed pre-M018 (or whose .env
# was manually edited at any point) could have COMPOSE_FILE=compose.yml
# alone, missing modules/llm/compose.devices.<amd|nvidia|cpu>.yml (2026.07
# Layout-A reorg moved these under modules/; this self-heal also migrates
# any box still carrying the pre-2026.07 top-level llm/ path). Symptom: the
# `gpustack` service isn't in the live compose graph, so
# `docker compose ps gpustack` returns nothing AND `compose up -d
# --force-recreate gpustack` fails with "name already in use" on the
# orphan container left from when the overlay WAS active. Repair is a
# single sed in .env. Idempotent: only writes when current ≠ desired.
# Discovered on the SEQIS prod worker (10.163) 2026-05-18 during the
# v2026.05-ga.4 cleanup pass; covered in ga.4 via the moved tag.
migrate_compose_file_chain() {
    print_step "Step 5b.0.6: COMPOSE_FILE chain self-heal (HARDWARE overlay)"
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || { print_substep "No .env — skipping."; return 0; }

    local hardware profiles current llm_profile want hw path
    hardware=$(grep '^HARDWARE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    profiles=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    current=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)

    # #448 defect 2: the device overlay is LLM-PROFILE-specific, not
    # HARDWARE-specific. Since #1448 the merged GPUStack 0.7.1 service
    # (`llm-legacy`) is the one that consumes a device overlay — it carries no
    # device wiring of its own and REFUSES to start without one (#434). `llm-cpu`
    # is self-contained. HARDWARE=amd is set on those boxes too, so keying off it
    # alone applied an overlay to a deploy that has no use for it.
    # #1447: the `llm` (2.x) arm is gone with the profile; a box still carrying
    # the token is migrated by Step 5b.1/5b.2 before this step is reached.
    llm_profile=""
    case ",${profiles}," in
        *,llm-legacy,*) llm_profile="llm-legacy" ;;
        # RETIRED tokens: a box reaching this step un-migrated still needs the
        # right overlay, and both retired names map to the same service now.
        *,llm-cpu,*)    llm_profile="llm-legacy" ;;
        *,llm-cuda,*)   llm_profile="llm-cuda" ;;
    esac

    want=""
    if [ "$llm_profile" = "llm-legacy" ] || [ "$llm_profile" = "llm-cuda" ]; then
        case "${hardware}" in
            amd|nvidia|cpu) want="modules/llm/compose.devices.${hardware}.yml" ;;
            "")  print_substep "  HARDWARE not set — leaving COMPOSE_FILE as-is."; return 0 ;;
            *)   print_substep "  HARDWARE='${hardware}' unrecognised — leaving COMPOSE_FILE as-is."; return 0 ;;
        esac
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_substep "  Would reconcile the device overlay to: ${want:-<none, ${llm_profile:-no llm profile} is self-contained>}"
        if [ "$hardware" = "nvidia" ]; then
            print_substep "  Would ensure compose.worker-agent-nvidia.yml is in the chain (#1014)"
        fi
        print_substep "  (every other overlay in '${current}' is preserved)"
        return 0
    fi

    # #448 defect 1: this used to rebuild COMPOSE_FILE from scratch —
    #     desired="compose.yml"; case $hardware in ...) desired="$desired:<dev>" ;;
    # which DELETED every other overlay on every upgrade: compose.no-build.yml
    # (#184 WS2a — its absence silently re-enables runtime builds and internet
    # reach on an installed box), the corporate-proxy overlay (#181/#280) and
    # compose.offline.yml. Seen in the field on 0.236: "current:
    # compose.yml:compose.no-build.yml → desired:
    # compose.yml:modules/llm/compose.devices.amd.yml. Repaired."
    #
    # Reconcile ONLY the device overlay, via the idempotent helpers in
    # scripts/lib.sh. Anything else in the chain is untouched by construction
    # rather than by remembering to re-append it.
    if [ -z "$current" ]; then
        # docker compose 2.40+ chokes on an empty COMPOSE_FILE value.
        update_env_value "$env_file" "COMPOSE_FILE" "compose.yml"
    fi
    for hw in amd nvidia cpu; do
        # Both the current path and the pre-2026.07 top-level one (#119 moved
        # these under modules/), so an old box does not keep a dangling entry.
        for path in "modules/llm/compose.devices.${hw}.yml" "llm/compose.devices.${hw}.yml"; do
            [ "$path" = "$want" ] && continue
            compose_file_overlay_remove "$env_file" "$path"
        done
    done
    [ -n "$want" ] && compose_file_overlay_add "$env_file" "$want"

    # #1014 (audit EXO-6/OPS-6): the worker-agent overlay is HARDWARE-specific,
    # not profile-specific — it gives llm-worker-agent the nvidia runtime so
    # nvidia-smi/NVML are visible to it. cli/init.sh hung it off the v2.x `llm`
    # branch, which an NVIDIA box can never reach (the #946 redirect turns
    # llm into llm-cuda first), and this self-heal never added it at all. So an
    # NVIDIA box — freshly installed or upgraded — ran its worker agent blind:
    # runtime._live_metrics reads nothing and the console's load chart stays on
    # "Collecting live samples…". Reconciled here for every existing box, with
    # the same idempotent helpers the device overlay uses.
    if [ "$hardware" = "nvidia" ]; then
        compose_file_overlay_add "$env_file" "compose.worker-agent-nvidia.yml"
    else
        compose_file_overlay_remove "$env_file" "compose.worker-agent-nvidia.yml"
    fi

    # ga.15: drop redundant `modules/**/compose.yml` entries the root compose.yml
    # already composes via include: (legacy pre-include: boxes carry these; a
    # directly-listed module resolves its own `env_file: ../../.env` to /home/.env
    # and breaks `docker compose` for that module — hit on the ga.15 re-domain).
    compose_file_strip_included_modules "$env_file"

    local now
    now=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ "$now" = "$current" ]; then
        print_substep "  COMPOSE_FILE already correct for ${llm_profile:-no-llm}/${hardware:-unset} (no change)."
    else
        print_substep "  COMPOSE_FILE: ${current:-<unset>} → ${now}"
    fi
}
migrate_compose_file_chain

# ==============================================================================
# Step 5b.2 (#1446, cutover C6): every box gets the LLM Manager trio
# ==============================================================================
# From 2026.09 the LLM Manager is the front of every box (#979 D1/D2): it serves
# the standard set itself or fronts GPUStack as a registered external backend
# (#1442). Two consequences for an INSTALLED box, and both need a migration
# rather than a default, because `.env` is the operator's file and nothing else
# rewrites COMPOSE_PROFILES:
#
#   * the manager trio (llm-manager, llm-registry, llm-worker-agent) has to be
#     IN the profile list, or the always-on module (#1443) has no containers;
#   * GPUStack 2.x is removed in this release (#979, confirmed 2026-09-04)
#     — that token goes, and the box is told what that means for its data
#     before it wonders.
#
# `llm` is the ONLY 2.x profile (modules/llm/compose.yml pins it to
# gpustack/gpustack:v2.1.2). `llm-cpu`, `llm-legacy` and `llm-cuda` all STAY
# here — this step only rewrites the list. #2813: a HARDWARE=cpu box is the one
# exception, and it is handled LATER (cpu_gpustack_handover_begin/finish, around
# the restart and the refresh): it moves to the manager's own engines and GPUStack
# is retired once they serve. AMD and NVIDIA keep GPUStack as the federated backend:
# they are the GPUStack 0.7.1 line — `llm-cpu` is v0.7.1-cpu, the un-deprecated
# CPU default (M029-S04) that the shipped `master-cpu`/`testvm-cpu` presets
# pick, and the other two are the optional backend the manager federates
# (#1442). An earlier revision of this block listed `llm-cpu` as 2.x and would
# have taken the LLM runtime out from under every CPU box on upgrade. This function only rewrites the profile list;
# the consumer stores (OWUI connection, Dify credential, agent envs) are
# migrated by post-install's own wiring, which every upgrade runs afterwards.
#
# Shape follows migrate_llm_profiles: token-exact, deduplicated, order-preserving,
# and COMPOSE_FILE-preserving — an operator overlay must survive (the 2026.05
# prod incident, see that function).
LLM_MANAGER_TRIO="llm-manager llm-registry llm-worker-agent"
#: GPUStack 2.x profiles, removed in 2026.09. Read off modules/llm/compose.yml:
#: `llm` is the ONLY profile on the 2.x line (image gpustack/gpustack:v2.1.2).
#: `llm-cpu` is v0.7.1-cpu — the 0.7 line, un-deprecated as the CPU default
#: (M029-S04) and picked by the shipped `master-cpu` and `testvm-cpu` presets.
#: Listing it here would have dropped the runtime out from under every CPU box
#: on upgrade. The rest of the tree groups it with llm-legacy/llm-cuda, not
#: with llm (scripts/lib-owui.sh:64, cli/post-install.sh:6907/7118).
#: tests/unit/scripts/test_1446_the_2x_set_follows_the_compose_file.py reads
#: the image tags and holds this constant to them.
LLM_GPUSTACK_2X_PROFILES="llm"

migrate_to_llm_manager_trio() {
    print_step "Step 5b.2 (#1446): LLM Manager is the front of this box"
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || { print_substep "No .env — skipping."; return 0; }

    local current
    current=$(grep '^COMPOSE_PROFILES=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    if [ -z "$current" ]; then
        print_substep "COMPOSE_PROFILES is empty/missing — skipping."
        return 0
    fi

    local tok dropped="" kept="" have_manager=false
    local _saved_ifs="$IFS"
    IFS=','
    # shellcheck disable=SC2086
    for tok in $current; do
        [ -n "$tok" ] || continue
        case " $LLM_GPUSTACK_2X_PROFILES " in
            *" $tok "*)
                dropped="${dropped:+$dropped }$tok"
                continue ;;
        esac
        [ "$tok" = "llm-manager" ] && have_manager=true
        case ",${kept}," in
            *",${tok},"*) : ;;                       # dedupe
            *) kept="${kept:+$kept,}$tok" ;;
        esac
    done
    IFS="$_saved_ifs"

    local added=""
    for tok in $LLM_MANAGER_TRIO; do
        case ",${kept}," in
            *",${tok},"*) : ;;
            *) kept="${kept:+$kept,}$tok"; added="${added:+$added }$tok" ;;
        esac
    done

    if [ -z "$added" ] && [ -z "$dropped" ] && [ "$kept" = "$current" ]; then
        print_substep "COMPOSE_PROFILES already carries the manager trio and no GPUStack 2.x profile — no-op."
        return 0
    fi

    # A box that ran GPUStack 2.x keeps its models in gpustack_db, whose 2.x
    # alembic schema the 0.7.1 line cannot read (#979 danger 7). We do NOT
    # touch the database here — dropping a volume from an upgrade script is
    # not a decision a script makes. The standard set is re-deployed by
    # post-install from the manifest; a CUSTOM model needs the operator.
    if [ -n "$dropped" ]; then
        print_warning "GPUStack 2.x (${dropped}) is removed in 2026.09 — this box moves to the LLM Manager (#979)."
        print_info    "  The standard set is re-deployed from the manifest by post-install; the manager serves it."
        print_info    "  Models YOU added to GPUStack 2.x are NOT converted: their weights stay in the gpustack-data"
        print_info    "  volume, and gpustack_db still holds the 2.x schema. Re-deploy them through the LLM Manager"
        print_info    "  console once the upgrade finished (#1446)."
        print_info    "  Step by step, including the DROP+CREATE this box needs BEFORE the optional GPUStack 0.7.1"
        print_info    "  backend can run here: docs/walkthroughs/gpustack-2x-to-llm-manager.md"
        local _legacy_active=false _p
        for _p in llm-legacy llm-cuda llm-cpu; do   # every 0.7.1 profile, llm-cpu included
            if printf '%s' "$current" | tr ',' '\n' | grep -Fxq "$_p"; then _legacy_active=true; fi
        done
        if [ "${have_manager}" = false ] && [ "$_legacy_active" = false ]; then
            print_info "  No GPUStack 0.7.1 profile is active either — the manager will serve the set from its own worker."
        fi
    fi

    if [ "$DRY_RUN" = "true" ]; then
        print_substep "Would set COMPOSE_PROFILES: ${current} → ${kept}"
        [ -n "$added" ] && print_substep "  adding: ${added}"
        [ -n "$dropped" ] && print_substep "  removing (GPUStack 2.x): ${dropped}"
        return 0
    fi

    update_env_value "$env_file" "COMPOSE_PROFILES" "$kept"
    print_substep "COMPOSE_PROFILES: ${current} → ${kept}"
    [ -n "$added" ] && print_substep "  added: ${added}"
    [ -n "$dropped" ] && print_substep "  removed (GPUStack 2.x, #979): ${dropped}"

    # The device overlay belongs to the 2.x profile only (see
    # migrate_compose_file_chain). Once that profile is gone the overlay would
    # add a service nothing runs — drop the managed slots, keep every operator
    # overlay, and never leave COMPOSE_FILE empty (docker compose 2.40+ reads
    # the project root as a file and aborts).
    if [ -n "$dropped" ]; then
        local existing_cf
        existing_cf=$(grep '^COMPOSE_FILE=' "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
        if [ -n "$existing_cf" ]; then
            local rebuilt="compose.yml" _entry
            _saved_ifs="$IFS"; IFS=':'
            # shellcheck disable=SC2086
            for _entry in $existing_cf; do
                case "$_entry" in
                    ""|"compose.yml"\
                    |"llm/compose.devices.amd.yml"|"llm/compose.devices.cpu.yml"|"llm/compose.devices.nvidia.yml"\
                    |"modules/llm/compose.devices.amd.yml"|"modules/llm/compose.devices.cpu.yml"|"modules/llm/compose.devices.nvidia.yml")
                        : ;;
                    *) rebuilt="${rebuilt}:${_entry}" ;;
                esac
            done
            IFS="$_saved_ifs"
            if [ "$rebuilt" != "$existing_cf" ]; then
                update_env_value "$env_file" "COMPOSE_FILE" "$rebuilt"
                print_substep "COMPOSE_FILE: ${existing_cf} → ${rebuilt} (the v2.x device overlay went with the profile)"
            fi
        fi
    fi
    return 0
}
migrate_to_llm_manager_trio

# Step 5b.1: rc6.7 #63 — refresh RENDER_GID for AMD hosts on every upgrade.
# Pre-fix the migrate_env step would have stamped the manifest's static
# default (992) into .env. That covers Ubuntu 24.04 / most modern distros
# but not all — AlmaLinux 9 puts render at 989, debian-bookworm at 109,
# etc. If we wrote the wrong value, gpustack restarts via restart_stack
# (which force-recreates) would land back at ACCEL_WORKING -13 EACCES on
# the inner GPU access. Detect at upgrade-time and overwrite when wrong.
refresh_render_gid_for_amd() {
    local env_file="${SCRIPT_DIR}/.env"
    [ -f "$env_file" ] || return 0
    local hardware
    hardware=$(read_env_value "$env_file" HARDWARE)
    [ "$hardware" = "amd" ] || return 0

    # #536 (2026.09): ask the DEVICE first. `getent group render` describes the
    # host's group table; /dev/dri/renderD128's group is what the kernel checks,
    # and it is the authority on a host whose group table has no `render` entry
    # (AlmaLinux 9, some minimal images) — which used to make this refresh a
    # silent no-op. Now that gpustack runs non-root by default, a stale
    # RENDER_GID is no longer cosmetic: it is the rc6.7 #63 hang.
    # `|| true` on BOTH probes: `getent group render` exits 2 when the group
    # does not exist, and under this script's `set -eo pipefail` a bare
    # `detected=$(getent … | cut …)` aborts the whole upgrade at that point.
    # The pre-#536 version had exactly that shape — an AMD box whose group
    # table has no `render` entry would die here instead of moving on.
    local detected
    detected=$(stat -c %g /dev/dri/renderD128 2>/dev/null || true)
    [ -n "$detected" ] || detected=$(getent group render 2>/dev/null | cut -d: -f3 || true)
    [ -n "$detected" ] || return 0

    local current
    current=$(read_env_value "$env_file" RENDER_GID)
    if [ "$current" = "$detected" ]; then
        return 0   # already correct, silent no-op
    fi

    print_step "Refreshing RENDER_GID for AMD GPU device access (rc6.7 #63)..."
    # #2138/#246: this step runs before the --check exit. A check names the
    # change and writes nothing (measured on the CI runner, whose render group
    # is not 992: `rzfz upgrade --check` rewrote .env).
    if [ "${DRY_RUN:-false}" = "true" ]; then
        print_substep "DRY RUN: would set RENDER_GID=$detected (now: ${current:-<unset>})"
        return 0
    fi
    if [ -z "$current" ]; then
        print_substep "Setting RENDER_GID=$detected (was unset)"
    else
        print_substep "Updating RENDER_GID: $current → $detected (host's render gid)"
    fi
    update_env_value "$env_file" RENDER_GID "$detected"
}
refresh_render_gid_for_amd

# Step 5c: Install / refresh kernel stability tunables (M018 / S03.5).
# Same template that razzfazz-init.sh installs on first install — keeps
# existing operators in sync as defaults evolve.
install_kernel_stability_tunables() {
    # #2524: --migrations-only changes no host files.
    if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
        print_substep "Kernel stability tunables: skipped (--migrations-only changes no host files)."
        return 0
    fi
    local src="${SCRIPT_DIR}/core/sysctl/99-razzfazz-stability.conf"
    local dst="/etc/sysctl.d/99-razzfazz-stability.conf"
    [ -r "$src" ] || return 0
    # ga.15 sudo-wall: honour --skip-host-updates BEFORE any sudo. Without this
    # early return the `sudo cp` below prompts interactively when sudo is not
    # cached on a tty — and the `2>/dev/null` on it hides the prompt, so a
    # non-interactive / offline fleet upgrade looks like a silent hang. The
    # FAILURE branch already checked the flag; the sudo ATTEMPT did not. Mirrors
    # ensure_host_hardened() (below), which checks the flag first.
    if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
        if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" 2>/dev/null; then
            print_substep "Skipping kernel-stability tunables (--skip-host-updates)"
            print_info "  Apply manually: sudo cp $src $dst && sudo sysctl --system"
        fi
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" 2>/dev/null; then
            print_step "Would install kernel stability tunables → $dst (M018 / S03.5)"
        fi
        return 0
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return 0   # already current
    fi
    print_step "Installing kernel stability tunables (M018 / S03.5)..."
    if sudo cp "$src" "$dst" 2>/dev/null && sudo sysctl --system >/dev/null 2>&1; then
        print_substep "Installed $dst + reloaded."
        print_substep "Active: vm.panic_on_oom=$(sysctl -n vm.panic_on_oom 2>/dev/null), kernel.panic=$(sysctl -n kernel.panic 2>/dev/null), vm.swappiness=$(sysctl -n vm.swappiness 2>/dev/null)"
    else
        # F-RC5-5: was a silent print_warning; now fatal-on-fail unless operator
        # explicitly opts out via --skip-host-updates. The previous behaviour
        # silently skipped on sudo failures and the operator missed the
        # warning in long upgrade logs (proven on 0.78 rc5 upgrade — sysctl
        # tunables never landed on the box).
        if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
            print_warning "Could not install $dst (sudo required) — bypassed via --skip-host-updates."
            print_info "  Apply manually: sudo cp $src $dst && sudo sysctl --system"
        else
            print_error "Could not install $dst (sudo required)."
            print_info "  Manual: sudo cp $src $dst && sudo sysctl --system"
            print_info "  Bypass: re-run razzfazz-upgrade.sh with --skip-host-updates"
            return 1
        fi
    fi
}
if ! install_kernel_stability_tunables; then
    # F-RC5-5: fatal-on-fail unless --skip-host-updates was passed.
    # The function itself prints a clear error + manual instructions.
    exit 1
fi

# The Settings portal queues time source changes; this path unit applies them as
# root on the host. Optional, never fatal: the manual `sudo rzfz setup
# --time-source` does the same. Honours --skip-host-updates before any sudo.
install_time_source_watcher() {
    local helper="${SCRIPT_DIR}/scripts/install-time-source-watcher.sh"
    [ -x "$helper" ] || return 0
    if [ "${MIGRATIONS_ONLY:-false}" = true ] || [ "${SKIP_HOST_UPDATES:-false}" = true ] || [ "$DRY_RUN" = "true" ]; then
        return 0
    fi
    command -v systemctl >/dev/null 2>&1 || return 0
    if sudo "$helper" >/dev/null 2>&1; then
        print_substep "Time source watcher installed (razzfazz-time-source.path)."
    else
        print_warning "Could not install the time source watcher: the Time source field of the portal will not apply by itself."
        print_info "  Install: sudo $helper"
    fi
}
install_time_source_watcher

# The hourly knowledge-scope and role reconcile (Dify, cognee, Synapse): a
# systemd timer, so a removal from a knowledge scope takes effect within the
# hour instead of at the next upgrade. Optional, never fatal; honours
# --skip-host-updates before any sudo.
install_scope_reconcile_timer() {
    local helper="${SCRIPT_DIR}/scripts/install-scope-reconcile-timer.sh"
    [ -x "$helper" ] || return 0
    if [ "${MIGRATIONS_ONLY:-false}" = true ] || [ "${SKIP_HOST_UPDATES:-false}" = true ] || [ "$DRY_RUN" = "true" ]; then
        return 0
    fi
    command -v systemctl >/dev/null 2>&1 || return 0
    if sudo "$helper" >/dev/null 2>&1; then
        print_substep "Hourly knowledge-scope reconcile installed (razzfazz-scope-reconcile.timer)."
    else
        print_warning "Could not install the hourly knowledge-scope reconcile: a removal from a knowledge scope applies only at the next upgrade or manual reconcile."
        print_info "  Install: sudo $helper"
    fi
}
install_scope_reconcile_timer

# #675: kernel 7.0.0-22 panics at boot in lp_register when systemd-modules
# autoloads the parallel-port stack — a full boot-panic loop (10.163 RCA
# 2026-08-24). No fleet box has a parallel-port use case. Same contract as
# the tunables above: idempotent, honours --skip-host-updates before any
# sudo, fatal only on the cp (the initramfs refresh is belt-and-braces —
# lp loads post-rootfs via systemd-modules, so modprobe.d alone already
# removes the trigger).
install_parport_blacklist() {
    # #2524: --migrations-only changes no host files.
    if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
        print_substep "Parallel-port blacklist: skipped (--migrations-only changes no host files)."
        return 0
    fi
    local src="${SCRIPT_DIR}/core/modprobe/blacklist-parport-panic.conf"
    local dst="/etc/modprobe.d/blacklist-parport-panic.conf"
    [ -r "$src" ] || return 0
    if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
        if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" 2>/dev/null; then
            print_substep "Skipping parport blacklist (--skip-host-updates)"
            print_info "  Apply manually: sudo cp $src $dst && sudo update-initramfs -u"
        fi
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst" 2>/dev/null; then
            print_step "Would install parport module blacklist → $dst (#675)"
        fi
        return 0
    fi
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        return 0   # already current
    fi
    print_step "Installing parport module blacklist (#675, 7.0.0-22 boot-panic guard)..."
    if sudo cp "$src" "$dst" 2>/dev/null; then
        print_substep "Installed $dst."
        sudo update-initramfs -u >/dev/null 2>&1 \
            || print_warning "update-initramfs failed — modprobe.d still guards the post-rootfs autoload."
    else
        if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
            print_warning "Could not install $dst (sudo required) — bypassed via --skip-host-updates."
        else
            print_error "Could not install $dst (sudo required)."
            print_info "  Manual: sudo cp $src $dst && sudo update-initramfs -u"
            print_info "  Bypass: re-run razzfazz-upgrade.sh with --skip-host-updates"
            return 1
        fi
    fi
}
if ! install_parport_blacklist; then
    exit 1
fi

# Step 5d: Host hardening (rc5.1 fleet rollout) — auto-run if not done yet.
# Marker file: /etc/razzfazz/host-hardened (written by scripts/harden-host.sh
# on success). Without --skip-host-updates and without the marker, run the
# hardening script with safe defaults (NO --restrict-ssh; SSH stays as-is).
# The operator can re-run harden-host.sh manually with --restrict-ssh later
# once they've confirmed pubkey access works.
ensure_host_hardened() {
    # #2524: --migrations-only changes no host files.
    if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
        print_substep "Host hardening: skipped (--migrations-only changes no host files)."
        return 0
    fi
    if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
        print_substep "Skipping host hardening check (--skip-host-updates)"
        return 0
    fi
    if [ -f /etc/razzfazz/host-hardened ]; then
        local ts
        ts=$(grep '^hardened_at=' /etc/razzfazz/host-hardened 2>/dev/null | cut -d= -f2-)
        print_substep "Host hardening already applied (${ts:-unknown date}). Skipping."
        return 0
    fi
    if [ ! -x "${SCRIPT_DIR}/scripts/harden-host.sh" ]; then
        print_warning "harden-host.sh not found or not executable — skipping host hardening"
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        print_step "Would run host hardening (scripts/harden-host.sh) — no marker present"
        return 0
    fi
    print_info "  SSH hardening NOT applied by default — re-run with --restrict-ssh after"
    print_info "  setting up your SSH pubkey if you want PasswordAuthentication=no."
    # The runner shared with init: never prompts without a terminal, keeps the
    # full log and returns harden-host's own exit status (a pipe into tail hid it).
    if razzfazz_run_host_hardening "$SCRIPT_DIR"; then
        return 0
    fi
    # #2216 (journey D, 0.175): this used to `exit 1` the WHOLE upgrade — after
    # the code update and the .env migrations, before version tracking — so a
    # box installed with `rzfz init` (which never ran harden-host, so the marker
    # is absent) failed its FIRST upgrade on the documented offline path:
    # harden-host refuses a LIVE stack non-interactively, and on the upgrade
    # path the stack is live by definition. Hardening is host posture, not the
    # upgrade's content: it is DEFERRED with a loud line, the run continues,
    # and the final summary repeats it. The refusal itself stays right.
    HOST_HARDENING_DEFERRED=true
    print_warning "Host hardening did not complete — DEFERRED, the upgrade continues (#2216)."
    if [ "${RAZZFAZZ_HOST_HARDENING_STATE:-}" = deferred ]; then print_info "  No terminal and no passwordless sudo. Afterwards, once:"
    else print_info "  A running stack is not hardened non-interactively. Afterwards, once:"; fi
    print_info "    cd ${SCRIPT_DIR} && ./rzfz down && sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh && ./rzfz start"
    print_info "    (in a maintenance window: every container is recreated under the new Docker defaults)"
    print_info "    If harden-host fails, the stack stays down: run ./rzfz start, fix the cause from the log, and repeat."
    print_info "  Inspect first: sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh --dry-run"
    journal_event "host-hardening" "warn" "deferred: harden-host.sh did not complete on the upgrade path (#2216)" 2>/dev/null || true
    return 0
}
HOST_HARDENING_DEFERRED=false
ensure_host_hardened || true

# Step 5d2: block cloud metadata addresses for every container (DOCKER-USER).
# Every upgrade, independent of the host-hardened marker, so a box hardened
# before this rule existed gets it; the install is idempotent. It touches one
# iptables chain and restarts nothing, so a live stack is fine.
ensure_metadata_block() {
    if [ "${MIGRATIONS_ONLY:-false}" = true ]; then
        print_substep "Cloud metadata block: skipped (--migrations-only changes no host files)."
        return 0
    fi
    if [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
        print_substep "Cloud metadata block: skipped (--skip-host-updates)."
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        print_step "Would install the cloud metadata block (scripts/metadata-block.sh install)"
        return 0
    fi
    if ! razzfazz_apply_metadata_block "$SCRIPT_DIR"; then
        METADATA_BLOCK_DEFERRED=true
        journal_event "metadata-block" "warn" "not applied: ${RAZZFAZZ_METADATA_BLOCK_STATE:-failed}" 2>/dev/null || true
    fi
    return 0
}
METADATA_BLOCK_DEFERRED=false
ensure_metadata_block || true

# A world-writable Docker socket is root for every local user. An earlier
# host-hardening script ran a comment's backticks as root and left the socket
# 0666 until Docker restarted. Every upgrade checks the mode and repairs it:
# chmod 660 and the docker group first (no container is touched), then a
# restart of docker.socket if that did not hold. The repair is logged.
ensure_docker_socket_private() {
    local sock="${RZFZ_DOCKER_SOCKET:-/var/run/docker.sock}" mode as=""
    [ -S "$sock" ] || return 0
    mode=$(stat -c %a "$sock" 2>/dev/null || true)
    case "$mode" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) return 0 ;;
    esac
    if [ $(( 8#$mode & 2 )) -eq 0 ]; then
        print_substep "Docker socket is not world-writable (mode ${mode})."
        return 0
    fi
    print_warning "Docker socket ${sock} is world-writable (mode ${mode}) — every local user is root."
    if [ "${MIGRATIONS_ONLY:-false}" = true ] || [ "${SKIP_HOST_UPDATES:-false}" = true ]; then
        print_info "  Not repaired in this run (host files are left alone). Fix: sudo systemctl restart docker.socket docker"
        return 0
    fi
    if [ "$DRY_RUN" = "true" ]; then
        print_step "Would restore ${sock} to 660 root:docker"
        return 0
    fi
    [ "$(id -u)" -eq 0 ] || as="sudo -n"
    if getent group docker >/dev/null 2>&1; then $as chgrp docker "$sock" 2>/dev/null || true; fi
    $as chmod 660 "$sock" 2>/dev/null || true
    mode=$(stat -c %a "$sock" 2>/dev/null || true)
    if [ $(( 8#${mode:-666} & 2 )) -ne 0 ]; then
        $as systemctl restart docker.socket 2>/dev/null || true
        mode=$(stat -c %a "$sock" 2>/dev/null || true)
    fi
    if [ $(( 8#${mode:-666} & 2 )) -eq 0 ]; then
        print_success "Docker socket repaired (mode ${mode})."
        journal_event "docker-socket" "warn" "repaired a world-writable Docker socket (now ${mode})" 2>/dev/null || true
    else
        print_warning "Docker socket is still world-writable (mode ${mode:-unknown}). Run: sudo systemctl restart docker.socket docker"
        journal_event "docker-socket" "warn" "world-writable Docker socket NOT repaired" 2>/dev/null || true
    fi
    return 0
}
ensure_docker_socket_private || true


# ==============================================================================
# #536 (2026.09 fleet flip): carry an EXISTING box across the gpustack de-root.
# ==============================================================================
# Defined here, RUN at Step 7c (after the image build, before restart_stack) and
# previewed in the --check block below, because it is the riskiest step of this
# upgrade and an operator must be able to see it coming.
#
# A fresh install needs none of this: modules/llm/gpustack/Dockerfile.vulkan
# owns /var/lib/gpustack and the gpustack tree, and Docker initialises a new
# named volume from the image directory INCLUDING its ownership. An installed
# box is the hard case — its gpustack-data volume has been written by ROOT for
# the box's whole life, and modules/llm/compose.yml now says
# `user: ${GPUSTACK_UID:-1000}:${GPUSTACK_GID:-1000}` for the llm-legacy
# variant, so the very next `up` runs as uid 1000.
#
# Both ways that ends badly are SILENT — the container starts, health goes
# green, and model deploys sit at ready_replicas:0 forever:
#   * gpustack-data still root-owned      -> the server cannot write its state;
#   * image predates the flip (pre-#858)  -> tools_manager._link_llama_box_
#     rpc_server() cannot symlink into its root-owned runner tree.
# So: verify the image by LABEL, re-own the volume once, and when either cannot
# be done, keep the box on ROOT (the state that works) and record the deferral
# so the next upgrade retries it. A fail-safe that latches forever is how a
# fleet silently never flips.
#
# Rollback note: `--rollback` restores .env from the pre-upgrade backup and
# checks out the previous commit, so the compose file loses `user:` and gpustack
# is root again. The volume ownership change is NOT reverted and does not need
# to be — root ignores ownership. It is one-way and harmless.
gpustack_nonroot_ownership_migration() {
    # rev-C (review, 0.79 round 5): every exit path SAYS something. The round-5
    # log had no line at all about the flip — neither "done" nor "skipped,
    # because …" — so the run could not be told apart from a step that never
    # executed. A migration that stays silent cannot be measured (#1373, and
    # the same class as #1310/#1337: the script must say what it did).
    print_step "Step 7c (#536): gpustack non-root flip"
    # #2524: a step of the image change; --migrations-only changes no image.
    [ "${MIGRATIONS_ONLY:-false}" = true ] && { [ "${DRY_RUN:-false}" = true ] || print_substep "gpustack_nonroot_ownership_migration: skipped (--migrations-only)."; return 0; }
    local env_file="${SCRIPT_DIR}/.env"
    if [ ! -f "$env_file" ]; then
        print_substep "  no .env at ${env_file} — nothing to migrate."
        return 0
    fi

    # Per-variant gate: only the AMD/Vulkan variant (profile llm-legacy) was
    # live-verified de-rooted and only it carries `user:` in
    # modules/llm/compose.yml. The upstream `llm`/`llm-cpu` images and
    # `llm-cuda` still run as root — re-owning their volume would hand a
    # still-root container a surprise it never asked for.
    local profiles
    profiles="$(read_env_value "$env_file" COMPOSE_PROFILES)"
    case ",${profiles}," in
        *,llm-legacy,*) ;;
        *)
            print_substep "  skipped: no llm-legacy profile in COMPOSE_PROFILES (profiles: ${profiles:-<empty>}). Only the AMD/Vulkan variant was verified de-rooted; llm/llm-cpu/llm-cuda stay root."
            return 0 ;;
    esac

    if ! command -v docker >/dev/null 2>&1; then
        print_substep "#536: docker unavailable — skipping the gpustack ownership migration (nothing was verified, nothing changed)."
        return 0
    fi

    local dry=false
    [ "${DRY_RUN:-false}" = "true" ] && dry=true

    # #1274 rev-B (#1316 collision, 0.79 round 5): the image NAME comes from
    # the compose file, never from a literal here. #1316 renames the registry
    # ref to razzfazz-gpustack:vulkan and keeps the old ref as the --rollback
    # anchor; a literal read the label off the OLD image forever and deferred
    # the flip on every upgrade. Reading the service's `image:` follows any
    # rename. Label key and uid stay in lockstep with modules/llm/compose.yml
    # (gpustack-legacy) and modules/llm/gpustack/Dockerfile.vulkan — guarded by
    # tests/unit/llm/test_536_nonroot_default.py.
    local image
    image="$(awk '/^  gpustack-legacy:/{f=1; next} f && /^  [A-Za-z]/{f=0} f && /^ *image:/{sub(/^ *image: */, ""); gsub(/"/, ""); print; exit}' "${SCRIPT_DIR}/modules/llm/compose.yml" 2>/dev/null || true)"
    local label_key="ai.razzfazz.gpustack.nonroot-uid"
    local helper="alpine:3.21"
    local default_uid=1000 default_gid=1000
    local defer=""
    if [ -z "$image" ]; then
        defer="modules/llm/compose.yml names no image for gpustack-legacy — nothing to inspect"
    fi

    local uid gid deferred
    uid="$(read_env_value "$env_file" GPUSTACK_UID)"; uid="${uid:-$default_uid}"
    gid="$(read_env_value "$env_file" GPUSTACK_GID)"; gid="${gid:-$default_gid}"
    deferred="$(read_env_value "$env_file" GPUSTACK_NONROOT_DEFERRED)"

    if [ "$uid" = "0" ] || [ "$gid" = "0" ]; then
        if [ "$deferred" = "1" ]; then
            # A previous upgrade deferred the flip. Retry it now.
            print_step "#536: retrying the deferred gpustack non-root flip..."
            uid="$default_uid"; gid="$default_gid"
        else
            # The documented one-line rollback. An upgrade that undid it would
            # re-break the box it was set to fix.
            print_substep "#536: GPUSTACK_UID=${uid} — gpustack stays ROOT by operator choice; no ownership migration."
            return 0
        fi
    fi

    # --- prerequisite 1: RENDER_GID (rc6.7 #63) -------------------------------
    # As root the render gid is decoration; as uid 1000 it is the ONLY path to
    # /dev/kfd + /dev/dri. Step 5b.1 refreshes it from `getent group render`;
    # re-check here against the DEVICE, which is the authority even on a host
    # whose group table has no `render` entry.
    if [ "$(read_env_value "$env_file" HARDWARE)" = "amd" ]; then
        local render_env render_host=""
        render_env="$(read_env_value "$env_file" RENDER_GID)"
        render_host="$(stat -c %g /dev/dri/renderD128 2>/dev/null || true)"
        [ -n "$render_host" ] || render_host="$(getent group render 2>/dev/null | cut -d: -f3 || true)"
        if [ -z "$render_host" ]; then
            print_warning "#536: cannot determine the host's render gid (no /dev/dri/renderD128, no 'render' group) — RENDER_GID=${render_env:-<unset>} is UNVERIFIED for a de-rooted gpustack."
            print_info "If model deploys hang at ready_replicas:0 after this upgrade, check this first: the log line is 'amdgpu_query_info(ACCEL_WORKING) failed (-13)' (rc6.7 #63)."
        elif [ "$render_env" != "$render_host" ]; then
            print_warning "#536/rc6.7 #63: RENDER_GID=${render_env:-<unset>} but this host's render gid is ${render_host} — a de-rooted gpustack would lose its ONLY path to the GPU."
            if [ "$dry" = true ]; then
                print_substep "DRY RUN: would correct RENDER_GID to ${render_host}."
            else
                update_env_value "$env_file" RENDER_GID "$render_host"
                print_substep "Corrected RENDER_GID → ${render_host}."
            fi
        fi
    fi

    # --- prerequisite 3: the image must carry the non-root contract -----------
    # Asked by LABEL so no container has to be started. A pre-flip image plus a
    # non-root `user:` is the #858 hang.
    local label
    label="$(docker image inspect --format "{{ index .Config.Labels \"${label_key}\" }}" "$image" 2>/dev/null || true)"
    case "$label" in "<no value>"|"<nil>") label="" ;; esac
    if [ "$label" != "$uid" ]; then
        if [ "$dry" = true ]; then
            print_substep "DRY RUN: gpustack image label ${label_key}='${label:-<none>}' (want '${uid}') — would rebuild gpustack-legacy before flipping."
        elif razzfazz_is_offline "$env_file" || [ "${SKIP_PULL:-false}" = true ]; then
            # #184 WS2a: an air-gapped/package box never builds at runtime.
            print_substep "#536: the gpustack image was not built for the flip and this box does not build at runtime (offline/package)."
        else
            print_step "#536: rebuilding the gpustack Vulkan image for the non-root flip..."
            COMPOSE_FILE="$(compose_file_for_build "$env_file")" razzfazz_compose_build gpustack-legacy 2>&1 | sed 's/^/    /' || true
            label="$(docker image inspect --format "{{ index .Config.Labels \"${label_key}\" }}" "$image" 2>/dev/null || true)"
            case "$label" in "<no value>"|"<nil>") label="" ;; esac
        fi
        [ -n "$defer" ] || [ "$label" = "$uid" ] || defer="the gpustack image was built before the flip (label ${label_key}='${label:-<none>}', need '${uid}')"
    fi

    # --- prerequisite 2: gpustack-data ownership ------------------------------
    if [ -z "$defer" ]; then
        local vol state owner mark
        vol="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E '_gpustack-data$' | head -n1 || true)"
        if [ -z "$vol" ]; then
            print_substep "#536: no gpustack-data volume on this box yet — a fresh one inherits uid ${uid} from the image."
        else
            # The probe AND the chown need a helper image that has a shell.
            # `alpine:3.21` is the one the rest of this script uses, but an
            # air-gapped box may never have pulled it — and a missing helper
            # makes `docker run` fail BEFORE it ever looks at the volume, which
            # surfaced as "the volume could not be re-owned" and blamed the
            # wrong thing. Fall back to the gpustack image: it is present by
            # definition at this point (its label was just read), it carries a
            # shell, and `--entrypoint sh -u 0` bypasses both its server
            # entrypoint and its own non-root `USER`. Either way $helper names
            # the image that was actually used, so the deferral text below can
            # say so.
            local -a probe=(docker run --rm -v "${vol}:/d" "$helper" sh -c)
            if ! docker image inspect "$helper" >/dev/null 2>&1; then
                print_substep "#536: helper image ${helper} is not present on this box — using the gpustack image for the ownership probe instead."
                helper="$image"
                probe=(docker run --rm -u 0 --entrypoint sh -v "${vol}:/d" "$image" -c)
            fi
            # ONE probe, two answers: the volume root's ownership and whether
            # the completion marker exists. Ownership of the root alone is not
            # proof the recursive pass finished — a killed upgrade leaves the
            # root re-owned and the tree half root-owned.
            state="$("${probe[@]}" 'printf "%s %s" "$(stat -c %u:%g /d)" "$([ -f /d/.razzfazz-nonroot-owned ] && echo marker || echo nomarker)"' 2>/dev/null || true)"
            owner="${state%% *}"; mark="${state##* }"
            [ -n "$state" ] || print_substep "#536: could not read the current ownership of ${vol} with helper image ${helper} — attempting the re-own anyway."
            if [ "$owner" = "${uid}:${gid}" ] && [ "$mark" = "marker" ]; then
                print_substep "#536: gpustack-data already owned by ${uid}:${gid} (no change)."
            elif [ "$dry" = true ]; then
                print_substep "DRY RUN: would chown -R ${uid}:${gid} the gpustack-data volume (${vol}, currently ${owner:-unknown})."
            else
                print_step "#536: re-owning the gpustack-data volume to ${uid}:${gid} (one-time, before gpustack restarts non-root)..."
                if "${probe[@]}" "chown -R ${uid}:${gid} /d && : > /d/.razzfazz-nonroot-owned && chown ${uid}:${gid} /d/.razzfazz-nonroot-owned" >/dev/null 2>&1; then
                    print_substep "gpustack-data re-owned (completion marker: /var/lib/gpustack/.razzfazz-nonroot-owned)."
                else
                    defer="the gpustack-data volume (${vol}) could not be re-owned to ${uid}:${gid} with helper image ${helper}"
                fi
            fi
        fi
    fi

    # --- fail-safe: an unprepared box keeps the uid that works ----------------
    if [ -n "$defer" ]; then
        if [ "$dry" = true ]; then
            print_substep "DRY RUN: would DEFER the non-root flip (${defer}) and keep gpustack on root."
            return 0
        fi
        print_warning "#536: deferring the gpustack non-root flip — ${defer}."
        print_info "Keeping gpustack as ROOT (GPUSTACK_UID=0). De-rooted on an unprepared box it does not crash — the worker hangs at ready_replicas:0 with every health signal green and every model call 503s (PermissionError in tools_manager._link_llama_box_rpc_server, #858)."
        print_info "Fix: rebuild the image ('docker compose build gpustack-legacy', or re-run the upgrade online / with a complete offline package). The next 'rzfz upgrade' retries the flip by itself."
        update_env_value "$env_file" GPUSTACK_UID 0
        update_env_value "$env_file" GPUSTACK_GID 0
        update_env_value "$env_file" GPUSTACK_NONROOT_DEFERRED 1
        journal_event "warning" "warn" "#536 gpustack non-root flip deferred: ${defer}" 2>/dev/null || true
        return 0
    fi

    if [ "$dry" = true ]; then
        print_substep "  DRY RUN: prerequisites met — the flip to ${uid}:${gid} would happen here."
        return 0
    fi

    if [ "$deferred" = "1" ] || [ "$(read_env_value "$env_file" GPUSTACK_UID)" != "$uid" ]; then
        update_env_value "$env_file" GPUSTACK_UID "$uid"
        update_env_value "$env_file" GPUSTACK_GID "$gid"
        update_env_value "$env_file" GPUSTACK_NONROOT_DEFERRED ""
        print_substep "#536: gpustack runs as ${uid}:${gid} after the restart."
    else
        # rev-C (review): the box is ALREADY flipped. Saying so is the
        # difference between "the step ran and had nothing to do" and "the step
        # never ran" — the round-5 log could not tell them apart.
        print_substep "  already non-root (GPUSTACK_UID=${uid}, GPUSTACK_GID=${gid}) — nothing to do."
    fi
    return 0
}

# #2512: derive_llm_master_role [dry-run] — LLM_MASTER_ROLE for a box that
# predates it (llm_master_role_ensure, cli/lib-llm-manager-deploy.sh). Defined
# here, ABOVE the dry-run exit, so `rzfz upgrade --check` previews the value
# and its reason; the real write happens at Step 6b.1 below.
derive_llm_master_role() {
    if ! declare -F llm_master_role_ensure >/dev/null 2>&1; then
        # shellcheck source=/dev/null
        . "${SCRIPT_DIR}/cli/lib-llm-manager-deploy.sh" 2>/dev/null || {
            print_warning "Could not load cli/lib-llm-manager-deploy.sh — LLM_MASTER_ROLE not derived (post-install --refresh asks again, #2512)."
            return 0
        }
    fi
    print_step "Step 6b.1 (#2512): the master's LLM role"
    llm_master_role_ensure ".env" "${1:-}" || true
}

# #2697: before the fix, `rzfz init` printed a generated admin password and the
# run-log tee kept it in razzfazz-init-*.log (0644, the last ten kept). Fixing
# init does not clean the logs a box already has, so every upgrade looks once
# more (scripts/scrub_run_logs.py): a razzfazz-*.log that holds the current
# admin password (or the backup password, which init set to the same value)
# gets `[redacted]` in its place, and every run log is tightened to 0600. The
# values reach python on stdin only (never argv, never the output); a clean box
# finds nothing, which makes the step idempotent. Only the operator's log
# directories are tightened to 0700; in TMPDIR and /tmp only this user's own
# files are touched and the directory is left alone. Defined here, ABOVE the
# dry-run exit, so `rzfz upgrade --check` reports it too (Step 10d runs it).
scrub_admin_password_from_run_logs() {  # $1 = .env
    local env_file="$1" result redacted tightened sudo_home sudo_logs="" shared_tmp="${TMPDIR:-/tmp}"
    local system_tmp=/tmp
    # The tests point this away from the real /tmp, only under their marker.
    [ "${_RZFZ_SCRUB_TEST:-}" = 1 ] && system_tmp="${_RZFZ_SYSTEM_TMP:-/tmp}"
    local -a args=(--skip "${RZFZ_RUN_LOG:-}" --shared "$shared_tmp" --shared "$system_tmp")
    [ "${DRY_RUN:-false}" = true ] && args+=(--dry-run)
    # The sudo user's log dir must resolve to a dir that user owns (--sudo-own).
    if [ -n "${SUDO_USER:-}" ]; then
        sudo_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
        [ -n "$sudo_home" ] && sudo_logs="$sudo_home/.razzfazz/logs" && args+=(--sudo-own "$sudo_logs")
    fi
    local d
    for d in "${RZFZ_LOG_DIR:-}" "$HOME/.razzfazz/logs"; do
        [ -n "$d" ] && [ "$d" != "$shared_tmp" ] && [ "$d" != "$system_tmp" ] && [ "$d" != /tmp ] \
            && [ "$d" != "$sudo_logs" ] && args+=(--own "$d")
    done
    result="$( { read_env_value "$env_file" AUTHENTIK_BOOTSTRAP_PASSWORD 2>/dev/null || true; echo
                 read_env_value "$env_file" BACKUP_ENCRYPTION_PASSWORD 2>/dev/null || true; echo; } \
        | python3 "${SCRIPT_DIR}/scripts/scrub_run_logs.py" "${args[@]}" 2>/dev/null)" || result=""
    read -r redacted tightened <<<"${result:-}"
    if [ -z "${redacted:-}" ]; then
        print_warning "Could not check the earlier run logs for the admin password (python3 failed)."
        return 0
    fi
    if [ "$redacted" -gt 0 ]; then
        if [ "${DRY_RUN:-false}" = true ]; then
            print_info "Would redact the admin password from ${redacted} earlier run log(s)."
        else
            print_warning "Redacted the admin password from ${redacted} earlier run log(s); it had been written there in clear text."
        fi
    fi
    if [ "${tightened:-0}" -gt 0 ]; then
        print_substep "Restricted ${tightened} earlier run log(s) to mode 0600."
    fi
}

# Step 6: Data migrations preview (dry run only)
if [ "$DRY_RUN" = "true" ]; then
    run_data_migrations
    # #536: show the fleet flip in --check too — it is the step most likely to
    # surprise an operator upgrading an AMD box into 2026.09.
    gpustack_nonroot_ownership_migration
    # #2512: what the role migration would write, and why.
    derive_llm_master_role dry-run
    # #2697: what Step 10d would redact in the earlier run logs.
    scrub_admin_password_from_run_logs ".env"
    # #2977: whether the Valkey password would be rotated.
    rotate_valkey_password_once
fi

# Dry run ends here
if [ "$DRY_RUN" = "true" ]; then
    if [ "$MIGRATIONS_ONLY" = true ]; then
        print_info "Migrations only — would then: reconcile .env (missing secrets, SSO derivations, LLM master role,"
        print_info "  network overlays); check the Authentik two-hop for the data's Authentik version (${OLD_AUTHENTIK_VERSION:-unknown});"
        print_info "  start the stack (rzfz start); flush Authentik sessions on a version change; reconcile outposts, Komodo"
        print_info "  prune and OWUI sign-up; run the data migrations above; agent volumes and Vaultwarden; OWUI retrieval"
        print_info "  defaults. No build, no pull, no backup, no host changes."
    fi
    echo ""
    print_info "Dry run complete. No changes were made."
    print_info "Run without --check to perform the actual upgrade."
    exit 0
fi

# Step 6: Confirmation
echo "DEBUG[prompt_check]: FORCE=$FORCE (skip prompt iff true)" >&2
# #2137: normally answered before the code update (confirm_upgrade_before_changes).
# Asked here only when an older upgrader, which had no early prompt, re-exec'd
# into this one.
if [ "$FORCE" != true ] && [ "${_UPGRADE_CONFIRMED:-false}" != true ]; then
    echo ""
    echo -e "${YELLOW}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║ Upgrade Summary                                           ║${NC}"
    echo -e "${YELLOW}╠═══════════════════════════════════════════════════════════╣${NC}"
    echo -e "${YELLOW}║${NC} From:  v${INSTALLED_VERSION} (${INSTALLED_COMMIT})"
    echo -e "${YELLOW}║${NC} To:    v${TARGET_VERSION} (${TARGET_COMMIT})"
    echo -e "${YELLOW}║${NC} Build: ${REQUIRES_BUILD:-true}  Pull: ${REQUIRES_PULL:-true}"
    echo -e "${YELLOW}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    read -r -p "Proceed with upgrade? [y/N] " confirm || confirm=""
    if [[ ! "$confirm" =~ ^[Yy] ]]; then
        # #2137: reached only when an older upgrader re-exec'd into this one:
        # the code and the env migration are ALREADY applied here. Restoring
        # only the .env (as this branch once did) left new code beside an old
        # .env; --rollback restores both.
        print_error "Upgrade stopped after the code update and the .env migration were applied (${INSTALLED_VERSION} -> ${TARGET_VERSION})."
        print_info  "The box now runs the new code with a migrated .env. To go back: rzfz upgrade --rollback"
        exit 1
    fi
fi

# Step 6b: Generate any missing secrets introduced by migrate_env / sync_dify_env
# (F-upgrade-missing-secrets-call): without this, new .env keys stay empty and
# downstream migrations / services fail when they read those values. Defined but
# unreferenced in the script prior to this fix.
# #140: capture whether SMTP_INTERNAL_PASSWORD was empty BEFORE secret-gen — if it
# gets generated now, the smtp-relay re-keys its SASL user, so any client with a
# HARDCODED old SMTP password (e.g. a Dify email/workflow node) starts failing 535.
__SMTP_PW_WAS_EMPTY=false
[ -z "$(read_env_value ".env" "SMTP_INTERNAL_PASSWORD")" ] && __SMTP_PW_WAS_EMPTY=true
generate_missing_secrets
if [ "$__SMTP_PW_WAS_EMPTY" = true ] && [ -n "$(read_env_value ".env" "SMTP_INTERNAL_PASSWORD")" ]; then
    print_warning "#140: SMTP_INTERNAL_PASSWORD was empty and is now generated — the smtp-relay re-keys its SASL user."
    print_substep "Any client with a HARDCODED SMTP password (Dify email/workflow nodes, scripts) will get 535 auth-failed until updated to the new value: grep ^SMTP_INTERNAL_PASSWORD= .env"
    journal_event "warning" "warn" "SMTP_INTERNAL_PASSWORD empty->generated; hardcoded SMTP clients need the new password (#140)"
fi
# #2507: after the secrets (the derivations read the final switch values),
# before restart_stack (which recreates every container with them).
reconcile_sso_env_derivations ".env"

# #2512: LLM_MASTER_ROLE for a box that predates it — derived from the joined
# workers (env-changes.json added it EMPTY; that format cannot say "derive").
# Before restart_stack on purpose: the manager of the running stack is asked,
# and the recreated worker-agent then starts with the role (a fleet master's
# own worker registers as draining). An operator-set value is never touched.
derive_llm_master_role

# Step 6b.2 (2026.11): OpenHands leaves the box (retire_openhands_module, defined above).
retire_openhands_module ".env"

# 2026.11: the LLM Manager service key OpenHands used (cost centre
# stack/openhands) is revoked, so a key of a removed module never authenticates
# again. After the restart, when the manager is up; idempotent (an already
# revoked key is left as it is) and silent when there is nothing to revoke.
revoke_retired_llm_service_keys() {
    [ "${DRY_RUN:-false}" = true ] && return 0
    [ "${MIGRATIONS_ONLY:-false}" != true ] || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x llm-manager >/dev/null || return 0
    local out
    out=$(docker exec -i llm-manager python3 - stack/openhands <<'REVOKEEOF' 2>/dev/null || true
import sys
sys.path.insert(0, "/app")
from app.db import session_scope
from app.models import ApiKey, CostCenter
try:  # the auth cache keeps a verdict for minutes; drop it like the keys API does
    from app.api.keys import _invalidate
    from app.cache import get_cache
    cache = get_cache()
except Exception:
    cache = None
n = 0
with session_scope() as s:
    for name in sys.argv[1:]:
        cc = s.query(CostCenter).filter(CostCenter.name == name).first()
        if cc is None:
            continue
        for k in s.query(ApiKey).filter(ApiKey.cost_center_id == cc.id, ApiKey.status != "revoked"):
            k.status = "revoked"
            n += 1
            if cache is not None:
                try:
                    _invalidate(cache, bytes(k.key_hash))
                except Exception:
                    pass
print(n)
REVOKEEOF
)
    if [ -n "$out" ] && [ "$out" != 0 ]; then
        print_substep "Revoked ${out} LLM Manager key(s) of the removed OpenHands module."
    fi
    return 0
}

# Step 6c: Network mode (#184 WS2b) — compose the right overlay into COMPOSE_FILE
# for the box's RAZZFAZZ_NETWORK_MODE (online|proxied|offline), and back-compat-
# migrate a legacy RAZZFAZZ_CORPORATE_PROXY/RAZZFAZZ_OFFLINE box to the mode key.
# Runs BEFORE build_and_pull/restart_stack so the overlay is in effect for the
# compose ops below. Best-effort; never aborts the upgrade.
# #2177: the operator can state the box's egress posture for this upgrade.
# Deliberately NOT derived from --package. The standing rule is package-first on
# ANY box — "if an image or model GGUF is present in the package, use it,
# regardless of RAZZFAZZ_NETWORK_MODE" — so upgrading from a package is the
# RECOMMENDED practice on a networked box, not evidence of an air gap. Deriving
# `offline` from it would flip those boxes into refusing the fallback download
# they rely on, which is worse than the gap this closes.
if [ -n "${CONFIG_NETWORK_MODE:-}" ]; then
    update_env_value ".env" RAZZFAZZ_NETWORK_MODE "$CONFIG_NETWORK_MODE"
    print_substep "Network mode: ${CONFIG_NETWORK_MODE} (explicit --network-mode)."
fi
ensure_network_mode_overlay ".env"
# Step 6c.1 (#277/#280/#275): on a proxied box, REGENERATE the box-local
# corporate-proxy overlay (+ Dify SSRF-chain squid.conf) from the CURRENT
# generator so overlay-side fixes land WITHOUT a manual `rzfz setup
# --corporate-proxy`. Level-C only (no sudo/host changes, no reachability gate).
# No-op on a non-proxied box. Runs BEFORE build_and_pull so the new sidecar image
# is pulled and the refreshed NO_PROXY/CA are in effect for the restart below.
if [ "$(read_env_value ".env" RAZZFAZZ_CORPORATE_PROXY)" = "1" ]; then
    "${SCRIPT_DIR}/scripts/apply-corporate-proxy.sh" --regenerate \
        || print_warning "corporate-proxy overlay regen returned non-zero (best-effort; run 'rzfz setup --corporate-proxy' to refresh)."
fi
# #184 WS2a: compose the UNIVERSAL no-runtime-build overlay (all modes). After
# this, `docker compose up -d`/enable/disable can never build — restart_stack's
# `up` below is thus build-proof. The build_and_pull step below strips it (via
# compose_file_for_build) so the ONE intended build still has the build: contexts.
ensure_nobuild_overlay ".env"
# #2526: the opt-in UDP 514 syslog port follows WAZUH_SYSLOG_UDP.
ensure_wazuh_syslog_udp_overlay ".env"

# Step 7: Build and pull images
# #693: the build phase next to a RUNNING full stack is what OOM-froze 0.208
# (one BuildKit trace building every custom image in parallel; 26.5 GB RSS on
# a 30-GB box). Guard: loud low-RAM warning + capped parallelism (inside
# build_and_pull via COMPOSE_PARALLEL_LIMIT); opt-in stack stop for the phase.
# STEP7_BUILD-BEGIN
# #2524: --migrations-only builds nothing: no notice, no RAM guard, no offer to stop the stack.
if [ "$MIGRATIONS_ONLY" != true ]; then
# SOURCE_BUILD_NOTICE-BEGIN
# Say what this hop compiles, and what it costs, BEFORE building. The offer to
# stop the stack is interactive only: an unattended run never blocks and never
# stops the stack on its own (it needs the explicit --stop-stack-for-build).
if ! razzfazz_is_offline; then
    _srcb=$(razzfazz_source_builds_pending .env 2>/dev/null || true)
    razzfazz_announce_source_builds "$_srcb" upgrade
    if [ -n "$_srcb" ] && [ "${STOP_STACK_FOR_BUILD:-false}" != true ] && razzfazz_stack_is_up; then
        if [ -t 0 ]; then
            read -r -p "Stop the stack for the build phase (--stop-stack-for-build)? [y/N] " _ans || _ans=""
            case "$_ans" in [Yy]*) STOP_STACK_FOR_BUILD=true ;; esac
        else
            print_warning "The stack stays up during the build (unattended). Builds run one service at a time with bounded jobs; for more headroom re-run with --stop-stack-for-build."
        fi
    fi
fi
# SOURCE_BUILD_NOTICE-END
razzfazz_build_ram_guard || true
fi
if [ "${STOP_STACK_FOR_BUILD:-false}" = true ] && ! razzfazz_is_offline; then
    # The cognee storage copy only works while the container runs, so it comes
    # first (with the stack stopped there is nothing left to write to it).
    if ! preserve_cognee_storage; then
        print_error "Upgrade aborted: cognee storage could not be preserved. Nothing was stopped or restarted."
        exit 1
    fi
    if ! preserve_wazuh_registry; then
        print_error "Upgrade aborted: the Wazuh agent registry could not be preserved (#2732). Nothing was stopped or restarted."
        exit 1
    fi
    # Record what runs, so an abort starts exactly those containers again.
    if _ids_before=$(docker compose ps -q --status running 2>/dev/null); then
        STACK_STOPPED_IDS=$(printf '%s\n' "$_ids_before" | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')
    else
        STACK_STOPPED_IDS=""
    fi
    if [ -n "$STACK_STOPPED_IDS" ]; then
        print_step "Stopping the stack for the build phase (--stop-stack-for-build)..."
        STACK_STOPPED_FOR_BUILD=true
        COGNEE_PRESERVED_BEFORE_STOP=true
        docker compose stop 2>&1 || print_warning "compose stop returned non-zero — continuing into the build."
    else
        print_warning "Could not record the running containers (or none run); the stack is NOT stopped for the build."
    fi
fi
if ! build_and_pull; then
    print_error "Image build/pull failed. Rolling back..."
    _rb_rc=0
    ROLLBACK_SKIP_REFRESH=true rollback || _rb_rc=$?
    # A rollback that recreated the stack has brought it back; any other outcome
    # leaves a stack we stopped down, so the regular restart path runs.
    [ "$_rb_rc" -ne 0 ] || STACK_STOPPED_FOR_BUILD=false
    restore_stack_stopped_for_build
    exit $(( _rb_rc != 0 ? _rb_rc : 1 ))
fi
# STEP7_BUILD-END

# What the box looks like when Step 7b stops it: true at that point of the run, on
# either arm. The rollback needs the backup's .env copy.
_image_stop_says_what_changed() {
    print_error "The running containers were NOT touched, but this run already switched the code to ${TARGET_VERSION:-the new release}, migrated .env and ran the database and secret steps; the version stamp still says ${INSTALLED_VERSION:-the old release}."
    print_error "Until that is resolved, do not restart services or switch modules: anything recreated now runs old images on new settings."
    if [ -f "${SCRIPT_DIR}/.env.pre-upgrade-backup" ]; then
        print_error "Go back: 'rzfz upgrade --rollback'."
    else
        print_error "No .env.pre-upgrade-backup exists (the backup was skipped), so 'rzfz upgrade --rollback' cannot go back."
    fi
}

# Step 7b: Image verification (#184 WS5, #2035) — BEFORE restart, on EVERY path.
# A missing image leaves a container un-creatable at `up`, so the expected set
# is verified first and the upgrade REFUSES to restart with an actionable
# message. `rzfz verify-images` is the shared hard check (sibling).
#
# Two scopes, one gate:
#   offline / package  -> the FULL set across all profiles: images come from the
#                         package (code_update_package: docker load + SKIP_PULL),
#                         the runtime never builds/pulls (WS2a), and a package
#                         must be complete.
#   online             -> the ACTIVE set (`--active`): what THIS box's compose
#                         render needs, i.e. what `up` will create. Until #2035
#                         the online path verified nothing — `docker compose
#                         pull --ignore-pull-failures` swallowed the failure,
#                         `|| print_warning` swallowed the rest, and the success
#                         lines printed regardless, so a pin naming a tag that
#                         does not exist produced one warning and an upgrade that
#                         reported success, then a container that could not be
#                         created. Operator decision 2026-09-13: strict.
if "${SCRIPT_DIR}/rzfz" --list 2>/dev/null | grep -x "verify-images" >/dev/null; then
    if [ "${SKIP_PULL:-false}" = true ] || razzfazz_is_offline; then
        print_step "Verifying all expected images are present before restart (rzfz verify-images)..."
        # A package must be COMPLETE, whatever the box's network mode says: with
        # --package the images a disabled profile pulls on enable are required.
        _verify_rc=0
        if [ -n "${PACKAGE_FILE:-}" ]; then
            "${SCRIPT_DIR}/rzfz" verify-images --strict || _verify_rc=$?
        else
            "${SCRIPT_DIR}/rzfz" verify-images || _verify_rc=$?
        fi
        if [ "$_verify_rc" -eq 3 ]; then
            # #2757: every image is present, but custom images are not this tree's
            # build and nothing here can rebuild them (offline, or a package).
            # Restarting would run the old builds — a fixed CVE kept — while the
            # upgrade reports success. Stop before anything is recreated.
            # The check before the first change normally said this already. This
            # is the backstop for what that check cannot foresee, so it says what
            # this run has already done.
            print_error "Custom images on this box are NOT this tree's build (listed as [stale] above) — refusing to restart the stack onto them."
            if [ -n "${PACKAGE_FILE:-}" ]; then
                print_error "This package does not carry them and this box cannot build them."
            else
                print_error "This box is set to offline mode and cannot build them."
            fi
            _image_stop_says_what_changed
            print_error "Or finish it: re-run the upgrade with a package that carries the images of ${TARGET_VERSION:-the target release} (see 'an offline package (Enterprise) --help'),"
            print_error "or, with network access, add '--network-mode online': it builds what is missing, and the box stays in online mode afterwards."
            exit 1
        fi
        if [ "$_verify_rc" -ne 0 ]; then
            print_error "One or more required images are MISSING — refusing to restart the stack."
            print_error "This box obtains images from the offline package and never builds/pulls at runtime."
            _image_stop_says_what_changed
            print_error "Or finish it: load the missing image(s) — re-run the offline upgrade with a COMPLETE --package,"
            print_error "or 'docker load -i <image>.tar' for each — then retry 'rzfz upgrade'."
            exit 1
        fi
        print_success "All expected images present — proceeding with restart."
    else
        print_step "Verifying the images this box runs are present before restart (rzfz verify-images --active)..."
        if ! "${SCRIPT_DIR}/rzfz" verify-images --active; then
            print_error "One or more images this box runs are MISSING — refusing to restart the stack."
            if [ "${PULL_RAN:-false}" = true ]; then
                print_error "The pull above did not deliver them: either the registry was unreachable or a pin"
                print_error "names a tag that does not exist. The running stack has NOT been touched."
            else
                print_error "No pull ran in this upgrade and they are not local."
                print_error "The running stack has NOT been touched. Pull them: docker compose pull"
            fi
            print_error "Fix the pin or the network, then retry 'rzfz upgrade'."
            exit 1
        fi
        print_success "All images this box runs are present — proceeding with restart."
    fi
else
    print_warning "rzfz verify-images not available on this box — skipping the pre-restart image check (upgrade continues)."
fi

# Step 7b.1: #2872 — Vaultwarden SQLite → Postgres: prove the SQLite check CAN run
# BEFORE restart_stack recreates Vaultwarden on the Postgres-wired compose. "Could
# not check" must never be read as "no users": an offline box without the pinned
# probe images would otherwise start Vaultwarden on an empty Postgres schema and
# leave the users' vaults behind in SQLite. Read-only; a no-op unless the
# vaultwarden profile is on and a vaultwarden-data volume exists.
if [ -x "${SCRIPT_DIR}/scripts/migrate-vaultwarden-to-postgres.sh" ]; then
    _vwpf_rc=0
    bash "${SCRIPT_DIR}/scripts/migrate-vaultwarden-to-postgres.sh" --preflight || _vwpf_rc=$?
    if [ "$_vwpf_rc" -ne 0 ]; then
        print_error "Vaultwarden's SQLite database cannot be checked or migrated safely (exit ${_vwpf_rc}) — refusing to restart the stack."
        print_error "Vaultwarden has NOT been switched to Postgres and its SQLite data is untouched; the running stack has NOT been touched."
        print_error "Fix what is listed above (load a COMPLETE offline package, or pull the pinned images), then retry 'rzfz upgrade'."
        restore_stack_stopped_for_build
        exit 1
    fi
fi

# Step 7c: #536 (2026.09 fleet flip) — prepare an existing box for a NON-ROOT
# gpustack. Must run AFTER the build (the image-readiness gate inspects the
# freshly built image) and BEFORE restart_stack (the container comes up with
# `user:` applied). Never aborts the upgrade: it defers the flip and leaves the
# box on root when it cannot make the flip safe. See the function header.
gpustack_nonroot_ownership_migration

# Step 7c: #2433 — copy cognee's live storage out of the container layer into
# the volumes BEFORE the recreate that applies the corrected mount roots. A run
# that stopped the stack for the build already did this before the stop (it needs
# the running container); otherwise it runs here, after the build, so writes made
# during a long build are still copied.
# STEP7C_COGNEE-BEGIN
if [ "${COGNEE_PRESERVED_BEFORE_STOP:-false}" != true ] && ! preserve_cognee_storage; then
    print_error "Upgrade aborted: cognee storage could not be preserved. Nothing was restarted."
    exit 1
fi
# STEP7C_COGNEE-END

# #2732 — the Wazuh agent registry (client.keys, agent databases) moves from the
# container layer into wazuh-manager-data before the recreate mounts it. Safe to
# repeat: a path that is already a mount is skipped.
if ! preserve_wazuh_registry; then
    print_error "Upgrade aborted: the Wazuh agent registry could not be preserved. Nothing was restarted."
    exit 1
fi

# #2455 — the volumes belong to the user the new cognee image runs as, before
# the recreate points it at them (covers the ga.15 path the preserve skips).
if ! align_cognee_volume_ownership; then
    print_error "Upgrade aborted: cognee volume ownership could not be aligned. Nothing was restarted."
    exit 1
fi

# Step 7d: #2690 — stored Gitea credentials, after the code update and BEFORE
# the recreate: the PAT mode is settled once, a rental box loses every stored
# token, and a stale STACK_GIT_CREDENTIALS_PATH is cleared before
# razzfazz-config is recreated with it. Unconditional: --skip-verify skips
# only the checks, never this.
# STEP7D_GIT_CREDENTIALS-BEGIN
settle_git_credentials
# STEP7D_GIT_CREDENTIALS-END

# Step 8: Restart stack
# STEP8_RESTART-BEGIN
STACK_STOPPED_FOR_BUILD=false   # restart_stack recreates everything on the new tree
restart_stack
# STEP8_RESTART-END
adopt_gpustack_weights_if_migrating
# #2813: a CPU box leaves GPUStack for the manager's engines (finished after the refresh).
cpu_gpustack_handover_begin
# #2393: engines from before 2026.11 leave the egress-capable default network.
migrate_master_engines_network
# An Authentik restart loop is the upgrade's verdict, not a quiet follow-on failure.
AUTHENTIK_CRASHLOOP=0
if [ "${DRY_RUN:-false}" != true ]; then
    authentik_crashloop_check || AUTHENTIK_CRASHLOOP=1
fi

# Step 8a: #2403 — the whole project up, the Authentik marker against the tree,
# the init run detached when behind. Its issue count feeds the final verdict.
AUTHENTIK_INIT_ISSUES=0
verify_authentik_init_marker || AUTHENTIK_INIT_ISSUES=$?

# #2455 — cognee's own user can write both storage roots after the recreate.
COGNEE_STORAGE_ISSUES=0
verify_cognee_storage_writable || COGNEE_STORAGE_ISSUES=1

# #148: flush Authentik sessions if this upgrade changed the Authentik version
# (prevents the stale-session LookupError 500 on /application/o/authorize).
flush_authentik_sessions_on_version_change

# #145/#147: reconcile outpost bindings from the host (idempotent) so a box whose
# init container couldn't run docker (restricted network) still ends up bound.
reconcile_outpost_bindings_hostside

# #539: keep Komodo's nightly image prune off on every upgraded box, not
# just fresh installs (post-install is not re-run by upgrades).
reconcile_komodo_auto_prune
reconcile_owui_oauth_signup ".env"
revoke_retired_llm_service_keys

# Step 8b: #2977 - the Valkey password, once, for every consumer together. After
# the restart (valkey and its consumers are up on the old one), before the
# post-install refresh that re-keys the agent instances.
rotate_valkey_password_once

# Step 9: Data migrations (targeted DB changes, preserves manual UI edits)
run_data_migrations

# Step 9b: M020 S07 — migrate global hermes/moltis/coding-tools volumes
# to per-user agent-manager volumes. Only acts when global volumes
# carry data; safe (idempotent) on fresh installs and re-runs.
if [ -x "${SCRIPT_DIR}/scripts/migrate-to-per-user-agents.sh" ]; then
    print_step "Migrating global agent volumes to per-user (M020 S07)..."
    if ! bash "${SCRIPT_DIR}/scripts/migrate-to-per-user-agents.sh"; then
        print_warning "Per-user agent migration exited non-zero (non-fatal)."
        print_info "See docs/migration-2026.05-per-user-agents.md for the manual runbook."
    fi
fi

# Step 9c: #226 — migrate an existing Vaultwarden SQLite DB into the shared
# Postgres (vaultwarden_db). Fully idempotent + self-guarding: no-op unless the
# vaultwarden profile is enabled AND SQLite holds data AND vaultwarden_db is
# still empty (so it skips fresh boxes and already-migrated boxes such as the
# prod hotfix). restart_stack above already recreated VW on the PG-wired compose;
# if a migration is needed the script stops VW, copies the data across, verifies
# parity, and leaves VW stopped for us to bring back up on the now-populated PG.
# A failure is non-fatal to the upgrade and leaves VW on SQLite (the script
# restarts it), so a password manager is never left broken by a bad migration.
if [ -x "${SCRIPT_DIR}/scripts/migrate-vaultwarden-to-postgres.sh" ]; then
    print_step "Migrating Vaultwarden SQLite → Postgres if needed..."
    _vwmig_rc=0
    bash "${SCRIPT_DIR}/scripts/migrate-vaultwarden-to-postgres.sh" || _vwmig_rc=$?
    if [ "$_vwmig_rc" -eq 0 ]; then
        # Ensure VW is running on PG (the script leaves it stopped only when it
        # actually migrated; harmless if it's already up).
        docker start vaultwarden >/dev/null 2>&1 || true
    elif [ "$_vwmig_rc" -eq 7 ]; then
        # #2872: the SQLite check could not run. Unknown is not "empty": stop here
        # instead of reporting an upgrade whose Vaultwarden data state is unknown.
        print_error "Vaultwarden's SQLite database could not be checked — stopping. Its SQLite data is untouched."
        print_error "Load the pinned probe images (offline package) or pull them, then re-run: scripts/migrate-vaultwarden-to-postgres.sh"
        exit 1
    else
        print_warning "Vaultwarden SQLite→Postgres migration exited non-zero (non-fatal)."
        print_info "Vaultwarden was left on SQLite; re-run: scripts/migrate-vaultwarden-to-postgres.sh"
    fi
fi

# Step 9b: M022 / rc6.8 — build local llama-* runner images.
# All three runners (vulkan, rocm, cpu) bundle modules/llm/runners/llama-server-shim
# which translates GPUStack-style `--flag=value` backend_parameters into
# `--flag value` (the form upstream llama-server requires). Idempotent —
# docker build skips unchanged layers; we also skip entirely if the tagged
# image is already present.
COMPOSE_PROFILES_NOW=$(grep '^COMPOSE_PROFILES=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
HARDWARE_NOW=$(grep '^HARDWARE=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
if [ "$MIGRATIONS_ONLY" = true ]; then
    print_substep "Step 9b: --migrations-only — no llama-runner image builds."
elif razzfazz_is_offline; then
    # #184 WS2b: llama-runner images are loaded from the offline package, never
    # built on an air-gapped box (the build pulls a base image + apt).
    print_substep "Step 9b: OFFLINE (RAZZFAZZ_NETWORK_MODE=offline) — skipping llama-runner image builds (loaded from the offline package)."
elif razzfazz_runner_images_wanted "${COMPOSE_PROFILES_NOW:-}" \
   && [ -d "${SCRIPT_DIR}/modules/llm/runners" ]; then
    _build_runner_img() {
        # #628: $3 pins the vulkan LLAMA_CPP_TAG — the Dockerfile default moves
        # with releases; an unpinned old-tag build gets the new binary.
        local img="$1" df="$2" barg="$3"
        if docker image inspect "$img" >/dev/null 2>&1; then
            return 0
        fi
        print_step "Building $img (llama-server-shim runner image)..."
        if razzfazz_docker_build -t "$img" ${barg:+--build-arg "$barg"} -f "${SCRIPT_DIR}/$df" "${SCRIPT_DIR}/$(razzfazz_runner_build_context "${SCRIPT_DIR}")"; then
            print_success "$img built."
        else
            print_warning "Failed to build $img; the matching custom backend will not be usable."
        fi
    }
    # #1516 (E5): driven by modules/llm/runners/runners.yaml — the same file
    # cli/post-install.sh and the publish step read, so the four build sites can
    # no longer drift (#1497). A `cuda` box builds BOTH CUDA targets: sm_120
    # (RTX PRO 6000) and sm_121a (GB10). CUDA 12.8's nvcc does not know sm_121,
    # so one image cannot serve both, and the wrong one dies with "no kernel
    # image is available for execution on the device" (measured on a GB10).
    _rt_hw=$(razzfazz_runner_hw_class "${HARDWARE_NOW:-amd}")
    _rt_rows=$(razzfazz_runner_manifest_rows "${SCRIPT_DIR:-.}")
    if [ -n "$_rt_rows" ]; then
        while IFS=$'\t' read -r _rt_img _rt_df _rt_class _rt_legacy _rt_args; do
            [ -n "$_rt_img" ] || continue
            [ "$_rt_class" = "$_rt_hw" ] || continue
            _build_runner_img "$_rt_img" "$_rt_df" $_rt_args
            # One cycle of back-compat for boxes pinned to the old name.
            if [ -n "$_rt_legacy" ] && docker image inspect "$_rt_img" >/dev/null 2>&1; then
                docker tag "$_rt_img" "$_rt_legacy" >/dev/null 2>&1 || true
            fi
        done <<< "$_rt_rows"
    else
        print_warning "modules/llm/runners/runners.yaml unreadable (PyYAML missing?) — no runner images built."
    fi
else
    # #1373: visible skip (see init.sh Step 7b).
    print_substep "Step 9b: no runner-image consumer profile active (one of: ${RAZZFAZZ_RUNNER_IMAGE_CONSUMER_PROFILES}) — skipping llama-runner image builds."
fi

# Step 10: Post-upgrade verification
UPGRADE_VERIFIED=false
if [ "$SKIP_VERIFY" = false ]; then
    if ! verify_upgrade; then
        print_warning "Upgrade completed with issues."
        print_info "Review logs: docker compose logs -f"
        print_info "To rollback: rzfz upgrade --rollback"
    else
        UPGRADE_VERIFIED=true
    fi
fi
# Step 10a: #3265 — a public-channel box keeps no development history. Only after a
# verified upgrade: a rollback needs the old commit (an unverified run says so and
# the next verified upgrade removes it).
if [ "$UPGRADE_VERIFIED" = true ]; then
    prune_dev_history_after_upgrade
elif [ "$(razzfazz_channel)" = public ] && ! razzfazz_prune_dev_history "$SCRIPT_DIR" check >/dev/null 2>&1; then
    print_warning "Development history from git.razzfazz.ai is still in this checkout; it is removed after the next verified upgrade."
fi
# #2216: the deferred hardening is repeated where the operator reads the end.
if [ "${HOST_HARDENING_DEFERRED:-false}" = true ]; then
    print_warning "Host hardening was DEFERRED during this upgrade — run once, in a maintenance window (every container is recreated under the new Docker defaults): cd ${SCRIPT_DIR} && ./rzfz down && sudo bash ${SCRIPT_DIR}/scripts/harden-host.sh && ./rzfz start. If harden-host fails, the stack stays down: run ./rzfz start, fix the cause from the log, and repeat."
fi

if [ "${METADATA_BLOCK_DEFERRED:-false}" = true ]; then
    print_warning "Cloud metadata block was NOT applied during this upgrade (${RAZZFAZZ_METADATA_BLOCK_STATE:-failed}): containers can reach 169.254.169.254. Run once: sudo bash ${SCRIPT_DIR}/scripts/metadata-block.sh install, then check: ./rzfz status | grep -i 'metadata block'"
fi

# #3303: Google sign-in still admitting the old template default domain. Said
# where the operator reads the end, journaled; the value is never rewritten (an
# internal box may use it, and only the operator knows).
report_google_domain_old_default_after_upgrade() {
    razzfazz_google_domain_is_old_default "${1:-.env}" || return 0
    print_warning "$(razzfazz_google_domain_old_default_advice)"
    print_info "  Check afterwards: ./rzfz status | grep -i 'google sign-in'"
    journal_event "google-domain" "warn" "GOOGLE_OAUTH_DOMAIN still equals the pre-2026.11 template default on a public-channel box; not changed"
}
report_google_domain_old_default_after_upgrade ".env"

# Step 10b (#908 follow-up): re-assert OWUI's retrieval defaults now that the
# stack is up — the ONE table + push post-install uses, without the rest of a
# provisioning run in front of it. Skipped in --check.
if [ "$DRY_RUN" != "true" ]; then
    reconcile_owui_retrieval_defaults_after_upgrade ".env"
fi

# Step 10b2: REPORT-ONLY — Open WebUI's code interpreter, direct tool servers
# and user tools. A new install starts with all of them off; an existing box
# keeps the rows it has (Open WebUI seeds only missing keys), so the report
# says what this box runs with and where an admin changes it.
report_owui_code_tools_after_upgrade() {
    local env_file="${1:-.env}" profiles out rc=0 line db pg_user
    profiles=$(read_env_value "$env_file" COMPOSE_PROFILES 2>/dev/null) || profiles=""
    echo "$profiles" | tr ',' '\n' | grep -qx "chat" || return 0
    db=$(read_env_value "$env_file" OPENWEBUI_DB 2>/dev/null) || db=""
    pg_user=$(read_env_value "$env_file" POSTGRES_USER 2>/dev/null) || pg_user=""
    out=$(python3 "${SCRIPT_DIR}/scripts/owui_config_reconcile.py" code-tools \
        --db "${db:-openwebui_db}" --pg-user "${pg_user:-docker}" 2>&1) || rc=$?
    if [ "$rc" -ne 0 ]; then
        print_info "Open WebUI code and tools: state not readable now ($(printf '%s\n' "$out" | head -n1)); see the config portal, Chat page."
        return 0
    fi
    print_step "Open WebUI code and tools (kept as they are; a new install starts with all off):"
    while IFS= read -r line; do
        [ -n "$line" ] && print_info "  ${line}"
    done <<< "$out"
    print_info "  Change them in Open WebUI: Admin Settings > Code Execution, and Users > Permissions or Groups."
    return 0
}
if [ "$DRY_RUN" != "true" ]; then
    report_owui_code_tools_after_upgrade ".env" || true
fi

# Step 10c (D7, #2518): REPORT-ONLY — list any agent-manager instance whose
# type is deprecated (coding-tools, superseded by opencode/codex/user-
# defined; gsd-pi, folded into user-defined) or unknown (a type row no
# longer in the catalog at all). This upgrade never stops, deletes or
# migrates such an instance — retiring a type does that to nobody
# automatically; it only tells the operator it exists, the same information
# `/api/tree` now shows the owning user (badged "deprecated: migrate or
# delete") instead of silently hiding it. Best-effort: a DB not reachable
# yet (agent-manager hasn't finished its own startup migration) or the
# `agents` profile being off just skips this, never fails the upgrade.
report_deprecated_agent_instances() {
    local active_profiles
    active_profiles=$(grep '^COMPOSE_PROFILES=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
    if ! echo "${active_profiles:-}" | tr ',' '\n' | grep -qx "agents"; then
        return 0
    fi
    local pg_user pg_pass am_db sql ok=false rows
    pg_user=$(grep "^POSTGRES_USER=" .env 2>/dev/null | cut -d= -f2)
    pg_user="${pg_user:-docker}"
    pg_pass=$(read_env_value .env POSTGRES_PASSWORD 2>/dev/null || true)
    am_db=$(read_env_value .env AGENT_MANAGER_DB 2>/dev/null || true)
    am_db="${am_db:-agent_manager_db}"
    sql="SELECT i.user_slug, i.agent_type, i.id, i.state,
          CASE WHEN t.id IS NULL THEN 'unknown-type'
               WHEN t.deprecated IS TRUE THEN 'deprecated'
               WHEN t.enabled IS FALSE THEN 'disabled'
               ELSE 'other' END AS reason
         FROM agent_instances i
         LEFT JOIN agent_types t ON i.agent_type = t.id
         WHERE i.state != 'destroyed'
           AND (t.id IS NULL OR t.deprecated IS TRUE OR t.enabled IS FALSE)
         ORDER BY i.user_slug, i.agent_type;"
    # #2216-style guard: `if VAR=$(cmd); then` suspends set -e for the
    # assignment (unlike `VAR=$(cmd)` as a bare statement), so a psql failure
    # here — e.g. the `deprecated` column not migrated yet — degrades to the
    # manual-command fallback below instead of aborting the upgrade.
    if rows=$(PGPASSWORD="$pg_pass" docker exec -e PGPASSWORD postgres \
            psql -U "$pg_user" -h 127.0.0.1 -d "$am_db" -tA -F $'\t' \
            -c "$sql" 2>/dev/null); then
        ok=true
    fi
    if [ "$ok" = true ]; then
        if [ -n "$rows" ]; then
            echo ""
            print_warning "Deprecated/unknown-type agent instances found (D7, #2518) — report-only, nothing stopped or changed:"
            echo "$rows" | awk -F'\t' '{printf "    - user=%-24s type=%-16s state=%-12s (%s) id=%s\n", $1, $2, $4, $5, $3}'
            echo "  These instances keep working as-is (start/stop/delete/upgrade all still"
            echo "  function); their type just no longer accepts NEW launches. The portal tree"
            echo "  now shows them to their owner with a \"deprecated: migrate or delete\" badge"
            echo "  instead of hiding them. Reach out to the listed users, or remove an instance"
            echo "  yourself from its Settings page once they have migrated."
        else
            print_substep "No deprecated or unknown-type agent instances found (D7, #2518)."
        fi
    else
        print_info "Could not query agent-manager for deprecated/unknown-type instances (DB not reachable yet, D7 #2518)."
        print_info "  Manual check once the stack is up:"
        print_info "    docker exec postgres psql -U \"$pg_user\" -d \"$am_db\" -c \"$sql\""
    fi
}
if [ "$DRY_RUN" != "true" ]; then
    report_deprecated_agent_instances
fi

# Step 10c2 (#2897): REPORT-ONLY - a thin worker has no stack checkout, so this
# upgrade never reaches its agent. Name the remote worker agents that now run
# another release line than this master, with the command to run on each. The
# verdict is the manager's (agent_version_skew on /api/workers, the data
# `rzfz status` shows); same release line and unknown versions are silent.
# Best-effort: no manager, no answer or a --check run says nothing and never
# fails the upgrade.
report_stale_worker_agents() {
    [ "${DRY_RUN:-false}" != "true" ] || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x llm-manager >/dev/null || return 0
    _handover_load_lib || return 0
    local json lines n name worker master cmd
    # The master side of the message is THIS tree's release (VERSION), not the
    # manager's RAZZFAZZ_VERSION: that env is the stamp from when the container
    # was created, which an upgrade writes last, so it still named the old
    # release here (an rc6 master said "master 2026.11-rc4").
    master="$(razzfazz_release_version 2>/dev/null || true)"
    json="$(_llmm_admin_api GET /api/workers 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    lines="$(printf '%s' "$json" | MASTER_RELEASE="$master" python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(rows, list):
    sys.exit(0)
master = os.environ.get("MASTER_RELEASE") or ""
def base(v):   # "2026.11-rc6+gabc123" -> "2026.11-rc6"
    return str(v or "").split("+")[0].strip()
for r in rows:
    if not isinstance(r, dict):
        continue
    k = r.get("agent_version_skew")
    wv = base(r.get("stack_version"))
    if not k and master and wv and not r.get("external") and not r.get("agent_managed") \
            and wv[:1].isdigit() and "-g" not in wv and wv != base(master):
        # same release line, other build: the manager only flags another LINE,
        # and its own idea of the master release can be stale (#2951)
        k = {"worker": wv, "master": master, "update": "rzfz node-upgrade"}
    if k:
        print("\t".join((str(r.get("display_name") or r.get("name") or "?"), str(k.get("worker")),
                         master or str(k.get("master")), str(k.get("update")))))
' 2>/dev/null || true)"
    [ -n "$lines" ] || return 0
    n=$(printf '%s\n' "$lines" | grep -c . || true)
    echo ""
    print_warning "${n} remote worker agent(s) run another release than this master - update each one ON that worker:"
    while IFS=$'\t' read -r name worker master cmd; do
        [ -n "$name" ] || continue
        print_info "    ${name}  agent ${worker}, master ${master}: ${cmd}"
    done <<< "$lines"
    return 0
}
if [ "$DRY_RUN" != "true" ]; then
    report_stale_worker_agents
fi

# Step 10c3 (#2951): REPORT-ONLY - the llm-manager.<domain> name is a deprecated
# alias of llm.<domain>. A thin worker enrolled before the cutover has the old
# name in its .env.node and keeps working through the alias, but should be
# re-pointed. The master does not record which name a worker uses, so this
# lists the remote candidates and prints the one command that fixes each.
# #2978: "remote" is decided by what the master records, not by the shape of
# the address: workers report container names (llm-worker-agent), which have no
# dot, so the old dot test skipped every worker and the report never fired. A
# remote worker is every enrolled worker that is not this box's own (its name
# is LLM_WORKER_NAME, empty = "master"), not an external endpoint and not a Mac
# agent bundle (that one is moved by "Upgrade agent", not node-upgrade).
# Best-effort and silent when there is nothing to say; never fails the upgrade.
report_workers_on_the_old_manager_name() {
    [ "${DRY_RUN:-false}" != "true" ] || return 0
    docker ps --format '{{.Names}}' 2>/dev/null | grep -x llm-manager >/dev/null || return 0
    local old new main json names n local_worker
    main="$(read_env_value ".env" MAIN_DOMAIN 2>/dev/null || true)"
    local_worker="$(read_env_value ".env" LLM_WORKER_NAME 2>/dev/null || true)"
    old="$(read_env_value ".env" LLM_MANAGER_DOMAIN 2>/dev/null || true)"
    new="$(read_env_value ".env" LLM_DOMAIN 2>/dev/null || true)"
    old="${old//\$\{MAIN_DOMAIN\}/$main}"; new="${new//\$\{MAIN_DOMAIN\}/$main}"
    [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
    _handover_load_lib || return 0
    json="$(_llmm_admin_api GET /api/workers 2>/dev/null || true)"
    [ -n "$json" ] || return 0
    names="$(printf '%s' "$json" | LOCAL_WORKER="${local_worker:-master}" python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(rows, dict):
    rows = rows.get("workers") or rows.get("items") or []
if not isinstance(rows, list):
    sys.exit(0)
local = os.environ.get("LOCAL_WORKER") or "master"
for r in rows:
    if not isinstance(r, dict) or r.get("external") or r.get("agent_managed"):
        continue
    if str(r.get("name") or "") == local:   # the worker of this box: it talks to the manager directly
        continue
    print(r.get("display_name") or r.get("name") or "?")
' 2>/dev/null || true)"
    [ -n "$names" ] || return 0
    n=$(printf '%s\n' "$names" | grep -c . || true)
    echo ""
    print_warning "${n} remote worker(s) may still use the deprecated name ${old} for this master:"
    print_info "    $(printf '%s' "$names" | paste -sd, - | sed 's/,/, /g')"
    print_info "  The master does not record which name a worker uses. On each worker, check:  grep LLM_MANAGER_URL /opt/rzfz-worker/.env.node"
    print_info "  If it names ${old}, make ${new} resolve there (same address as ${old}: /etc/hosts or DNS), then run ON THE WORKER:"
    print_info "    rzfz node-upgrade --manager-url https://${new}"
    print_info "  (it checks https://${new}/healthz first, backs up .env.node, rewrites LLM_MANAGER_URL and RZFZ_MASTER_URL in place and recreates only the agent; engines keep running)"
    print_info "  The old name keeps answering workers until they are re-pointed; it will be removed in a later release."
    return 0
}
if [ "$DRY_RUN" != "true" ]; then
    report_workers_on_the_old_manager_name
fi

# Step 10d (#2697): the earlier run logs lose the admin password (function
# above the dry-run exit, so --check reports what this would do).
scrub_admin_password_from_run_logs ".env"

# MIGRATIONS_ONLY_TAIL-BEGIN
# #2524: --migrations-only ends here — the post-install self-heal, the
# acceptance probes and the upgrade banner are a full upgrade's.
if [ "$MIGRATIONS_ONLY" = true ]; then
    echo ""
    _mo_issues=0
    if [ "${AUTHENTIK_INIT_ISSUES:-0}" -gt 0 ]; then
        print_warning "The Authentik blueprints are not confirmed on this box (${AUTHENTIK_INIT_ISSUES} issue(s), see above)."
        _mo_issues=1
    fi
    if [ "${COGNEE_STORAGE_ISSUES:-0}" -gt 0 ]; then
        print_warning "cognee cannot write its storage roots (see above)."
        _mo_issues=1
    fi
    if [ "${AUTHENTIK_CRASHLOOP:-0}" -gt 0 ]; then
        print_warning "authentik-server is in a restart loop (see above)."
        _mo_issues=1
    fi
    if [ "$_mo_issues" -ne 0 ]; then
        print_error "Migrations v${INSTALLED_VERSION} -> v${TARGET_VERSION} ran, with the issues above. Fix them and re-run this command (it is safe to re-run)."
        echo "Migrations only: completed with issues $(date -Iseconds)" >> "$UPGRADE_LOG"
        exit 1
    fi
    print_success "Migrations v${INSTALLED_VERSION} -> v${TARGET_VERSION} complete. Re-running this command is safe."
    echo "Migrations only: completed $(date -Iseconds)" >> "$UPGRADE_LOG"
    exit 0
fi
# MIGRATIONS_ONLY_TAIL-END

# A box whose Authentik is in a restart loop has no single sign-on: not "Upgrade Complete".
if [ "${AUTHENTIK_CRASHLOOP:-0}" -gt 0 ]; then
    echo ""
    print_error "Upgrade v${TARGET_VERSION} finished, but authentik-server is in a restart loop (reason above). Fix it and re-run this command; to go back: rzfz upgrade --rollback"
    echo "Completed (Authentik restart loop): $(date -Iseconds)" >> "$UPGRADE_LOG"
    exit 1
fi

# Done
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                                  ║${NC}"
echo -e "${GREEN}║              Upgrade Complete: v${TARGET_VERSION}$(printf '%*s' $((27 - ${#TARGET_VERSION})) '')║${NC}"
echo -e "${GREEN}║                                                                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ga.1 (upgrade day-1-green): auto-apply the idempotent post-install provisioning
# self-heal so an UPGRADED box is day-1-green, not just a fresh install. Before
# this, `rzfz upgrade` only PRINTED a reminder to run `post-install --refresh`; a
# box that skipped it kept whatever provisioning state it had — OWUI's persisted
# placeholder GPUStack key (0 models), an un-recreated model-sync (0 models),
# un-prebuilt custom-build images (later module-enable 403s on egress-restricted
# boxes), coding agents wired to the placeholder key. `--refresh` is the SAFE mode:
# it runs ONLY idempotent, non-destructive fixes and does NOT re-deploy models or
# reset Open WebUI / Dify / Gitea defaults (that is `--preset --force`, still
# operator-only below — its "already has models" short-circuit never applies to
# --refresh, so the wiring fixes run regardless of GPUStack model state). It runs
# AFTER verify (stack up + version written), is best-effort (a non-zero here warns,
# never fails the upgrade), and is skipped only in --check (no real upgrade).
# #2222: the refresh's offline PLUGIN staging (the Dify plugins and their
# dependency cache) reads the package at APPLIANCE_OFFLINE_PKG, which this
# script never set: an air-gapped box upgraded with --package looked for the
# APPLIANCE stick, found nothing and returned 0 with an info line, while the
# upgrade package it had just loaded images from sat next to it carrying the
# plugins. Hand the refresh that file (the env knob journey C already uses to
# point elsewhere; an operator's own value is kept). Model GGUFs are NOT this:
# the upgrade stages them itself above (#184 WS7a) — into a gpustack container
# only, which a manager box does not run; that is #2227.
if [ -n "${PACKAGE_FILE:-}" ] && [ -f "$PACKAGE_FILE" ] && [ -z "${APPLIANCE_OFFLINE_PKG:-}" ]; then
    export APPLIANCE_OFFLINE_PKG="$PACKAGE_FILE"
    print_substep "Offline staging reads the upgrade package: ${APPLIANCE_OFFLINE_PKG} (#2222)"
fi
model_bundle_after_crossing
upgrade_post_install_refresh
cpu_gpustack_handover_finish
# #2977: after the refresh re-keyed the agent instances: nothing may hold the old Valkey password.
valkey_rotation_final_gate || UPGRADE_VALKEY_ROTATION_FAILED=true
upgrade_relaunch_engines

# #1941: an existing box gets the PATH entry on upgrade too — otherwise the
# documentation only becomes true for boxes installed after this release, and
# the fleet stays split between "the docs work" and "the docs do not". Never
# fatal, and a foreign entry is refused rather than overwritten.
razzfazz_link_cli_onto_path "$SCRIPT_DIR" || true

# Post-upgrade guidance. The idempotent --refresh self-heal above already ran; the
# notes below only surface the OTHER (read-only / operator-only) follow-up modes.
echo -e "${YELLOW}Post-upgrade notes${NC}"
echo "  The idempotent 'rzfz post-install --refresh' self-heal ran automatically:"
echo "    • Refreshed /etc/hosts entries for any new <module>.${MAIN_DOMAIN:-<domain>} FQDNs"
echo "    • Auto-provisioned GPUSTACK_API_KEY if it was still empty/placeholder"
echo "    • Re-wired Open WebUI / model-sync / Dify / coding agents to the live key"
echo "    • Pre-built the custom-build module images (offline-safe module enable)"
echo "  Re-run it any time — it is safe + idempotent."
echo ""
case "$UPGRADE_ENGINE_RELAUNCH" in
    done)    echo "  LLM engines: on this release's runner and engine settings." ;;
    failed)  echo -e "  ${YELLOW}LLM engines: not all switched (see above) — they keep serving; re-run: rzfz relaunch-engines${NC}" ;;
    skipped) echo "  LLM engines: left as they were (--no-engine-relaunch) — when it suits: rzfz relaunch-engines" ;;
esac
echo ""
echo "  Other safe modes:"
echo "    rzfz post-install --verify    Read-only verification suite (no changes)"
echo ""
echo -e "  ${YELLOW}NOT recommended on an existing stack:${NC}"
echo "    --preset standard|developer   Destructive — will only run with --force."
echo "                                  Re-deploys default models, resets Open WebUI /"
echo "                                  Dify / Gitea defaults, re-installs Dify plugins."
echo ""

echo "Upgrade log saved to: ${UPGRADE_LOG}"
echo ""
# #3187: per-user agent instances whose image changed in this upgrade are not
# silently left on the old one — name how many are behind and the command that
# recreates them (read-only probe; never fails the upgrade, skipped in --check
# and --skip-verify like the status print below).
_agents_behind=0
if [ -x "${SCRIPT_DIR}/agents.sh" ] && [ "${DRY_RUN:-false}" != "true" ] && [ "${SKIP_VERIFY:-false}" != "true" ]; then
    _agents_behind="$("${SCRIPT_DIR}/agents.sh" list --behind-count 2>/dev/null || echo 0)"
    case "$_agents_behind" in ''|*[!0-9]*) _agents_behind=0 ;; esac
fi
echo "Useful commands:"
echo "  • View status:    docker compose ps"
echo "  • Assessment:     rzfz upgrade --status   (or rzfz status)"
echo "  • View logs:      docker compose logs -f"
echo "  • Rollback:       rzfz upgrade --rollback"
if [ "$_agents_behind" -gt 0 ]; then
    echo "  • Update agents:  rzfz agents update    ($_agents_behind agent instance(s) still run an old image after this upgrade)"
fi
echo ""

# Auto-print the assessment so the operator sees the post-upgrade posture
# without having to ask for it. Skipped in --check (already not invoking
# real upgrade) and on --skip-verify (operator opted out of all checks).
if [ -x "${SCRIPT_DIR}/rzfz" ] && [ "$SKIP_VERIFY" != true ]; then
    echo ""
    "${SCRIPT_DIR}/rzfz" status --short 2>/dev/null || true
    echo ""
    echo "  (run 'rzfz status' for the full per-item report)"
    echo ""
fi

# M032-S06: opt-in post-upgrade acceptance probes. Runs LAST so every
# upgrade step (code update, image rebuild/pull, .env migration, post-
# upgrade verify, status print) has finished. Skipped in --check (no real
# upgrade happened) and skipped if --skip-verify (operator opted out of
# all post-upgrade checks). Probe failure surfaces as exit 4.
if [ "$WITH_ACCEPTANCE" = true ]; then
    if [ "$DRY_RUN" = "true" ]; then
        print_info "--with-acceptance is a no-op in --check / --dry-run mode."
    elif [ "$SKIP_VERIFY" = true ]; then
        print_warning "--skip-verify was set; skipping --with-acceptance probes too."
    else
        if run_acceptance_probes; then
            :
        else
            ACCEPTANCE_RC=$?
            echo ""
            print_error "Upgrade body completed, but acceptance probes failed (exit code $ACCEPTANCE_RC)."
            print_info "Review tests/results/latest/acceptance-report.md and decide whether to roll back."
            echo "Completed (with probe failures): $(date -Iseconds)" >> "$UPGRADE_LOG"
            exit "$ACCEPTANCE_RC"
        fi
    fi
fi

# #2873: the final health gate failed - the stack is not up, so the run is not
# a success. Exit non-zero AFTER the bookkeeping and the status print above.
if [ "${UPGRADE_HEALTH_GATE_FAILED:-false}" = true ]; then
    echo ""
    print_error "Upgrade INCOMPLETE: the stack is not healthy (see the final health gate above)."
    print_info "  The version was not advanced. Fix the cause, run: docker compose up -d   then re-run: rzfz upgrade"
    print_info "  To rollback: rzfz upgrade --rollback"
    echo "Completed (health gate FAILED): $(date -Iseconds)" >> "$UPGRADE_LOG"
    exit 7
fi

# #2977: the Valkey password was not rotated cleanly, or a process still holds
# the old one (named above): that process fails its next connection. Not a quiet
# success.
if [ "${UPGRADE_VALKEY_ROTATION_FAILED:-false}" = true ]; then
    echo ""
    print_error "Upgrade INCOMPLETE: the Valkey password rotation is not clean (see above)."
    print_info "  The services listed above keep the OLD password until they are recreated; the rest of the upgrade is applied."
    print_info "  Fix the cause, then re-run: rzfz upgrade   (the rotation is not repeated once it has happened)"
    echo "Completed (Valkey rotation INCOMPLETE): $(date -Iseconds)" >> "$UPGRADE_LOG"
    exit 7
fi

echo "Completed: $(date -Iseconds)" >> "$UPGRADE_LOG"
