#!/usr/bin/env bash
# Validate core/Caddy/Caddyfile with the REAL Caddy binary. (#455)
#
# WHY. Caddy is the single external entry point on every box: one syntax error
# or one directive with a missing argument and NOTHING is reachable — every
# domain, on every install. CI lints the release docs and the command reference,
# but until now nothing checked the file that decides whether the box answers at
# all.
#
# It has to be the CUSTOM image. Stock `caddy:2-alpine` rejects our Caddyfile
# outright ("forward_proxy is not a registered directive") because the ingress
# is built with xcaddy against caddyserver/forwardproxy + mholt/caddy-ratelimit,
# so a stock binary cannot tell a real error from a plugin it has never heard of.
# `caddy fmt` is not a substitute either: it exits non-zero on our valid file
# purely because the file is not gofmt-style formatted (2625 diff lines), so it
# cannot distinguish "malformed" from "unformatted".
#
# PLACEHOLDERS. The Caddyfile is templated with {$VAR}. `caddy validate` needs
# them resolvable, so this derives the full list FROM THE FILE and supplies a
# syntactically plausible value for each. Deriving beats a hard-coded list: a
# new {$VAR} would otherwise silently validate against nothing.
#
# One subtlety worth keeping: a var is left UNSET rather than set empty when the
# Caddyfile gives it a `{$VAR:default}` fallback, because the fallback applies
# only when unset. Setting it empty is a different thing entirely — it yields a
# bare `allow` with no argument, which is exactly the startup crash the comment
# at core/Caddy/Caddyfile:1470 warns about. This script reproduces and catches
# that, which is the whole point of having it.
#
# Usage:  scripts/validate-caddyfile.sh [path/to/Caddyfile]
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
CADDYFILE="${1:-core/Caddy/Caddyfile}"
IMAGE="razzfazz-caddy-validate:local"

if [ ! -f "$CADDYFILE" ]; then
    echo "ERROR: $CADDYFILE not found" >&2
    exit 2
fi

if ! docker info >/dev/null 2>&1; then
    echo "SKIP: docker unavailable — cannot validate the Caddyfile." >&2
    echo "      This is a real gap, not a pass. Run where docker is reachable." >&2
    exit 3          # distinct from failure: could not check
fi

# Build the plugin-carrying binary. Cached after the first run; the build is
# the only reason this is not instant.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE (xcaddy + forwardproxy + ratelimit) ..."
    if ! docker build -q -t "$IMAGE" core/Caddy >/dev/null; then
        echo "ERROR: could not build the custom Caddy image." >&2
        exit 2
    fi
fi

# --- derive every {$VAR} the file references --------------------------------
envfile="$(mktemp)"
trap 'rm -f "$envfile"' EXIT

while IFS= read -r var; do
    [ -n "$var" ] || continue
    # A var WITH a {$VAR:default} fallback must stay unset — see header.
    if grep -q "{\$${var}:" "$CADDYFILE"; then
        continue
    fi
    case "$var" in
        *_DIRECTIVE)                 echo "${var}=" ;;
        TLS_DIRECTIVE)               echo "${var}=tls internal" ;;
        *WINDOW*)                    echo "${var}=1m" ;;
        *MAX_CONCURRENT*|*PORT*)     echo "${var}=8080" ;;
        *DOMAIN*)                    echo "${var}=$(echo "$var" | tr 'A-Z_' 'a-z-').example.test" ;;
        *)                           echo "${var}=validate.example.test" ;;
    esac
done < <(grep -oE '.\$[A-Z0-9_]+' "$CADDYFILE" | sed 's/.*\$//' | sort -u) > "$envfile"

echo "validating $CADDYFILE ($(grep -c . "$envfile") placeholders supplied) ..."
out="$(docker run --rm --entrypoint caddy --env-file "$envfile" \
        -v "$PWD/$CADDYFILE":/etc/caddy/Caddyfile:ro \
        "$IMAGE" validate --config /etc/caddy/Caddyfile 2>&1)"
rc=$?

if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "Valid configuration"; then
    echo "  ✓ Caddyfile is valid"
    exit 0
fi

echo "  ✗ Caddyfile is INVALID — every domain on every box would be unreachable:" >&2
printf '%s\n' "$out" | grep -vE '"level":"info"' | tail -6 | sed 's/^/      /' >&2
exit 1
