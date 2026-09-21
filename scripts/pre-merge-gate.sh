#!/usr/bin/env bash
# Run the CI gates LOCALLY, before merging a PR. (#444, operator decision
# 2026-08-26 — which superseded 2026-08-18 and 2026-08-23 on WHERE the tests
# run. THIS SCRIPT is the mechanical form of that rule; `.gitea/workflows/
# ci.yml` states it in prose and points here. #791 wired the two together
# after they drifted apart, and tests/unit/ci/test_791_pre_merge_gate_wiring.py
# keeps them together.)
#
# The 'ci' label is NOT the follow-up to a partial run — see the PARTIAL branch
# at the bottom of this file for what is.
#
# WHY THIS EXISTS. The unit and api tiers are deterministic developer tests —
# no live stack, no secrets, no network. Running them on a shared CI runner for
# every commit and every PR push turned one `razzfazz-ci` runner into an
# hour-deep queue where every reported failure was for a commit that had already
# been superseded. So the gate moved to where the code is written, and CI now
# answers only the question that cannot be answered before the merge: is `main`
# still good AFTER the merge landed?
#
# Run this before you merge. Paste the verdict block into the PR.
#
#   scripts/pre-merge-gate.sh                 # gates for the diff vs origin/main
#   scripts/pre-merge-gate.sh --base <ref>    # diff against something else
#   scripts/pre-merge-gate.sh --all           # force every gate regardless
#
# Exits non-zero if any selected gate fails. Scope selection is the SAME
# classifier CI uses (scripts/ci-changed-scopes.sh), so local and post-merge runs
# agree on what is relevant.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
REPO_ROOT="$PWD"

base="origin/main"
force_all=false
allow_live_writes=false
# Read ONLY pytest's final summary line. Scanning the whole log also matched
# counts inside progress output and the coverage table, so a FAILING run could
# render as `[34955 failed34955 failed…3133 passed]` — a verdict people paste
# into a PR, showing numbers that appear nowhere in the run. (Observed once;
# the exact contributing line was not reproducible afterwards, so this anchors
# the whole class rather than patching one pattern.) Green runs were never
# affected, which is why it went unnoticed.
#
# Exposed via `--parse-summary <file>` so a test can exercise THIS code rather
# than a copy of the pipeline.
parse_tier_summary() {
    grep -E '^=*[= ]*[0-9]+ (passed|failed|error)' "$1" | tail -1 \
        | grep -oE '[0-9]+ (passed|failed|skipped|errors?|xfailed|xpassed|deselected)' \
        | tr '\n' '|' | sed 's/|$//; s/|/, /g'
}

if [ "${1:-}" = "--parse-summary" ]; then
    parse_tier_summary "$2"
    exit 0
fi

# --- the verdict (#2070) ------------------------------------------------------
# A PARTIAL run used to say "DB-backed tests did not run (no docker)" once, in
# prose, under a summary whose "0 failed" is what the eye reads. Twice on
# 2026-09-13 a tier that had not run was read as a tier that had passed. The
# verdict therefore names every tier that did NOT verify, one line each, and
# separates the two kinds — because only one of them is fixable by the reader:
#   could not run here   — no docker daemon, no MAIN_DOMAIN: an environment limit
#   was NOT ASKED to run — the api tier's DB-backed tests run without docker
#                          when RAZZFAZZ_TEST_PG_DSN points at a Postgres
#                          (#1631); a sandbox that has one and did not pass it
#                          made a configuration mistake that looked like a limit
# plus one machine-readable line, `UNVERIFIED_TIERS: a,b` (or `none`), for the
# waiters and comment templates that read the log.
#
# Exposed via `--render-verdict <results-file>` (env: GATE_BRANCH, GATE_HEAD,
# GATE_DEGRADED, GATE_MAIN_DOMAIN, GATE_INCOMPLETE, GATE_OVERALL,
# RAZZFAZZ_TEST_PG_DSN) so a test exercises THIS code, not a copy.
render_verdict() {   # uses: results[] degraded main_domain incomplete overall head_sha branch_name
    local unverified=() fixable=false r
    if [ "$degraded" = true ]; then
        if [ -n "${RAZZFAZZ_TEST_PG_DSN:-}" ]; then
            # DB-backed api tests ran through the DSN — verified. What still did
            # not run are the tests that start containers THEMSELVES.
            unverified+=("docker-fixtures|could not run here: no docker daemon — tests that start their own containers skipped (the DB-backed api tests DID run, through RAZZFAZZ_TEST_PG_DSN)")
        else
            unverified+=("db-backed-api|was NOT ASKED to run: no docker, and RAZZFAZZ_TEST_PG_DSN unset — set it to a test Postgres (#1631) and the DB-backed api tests run here")
            fixable=true
        fi
    fi
    [ -z "$main_domain" ] && unverified+=("live-stack|could not run here: .env has no MAIN_DOMAIN (needs a disposable box)")
    for r in "${results[@]}"; do
        case "$r" in
            "SKIP  "*"(could not verify"*)
                unverified+=("$(printf '%s' "$r" | sed -E 's/^SKIP  (.*) \(could not verify.*$/\1/')|could not run here: the check reported 3 (missing tool or daemon — see its output)") ;;
        esac
    done
    echo "=============================================================="
    echo " VERDICT — paste this into the PR before merging"
    echo "=============================================================="
    echo '```'
    echo "pre-merge gate: ${branch_name} @ ${head_sha}"
    echo "host: $(hostname)   date: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    if [ "$degraded" = true ] && [ -n "${RAZZFAZZ_TEST_PG_DSN:-}" ]; then
        echo "docker: UNAVAILABLE — DB-backed api tests ran through RAZZFAZZ_TEST_PG_DSN (#1631)"
    else
        echo "docker: $([ "$degraded" = true ] && echo 'UNAVAILABLE — DB-backed tests skipped' || echo available)"
    fi
    [ -n "$main_domain" ] && echo "live stack: $main_domain" || echo "live stack: none (.env has no MAIN_DOMAIN — live-stack tests skipped)"
    for r in "${results[@]}"; do echo "  $r"; done
    if [ "${#unverified[@]}" -gt 0 ]; then
        echo "NOT VERIFIED by this run (a PASS above says nothing about these):"
        for r in "${unverified[@]}"; do echo "  UNVERIFIED: ${r%%|*} — ${r#*|}"; done
        echo "UNVERIFIED_TIERS: $(printf '%s\n' "${unverified[@]}" | cut -d'|' -f1 | paste -sd, -)"
    else
        echo "UNVERIFIED_TIERS: none"
    fi
    if [ "$overall" -ne 0 ]; then
        echo "RESULT: FAIL — do not merge"
        return 1
    elif [ "${#unverified[@]}" -gt 0 ] || [ "$incomplete" = true ]; then
        # Never PASS on a run that could not execute whole categories of test.
        # Exit 3 is distinct from FAIL(1) so a caller can tell "broken" from
        # "incomplete".
        echo "RESULT: PARTIAL — nothing failed, but this run did not verify: $(printf '%s\n' "${unverified[@]}" | cut -d'|' -f1 | paste -sd, -)"
        [ "$fixable" = true ] && echo "   At least one of those was NOT ASKED to run — fix the invocation and re-run before trusting a PARTIAL on a PR that touches it."
        # #791: this used to advise the 'ci' label as the next step (2026-08-23
        # amendment to #444). The 2026-08-26 decision retired that: no label, so
        # no PR run. Ending every PARTIAL run by recommending the opposite of the
        # standing rule trained the wrong habit once per run.
        echo "   Merge is allowed only if no unverified tier covers what this PR changes."
        echo "   main is verified afterwards by the batched workflow_dispatch that"
        echo "   agent-seqis owns (2026-08-26 decision), with the nightly 03:00 UTC run as the floor."
        echo "   The 'ci' label is an EXCEPTION, not the next step — use it only for"
        echo "   work that provably cannot be checked locally, and put the reason in"
        echo "   the PR. Watch a run: /api/v1/repos/.../actions/runs (NOT /actions/tasks)"
        return 3
    else
        echo "RESULT: PASS — safe to merge"
        return 0
    fi
}
if [ "${1:-}" = "--render-verdict" ]; then
    results=()
    while IFS= read -r line; do [ -n "$line" ] && results+=("$line"); done < "${2:?--render-verdict needs a results file}"
    degraded="${GATE_DEGRADED:-false}"; main_domain="${GATE_MAIN_DOMAIN:-}"
    incomplete="${GATE_INCOMPLETE:-false}"; overall="${GATE_OVERALL:-0}"
    head_sha="${GATE_HEAD:-0000000}"; branch_name="${GATE_BRANCH:-test}"
    render_verdict; exit $?
fi

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base) base="${2:?--base needs a ref}"; shift 2 ;;
        --all)  force_all=true; shift ;;
        --allow-live-writes) allow_live_writes=true; shift ;;
        # #791: was `sed -n '1,25p'`. A fixed line range silently truncates the
        # header the moment anyone adds a line to it — which this issue did.
        # Print the comment header instead, whatever length it grows to.
        -h|--help) awk 'NR>1 && !/^#/ {exit} NR>1 {print}' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# --- PRECONDITION 1: docker, or this gate lies (#458) -----------------------
# Without a reachable docker daemon the ephemeral_db fixtures cannot start and
# ~108 llm-manager tests SKIP — the DB-backed paths that carry most of the
# coverage. cli/test.sh then compares the resulting coverage to a fixed
# threshold with no notion of how many tests skipped, so a degraded run is
# indistinguishable from a real regression. That produced a confident, specific,
# WRONG verdict (59% vs the runner's 72%) that was reported three times before
# anyone checked the environment. A local gate that can silently degrade is
# worse than no local gate, so refuse rather than guess.
degraded=false
if ! docker info >/dev/null 2>&1; then
    degraded=true
    if [ -n "${RAZZFAZZ_TEST_PG_DSN:-}" ]; then
        echo "NOTICE: docker daemon not reachable — DB-backed api tests run through RAZZFAZZ_TEST_PG_DSN (#1631); live-stack and docker-fixture tests still skip." >&2
    else
        echo "NOTICE: no docker daemon AND no RAZZFAZZ_TEST_PG_DSN — the DB-backed api tests were NOT ASKED to run. A sandbox with a test Postgres passes the DSN (#1631); the verdict will list this tier as UNVERIFIED (#2070)." >&2
    fi
    cat >&2 <<'EOF'
NOTICE: docker daemon not reachable — this run is DEGRADED.

The ephemeral postgres fixtures cannot start, so the DB-backed tests (~108 in
llm-manager alone) will SKIP. Their coverage is exactly what the coverage gate
measures, so a threshold comparison here is meaningless (#458): that is how a
local run reported 59% against the runner's 72%.

The run continues, because an agent running inside a container cannot have a
docker daemon and still needs the signal it CAN get. But the verdict will say
PARTIAL, never PASS, and the merge decision then rests on the post-merge CI run
on main — which does have docker.
EOF
fi

# --- PRECONDITION 2: the api tier WRITES to a live box -----------------------
# tests/api/authentik/test_group_enforcement.py creates an Authentik user, sets
# its password and deletes it; test_rate_limit_login.py fires a dozen login
# POSTs until the limiter trips; dify-api/test_reset_password_phase.py POSTs
# into Dify's reset flow. They target https://auth.$MAIN_DOMAIN read from .env,
# and skip ONLY when that is unset or unreachable. They are self-cleaning, but a
# killed run orphans the user — and "non-destructive" is not the same property
# as "safe against production".
if [ -f .env ]; then
    # never source .env: operator-edited values carry spaces and shell metachars
    main_domain="$(grep -E '^MAIN_DOMAIN=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"'' | tr -d '[:space:]')"
else
    main_domain=""
fi
# Deliberately NOT a domain-name allow-list: guessing "disposable" from a string
# like `rzfz.box` is exactly the kind of inference that eventually matches a
# customer box. The box must SAY it is disposable.
if [ -n "$main_domain" ] \
   && [ "$allow_live_writes" != true ] \
   && [ "${RAZZFAZZ_DISPOSABLE_BOX:-}" != "1" ]; then
    cat >&2 <<EOF
REFUSING TO RUN: .env points MAIN_DOMAIN at '$main_domain'.

The api tier WRITES to that box:
  tests/api/authentik/test_group_enforcement.py   creates, sets a password on,
                                                  and deletes an Authentik user
  tests/api/authentik/test_rate_limit_login.py    fires login POSTs until the
                                                  rate limiter trips
  tests/api/dify-api/test_reset_password_phase.py POSTs Dify's reset flow

They are self-cleaning, but an interrupted run orphans the user, and a tripped
rate limiter locks logins for its window. Fine on a disposable box; not fine on
prod, demo or a customer box. "Non-destructive" and "safe against production"
are different properties.

If this box is disposable, either:
    scripts/pre-merge-gate.sh --allow-live-writes
    RAZZFAZZ_DISPOSABLE_BOX=1 scripts/pre-merge-gate.sh
EOF
    exit 2
fi
[ -n "$main_domain" ] && \
    echo "NOTE: api tier may write to '$main_domain' (declared disposable)" >&2

if ! git rev-parse --verify "$base^{commit}" >/dev/null 2>&1; then
    echo "base ref '$base' not found — try: git fetch origin main" >&2
    exit 2
fi

head_sha="$(git rev-parse --short HEAD)"
# Diff against the MERGE BASE, not the tip: otherwise everything that landed on
# main since this branch started counts as "changed here" and every gate runs.
merge_base="$(git merge-base "$base" HEAD)"

if [ "$force_all" = true ]; then
    scopes="code=true
docs=true
ui=true"
    changed="(--all: every gate forced)"
else
    changed="$(git diff --name-only "$merge_base" HEAD)"
    scopes="$(printf '%s\n' "$changed" | "$REPO_ROOT/scripts/ci-changed-scopes.sh")"
fi

get() { printf '%s\n' "$scopes" | sed -n "s/^$1=//p"; }
run_code="$(get code)"; run_docs="$(get docs)"; run_ui="$(get ui)"
run_caddy="$(get caddy)"; [ -n "$run_caddy" ] || run_caddy=false

echo "=============================================================="
echo " pre-merge gate — $(git rev-parse --abbrev-ref HEAD) @ $head_sha"
echo " base: $base (merge-base ${merge_base:0:8})"
echo "=============================================================="
echo "changed files:"
printf '%s\n' "${changed:-  (none)}" | sed 's/^/  /'
echo
echo "gates selected:  code=$run_code  docs=$run_docs  ui=$run_ui  caddy=$run_caddy"
echo

results=()
overall=0
incomplete=false

# EXIT-CODE CONVENTION (#730). A check reports THREE outcomes, not two:
#   0  verified, good
#   3  COULD NOT VERIFY (missing docker, missing tool, no live stack)
#   *  verified, broken
# 3 is the same code this gate itself exits with for PARTIAL, and
# scripts/validate-caddyfile.sh already uses it ("This is a real gap, not a
# pass"). Treating it as FAIL made the gate say "do not merge" about a main
# that CI had just certified green — the mirror image of the silent-degrade
# failure this script's header warns about, and just as corrosive: a gate that
# accuses a healthy tree gets ignored, and then it no longer protects when it
# is right.
gate() {   # gate <label> <should_run> <command...>
    local label="$1" should="$2"; shift 2
    if [ "$should" != "true" ]; then
        results+=("SKIP  $label (not touched by this change)")
        return
    fi
    echo "-------- $label --------"
    local rc=0
    "$@" || rc=$?
    case "$rc" in
        0) results+=("PASS  $label") ;;
        3) results+=("SKIP  $label (could not verify — see output above)")
           incomplete=true ;;
        *) results+=("FAIL  $label")
           overall=1 ;;
    esac
    echo
}


# The unit+api+scripts tiers, via the SAME entry point CI uses. `./rzfz test`
# differs from a raw `pytest tests/unit` (marker selection, coverage gate), so
# running pytest directly here would gate on something CI does not check.
#
# `--scripts` was missing (#1318). CI runs THREE jobs — unit-api, scripts,
# docs — and this gate ran two of them, so the whole `scripts` tier reached a
# merge decision only afterwards, in the batched main run. That tier is 309
# tests and it wraps 35 standalone bash suites
# (tests/scripts/prepare-release/test_legacy_runner.py): the .env
# inode-preservation guard (#1189 — the read-only-.env class the operator hit
# on 0.91), the version-ordering guard (#1890), the password broker, the
# offline/air-gap gates. Exactly the operator-script surface where this cycle's
# defects lived, and it was the one tier a local gate did not see. It costs
# ~85 s on a run that takes forty minutes.
#
# The output is captured so the verdict can report how many tests SKIPPED.
# A pass with an unusual number of skips is the signature of a degraded
# environment, not of working code (#458) — printing the count is what makes
# that visible to whoever reads the PR instead of buried in a coverage number.
tier_summary=""
if [ "$run_code" = "true" ]; then
    echo "-------- unit + api + scripts --------"
    log="$(mktemp)"
    if ./rzfz test --unit --api --scripts all 2>&1 | tee "$log"; then
        rc=0
    else
        rc="${PIPESTATUS[0]}"
    fi
    tier_summary="$(parse_tier_summary "$log")"
    skipped="$(printf '%s' "$tier_summary" | grep -oE '[0-9]+ skipped' | grep -oE '^[0-9]+' || true)"
    if [ "$rc" -eq 0 ]; then
        results+=("PASS  unit + api + scripts  [${tier_summary:-no summary parsed}]")
        if [ -n "${skipped:-}" ] && [ "$skipped" -ge 50 ]; then
            results+=("WARN  ${skipped} tests SKIPPED — verify the environment before trusting this pass (#458)")
        fi
    else
        results+=("FAIL  unit + api + scripts  [${tier_summary:-no summary parsed}]")
        overall=1
    fi
    rm -f "$log"
    echo
else
    results+=("SKIP  unit + api + scripts (not touched by this change)")
fi
gate "docs structure" "$run_docs" ./tests/test-docs-structure.sh
# #455: the ingress decides whether the box answers at all. Only on Caddy
# changes — it builds a plugin-carrying image, so it is not free.
gate "caddyfile validate" "$run_caddy" ./scripts/validate-caddyfile.sh
gate "release-docs lint" "$run_docs" ./scripts/lint-release-docs.sh

if [ "$run_ui" = "true" ]; then
    echo "-------- manager-ui build --------"
    if ( cd modules/llm/manager-ui && npm ci --no-audit --no-fund && npm run build ); then
        results+=("PASS  manager-ui type-check + build")
    else
        results+=("FAIL  manager-ui type-check + build"); overall=1
    fi
    echo
else
    results+=("SKIP  manager-ui type-check + build (not touched by this change)")
fi

branch_name="$(git rev-parse --abbrev-ref HEAD)"
render_verdict
exit $?
