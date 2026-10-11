#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/upgrade-diagnose.sh — post-upgrade diagnose-gate (#143 B)
# =============================================================================
# Runs a set of CONSISTENCY asserts after an upgrade and emits a readable
# "PASS / N wrong + likely cause + fix" report — catching the "healthy but
# wrong" class that a plain healthcheck misses (Authentik up but outpost
# unbound, a declared service not running, etc.).
#
# Reuses what we already have (operator directive): invokes
# razzfazz-post-install.sh --verify (the read-only verification suite) in
# addition to the targeted asserts below.
#
# Standalone-capable: run it any time to get a befund of the live stack. When
# $RAZZFAZZ_JOURNAL_JSON is set (i.e. called from razzfazz-upgrade.sh), each
# finding is also written to the structured journal.
#
# Exit code = number of problems found (0 = clean).
# =============================================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/lib.sh"
# shellcheck source=scripts/lib-journal.sh disable=SC1091
source "${SCRIPT_DIR}/lib-journal.sh" 2>/dev/null || true
cd "$STACK_DIR" || exit 0

PROBLEMS=0
# report <ok|fail> <check> <detail> <fix>
report() {
    local status=$1 check=$2 detail=$3 fix=${4:-}
    if [ "$status" = ok ]; then
        print_substep "diagnose: ${check} — OK"
        journal_event "diagnose" "ok" "${check}: ${detail}"
    else
        print_warning "diagnose: ${check} — ${detail}"
        [ -n "$fix" ] && print_substep "  fix: ${fix}"
        journal_event "diagnose" "fail" "${check}: ${detail} | fix: ${fix}"
        PROBLEMS=$((PROBLEMS + 1))
    fi
}

print_step "Diagnose-gate (#143 B): post-upgrade consistency asserts"

# ── Assert 1: embedded outpost has ALL proxy providers bound ──────────────────
# The MEGAS/Chris class: an unbound outpost → gated apps 500/404 ("Server Error"
# / "Not found — powered by Authentik") while every container looks healthy.
if docker ps --format '{{.Names}}' | grep -qx authentik-worker; then
    bound=$(docker exec authentik-worker bash -c \
        'DJANGO_SETTINGS_MODULE=authentik.root.settings python -c "
import django; django.setup()
from authentik.outposts.models import Outpost
from authentik.providers.proxy.models import ProxyProvider
o=Outpost.objects.filter(name=\"authentik Embedded Outpost\").first()
print(f\"{o.providers.count()}/{ProxyProvider.objects.count()}\" if o else \"0/0\")"' \
        2>/dev/null | tail -1)
    attached=${bound%%/*}; total=${bound##*/}
    if [ -n "$total" ] && [ "$total" != 0 ] && [ "$attached" = "$total" ]; then
        report ok "outpost-bindings" "${bound} providers attached"
    else
        report fail "outpost-bindings" "only ${bound:-?} providers bound to the embedded outpost → SSO apps will 500/404" \
            "docker cp core/Authentik/apply-policy-bindings.py authentik-worker:/tmp/apb.py && docker exec -e COMPOSE_PROFILES=\"\$(grep -oE '^COMPOSE_PROFILES=.*' .env | cut -d= -f2-)\" authentik-worker python /tmp/apb.py"
    fi
else
    print_substep "diagnose: outpost-bindings — skipped (authentik-worker not running)"
fi

# ── Assert 2: no container in a BAD runtime state (restarting / unhealthy) ─────
# Reliable signal (unlike "declared but not running", which false-positives on
# build-only *-image services, inactive-profile *-legacy, and container_name ≠
# service_name). One-shot/init containers Exit 0 by design and are excluded.
bad=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null \
    | grep -iE 'restarting|unhealthy' \
    | grep -ivE 'migrat|reconcile|init|hop|seed|media-migrator|permissions|-image' || true)
if [ -z "$bad" ]; then
    report ok "container-health" "no restarting/unhealthy containers"
else
    report fail "container-health" "bad-state container(s): $(echo "$bad" | tr '\n' '; ')" \
        "docker compose logs <name> for the cause; often a missing env/secret or failed migration"
    # Capture the offending containers' logs into the journal so the befund
    # travels with it (no second round-trip to the box).
    echo "$bad" | awk '{print $1}' | while read -r c; do
        [ -n "$c" ] && journal_capture_failure "$c"
    done
fi

# ── Assert 3 (INFORMATIONAL): reuse the existing read-only verification suite ──
# Run it and journal the result, but do NOT count it as a hard problem yet — on
# a not-fully-provisioned box it reports benign gaps (experimental modules,
# known test noise #130/#131). Becomes a hard assert once those are clean.
if [ -x "${STACK_DIR}/rzfz" ]; then
    vfile="/tmp/rz-verify.$$"
    if timeout 180 "${STACK_DIR}/rzfz" post-install --verify >"$vfile" 2>&1; then
        print_substep "diagnose: post-install-verify — clean (informational)"
        journal_event "diagnose" "info" "post-install --verify: clean"
    else
        local_fails=$(grep -cE '✗ FAIL|\[✗\]' "$vfile" 2>/dev/null || echo "?")
        print_substep "diagnose: post-install-verify — ${local_fails} check(s) flagged (informational; see ${vfile})"
        journal_event "diagnose" "info" "post-install --verify: ${local_fails} flagged (informational; not counted)"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
if [ "$PROBLEMS" -eq 0 ]; then
    print_success "Diagnose-gate: PASS — no consistency problems."
    journal_event "diagnose" "summary" "PASS (0 problems)"
else
    print_warning "Diagnose-gate: ${PROBLEMS} problem(s) found (cause + fix above)."
    journal_event "diagnose" "summary" "${PROBLEMS} problem(s)"
fi
exit "$PROBLEMS"
