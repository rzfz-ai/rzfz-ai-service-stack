#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# lint-compose.sh — extra_hosts regression watchdog (BSB-10).
#
# Background
# ----------
# Until 2026-05-11 the stack injected `extra_hosts: "<DOMAIN>:<CADDY_IP>"`
# into every consumer that needed to reach a public hostname (Authentik
# OIDC discovery, Matrix self-issued OIDC redirect URIs, Vaultwarden SSO,
# …). The CADDY_IP came from a runtime detection step in razzfazz-init.sh
# / profile_provisioner.py — and broke every time Caddy's bridge IP
# changed (which happens on every container recreate). We burned 2 h on
# this when vaultwarden + matrix silently lost their SSO path.
#
# The fix: Caddy now carries the public hostnames as Docker network
# aliases on the default bridge (see core/compose.yml service `caddy`,
# `networks.default.aliases`). Docker's embedded DNS resolves them to
# whatever IP Caddy currently holds — survives Caddy restarts without
# any static-IP detection. Consumers no longer need an extra_hosts
# entry at all.
#
# This linter asserts NO compose file regresses to the old pattern.
# It is READ-ONLY: parses + asserts; never modifies compose files.
#
# Bad pattern (FAIL):
#   extra_hosts:
#     - "${AUTHENTIK_DOMAIN}:172.18.0.5"
#     - "auth.example.com:${CADDY_IP}"
#
# Good pattern (PASS — Docker-native gateway, unrelated to CADDY_IP):
#   extra_hosts:
#     - "host.docker.internal:host-gateway"
#
# Usage:
#   bash scripts/lint-compose.sh
#
# Exit codes:
#   0 — no regressions
#   1 — at least one bad extra_hosts entry detected
#
# Wired into:
#   - scripts/prepare-release.sh (release gate, Check 14)
#   - operator may also install as a git pre-commit hook

set -eo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

# Discovery: same pattern as scripts/lint-ports.sh — git-tracked
# compose.yml files when in a git tree, fall back to a filesystem
# scan in extracted offline packages.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    mapfile -t composes < <(git ls-files '**/compose.yml' 'compose.yml' 2>/dev/null)
else
    mapfile -t composes < <(find . -name compose.yml \
        -not -path './.git/*' -not -path './.claude/*' \
        -not -path './node_modules/*' -not -path './backups/*' \
        -not -path './security-run/*' 2>/dev/null)
fi

if [ "${#composes[@]}" -eq 0 ]; then
    echo "lint-compose: no compose.yml files found — nothing to check"
    exit 0
fi

EXIT=0
SCANNED=0
BAD_ENTRIES=()

# A "bad entry" inside a top-level extra_hosts: list is one whose
# left-of-colon resolves to a *_DOMAIN reference OR contains ${CADDY_IP}.
# We do a lightweight YAML walk: track the current top-level key under
# a service block, and only assess `- "..."` lines while we're inside
# `extra_hosts:`. Comments and lines outside extra_hosts blocks are
# ignored — those are just narrative.
for f in "${composes[@]}"; do
    [ -f "$f" ] || continue
    SCANNED=$((SCANNED + 1))

    # Use awk to walk each file once. Emits "FILE:LINENO:CONTENT" for
    # every offending list-item line under an extra_hosts: block.
    while IFS= read -r hit; do
        [ -z "$hit" ] && continue
        BAD_ENTRIES+=("$hit")
        EXIT=1
    done < <(awk '
        BEGIN { in_eh = 0; eh_indent = -1 }
        {
            line = $0
            # strip trailing CR for safety
            sub(/\r$/, "", line)
            # Skip pure-comment lines for state tracking but preserve
            # line numbering via NR.
            if (line ~ /^[[:space:]]*#/) next

            # Compute leading whitespace (in spaces; tabs counted as 1
            # which is fine — compose files use spaces).
            indent = match(line, /[^[:space:]]/) - 1

            if (in_eh) {
                # Are we still inside the extra_hosts block?
                # We exit when we hit a non-list line at indent <= eh_indent.
                if (line ~ /^[[:space:]]*-[[:space:]]/) {
                    # Extract the value between quotes (or bare).
                    val = line
                    sub(/^[[:space:]]*-[[:space:]]*/, "", val)
                    gsub(/^["'\'']|["'\'']$/, "", val)
                    # Strip trailing comment.
                    sub(/[[:space:]]*#.*$/, "", val)
                    # Get the host part (before the first colon).
                    host = val
                    sub(/:.*$/, "", host)
                    rest = val
                    sub(/^[^:]*:/, "", rest)

                    # Allowed: host.docker.internal:host-gateway.
                    if (host == "host.docker.internal" && rest == "host-gateway") {
                        next
                    }
                    # Bad: host part references a *_DOMAIN env var.
                    if (host ~ /\$\{[A-Z0-9_]*DOMAIN\}/) {
                        printf "%s:%d: BAD extra_hosts entry (CADDY_IP-style domain injection): %s\n", FILENAME, NR, line
                        next
                    }
                    # Bad: rhs uses ${CADDY_IP}.
                    if (rest ~ /\$\{CADDY_IP\}/) {
                        printf "%s:%d: BAD extra_hosts entry (CADDY_IP variable usage): %s\n", FILENAME, NR, line
                        next
                    }
                    # Bare-IP entry against a literal hostname is also
                    # suspect IF the hostname looks like a public domain
                    # (contains a dot). We allow purely internal aliases
                    # like "some-internal-name:1.2.3.4" to keep this lint
                    # narrowly-scoped, but flag literal FQDNs which are
                    # the legacy CADDY_IP pattern with the env-var
                    # already substituted.
                    if (host ~ /\./ && rest ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {
                        printf "%s:%d: BAD extra_hosts entry (literal FQDN→IP — CADDY_IP pattern): %s\n", FILENAME, NR, line
                        next
                    }
                    next
                }
                # Non-list line: are we still nested deeper than the
                # extra_hosts: key? If indent dropped to <= eh_indent we
                # are out of the block.
                if (indent >= 0 && indent <= eh_indent && line !~ /^[[:space:]]*$/) {
                    in_eh = 0
                    eh_indent = -1
                }
            }
            # Detect entry into an extra_hosts: block.
            if (line ~ /^[[:space:]]*extra_hosts:[[:space:]]*$/) {
                in_eh = 1
                eh_indent = indent
            }
        }
    ' "$f")
done

if [ "$EXIT" -ne 0 ]; then
    {
        echo "lint-compose: FAIL — ${#BAD_ENTRIES[@]} regression(s) detected:"
        echo
        for e in "${BAD_ENTRIES[@]}"; do
            echo "  $e"
        done
        echo
        echo "These compose files reintroduce the deprecated CADDY_IP"
        echo "extra_hosts pattern. Caddy carries public hostnames as"
        echo "Docker network aliases (see core/compose.yml). Remove the"
        echo "extra_hosts entry; consumers reach the host via embedded DNS."
        echo "If the entry is host.docker.internal:host-gateway, that's"
        echo "the Docker-native gateway pattern and is allowed."
    } >&2
    exit 1
fi

echo "lint-compose: OK ($SCANNED compose file(s) scanned, no extra_hosts regressions)"
exit 0
