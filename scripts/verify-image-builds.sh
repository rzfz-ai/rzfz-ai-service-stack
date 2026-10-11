#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/verify-image-builds.sh — pre-release build gate (#133)
# =============================================================================
# Verifies that EVERY locally-built image actually builds — for both the upgrade
# path and a clean install. An unbuildable custom image is a release blocker
# (e.g. ga.7's gpustack-legacy poetry/lxml failure on a transient PyPI error),
# and the only reliable way to catch it is to actually build them.
#
# Retry resilience (operator directive): a failed build is retried ONCE before
# being declared a failure, so a transient PyPI/registry/network flake doesn't
# fail the gate. Centralised here rather than editing every Dockerfile.
#
# Usage:
#   scripts/verify-image-builds.sh --list        # list custom-build services
#   scripts/verify-image-builds.sh               # build all (1 retry each)
#   scripts/verify-image-builds.sh --no-cache    # true clean-build (slow, thorough)
#   scripts/verify-image-builds.sh --only a,b    # subset
#
# Exit code = number of services that failed to build (0 = all green).
# =============================================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/lib.sh"
cd "$STACK_DIR" || exit 1

NO_CACHE=""; LIST_ONLY=false; ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-cache) NO_CACHE="--no-cache"; shift ;;
        --list)     LIST_ONLY=true; shift ;;
        --only)     ONLY="$2"; shift 2 ;;
        -h|--help)  grep '^#' "$0" | sed -n '1,24p' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) print_error "Unknown option: $1"; exit 1 ;;
    esac
done

# Custom-build services = those with a `build:` key in the resolved compose.
mapfile -t SERVICES < <(docker compose config --format json 2>/dev/null | python3 -c "
import json,sys
try: c=json.load(sys.stdin)
except Exception: sys.exit(0)
for n,s in sorted(c.get('services',{}).items()):
    if isinstance(s,dict) and 'build' in s: print(n)
")

if [ "${#SERVICES[@]}" -eq 0 ]; then
    print_error "No custom-build services found (docker compose config failed?)."
    exit 1
fi
if [ -n "$ONLY" ]; then
    IFS=',' read -r -a want <<< "$ONLY"
    filtered=()
    for s in "${SERVICES[@]}"; do for w in "${want[@]}"; do [ "$s" = "$w" ] && filtered+=("$s"); done; done
    SERVICES=("${filtered[@]}")
fi

if [ "$LIST_ONLY" = true ]; then
    printf '%s\n' "${SERVICES[@]}"
    exit 0
fi

print_step "Build gate (#133): ${#SERVICES[@]} custom images${NO_CACHE:+ (--no-cache)}"
# ── Function-level smoke check ───────────────────────────────────────────────
# An image that BUILDS but whose key binary can't RUN (missing .so, GLIBC/GLIBCXX
# mismatch, wrong install path) is STILL a release blocker — build-success != works.
# Operator directive: verify the build IMMEDIATELY on a bump, never discover it at a
# customer clean install. This catches the gpustack-legacy class at BUILD time:
#   1) libvulkan/libpcre missing (corrupt apt layer) → loader error
#   2) runner copied to a python path gpustack can't see → not found
#   3) patched llama-server built on a newer glibc → "GLIBC_2.38 not found"
# All three make `llama-server --version` exit non-zero, so one check covers them.
_svc_image() {
    docker compose config --format json 2>/dev/null | python3 -c "
import json,sys
c=json.load(sys.stdin); print(c.get('services',{}).get('$1',{}).get('image',''))" 2>/dev/null
}
smoke_check() {
    local svc="$1" img; img="$(_svc_image "$svc")"
    [ -z "$img" ] && return 0
    case "$svc" in
        gpustack-legacy|gpustack-cpu|gpustack*)
            docker run --rm --entrypoint sh "$img" -c '
                set -e
                P=$(python3 -c "import gpustack,os;print(os.path.dirname(gpustack.__file__))")
                B="$P/third_party/bin/llama-box/llama-box-default/llama-server"
                [ -x "$B" ] || { echo "  runner not under gpustack path ($B)"; exit 1; }
                "$B" --version >/dev/null 2>/tmp/verr || { echo "  runner will not run:"; sed "s/^/    /" /tmp/verr | head -3; exit 1; }
            ' 2>&1 | sed "s/^/      /"
            return "${PIPESTATUS[0]}"
            ;;
        *) return 0 ;;
    esac
}

FAILED=0; declare -a FAIL_LIST=()
for svc in "${SERVICES[@]}"; do
    built=false
    if docker compose build $NO_CACHE "$svc" >/dev/null 2>&1; then
        built=true
    else
        print_warning "build ${svc} — failed, retrying once (transient-flake resilience)..."
        docker compose build $NO_CACHE "$svc" >/dev/null 2>&1 && built=true
    fi
    if [ "$built" != true ]; then
        print_error "build ${svc} — FAILED after retry"
        FAILED=$((FAILED+1)); FAIL_LIST+=("$svc (build)")
        continue
    fi
    if smoke_check "$svc"; then
        print_substep "build ${svc} — OK (builds + runs)"
    else
        print_error "build ${svc} — BUILT but FUNCTION check failed (binary won't run)"
        FAILED=$((FAILED+1)); FAIL_LIST+=("$svc (smoke)")
    fi
done

echo
if [ "$FAILED" -eq 0 ]; then
    print_success "Build gate: all ${#SERVICES[@]} custom images build."
else
    print_error "Build gate: ${FAILED} image(s) failed to build: ${FAIL_LIST[*]}"
    print_info "Re-run a single one verbosely: docker compose build ${FAIL_LIST[0]}"
fi
exit "$FAILED"
