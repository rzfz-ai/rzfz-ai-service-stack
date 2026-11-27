#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# verify-hub-images.sh - does the hub serve the agent image of THIS release for
# every architecture it claims? (#2952)
# ==============================================================================
# Reads the hub registry API (no docker needed). For the tag, and for the
# per-architecture tags <tag>-amd64 / <tag>-arm64, it resolves each image to
# its config and asserts RAZZFAZZ_IMAGE_VERSION equals the expected release and
# that the config's architecture is the one the tag names. A manifest list must
# cover amd64 AND arm64 unless --allow-single-arch is given. Exit 0 = all good,
# 1 = something is stale or missing, 2 = usage / registry unreachable.
# (set -e is on: every command that may fail sits in an if / || so the findings
# accumulate and the exit code is the verdict, never a silent abort.)
#
# Usage: scripts/verify-hub-images.sh <tag> [--expect VERSION] [--registry URL]
#            [--image NAME] [--allow-single-arch] [--cacert FILE]
#   <tag>       the tag to verify, e.g. 2026.11-rc6 or latest
#   --expect    the release the images must carry (default: <tag>, or this
#               checkout's VERSION file for 'latest')
#   --registry  default http://127.0.0.1:${LLM_REGISTRY_PORT:-8093}
#   --image     repeatable; default razzfazz-llm-worker-agent
# Hub login for a remote registry: RZFZ_HUB_USER / RZFZ_HUB_PASSWORD.
# ==============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TAG=""; EXPECT=""; REGISTRY=""; ALLOW_SINGLE=""; CACERT=""; IMAGES=()
die() { echo "verify-hub-images: $1" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case "$1" in
        --expect) EXPECT="${2:?--expect needs a version}"; shift 2 ;;
        --registry) REGISTRY="${2:?--registry needs a URL}"; shift 2 ;;
        --image) IMAGES+=("${2:?--image needs a name}"); shift 2 ;;
        --allow-single-arch) ALLOW_SINGLE=1; shift ;;
        --cacert) CACERT="${2:?--cacert needs a file}"; shift 2 ;;
        -h|--help) sed -n '/^# Usage:/,/^# ====/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $1" ;;
        *) [ -z "$TAG" ] || die "one tag only"; TAG="$1"; shift ;;
    esac
done
[ -n "$TAG" ] || die "usage: verify-hub-images.sh <tag> (see --help)"
if [ -z "$EXPECT" ]; then
    if [ "$TAG" = latest ]; then
        EXPECT="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || true)"
        [ -n "$EXPECT" ] || die "no VERSION file to compare 'latest' against - pass --expect"
    else
        EXPECT="$TAG"
    fi
fi
[ "${#IMAGES[@]}" -gt 0 ] || IMAGES=(razzfazz-llm-worker-agent)
if [ -z "$REGISTRY" ]; then
    port="$(sed -n 's/^LLM_REGISTRY_PORT=//p' "$ROOT/.env" 2>/dev/null | head -1 || true)"
    REGISTRY="http://127.0.0.1:${port:-8093}"
fi
REGISTRY="${REGISTRY%/}"; case "$REGISTRY" in *://*) ;; *) REGISTRY="https://${REGISTRY}" ;; esac

ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
_get() {  # $1 path [accept]; the hub login travels on stdin, never on argv
    local cfg=""
    [ -z "${RZFZ_HUB_USER:-}" ] || cfg="user = \"${RZFZ_HUB_USER}:${RZFZ_HUB_PASSWORD:-}\""
    printf '%s\n' "$cfg" | curl -fsSL -K - ${CACERT:+--cacert "$CACERT"} --max-time 30 \
        -H "Accept: ${2:-$ACCEPT}" "${REGISTRY}$1"
}
_py() { python3 -I -c "$1" "${@:2}"; }

rc=0
fail() { echo "  FAIL $*"; rc=1; }

# One reference (tag or digest) -> "<arch> <version>" lines, one per image in it.
resolve() {  # $1 repo  $2 reference  -> lines "ARCH VERSION" (VERSION '-' if absent)
    local repo="$1" ref="$2" doc kind cfgd d
    doc="$(_get "/v2/${repo}/manifests/${ref}")" || return 3
    kind="$(printf '%s' "$doc" | _py 'import json,sys
d=json.load(sys.stdin)
print("index" if "manifests" in d else "image")')" || return 3
    if [ "$kind" = index ]; then
        for d in $(printf '%s' "$doc" | _py 'import json,sys
for m in json.load(sys.stdin)["manifests"]:
    p=m.get("platform") or {}
    if p.get("architecture") in (None,"unknown"): continue
    print(m["digest"])'); do
            resolve "$repo" "$d" || return 3
        done
        return 0
    fi
    cfgd="$(printf '%s' "$doc" | _py 'import json,sys; print(json.load(sys.stdin)["config"]["digest"])')" || return 3
    _get "/v2/${repo}/blobs/${cfgd}" | _py 'import json,sys
c=json.load(sys.stdin)
v="-"
for e in (c.get("config") or {}).get("Env") or []:
    if e.startswith("RAZZFAZZ_IMAGE_VERSION="): v=e.split("=",1)[1] or "-"
print(c.get("architecture","?"), v)'
}

check_tag() {  # $1 repo  $2 tag  $3 required arch list ("amd64 arm64" | "arm64" | "")
    local repo="$1" t="$2" want="$3" lines line a v seen=" " r
    echo "${repo}:${t}"
    if ! lines="$(resolve "$repo" "$t")"; then
        fail "${repo}:${t} is not readable on the hub (${REGISTRY})"; return
    fi
    while read -r a v; do
        [ -n "$a" ] || continue
        seen="${seen}${a} "
        if [ "$v" = "$EXPECT" ]; then echo "  ok   ${a}: ${v}"
        else fail "${a}: RAZZFAZZ_IMAGE_VERSION=${v}, expected ${EXPECT} - the hub has no ${a} agent for ${EXPECT}"; fi
    done <<<"$lines"
    for r in $want; do
        case "$seen" in *" $r "*) ;; *) fail "no ${r} image under ${repo}:${t}" ;; esac
    done
    if [ -n "$want" ] && [ "$(wc -w <<<"$want")" = 1 ]; then
        # a per-arch tag must hold exactly that architecture
        [ "$seen" = " $want " ] || fail "${repo}:${t} holds [${seen# }] , expected only ${want}"
    fi
}

all="amd64 arm64"; [ -z "$ALLOW_SINGLE" ] || all=""
for img in "${IMAGES[@]}"; do
    repo="node/${img}"
    check_tag "$repo" "$TAG" "$all"
    for a in amd64 arm64; do
        # the arch-suffixed tags exist when the list was built from them
        if _get "/v2/${repo}/manifests/${TAG}-${a}" >/dev/null 2>&1; then
            check_tag "$repo" "${TAG}-${a}" "$a"
        elif [ -z "$ALLOW_SINGLE" ]; then
            echo "${repo}:${TAG}-${a}"; fail "tag ${TAG}-${a} is missing"
        fi
    done
done
if [ "$rc" = 0 ]; then echo "verify-hub-images: every image under ${TAG} is release ${EXPECT}."
else echo "verify-hub-images: FAILED - the hub serves an image that is not release ${EXPECT}. Re-run 'rzfz post-install --refresh' on the master (it needs arm64 emulation: docker run --privileged --rm tonistiigi/binfmt --install arm64)." >&2; fi
exit "$rc"
