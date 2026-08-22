#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# razzfazz-test.sh — M032 test wrapper
#
# Wraps pytest with:
#   - venv self-bootstrap on first run
#   - tier marker mapping (--unit / --api / --ui / --acceptance / --all-markers)
#   - module-name → tests/{tier}/<mod>/ subtree resolution
#   - results dir rotation (default keep 5)
#   - coverage gate ON by default; --no-coverage to disable
#   - --bootstrap-only to pre-warm the venv without running tests
#   - --include-disabled flag stub (S02 wires the actual cycling logic)
#
# See .gsd/milestones/M032/plans/S01-plan.md for the full spec.

set -eu

WRAPPER_VERSION="0.3.0-M032-S06"

# --- repo root resolution ----------------------------------------------------
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

TESTS_DIR="$SCRIPT_DIR/tests"
VENV_DIR="$TESTS_DIR/.venv"
REQUIREMENTS_FILE="$TESTS_DIR/requirements.txt"
RESULTS_ROOT="$TESTS_DIR/results"

# --- usage -------------------------------------------------------------------
usage() {
    cat <<'EOF'
Usage: rzfz test <module|all> [OPTIONS]

ARGUMENTS:
  module             Module name (e.g. razzfazz-config, agent-manager); must
                     match a subtree under tests/{unit,api,ui,acceptance}/.
  all                Run every collected test under tests/.

OPTIONS:
  --acceptance       Run acceptance probes only      (pytest -m acceptance)
  --unit             Run unit tests only             (pytest -m unit)
  --api              Run API tests only              (pytest -m api)
  --ui               Run UI tests only               (pytest -m ui)
  --scripts          Run operator-script tests only  (pytest -m scripts)
  --all-markers      Run every marker (default)
  --no-coverage      Disable the coverage gate (coverage is on by default)
  --html             Also produce tests/results/<run-id>/report.html
  --parallel <N>     Run with N pytest-xdist workers
  --keep-results <N> Override default retention (default: 5 most recent)
  --include-disabled Acceptance probes for currently-disabled modules
                     (M032-S02): for each disabled profile, temporarily
                     `compose up -d`, run its probes, `compose down
                     --remove-orphans` (volumes preserved). One profile
                     at a time. Use for kassasturz / customer handover.
                     Adds 5-10 min to runtime (sequential per profile).
                     IMPLIED by --customer-report unless overridden by
                     --no-include-disabled.
  --no-include-disabled
                     Skip the disabled-profile cycling. Useful with
                     --customer-report when the operator wants a fast
                     scoped run and accepts that disabled-module probes
                     will report SKIP.
  --bootstrap-only   Bootstrap the venv and exit 0; do not run tests
  --ci-mode          (M032-S06) Machine-consumable mode for the
                     init/upgrade --with-acceptance hook + future CI:
                       * always emits JUnit XML at
                         tests/results/<run-id>/junit.xml
                       * always emits a structured summary.json next to it
                         (passed/failed/skipped/errors counts + run_id +
                          path-list of failing-test ids)
                       * also emits acceptance-report.md (Markdown digest
                         of summary.json) for the operator
                       * disables coverage (faster, doesn't matter for
                         post-deploy probes)
                       * symlinks tests/results/latest -> the new run dir
                       * REQUIRES at least 1 acceptance probe collected
                         when combined with --acceptance; if 0 collected,
                         exits 5 (no-probes-found) instead of 0 — guards
                         against an early-adoption box where the probe
                         suite isn't deployed yet (R-S06-3 in S06 plan).
  --customer-report  (2026-05-15) Run the full suite and generate a
                     customer-facing test report under
                     tests/results/<run-id>/test-report.{md,pdf}.
                     Implies --ci-mode but RE-ENABLES coverage so the
                     report's coverage section is populated. The
                     per-module coverage gate is bypassed
                     (RAZZFAZZ_SKIP_COVERAGE_GATE=1) so a documented
                     carry-forward doesn't poison the customer-visible
                     exit code; the report still surfaces under-
                     threshold modules clearly.
                     Also IMPLIES --include-disabled so probes for
                     currently-disabled modules report real status
                     instead of SKIP — adds 5-10 min for sequential
                     per-profile cycling. Pass --no-include-disabled
                     to opt out for a fast scoped run.
  --version          Print wrapper + pytest + Playwright versions
  -h, --help         Show this message

EXIT CODES:
  0  all tests passed (or none collected)
  1  one or more tests failed
  2  invocation error (unknown module, bad flag, bootstrap failed)
  3  coverage gate failed (tests passed but threshold not met)
  5  --ci-mode + --acceptance but zero probes collected (M032-S06)

ENV OVERRIDES:
  RAZZFAZZ_SKIP_COVERAGE_GATE=1   bypass the per-module coverage gate
                                  (coverage still runs; LOGGED as warning).
                                  Use only for non-test-relevant hotfixes.

See docs/dev/testing.md for fixtures, markers, and how to add a module.
EOF
}

# --- arg parsing -------------------------------------------------------------
MODULE=""
MARKER=""           # set when --unit/--api/--ui/--acceptance given
NO_COVERAGE=0
HTML_REPORT=0
PARALLEL=""
KEEP_RESULTS=5
INCLUDE_DISABLED=0
# Tracks whether the operator explicitly passed --include-disabled or
# --no-include-disabled on the command line. Used to let --customer-report
# default-set INCLUDE_DISABLED=1 only when the operator hasn't overridden.
# Values: "" (not set) | "yes" | "no"
INCLUDE_DISABLED_EXPLICIT=""
BOOTSTRAP_ONLY=0
SHOW_VERSION=0
CI_MODE=0
CUSTOMER_REPORT=0

if [ "$#" -eq 0 ]; then
    usage >&2
    exit 2
fi

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --version)
            SHOW_VERSION=1
            shift
            ;;
        --acceptance)
            # Tier flags ACCUMULATE into a pytest `-m "a or b or ..."` expression.
            # Passing --acceptance --unit --api selects all three tiers; before
            # this fix each flag overwrote the last, so `--acceptance --unit --api`
            # silently ran `-m api` only (unit/acceptance never executed — the
            # cause of the perpetually-failing per-module coverage gate).
            MARKER="${MARKER:+$MARKER or }acceptance"
            shift
            ;;
        --unit)
            MARKER="${MARKER:+$MARKER or }unit"
            shift
            ;;
        --api)
            MARKER="${MARKER:+$MARKER or }api"
            shift
            ;;
        --ui)
            MARKER="${MARKER:+$MARKER or }ui"
            shift
            ;;
        --scripts)
            MARKER="${MARKER:+$MARKER or }scripts"
            shift
            ;;
        --all-markers)
            MARKER=""
            shift
            ;;
        --no-coverage)
            NO_COVERAGE=1
            shift
            ;;
        --html)
            HTML_REPORT=1
            shift
            ;;
        --parallel)
            shift
            if [ "$#" -eq 0 ]; then
                echo "ERROR: --parallel requires a worker count" >&2
                exit 2
            fi
            PARALLEL="$1"
            shift
            ;;
        --keep-results)
            shift
            if [ "$#" -eq 0 ]; then
                echo "ERROR: --keep-results requires a count" >&2
                exit 2
            fi
            KEEP_RESULTS="$1"
            shift
            ;;
        --include-disabled)
            # M032-S02: passed through to pytest as `--include-disabled`
            # (registered in tests/acceptance/conftest.py). The autouse
            # `profile_temporarily_enabled` fixture cycles each disabled
            # profile up before its probes and back down after.
            INCLUDE_DISABLED=1
            INCLUDE_DISABLED_EXPLICIT="yes"
            shift
            ;;
        --no-include-disabled)
            # 2026-05-15 (TRF-DEC-03): explicit opt-out for the
            # --customer-report → --include-disabled implication added in Q5.
            # Useful for fast scoped runs when the operator accepts SKIP
            # for disabled-module probes.
            INCLUDE_DISABLED=0
            INCLUDE_DISABLED_EXPLICIT="no"
            shift
            ;;
        --bootstrap-only)
            BOOTSTRAP_ONLY=1
            shift
            ;;
        --ci-mode)
            # M032-S06: machine-consumable mode for the init/upgrade
            # --with-acceptance hook. Forces JUnit XML + summary.json,
            # disables coverage, requires ≥1 acceptance probe collected
            # when paired with --acceptance, and symlinks
            # tests/results/latest -> the new run dir for stable paths.
            CI_MODE=1
            NO_COVERAGE=1
            shift
            ;;
        --customer-report)
            # 2026-05-15: customer-facing report mode. Implies --ci-mode
            # for the JUnit + summary artifacts, but RE-ENABLES coverage
            # so the customer report's section 8 is populated. The
            # coverage gate is bypassed (RAZZFAZZ_SKIP_COVERAGE_GATE=1)
            # so a documented carry-forward doesn't poison the exit
            # code. After the run completes, the wrapper invokes
            # scripts/generate-test-report.py to produce the Markdown
            # + PDF artifacts under tests/results/<run-id>/.
            CI_MODE=1
            CUSTOMER_REPORT=1
            # NOTE: --ci-mode flips NO_COVERAGE=1; we revert that below
            # AFTER both flags have been parsed (the order of --ci-mode
            # vs --customer-report on the command line shouldn't matter).
            shift
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "ERROR: unknown flag: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ -n "$MODULE" ]; then
                echo "ERROR: multiple module names given: '$MODULE' and '$1'" >&2
                exit 2
            fi
            MODULE="$1"
            shift
            ;;
    esac
done

# --customer-report re-enables coverage (overrides --ci-mode's auto-disable)
# and bypasses the per-module coverage gate so the customer-visible exit code
# isn't tripped by a documented carry-forward — the report still surfaces it.
if [ "$CUSTOMER_REPORT" -eq 1 ]; then
    NO_COVERAGE=0
    export RAZZFAZZ_SKIP_COVERAGE_GATE=1
    # Q5/TRF-DEC-03: --customer-report implies --include-disabled so probes
    # for currently-disabled modules report real status instead of SKIP.
    # Operator can opt out with --no-include-disabled.
    if [ -z "$INCLUDE_DISABLED_EXPLICIT" ]; then
        INCLUDE_DISABLED=1
        echo "(--customer-report implies --include-disabled — adds 5-10 min" >&2
        echo " for sequential per-profile cycling. Pass --no-include-disabled" >&2
        echo " to opt out for a fast scoped run.)" >&2
    fi
    # Quick-wins tier (2026-05-16): a customer-facing run should exercise
    # everything we can on the box, including:
    #  - Heavy profiles (onyx, matrix, observability, openhands) — these cost
    #    5+ min each to cycle but are EXPECTED to be part of full coverage.
    #  - Live-Authentik tests that mutate the dev stack's Authentik (group
    #    enforcement, MFA policy, rate-limit storm, group-lint live). These
    #    were default-off because they mutate state on dev; on a test box
    #    running --customer-report they're appropriate.
    # Operator can opt out via the per-var explicit override:
    #   RAZZFAZZ_ALLOW_HEAVY_CYCLES=0 rzfz test --customer-report all
    : "${RAZZFAZZ_ALLOW_HEAVY_CYCLES:=1}"
    : "${RAZZFAZZ_TEST_GROUP_ENFORCEMENT:=1}"
    : "${RAZZFAZZ_TEST_MFA_POLICY:=1}"
    : "${RAZZFAZZ_TEST_RATE_LIMIT:=1}"
    : "${RAZZFAZZ_TEST_GROUP_LINT_LIVE:=1}"
    export RAZZFAZZ_ALLOW_HEAVY_CYCLES RAZZFAZZ_TEST_GROUP_ENFORCEMENT \
           RAZZFAZZ_TEST_MFA_POLICY RAZZFAZZ_TEST_RATE_LIMIT \
           RAZZFAZZ_TEST_GROUP_LINT_LIVE
fi

# --- venv bootstrap ----------------------------------------------------------
bootstrap_venv() {
    # A previous half-failed bootstrap can leave $VENV_DIR present but
    # without pip (e.g. when python3-venv was missing the first time).
    # Treat "no pip binary" as "venv broken — reset" rather than silently
    # short-circuiting and confusing the next pytest invocation.
    if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/pip" ]; then
        echo "Stale/broken venv at $VENV_DIR — resetting." >&2
        rm -rf "$VENV_DIR"
    fi
    if [ ! -d "$VENV_DIR" ]; then
        echo "Bootstrapping test venv at $VENV_DIR (one-time)..." >&2
        python3 -m venv "$VENV_DIR" || {
            echo "ERROR: failed to create venv" >&2
            rm -rf "$VENV_DIR"
            return 2
        }
        if [ ! -f "$REQUIREMENTS_FILE" ]; then
            echo "ERROR: $REQUIREMENTS_FILE missing" >&2
            return 2
        fi
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip || {
            echo "ERROR: pip upgrade failed" >&2
            return 2
        }
        "$VENV_DIR/bin/pip" install --quiet -r "$REQUIREMENTS_FILE" || {
            echo "ERROR: pip install -r $REQUIREMENTS_FILE failed" >&2
            return 2
        }
        echo "Test venv bootstrapped." >&2
    fi
}

bootstrap_playwright() {
    # Operator decision (S01-DEC-02 → option 1, 2026-05-15): auto-install
    # chromium during --bootstrap-only. Heavy (~250 MB), takes minutes on
    # first run; subsequent runs are no-ops (playwright detects existing
    # install). Failure is non-fatal — UI tests skip cleanly without it.
    local pw="${VENV_DIR}/bin/playwright"
    [ -x "$pw" ] || return 0  # playwright not in venv yet (race)
    # Cheap "already-installed?" check via dry-run output
    if "$pw" install --dry-run chromium 2>&1 | grep -qE "is already installed|browsers? to install: 0"; then
        return 0
    fi
    echo "Installing Playwright chromium (~250 MB, one-time)..." >&2
    "$pw" install --with-deps chromium >&2 || {
        echo "Warning: chromium install failed; UI tests will skip cleanly." >&2
        return 0
    }
    echo "Playwright chromium installed." >&2
}

# --- early exits -------------------------------------------------------------
if [ "$SHOW_VERSION" -eq 1 ]; then
    echo "razzfazz-test.sh $WRAPPER_VERSION"
    if [ -d "$VENV_DIR" ]; then
        "$VENV_DIR/bin/python" -c "import pytest; print('pytest', pytest.__version__)" 2>/dev/null || echo "pytest: not installed"
        "$VENV_DIR/bin/python" -c "import playwright; print('playwright', playwright.__version__)" 2>/dev/null || echo "playwright: not installed"
    else
        echo "(test venv not yet bootstrapped — run with --bootstrap-only)"
    fi
    exit 0
fi

if [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
    bootstrap_venv || exit 2
    bootstrap_playwright || true
    echo "Bootstrap complete." >&2
    exit 0
fi

# Module argument is required for non-bootstrap invocations
if [ -z "$MODULE" ]; then
    echo "ERROR: module name (or 'all') is required" >&2
    usage >&2
    exit 2
fi

# Bootstrap before module resolution so the venv is always present for pytest
bootstrap_venv || exit 2

# --- module resolution -------------------------------------------------------
PYTEST_PATHS=()
TIERS="unit api ui acceptance scripts"

if [ "$MODULE" = "all" ]; then
    PYTEST_PATHS=("$TESTS_DIR/unit" "$TESTS_DIR/api" "$TESTS_DIR/ui" "$TESTS_DIR/acceptance" "$TESTS_DIR/scripts")
elif [ "$MODULE" = "scripts" ]; then
    # Top-level "scripts" module argument: run the entire scripts/ tree.
    PYTEST_PATHS=("$TESTS_DIR/scripts")
else
    found=0
    for tier in $TIERS; do
        if [ -d "$TESTS_DIR/$tier/$MODULE" ]; then
            PYTEST_PATHS+=("$TESTS_DIR/$tier/$MODULE")
            found=1
        fi
    done
    if [ "$found" -eq 0 ]; then
        echo "ERROR: module '$MODULE' has no tests yet (no subtree under tests/{unit,api,ui,acceptance,scripts}/$MODULE/)" >&2
        echo "       create tests/<tier>/$MODULE/ to start." >&2
        exit 2
    fi
fi

# When user passed --scripts but no module arg, default to the scripts tree.
# (The wrapper currently REQUIRES a module arg; bare `--scripts` would error.
# We support both `--scripts scripts` and `--scripts all` — the latter is
# also explicit. If the user did `--scripts <module>`, we keep that scoping.)

# --- results dir + rotation --------------------------------------------------
mkdir -p "$RESULTS_ROOT"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$RESULTS_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"

# Sweep any orphan ephemeral-postgres containers from prior SIGKILL'd test
# sessions (the conftest.py session fixture's `finally` block can't run on
# SIGKILL). Cumulative leaks were the root cause of the 2026-05-16 OOM
# cascade. The conftest does the same sweep at fixture init; this is the
# wrapper-layer belt-and-braces.
if command -v docker >/dev/null 2>&1; then
    ORPHANS=$(docker ps -a --filter 'name=^pytest-eph-pg-' --format '{{.Names}}' 2>/dev/null || true)
    if [ -n "$ORPHANS" ]; then
        echo "razzfazz-test: sweeping $(echo "$ORPHANS" | wc -l) orphan pytest-eph-pg-* container(s) from prior session(s)" >&2
        echo "$ORPHANS" | xargs -r docker rm -f >/dev/null 2>&1 || true
    fi
fi

# Rotate: keep only the N most recent run dirs (counting the one we just
# created). RUN_IDs are lexically sortable (UTC timestamp + PID), so we
# sort by basename rather than relying on `ls -t` mtime resolution — same-
# second runs disambiguate cleanly by PID. We rotate AFTER creating the
# new dir so the count reflects reality and N=3 means "3 dirs remain
# after this run".
rotate_results() {
    if [ "$KEEP_RESULTS" -le 0 ]; then
        return 0
    fi
    # Collect dirs sorted newest-first by name (PID-tiebroken).
    local -a dirs=()
    while IFS= read -r line; do
        dirs+=("$line")
    done < <(find "$RESULTS_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r)
    local i=0
    for d in "${dirs[@]}"; do
        i=$((i + 1))
        if [ "$i" -gt "$KEEP_RESULTS" ]; then
            rm -rf "$d"
        fi
    done
}
rotate_results

# --- pytest invocation -------------------------------------------------------
PYTEST="$VENV_DIR/bin/pytest"
if [ ! -x "$PYTEST" ]; then
    echo "ERROR: pytest not found at $PYTEST after bootstrap" >&2
    exit 2
fi

PYTEST_ARGS=()

# coverage on by default; --no-coverage skips
#
# B-13 (2026-05-15): in addition to `--cov=tests`, we collect coverage
# against the source roots whose corresponding test module is part of
# THIS run, per `[tool.razzfazz_test.module_source_map]` in
# `tests/pyproject.toml`. Scoping by module-in-run avoids the trap of
# pulling in Tier-A source files at 0% during a narrow run that doesn't
# actually exercise them (e.g. `razzfazz-test razzfazz-test` shouldn't
# fire a backup-manager threshold).
#
# The post-pytest gate (further down) re-uses the same scoping decision
# so narrow runs don't fire thresholds for out-of-scope modules.
COV_SOURCE_ROOTS_IN_SCOPE=()
if [ "$NO_COVERAGE" -eq 0 ]; then
    PYTEST_ARGS+=("--cov=tests" "--cov-report=term" "--cov-report=html:$RUN_DIR/htmlcov")
    # 2026-05-15: also emit coverage.xml so the customer-report generator
    # has a structured input. Cheap (single XML write at end of run); no
    # impact on the in-progress threshold gate which reads the .coverage
    # data file directly.
    PYTEST_ARGS+=("--cov-report=xml:$RUN_DIR/coverage.xml")

    if [ -f "$TESTS_DIR/pyproject.toml" ]; then
        # Build the in-scope source-root list. Inputs to the helper:
        #   $1 — pyproject.toml path
        #   $2 — module-arg the operator passed (`all`, `backup-manager`, …)
        #   $3 — space-separated list of test subtrees pytest will run
        # shellcheck disable=SC2207
        COV_SOURCE_ROOTS_IN_SCOPE=($(
            "$VENV_DIR/bin/python" - "$TESTS_DIR/pyproject.toml" "$MODULE" "${PYTEST_PATHS[*]}" <<'PYEOF' 2>/dev/null || true
import sys
from pathlib import PurePosixPath
try:
    import tomllib
except ImportError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as fh:
    data = tomllib.load(fh)
module_arg = sys.argv[2]
pytest_paths = sys.argv[3].split()
mod_map = (
    data.get("tool", {})
    .get("razzfazz_test", {})
    .get("module_source_map", {})
)
# A test module is "in scope" if any pytest path ends in /<module>
# (or contains /<module>/ — handles tier subtree like tests/unit/backup-manager).
in_scope_modules = set()
if module_arg == "all":
    in_scope_modules.update(mod_map.keys())
else:
    for mod in mod_map:
        for p in pytest_paths:
            parts = PurePosixPath(p).parts
            if mod in parts:
                in_scope_modules.add(mod)
                break
roots = []
for mod in sorted(in_scope_modules):
    for r in mod_map[mod]:
        if r not in roots:
            roots.append(r)
print("\n".join(roots))
PYEOF
        ))
        for root in "${COV_SOURCE_ROOTS_IN_SCOPE[@]}"; do
            [ -n "$root" ] && PYTEST_ARGS+=("--cov=$root")
        done
    fi
fi

# tier marker filter
if [ -n "$MARKER" ]; then
    PYTEST_ARGS+=("-m" "$MARKER")
fi

# parallel
if [ -n "$PARALLEL" ]; then
    PYTEST_ARGS+=("-n" "$PARALLEL")
fi

# JUnit XML always
PYTEST_ARGS+=("--junit-xml=$RUN_DIR/junit.xml")

# HTML report optional
if [ "$HTML_REPORT" -eq 1 ]; then
    PYTEST_ARGS+=("--html=$RUN_DIR/report.html" "--self-contained-html")
fi

# --include-disabled: pass through to pytest. The acceptance conftest
# registers the option + autouse cycling fixture (M032-S02).
if [ "$INCLUDE_DISABLED" -eq 1 ]; then
    PYTEST_ARGS+=("--include-disabled")
fi

# Pass through any extra args after `--` directly to pytest.
# (Plain S01 wrapper consumed `--` but discarded the trailing args; S02
# fix preserves them so callers can do e.g. `… -- --collect-only -q`.)
EXTRA_PYTEST_ARGS=("$@")

# Run
set +e
"$PYTEST" "${PYTEST_ARGS[@]}" "${PYTEST_PATHS[@]}" "${EXTRA_PYTEST_ARGS[@]}"
PYTEST_RC=$?
set -e

# pytest exit codes:
#   0 = all passed
#   1 = tests failed
#   2 = test execution interrupted
#   3 = internal error
#   4 = pytest CLI usage error
#   5 = no tests collected (we treat as 0)
case "$PYTEST_RC" in
    0)
        FINAL_RC=0
        ;;
    5)
        FINAL_RC=0
        echo "(no tests collected — passing)" >&2
        ;;
    1)
        FINAL_RC=1
        ;;
    2|3|4)
        FINAL_RC=2
        ;;
    *)
        FINAL_RC=$PYTEST_RC
        ;;
esac

# --- coverage gate (B-13, 2026-05-15) ----------------------------------------
# Per-module coverage threshold enforcement. Reads the table from
# `tests/pyproject.toml [tool.razzfazz_test.coverage_thresholds]` and
# evaluates each entry's threshold against the .coverage data file pytest
# just wrote. Skipped when:
#   * --no-coverage was passed (no data to evaluate)
#   * pytest exited with an error category (rc 2/3/4 — we already report
#     INVOCATION/INTERRUPT, the gate signal would be noise)
#   * no tests were collected (rc 5)
#   * RAZZFAZZ_SKIP_COVERAGE_GATE=1 (operator override; LOGGED)
#
# Composition rule: the gate result LOWERS coverage to FINAL_RC=3 only
# when the underlying run was rc 0 (otherwise the test-failure signal is
# more important and dominates). The gate report is still PRINTED in the
# rc=1 case so the operator sees both kinds of issues.
COVERAGE_GATE_FIRED=0
if [ "$NO_COVERAGE" -eq 0 ] && [ "$PYTEST_RC" -ne 2 ] && [ "$PYTEST_RC" -ne 3 ] && [ "$PYTEST_RC" -ne 4 ] && [ "$PYTEST_RC" -ne 5 ]; then
    if [ "${RAZZFAZZ_SKIP_COVERAGE_GATE:-0}" = "1" ]; then
        echo "" >&2
        echo "WARNING: RAZZFAZZ_SKIP_COVERAGE_GATE=1 — per-module coverage gate bypassed (operator override)." >&2
        echo "         Use only for non-test-relevant hotfixes; the next prepare-release run will re-evaluate." >&2
    elif [ -f "$TESTS_DIR/pyproject.toml" ] && [ -x "$VENV_DIR/bin/coverage" ]; then
        # Run the gate evaluator. It returns:
        #   0 = no thresholds in scope OR all in-scope modules passed
        #   1 = at least one in-scope module failed its threshold
        #   2 = pyproject.toml unreadable or coverage data missing
        # We pipe its stdout to the operator (the per-module breakdown).
        # The in-scope source-roots list (from the pre-pytest mapping) is
        # passed as $3 so the gate skips out-of-scope thresholds.
        SCOPED_ROOTS_STR="${COV_SOURCE_ROOTS_IN_SCOPE[*]:-}"
        set +e
        "$VENV_DIR/bin/python" - "$TESTS_DIR/pyproject.toml" "$SCRIPT_DIR/.coverage" "$SCOPED_ROOTS_STR" <<'PYEOF'
import io
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

pyproject_path = Path(sys.argv[1])
coverage_data = Path(sys.argv[2])
scoped_roots = sys.argv[3].split() if len(sys.argv) > 3 and sys.argv[3] else []

if not pyproject_path.is_file():
    sys.exit(0)

with open(pyproject_path, "rb") as fh:
    data = tomllib.load(fh)
thresholds = (
    data.get("tool", {})
    .get("razzfazz_test", {})
    .get("coverage_thresholds", {})
)
if not thresholds:
    sys.exit(0)

if not coverage_data.is_file():
    print("WARNING: coverage gate skipped — no .coverage file found at", coverage_data, file=sys.stderr)
    sys.exit(0)

# Filter thresholds to those whose key is under one of the in-scope source
# roots — narrow runs only see their own module's gate, `all` sees them all.
def _in_scope(key):
    if not scoped_roots:
        return False
    for root in scoped_roots:
        root = root.rstrip("/")
        if key == root or key.startswith(root + "/"):
            return True
    return False

thresholds = {k: v for k, v in thresholds.items() if _in_scope(k)}
if not thresholds:
    sys.exit(0)

# Evaluate each threshold via `coverage report --include=<glob>`.
# coverage's exit code is non-zero when fail_under isn't met; we parse
# the printed percentage either way for a clear per-module message.
print("")
print("======================== Coverage gate (B-13) ==========================")
violations = []
in_scope = 0
for target, threshold in sorted(thresholds.items()):
    # `--include` accepts globs; for a directory target, broaden to <dir>/*
    if target.endswith(".py"):
        include_glob = target
    else:
        include_glob = f"{target.rstrip('/')}/*"
    cmd = [
        sys.executable, "-m", "coverage", "report",
        f"--include={include_glob}",
        f"--fail-under={threshold}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout or "") + (res.stderr or "")
    # Detect "no data" (coverage prints "No data to report." on stderr)
    if "No data to report" in out:
        print(f"  - {target}: not exercised this run (skipped)")
        continue
    in_scope += 1
    # Parse the TOTAL line, e.g. "TOTAL                 423    213    50%"
    m = re.search(r"^TOTAL\s+\d+\s+\d+\s+(\d+(?:\.\d+)?)%", out, re.MULTILINE)
    actual = m.group(1) if m else "?"
    if res.returncode != 0:
        violations.append((target, actual, threshold))
        print(f"  - {target}: {actual}% < {threshold}% threshold  [FAIL]")
    else:
        print(f"  - {target}: {actual}% >= {threshold}% threshold  [PASS]")

if not in_scope:
    print("  (no Tier-A source modules exercised — gate not applicable)")
    print("========================================================================")
    sys.exit(0)

print("========================================================================")
if violations:
    print(f"Coverage gate FAILED: {len(violations)} module(s) below threshold.")
    for target, actual, threshold in violations:
        print(f"  module {target} failed: {actual}% < {threshold}% threshold")
    sys.exit(1)
print("Coverage gate passed.")
sys.exit(0)
PYEOF
        GATE_RC=$?
        set -e
        if [ "$GATE_RC" -eq 1 ]; then
            COVERAGE_GATE_FIRED=1
            # The gate fires only on top of green pytest runs; a real test
            # failure (FINAL_RC=1) keeps its rc-1 signal but the gate
            # report is still surfaced above for the operator.
            if [ "$FINAL_RC" -eq 0 ]; then
                FINAL_RC=3
            fi
        fi
    fi
fi

# --- CI-mode post-processing (M032-S06) --------------------------------------
# Generate summary.json + acceptance-report.md from the JUnit XML, symlink
# tests/results/latest -> $RUN_DIR, and if --acceptance was passed but zero
# probes were collected (pytest RC 5), upgrade FINAL_RC to 5 (no-probes-found).
# This is the contract the init/upgrade --with-acceptance hook relies on.
if [ "$CI_MODE" -eq 1 ]; then
    JUNIT_XML="$RUN_DIR/junit.xml"
    SUMMARY_JSON="$RUN_DIR/summary.json"
    REPORT_MD="$RUN_DIR/acceptance-report.md"

    "$VENV_DIR/bin/python" - "$JUNIT_XML" "$SUMMARY_JSON" "$REPORT_MD" "$RUN_ID" "$MARKER" "$PYTEST_RC" <<'PYEOF'
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

junit_xml, summary_json, report_md, run_id, marker, pytest_rc = sys.argv[1:7]
pytest_rc = int(pytest_rc)

summary = {
    "run_id": run_id,
    "marker": marker or "all",
    "pytest_rc": pytest_rc,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "tests": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0},
    "failing_tests": [],
    "skipped_tests": [],
}

if os.path.isfile(junit_xml):
    try:
        root = ET.parse(junit_xml).getroot()
        # Pytest emits <testsuites><testsuite ...><testcase .../></testsuite></testsuites>
        suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
        for suite in suites:
            summary["tests"]["total"] += int(suite.get("tests", 0))
            summary["tests"]["failed"] += int(suite.get("failures", 0))
            summary["tests"]["errors"] += int(suite.get("errors", 0))
            summary["tests"]["skipped"] += int(suite.get("skipped", 0))
            for case in suite.findall("testcase"):
                tid = f"{case.get('classname', '')}::{case.get('name', '')}"
                if case.find("failure") is not None or case.find("error") is not None:
                    summary["failing_tests"].append(tid)
                elif case.find("skipped") is not None:
                    summary["skipped_tests"].append(tid)
        summary["tests"]["passed"] = (
            summary["tests"]["total"]
            - summary["tests"]["failed"]
            - summary["tests"]["errors"]
            - summary["tests"]["skipped"]
        )
    except ET.ParseError as e:
        summary["junit_parse_error"] = str(e)

with open(summary_json, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2, sort_keys=True)

# Markdown digest
t = summary["tests"]
lines = [
    f"# Acceptance probe report — {run_id}",
    "",
    f"- Generated: {summary['generated_at']}",
    f"- Marker: `{summary['marker']}`",
    f"- Pytest exit code: {pytest_rc}",
    "",
    "## Counts",
    "",
    f"- Total: {t['total']}",
    f"- Passed: {t['passed']}",
    f"- Failed: {t['failed']}",
    f"- Errors: {t['errors']}",
    f"- Skipped: {t['skipped']}",
    "",
]
if summary["failing_tests"]:
    lines.append("## Failing tests")
    lines.append("")
    for tid in summary["failing_tests"]:
        lines.append(f"- `{tid}`")
    lines.append("")
if summary["skipped_tests"]:
    lines.append(f"## Skipped tests ({len(summary['skipped_tests'])})")
    lines.append("")
    # Truncate long lists for readability; full list in summary.json
    shown = summary["skipped_tests"][:25]
    for tid in shown:
        lines.append(f"- `{tid}`")
    if len(summary["skipped_tests"]) > len(shown):
        lines.append(f"- ... ({len(summary['skipped_tests']) - len(shown)} more — see summary.json)")
    lines.append("")

with open(report_md, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
PYEOF

    # Stable "latest" symlink so callers (init/upgrade --with-acceptance) can
    # always point at tests/results/latest/acceptance-report.md.
    ln -sfn "$RUN_ID" "$RESULTS_ROOT/latest"

    # R-S06-3 enforcement: --ci-mode + --acceptance + 0 probes collected
    # → exit code 5 (no-probes-found). Differs from the default behavior
    # (which downgrades pytest RC 5 to 0) — for CI we want a hard signal
    # that the probe suite isn't deployed.
    if [ "$MARKER" = "acceptance" ] && [ "$PYTEST_RC" -eq 5 ]; then
        echo "ERROR: --ci-mode --acceptance: zero probes collected." >&2
        echo "       Test infra is present but no acceptance probes — see M032 S02." >&2
        FINAL_RC=5
    fi
fi

echo "Run ID: $RUN_ID" >&2
echo "Results: $RUN_DIR" >&2
if [ "$CI_MODE" -eq 1 ]; then
    echo "Summary: $RUN_DIR/summary.json" >&2
    echo "Report:  $RUN_DIR/acceptance-report.md" >&2
fi

# --- customer-facing report (2026-05-15) ------------------------------------
# Run the report generator AFTER the CI-mode summary so the JUnit XML and
# coverage.xml are both finalized. Generator failure is non-fatal — the
# raw artifacts under $RUN_DIR are still present for an operator to debug.
if [ "$CUSTOMER_REPORT" -eq 1 ]; then
    GEN="$SCRIPT_DIR/scripts/generate-test-report.py"
    if [ -x "$GEN" ] || [ -f "$GEN" ]; then
        echo "Generating customer-facing report..." >&2
        # Export RAZZFAZZ_REPO_ROOT so the generator can locate .env via
        # TRF-DEC-01's resolution chain step 2 even when invoked from a
        # sibling worktree (which won't have its own .env).
        export RAZZFAZZ_REPO_ROOT="$SCRIPT_DIR"
        if "$VENV_DIR/bin/python" "$GEN" "$RUN_ID" --repo-root "$SCRIPT_DIR" --env-file "$SCRIPT_DIR/.env"; then
            echo "Customer report (Markdown): $RUN_DIR/test-report.md" >&2
            if [ -f "$RUN_DIR/test-report.pdf" ]; then
                echo "Customer report (PDF):      $RUN_DIR/test-report.pdf" >&2
            fi
        else
            echo "WARNING: customer-report generator failed (raw artifacts in $RUN_DIR)" >&2
        fi
    else
        echo "WARNING: $GEN not found; skipping customer-report generation" >&2
    fi
fi

exit "$FINAL_RC"
