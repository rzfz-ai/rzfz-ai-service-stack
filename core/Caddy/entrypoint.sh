#!/bin/sh
# ==============================================================================
# Caddy Entrypoint - Derives TLS_DIRECTIVE from TLS_MODE
# ==============================================================================
# This script ensures TLS_DIRECTIVE is always correctly set based on TLS_MODE,
# even if TLS_DIRECTIVE is missing or empty in .env. It also derives
# ACME_EMAIL_DIRECTIVE so an empty LETSENCRYPT_EMAIL never crash-loops Caddy
# under non-ACME TLS modes (#60).
#
# TLS_MODE values:
#   "internal"    → TLS_DIRECTIVE="tls internal"           (self-signed certificates)
#   "certificate" → TLS_DIRECTIVE="tls /certs/cert.pem ..." (custom wildcard certificate)
#   "" (empty)    → TLS_DIRECTIVE=""                        (Let's Encrypt / ACME)
#
# TLS_WILDCARD_ISSUER (#1594) is derived HERE for the same reason TLS_DIRECTIVE
# is: .env can be stale, and a box that UPGRADES gets whatever the migration
# wrote once. The two WILDCARD vHosts (*.agents, *.mcp) cannot use the
# certificate path — a wildcard covers ONE label, so `*.<domain>` does not match
# `<user>.agents.<domain>`, and ACME can only validate a wildcard by dns-01,
# for which no plugin ships. Measured on a customer box: Caddy retried for
# `max_duration: 2592000` — thirty days.
#   internal | certificate → "issuer internal" (Caddy's local CA; these hosts
#                            are per-user proxies behind Authentik)
#   "" (ACME)              → "on_demand"       (per-name HTTP-01 on first visit)
# Deriving it from TLS_MODE at every start is what makes an UPGRADED box right
# without a per-box handgrip — a reviewer measured that the .env default alone
# left exactly the boxes this issue is about unchanged.
#
# ACME_EMAIL_DIRECTIVE (injected into the Caddyfile global block):
#   ACME + non-empty LETSENCRYPT_EMAIL → "email <addr>"
#   otherwise (certificate/internal, or ACME w/o email) → "" (harmless)
# ==============================================================================

# ── Caddy hygiene: stale ACME lock cleanup (B-9 / M031-FOLLOWUPS C1) ─────────
# Caddy holds an `issue_cert_<host>.lock` while it's issuing or renewing a
# cert. If Caddy is killed mid-issuance (container restart, OOM, host reboot,
# upstream ACME timeout), the lock file survives and blocks every subsequent
# attempt for that hostname until manually removed. Manual fix used to be
# `rm -f /data/caddy/locks/issue_cert_*.lock` + `compose restart caddy` —
# documented in memory `project_caddy_stale_acme_lock_blocks_renewal.md`
# and BLIND-SPOTS B-9. Now automated here.
#
# Safety:
#   - This runs at container START, BEFORE `exec caddy run`. There is no
#     live Caddy holding a real lock at this point, so we cannot race.
#   - We only consider locks OLDER than CADDY_ACME_LOCK_MAX_AGE_SECONDS
#     (default 3600 = 1 h). A genuine in-flight issuance never lives that
#     long; anything older is by definition orphaned from a prior crash.
#   - We only touch files matching `issue_cert_*.lock`. Other files in
#     the locks dir (if Caddy ever adds new lock kinds) are untouched.
#
# Test hooks:
#   - CADDY_LOCKS_DIR overrides /data/caddy/locks (used by test harness).
#   - CADDY_CERTS_DIR overrides /data/caddy/certificates/local.
#   - CADDY_DRY_RUN_NO_EXEC=1 makes the script return instead of exec'ing
#     caddy — used so the cleanup logic can be exercised under pytest
#     without needing the caddy binary.
CADDY_LOCKS_DIR="${CADDY_LOCKS_DIR:-/data/caddy/locks}"
CADDY_CERTS_DIR="${CADDY_CERTS_DIR:-/data/caddy/certificates/local}"
CADDY_ACME_LOCK_MAX_AGE_SECONDS="${CADDY_ACME_LOCK_MAX_AGE_SECONDS:-3600}"

if [ -d "$CADDY_LOCKS_DIR" ]; then
    # Build the candidate list ourselves so we can age-filter portably.
    # `find -mmin` would round to whole minutes; we want second-granularity
    # so test_max_age_env_var_honored (10 s threshold) works deterministically.
    now=$(date +%s)
    removed=0
    kept_fresh=0
    for lock in "$CADDY_LOCKS_DIR"/issue_cert_*.lock; do
        [ -e "$lock" ] || continue   # no matches → glob stays literal
        # Get mtime in epoch seconds; portable across BusyBox + GNU stat.
        lock_mtime=$(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo 0)
        age=$((now - lock_mtime))
        if [ "$age" -ge "$CADDY_ACME_LOCK_MAX_AGE_SECONDS" ]; then
            echo "[entrypoint] removing stale ACME lock $(basename "$lock") (age ${age}s ≥ ${CADDY_ACME_LOCK_MAX_AGE_SECONDS}s)"
            rm -f "$lock"
            removed=$((removed + 1))
        else
            kept_fresh=$((kept_fresh + 1))
        fi
    done
    if [ "$removed" -gt 0 ]; then
        echo "[entrypoint] ACME lock cleanup: removed $removed stale lock(s); kept $kept_fresh fresh"
    elif [ "$kept_fresh" -gt 0 ]; then
        echo "[entrypoint] ACME lock cleanup: no stale locks found ($kept_fresh fresh lock(s) preserved)"
    else
        echo "[entrypoint] ACME lock cleanup: no stale locks found (locks dir empty)"
    fi
else
    # Locks dir absent on a fresh install — nothing to clean.
    :
fi

# C2: a per-host cert dir with .crt but no .key is a half-issued state from
# an interrupted issuance. Caddy's renewal logic looks for the missing key
# and loops forever. Wipe such dirs so on-demand TLS re-issues cleanly.
if [ -d "$CADDY_CERTS_DIR" ]; then
    for d in "$CADDY_CERTS_DIR"/*/; do
        [ -d "$d" ] || continue
        crt=$(ls "$d"*.crt 2>/dev/null | head -1)
        key=$(ls "$d"*.key 2>/dev/null | head -1)
        if [ -n "$crt" ] && [ -z "$key" ]; then
            echo "[entrypoint] cert dir $(basename "$d") has no .key — wiping for re-issuance"
            rm -rf "$d"
        fi
    done
fi

# Internal (self-signed) certs from Caddy's local CA have a FIXED ~12h leaf
# lifetime — the `lifetime` knob on the internal issuer parses but does NOT
# extend the leaf (verified on 0.91: cert stays 12h). That's fine as long as the
# host clock is correct: Caddy renews at ~6h, no gap. The recurring breakage on
# VMs (Multipass) where start.<domain> shows a cert error — mis-read as "lost
# the outpost" — is a CLOCK JUMP on host suspend/resume making the 12h cert look
# expired. The fix lives in scripts/harden-host.sh (chrony `makestep 1 -1`,
# step-corrects the clock), NOT here. Keep this directive plain.
case "$TLS_MODE" in
    internal|selfsigned|self-signed)
        export TLS_DIRECTIVE="tls internal"
        export TLS_WILDCARD_ISSUER="issuer internal"
        echo "[entrypoint] TLS_MODE=$TLS_MODE → Using self-signed certificates (tls internal)"
        ;;
    certificate|custom)
        CERT_FILE="${TLS_CERT_PATH:-/certs/cert.pem}"
        KEY_FILE="${TLS_KEY_PATH:-/certs/key.pem}"
        if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
            echo "[entrypoint] ERROR: TLS_MODE=certificate but certificate files not found!"
            echo "[entrypoint]   Expected cert: $CERT_FILE"
            echo "[entrypoint]   Expected key:  $KEY_FILE"
            echo "[entrypoint]   Place your wildcard certificate files in ./certs/ on the host."
            echo "[entrypoint]   Falling back to self-signed certificates."
            export TLS_DIRECTIVE="tls internal"
            export TLS_WILDCARD_ISSUER="issuer internal"
        else
            export TLS_DIRECTIVE="tls $CERT_FILE $KEY_FILE"
            export TLS_WILDCARD_ISSUER="issuer internal"
            echo "[entrypoint] TLS_MODE=certificate → Using custom certificate ($CERT_FILE)"
        fi
        ;;
    *)
        # If TLS_MODE is empty/unset → Let's Encrypt (ACME)
        # Force TLS_DIRECTIVE empty regardless of what env_file set
        export TLS_DIRECTIVE=""
        export TLS_WILDCARD_ISSUER="on_demand"
        if [ -z "$TLS_MODE" ]; then
            echo "[entrypoint] TLS_MODE is empty → Using Let's Encrypt (ACME)"
        else
            echo "[entrypoint] TLS_MODE=$TLS_MODE (unknown) → Defaulting to Let's Encrypt (ACME)"
        fi
        ;;
esac

# ── #60: derive ACME_EMAIL_DIRECTIVE from TLS_MODE + LETSENCRYPT_EMAIL ───────
# The Caddyfile global block injects `{$ACME_EMAIL_DIRECTIVE:}` instead of a
# hard-coded `email {$LETSENCRYPT_EMAIL}`. That old form expanded to a bare
# `email` (no argument) whenever LETSENCRYPT_EMAIL was empty — which is the
# LEGITIMATE case under TLS_MODE=certificate (operator-provided signed cert)
# and TLS_MODE=internal (self-signed), where ACME/Let's-Encrypt isn't used.
# A bare `email` fails Caddyfile adaptation ("wrong argument count") and Caddy
# crash-loops. We now only emit the `email <addr>` line when ACME is actually
# in use (TLS_MODE empty/unknown → TLS_DIRECTIVE empty) AND the address is
# non-empty; otherwise the directive is empty and harmless.
#
# Mirrors the existing TLS_DIRECTIVE env-injection pattern.
_le_email="${LETSENCRYPT_EMAIL:-}"
# Trim surrounding whitespace so a whitespace-only value counts as empty.
_le_email="$(printf '%s' "$_le_email" | tr -d '[:space:]')"
if [ -z "$TLS_DIRECTIVE" ] && [ -n "$_le_email" ]; then
    # ACME path with a real address → set the account email.
    export ACME_EMAIL_DIRECTIVE="email $_le_email"
    echo "[entrypoint] ACME account email set (LETSENCRYPT_EMAIL=$_le_email)"
else
    # Non-ACME (certificate/internal) OR ACME with no email → no directive.
    export ACME_EMAIL_DIRECTIVE=""
    if [ -z "$TLS_DIRECTIVE" ]; then
        echo "[entrypoint] ACME in use but LETSENCRYPT_EMAIL empty → no account email (zero-config ACME)"
    else
        echo "[entrypoint] Non-ACME TLS → LETSENCRYPT_EMAIL ignored (no email directive)"
    fi
fi

# #253 X5 S2: Caddy's admin-API /metrics is OFF unless the `metrics` server
# option is set. Emit it ONLY when the observability metrics overlay that
# scrapes Caddy is selected — a box that does not export metrics pays no
# collection cost and exposes nothing extra (the admin API is docker-network
# only either way). Mirrors the TLS_DIRECTIVE / ACME_EMAIL_DIRECTIVE pattern:
# the Caddyfile injects {$CADDY_METRICS_DIRECTIVE:} into the servers block.
#
# #2571: the collector no longer reads /metrics from the admin API. The admin
# API is unauthenticated and must stay off the networks user code runs on, so
# the scrape goes to a SEPARATE listener, caddy:2020, that serves only the
# `metrics` handler on /metrics and 404 everywhere else (no /config, no /load).
# CADDY_METRICS_SITE is that whole site block; Caddy expands `{$VAR}` before it
# parses, so a multi-line value becomes a site. Empty when the overlay is off,
# so a box that does not scrape has no :2020 listener at all.
case "${OTEL_COLLECTOR_METRICS_OVERLAY:-}" in
    *scrape*)
        export CADDY_METRICS_DIRECTIVE="metrics"
        CADDY_METRICS_SITE=$(printf 'http://:2020 {\n\troute {\n\t\thandle /metrics {\n\t\t\tmetrics\n\t\t}\n\t\thandle {\n\t\t\trespond 404\n\t\t}\n\t}\n}')
        export CADDY_METRICS_SITE
        echo "[entrypoint] metrics overlay '${OTEL_COLLECTOR_METRICS_OVERLAY}' selected → Caddy metrics ON, served on :2020/metrics only"
        ;;
    *)
        export CADDY_METRICS_DIRECTIVE=""
        export CADDY_METRICS_SITE=""
        ;;
esac

# ── #2526: the minimal access log for Wazuh ──────────────────────────────────
# CADDY_ACCESS_LOG: auto (default) = on when Wazuh is enabled on this box (the
# `wazuh` profile, or WAZUH_AGENT_AUTO_ENROLL=true); true/false force it. On,
# every stack site imports the `rzfz_access_log` snippet (status, method, host,
# path, client IP, duration - no query string, no headers), written to
# /var/log/caddy/access.log, which the host's Wazuh agent reads.
_access_log_auto() {
    # spaces are allowed around the commas ("chat, wazuh")
    _profiles=$(printf '%s' "${COMPOSE_PROFILES:-}" | tr -d ' \t')
    case ",${_profiles}," in *,wazuh,*) return 0 ;; esac
    [ "${WAZUH_AGENT_AUTO_ENROLL:-false}" = "true" ]
}
_access_log="${CADDY_ACCESS_LOG:-auto}"
_access_log_on=0
case "$_access_log" in
    true|on|yes|1)   _access_log_on=1 ;;
    false|off|no|0)  _access_log_on=0 ;;
    *)
        [ "$_access_log" = auto ] \
            || echo "[entrypoint] WARN: CADDY_ACCESS_LOG='$_access_log' is not auto/true/false - treated as auto" >&2
        _access_log_auto && _access_log_on=1
        ;;
esac
if [ "$_access_log_on" = 1 ]; then
    export CADDY_ACCESS_LOG_DIRECTIVE="import rzfz_access_log"
    echo "[entrypoint] minimal access log ON (CADDY_ACCESS_LOG=$_access_log) -> /var/log/caddy/access.log"
else
    export CADDY_ACCESS_LOG_DIRECTIVE=""
fi

# ── #2920: an optional site block is only LIVE when its module is enabled ────
# Every site block in the Caddyfile is always parsed, so a name whose module is
# off still got a site address - and under Let's Encrypt Caddy then tried to get
# a certificate for it, about 70 failed ACME attempts an hour for a name with no
# public DNS (gpustack.<domain>, matrix on a box without Matrix). A gated key
# whose profile is NOT in COMPOSE_PROFILES is therefore exported as an INERT name
# `<label>.off.internal`: `.internal` never qualifies for a public certificate
# (Caddy signs it with its own CA, no ACME), and nothing resolves or links to it.
# The block stays in the file; it just answers on a name nobody can reach.
# Enabling the module (profile in COMPOSE_PROFILES) restores the .env value.
#   KEY:PROFILE   - KEY's site block belongs to PROFILE
#     GPUSTACK_DOMAIN                 llm-legacy  (the optional GPUStack backend)
#     MATRIX_DOMAIN, ELEMENT_WEB_DOMAIN matrix    (and the apex .well-known)
# Retired names (the llm-manager alias after the cutover) are not here: they
# are removed from the Caddyfile. Always-on services need no gate.
_gated_profiles=$(printf '%s' "${COMPOSE_PROFILES:-}" | tr -d ' \t')
for _gate in GPUSTACK_DOMAIN:llm-legacy MATRIX_DOMAIN:matrix ELEMENT_WEB_DOMAIN:matrix; do
    _gk="${_gate%%:*}"
    _gp="${_gate#*:}"
    case ",${_gated_profiles}," in
        *,"${_gp}",*) ;;
        *)
            _glabel=$(printf '%s' "$_gk" | sed 's/_DOMAIN$//' | tr 'A-Z_' 'a-z-')
            export "$_gk=${_glabel}.off.internal"
            echo "[entrypoint] #2920: profile '${_gp}' is not enabled -> ${_gk} is inert (${_glabel}.off.internal), no certificate is requested for it"
            ;;
    esac
done

# APEX_TLS=internal: the apex ({$MAIN_DOMAIN}) block serves Matrix discovery, the
# CA download and the worker install on a box whose apex DNS points HERE. Where
# the apex points elsewhere (the company website), public ACME for it can never
# succeed; `tls internal` stops the retries. Only meaningful under ACME: with
# TLS_MODE=internal/certificate the apex already has its directive.
if [ "${APEX_TLS:-}" = "internal" ] && [ -z "$TLS_DIRECTIVE" ]; then
    export APEX_TLS_DIRECTIVE="tls internal"
    echo "[entrypoint] #2920: APEX_TLS=internal -> the apex site uses Caddy's own CA (no ACME for ${MAIN_DOMAIN:-the apex})"
else
    export APEX_TLS_DIRECTIVE=""
fi

# ── #2117: a `*_DOMAIN` key the Caddyfile references must never be EMPTY ─────
# Caddy expands an unset `{$VAR}` to the empty string, so a site block that
# opens as `{$X_DOMAIN} {` becomes a bare `{` — Caddy's syntax for the GLOBAL
# OPTIONS block, legal only first in the file. `adapt` then fails with
# "server block without any key is global configuration, and if used, it must
# be first" and NOT ONE site is served: one stale .env key takes the whole
# box's ingress down (11,657 restarts on the agent dev box; scenario 05 on
# 0.175). Measured causes: a .env that predates a key (SETTINGS_DOMAIN,
# LLM_MANAGER_DOMAIN, …) and never had it added, or a half-written .env at
# first start.
#
# Rule: every site-address key the Caddyfile references resolves NON-EMPTY
# before caddy reads it. A missing/empty `X_Y_DOMAIN` gets the STATED fallback
# `x-y.${MAIN_DOMAIN}` (the .env.example convention for all but two keys) and
# a loud line naming the key and the value, so the other ~30 sites serve and
# the operator can put the intended value in .env. Without a MAIN_DOMAIN there
# is nothing to derive from: refuse, naming every key, instead of letting
# caddy fail with the global-block message that names none.
#   CADDYFILE_PATH   test hook (default /etc/caddy/Caddyfile, the bind mount)
CADDYFILE_PATH="${CADDYFILE_PATH:-/etc/caddy/Caddyfile}"
if [ -f "$CADDYFILE_PATH" ]; then
    # Site-address lines only: `{$KEY} {`, `*.{$KEY} {`, `{$A}, {$B} {`,
    # `sub.{$MAIN_DOMAIN} {`. A `{$KEY:default}` form already has its own
    # fallback and is not matched (the `:` is excluded from the key class).
    _site_keys=$(grep -E '^(\*\.|[a-z0-9-]+\.)?\{\$[A-Z0-9_]+[}:].*\{[[:space:]]*$' "$CADDYFILE_PATH" \
        | grep -oE '\{\$[A-Z0-9_]+\}' | tr -d '{}$' | sort -u)
    # #2476 (A18, 0.91): `{$KEY:default}` falls back to its default only when
    # KEY is UNSET. A KEY that is set but EMPTY (`OPENBAO_DOMAIN=` in .env, which
    # compose passes through as "") expands to "" — the block is keyless again
    # and caddy refuses the whole file ("server block without any key"). An
    # empty default-form site key is therefore made unset, so its default holds.
    _default_keys=$(grep -E '^(\*\.|[a-z0-9-]+\.)?\{\$[A-Z0-9_]+[}:].*\{[[:space:]]*$' "$CADDYFILE_PATH" \
        | grep -oE '\{\$[A-Z0-9_]+:[^}]+\}' | sed 's/^{\$//; s/:.*//' | sort -u)
    for _k in $_default_keys; do
        # printenv is 0 for an exported-but-empty variable, 1 for an unset one
        # (no `${$_k+x}`: bash-only under BusyBox ash, test_1148).
        _v=""; eval "_v=\$$_k"
        if [ -z "$_v" ] && printenv "$_k" >/dev/null 2>&1; then
            unset "$_k"
            echo "[entrypoint] WARN(#2476): $_k is set but empty → using the Caddyfile default for its site address"
        fi
    done
    _missing=""
    for _k in $_site_keys; do
        # `eval "_v=\$$_k"`: the value of the variable named by $_k, in a form
        # BusyBox ash accepts (`${$_k:-}` is bash-only, test_1148).
        _v=""; eval "_v=\$$_k"
        [ -n "$_v" ] && continue
        _missing="$_missing $_k"
    done
    if [ -n "$_missing" ]; then
        if [ -z "${MAIN_DOMAIN:-}" ]; then
            echo "[entrypoint] ERROR(#2117): MAIN_DOMAIN is empty and the Caddyfile references unset site keys:$_missing"
            echo "[entrypoint]   Set MAIN_DOMAIN (and the keys above) in .env — an empty site key makes caddy refuse the WHOLE config."
            exit 1
        fi
        for _k in $_missing; do
            # SETTINGS_DOMAIN → settings, LLM_MANAGER_DOMAIN → llm-manager
            _label=$(printf '%s' "$_k" | sed 's/_DOMAIN$//' | tr 'A-Z_' 'a-z-')
            _fallback="${_label}.${MAIN_DOMAIN}"
            export "$_k=$_fallback"
            echo "[entrypoint] WARN(#2117): $_k is unset/empty in .env → serving it as $_fallback (stated fallback; put the intended value in .env)"
        done
    fi
else
    echo "[entrypoint] Caddyfile not found at $CADDYFILE_PATH — site-key check skipped"
fi

# ── #2675: the Dify SSRF proxy's (:8195) box-address deny and operator allow ─
# Computed by core/Caddy/ssrf-box.sh, the same code that renders the squid
# chain replacing :8195 on a corporate-proxy box (modules/dify/ssrf-chain/):
#   DIFY_SSRF_DENY_BOX          the box's own addresses (HOST_IP and what
#                               MAIN_DOMAIN and the *_DOMAIN names under it,
#                               never *_OAUTH_DOMAIN, resolve to, or the operator's
#                               DIFY_SSRF_DENY_BOX_OVERRIDE, plus
#                               DIFY_SSRF_DENY_BOX_EXTRA), so a workflow cannot
#                               hairpin to a port the host publishes;
#   DIFY_SSRF_ALLOW_RANGES_VALID the operator's DIFY_SSRF_ALLOW_RANGES, validated
#                               (CGNAT/benchmark sub-ranges only).
# Caddy is the whole ingress: nothing here refuses to start. Both are ALWAYS
# exported, never empty (an empty value expands to a bare `deny`/`allow` and
# Caddy refuses the file); `disabled.invalid` is a hostname rule matching
# nothing.
_ssrf_lib="$(dirname "$0")/ssrf-box.sh"
[ -f "$_ssrf_lib" ] || _ssrf_lib=/ssrf-box.sh
_box=""
_allow=""
if [ -f "$_ssrf_lib" ]; then
    # shellcheck source=core/Caddy/ssrf-box.sh
    . "$_ssrf_lib"
    _box=$(ssrf_box_list)
    _allow=$(ssrf_allow_ranges)
else
    echo "[entrypoint] WARN(#2675): $_ssrf_lib missing: the Dify SSRF proxy does not deny the box's own addresses"
fi
if [ -n "$_box" ]; then
    export DIFY_SSRF_DENY_BOX="$_box"
    echo "[entrypoint] Dify SSRF proxy also denies the box's own addresses: $DIFY_SSRF_DENY_BOX"
else
    export DIFY_SSRF_DENY_BOX="disabled.invalid"
    echo "[entrypoint] WARN(#2675): no usable box address (HOST_IP, *_DOMAIN): the Dify SSRF proxy does not deny the box's own addresses"
fi
if [ -n "$_allow" ]; then
    export DIFY_SSRF_ALLOW_RANGES_VALID="$_allow"
    echo "[entrypoint] Dify SSRF proxy allows the operator's ranges (DIFY_SSRF_ALLOW_RANGES): $DIFY_SSRF_ALLOW_RANGES_VALID"
else
    export DIFY_SSRF_ALLOW_RANGES_VALID="disabled.invalid"
fi

if [ -n "${CADDY_DRY_RUN_NO_EXEC:-}" ]; then
    echo "[entrypoint] CADDY_DRY_RUN_NO_EXEC set — skipping caddy exec (test mode)"
    exit 0
fi

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
