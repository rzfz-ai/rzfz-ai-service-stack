#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/apply-corporate-proxy.sh   (#181 — 2026.08 corporate-proxy)
# =============================================================================
# Opt-in, idempotent host + container enablement for a customer site whose
# egress goes ONLY through a corporate HTTP(S) proxy that does TLS interception
# with its own private CA. Off by default — a non-proxied box never runs this.
#
# Four independent TLS-trust surfaces are handled, in order:
#
#   LEVEL A  Host + docker daemon
#     · install the proxy CA → update-ca-certificates   (host apt/git/curl trust
#       the MITM cert, AND the docker DAEMON trusts it for registry image pulls)
#     · systemd drop-in http-proxy.conf                 (docker pull via proxy)
#     · daemon.json  dns: <corporate resolver>          (container external DNS)
#     · /etc/environment + apt 95proxy                  (host tools via proxy)
#   LEVEL B  Build
#     · ~/.docker/config.json proxies.default           (build + run inherit
#                                                        HTTP(S)_PROXY/NO_PROXY)
#   (bundle) regenerate certs/caddy-ca.pem               (the SUPERSET bundle the
#       stack already mounts into OIDC clients — now also carries the proxy CA)
#   LEVEL C  Container runtime
#     · generate compose.corporate-proxy.yml            (per-egress-service CA +
#       proxy env + mount) and wire it into COMPOSE_FILE
#
# Design + rationale: .gsd/reports/2026.08-corporate-proxy-ca-design.md
#
# USAGE
#   apply-corporate-proxy.sh --proxy-url URL --ca-file PATH [--dns IP[,IP]]
#                            [--no-proxy CSV] [--proxy-host-pin HOST:IP]
#                            [--egress-set standard|all]
#   apply-corporate-proxy.sh --status         # show current corporate-proxy state
#   apply-corporate-proxy.sh --check          # dry-run: print intended actions
#   apply-corporate-proxy.sh --off            # disable: drop overlay from
#                                             # COMPOSE_FILE + clear the toggle
#                                             # (host CA + daemon left in place;
#                                             #  they are harmless and shared)
#
# FAIL-CLOSED: enabling refuses to proceed if the proxy is unreachable, so a box
# on the wrong network is never half-configured. --status/--check/--off skip it.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=scripts/lib.sh disable=SC1091
source "${SCRIPT_DIR}/scripts/lib.sh"

cd "$SCRIPT_DIR"

# RAZZFAZZ_ENV_FILE override exists for the static test harness; production
# always targets the repo-root .env.
ENV_FILE="${RAZZFAZZ_ENV_FILE:-${SCRIPT_DIR}/.env}"
OVERLAY_FILE="compose.corporate-proxy.yml"      # repo-root relative (COMPOSE_FILE entry)
CA_DEST_HOST="/usr/local/share/ca-certificates/razzfazz-corporate-proxy.crt"
CA_STAGED="certs/corporate-proxy-ca.pem"        # box-local canonical location (gitignored)
DAEMON_JSON="/etc/docker/daemon.json"
SYSTEMD_DROPIN="/etc/systemd/system/docker.service.d/http-proxy.conf"
DOCKER_CLIENT_CFG="${HOME}/.docker/config.json"

# ---- args -------------------------------------------------------------------
MODE="apply"
PROXY_URL=""
CA_FILE=""
DNS=""
NO_PROXY_EXTRA=""
PROXY_HOST_PIN=""
EGRESS_SET="standard"

while [ $# -gt 0 ]; do
    case "$1" in
        --status)        MODE="status" ;;
        --check)         MODE="check" ;;
        --off|--disable) MODE="off" ;;
        --proxy-url)     PROXY_URL="${2:-}"; shift ;;
        --ca-file)       CA_FILE="${2:-}"; shift ;;
        --dns)           DNS="${2:-}"; shift ;;
        --no-proxy)      NO_PROXY_EXTRA="${2:-}"; shift ;;
        --proxy-host-pin) PROXY_HOST_PIN="${2:-}"; shift ;;
        --egress-set)    EGRESS_SET="${2:-standard}"; shift ;;
        -h|--help)       grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
        *) print_error "Unknown argument: $1"; exit 64 ;;
    esac
    shift
done

_env_get()  { read_env_value "$ENV_FILE" "$1"; }
_env_set()  { update_env_value "$ENV_FILE" "$1" "$2"; }

# Daemon/host-level NO_PROXY. Deliberately lean (localhost + private ranges +
# MAIN_DOMAIN + operator extras). Registry pulls SHOULD go through the proxy, so
# we do NOT exclude registries here — only same-box / internal traffic.
_compute_no_proxy() {
    local md np; md="$(_env_get MAIN_DOMAIN)"
    np="localhost,127.0.0.1,::1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16,.internal,.svc"
    [ -n "$md" ] && np="${np},${md},.${md}"
    [ -n "$NO_PROXY_EXTRA" ] && np="${np},${NO_PROXY_EXTRA}"
    printf '%s' "$np"
}

# Merge {"dns":[...]} into /etc/docker/daemon.json, PRESERVING every existing
# key (log-opts, no-new-privileges, …). Pure-stdlib python; written via sudo.
_merge_daemon_json_dns() {
    local dns="$1"
    local tmp; tmp="$(mktemp)" || return 1
    DNS_CSV="$dns" DAEMON_SRC="$DAEMON_JSON" python3 - "$tmp" <<'PY' || { rm -f "$tmp"; return 1; }
import json, os, sys
out = sys.argv[1]
src = os.environ["DAEMON_SRC"]
dns = [x.strip() for x in os.environ["DNS_CSV"].split(",") if x.strip()]
cfg = {}
if os.path.exists(src):
    try:
        with open(src) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
cfg["dns"] = dns
with open(out, "w") as f:
    json.dump(cfg, f, indent=2)
PY
    sudo mkdir -p "$(dirname "$DAEMON_JSON")" 2>/dev/null
    if sudo cp "$tmp" "$DAEMON_JSON"; then rm -f "$tmp"; return 0; fi
    rm -f "$tmp"; return 1
}

# Merge proxies.default into ~/.docker/config.json (build + run inherit proxy).
_merge_docker_client_proxies() {
    mkdir -p "$(dirname "$DOCKER_CLIENT_CFG")" 2>/dev/null || return 1
    local np; np="$(_compute_no_proxy)"
    PROXY_URL="$PROXY_URL" NO_PROXY="$np" CFG="$DOCKER_CLIENT_CFG" python3 - <<'PY' || return 1
import json, os
cfg_path = os.environ["CFG"]
cfg = {}
if os.path.exists(cfg_path):
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
cfg.setdefault("proxies", {})["default"] = {
    "httpProxy":  os.environ["PROXY_URL"],
    "httpsProxy": os.environ["PROXY_URL"],
    "noProxy":    os.environ["NO_PROXY"],
}
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
    return 0
}

# COMPOSE_FILE helpers ---------------------------------------------------------
# Append the overlay once (idempotent); a bare/empty COMPOSE_FILE becomes
# compose.yml:<overlay> (docker compose 2.40+ chokes on an empty value).
compose_file_add_overlay() {
    local cur; cur="$(_env_get COMPOSE_FILE)"
    case ":${cur}:" in
        *":${OVERLAY_FILE}:"*) return 0 ;;   # already present
    esac
    if [ -z "$cur" ]; then
        cur="compose.yml"
    fi
    _env_set COMPOSE_FILE "${cur}:${OVERLAY_FILE}"
}
compose_file_remove_overlay() {
    local cur; cur="$(_env_get COMPOSE_FILE)"
    [ -n "$cur" ] || return 0
    local out="" e _ifs="$IFS"
    IFS=':'
    for e in $cur; do
        [ "$e" = "$OVERLAY_FILE" ] && continue
        [ -z "$e" ] && continue
        out="${out:+${out}:}${e}"
    done
    IFS="$_ifs"
    _env_set COMPOSE_FILE "${out:-compose.yml}"
}

# ---- STATUS -----------------------------------------------------------------
show_status() {
    print_step "Corporate-proxy status"
    local enabled url dns caf; enabled="$(_env_get RAZZFAZZ_CORPORATE_PROXY)"
    url="$(_env_get RAZZFAZZ_PROXY_URL)"; dns="$(_env_get RAZZFAZZ_CORPORATE_DNS)"
    caf="$(_env_get RAZZFAZZ_EXTRA_CA_FILE)"
    print_substep "RAZZFAZZ_CORPORATE_PROXY = ${enabled:-0}"
    print_substep "RAZZFAZZ_PROXY_URL       = ${url:-<unset>}"
    print_substep "RAZZFAZZ_CORPORATE_DNS   = ${dns:-<unset>}"
    print_substep "RAZZFAZZ_EXTRA_CA_FILE   = ${caf:-<unset>}"
    print_substep "COMPOSE_FILE             = $(_env_get COMPOSE_FILE)"
    # overlay present in COMPOSE_FILE?
    case ":$(_env_get COMPOSE_FILE):" in
        *":${OVERLAY_FILE}:"*) print_substep "overlay in COMPOSE_FILE   = yes" ;;
        *)                     print_substep "overlay in COMPOSE_FILE   = no" ;;
    esac
    [ -f "$OVERLAY_FILE" ] && print_substep "overlay file             = present" \
        || print_substep "overlay file             = MISSING"
    # proxy CA in the mounted superset bundle?
    if [ -n "$caf" ] && [ -s "$caf" ] && [ -s certs/caddy-ca.pem ]; then
        local fp; fp="$(openssl x509 -in "$caf" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 || true)"
        if [ -n "$fp" ] && openssl crl2pkcs7 -nocrl -certfile certs/caddy-ca.pem 2>/dev/null \
             | openssl pkcs7 -print_certs -noout -fingerprint -sha256 2>/dev/null \
             | grep -qiF "$fp"; then
            print_success "proxy CA present in certs/caddy-ca.pem (mounted trust bundle)"
        else
            print_warning "proxy CA NOT found in certs/caddy-ca.pem — re-run \`rzfz setup --corporate-proxy\` to regenerate the bundle"
        fi
    fi
    # host store?
    if [ -f "$CA_DEST_HOST" ]; then
        print_substep "host CA store            = installed ($CA_DEST_HOST)"
    else
        print_substep "host CA store            = not installed"
    fi
}

# ---- reachability gate ------------------------------------------------------
proxy_reachable() {
    local url="$1"
    # Try an HTTPS CONNECT through the proxy to a well-known host. --proxy-anyauth
    # + -k: we only care that the CONNECT tunnel establishes (the intercept cert
    # is expected to be untrusted at THIS point — we're about to trust it).
    curl -sS -o /dev/null --max-time 12 -x "$url" -k \
        https://www.google.com/generate_204 >/dev/null 2>&1 && return 0
    # Fallback: bare TCP to host:port of the proxy.
    local hp; hp="${url#*://}"; hp="${hp#*@}"
    local h="${hp%%[:/]*}" p="${hp##*:}"; p="${p%%/*}"
    [ "$p" = "$h" ] && p=3128
    (exec 3<>"/dev/tcp/${h}/${p}") >/dev/null 2>&1 && return 0
    return 1
}

# =============================================================================
if [ "$MODE" = "status" ]; then show_status; exit 0; fi

if [ "$MODE" = "off" ]; then
    print_step "Disabling corporate-proxy container overlay"
    compose_file_remove_overlay
    _env_set RAZZFAZZ_CORPORATE_PROXY 0
    print_substep "Removed ${OVERLAY_FILE} from COMPOSE_FILE; set RAZZFAZZ_CORPORATE_PROXY=0"
    print_info  "Host CA install + daemon proxy/DNS are LEFT in place (harmless, shared)."
    print_info  "Apply with: docker compose up -d --force-recreate"
    exit 0
fi

# ---- apply / check ----------------------------------------------------------
DRY=""; [ "$MODE" = "check" ] && DRY="[dry-run] "

# Re-use previously captured values when a flag is omitted (idempotent re-run).
[ -n "$PROXY_URL" ] || PROXY_URL="$(_env_get RAZZFAZZ_PROXY_URL)"
[ -n "$DNS" ]       || DNS="$(_env_get RAZZFAZZ_CORPORATE_DNS)"
[ -n "$CA_FILE" ]   || CA_FILE="$(_env_get RAZZFAZZ_EXTRA_CA_FILE)"
[ -n "$NO_PROXY_EXTRA" ] || NO_PROXY_EXTRA="$(_env_get RAZZFAZZ_PROXY_NOPROXY)"
[ -n "$PROXY_HOST_PIN" ] || PROXY_HOST_PIN="$(_env_get RAZZFAZZ_PROXY_HOST_PIN)"

if [ -z "$PROXY_URL" ]; then
    print_error "No proxy URL. Pass --proxy-url http://host:port (or set RAZZFAZZ_PROXY_URL)."
    exit 64
fi
if [ -z "$CA_FILE" ] || [ ! -s "$CA_FILE" ]; then
    print_error "Proxy CA file missing/empty. Pass --ca-file <path-to-proxy-ca.pem>."
    exit 64
fi
if ! openssl x509 -in "$CA_FILE" -noout >/dev/null 2>&1; then
    print_error "--ca-file '$CA_FILE' is not a readable PEM certificate."
    exit 64
fi

print_step "${DRY}Corporate-proxy enable  (proxy=${PROXY_URL}, egress-set=${EGRESS_SET})"

# 0) Reachability gate — fail-closed on a real apply.
if [ "$MODE" = "apply" ]; then
    if proxy_reachable "$PROXY_URL"; then
        print_success "Proxy ${PROXY_URL} is reachable."
    else
        print_error "Proxy ${PROXY_URL} is NOT reachable from this host."
        print_error "Refusing to configure (fail-closed) — verify you are on the customer network and the URL is correct."
        exit 2
    fi
fi

MAIN_DOMAIN="$(_env_get MAIN_DOMAIN)"

# ---- LEVEL A: host + daemon --------------------------------------------------
print_step "${DRY}Level A — host trust store + docker daemon"

# A1. stage the CA box-local (gitignored) then install into the host store.
if [ "$MODE" = "apply" ]; then
    mkdir -p certs
    cp "$CA_FILE" "$CA_STAGED"
    _env_set RAZZFAZZ_EXTRA_CA_FILE "$CA_STAGED"
    if command -v update-ca-certificates >/dev/null 2>&1; then
        sudo cp "$CA_STAGED" "$CA_DEST_HOST" \
            && sudo update-ca-certificates >/dev/null 2>&1 \
            && print_success "Installed proxy CA into host store (update-ca-certificates)." \
            || print_warning "Could not install proxy CA into host store (needs sudo). Host apt/git/daemon-pull may still fail — install manually: sudo cp $CA_STAGED $CA_DEST_HOST && sudo update-ca-certificates"
    else
        print_warning "update-ca-certificates not found (non-Debian host). Install the CA into the host trust store manually before pulling images."
    fi
else
    print_substep "${DRY}would stage CA → ${CA_STAGED} and install → ${CA_DEST_HOST} (sudo update-ca-certificates)"
fi

# A2. systemd docker proxy drop-in (daemon pulls via proxy).
_write_systemd_dropin() {
    sudo mkdir -p "$(dirname "$SYSTEMD_DROPIN")" 2>/dev/null || return 1
    local np; np="$(_compute_no_proxy)"
    sudo tee "$SYSTEMD_DROPIN" >/dev/null <<EOF
[Service]
Environment="HTTP_PROXY=${PROXY_URL}"
Environment="HTTPS_PROXY=${PROXY_URL}"
Environment="NO_PROXY=${np}"
EOF
}
if [ "$MODE" = "apply" ]; then
    if _write_systemd_dropin; then
        sudo systemctl daemon-reload 2>/dev/null || true
        print_success "Wrote docker systemd proxy drop-in ($SYSTEMD_DROPIN)."
    else
        print_warning "Could not write $SYSTEMD_DROPIN (needs sudo) — docker image PULLS may not use the proxy."
    fi
else
    print_substep "${DRY}would write $SYSTEMD_DROPIN with HTTP(S)_PROXY=${PROXY_URL}"
fi

# A3. daemon.json — merge dns + (optional) proxies, preserving existing keys.
if [ "$MODE" = "apply" ] && [ -n "$DNS" ]; then
    if _merge_daemon_json_dns "$DNS"; then
        print_success "Merged corporate DNS ${DNS} into $DAEMON_JSON (dns upstream)."
    else
        print_warning "Could not merge dns into $DAEMON_JSON — container external DNS may fail. Set \"dns\": [\"${DNS}\"] manually and restart docker."
    fi
elif [ -n "$DNS" ]; then
    print_substep "${DRY}would merge \"dns\": [\"${DNS}\"] into $DAEMON_JSON"
fi

# A4/B. docker client proxies (build + run inherit HTTP(S)_PROXY/NO_PROXY).
if [ "$MODE" = "apply" ]; then
    _merge_docker_client_proxies && print_success "Set ~/.docker/config.json proxies.default (build inherits proxy)." \
        || print_warning "Could not update $DOCKER_CLIENT_CFG — image BUILDS may not use the proxy."
else
    print_substep "${DRY}would set proxies.default in $DOCKER_CLIENT_CFG"
fi

# A5. apt proxy (host package installs).
if [ "$MODE" = "apply" ]; then
    sudo tee /etc/apt/apt.conf.d/95proxy >/dev/null 2>&1 <<EOF
Acquire::http::Proxy "${PROXY_URL}";
Acquire::https::Proxy "${PROXY_URL}";
EOF
    print_substep "Wrote /etc/apt/apt.conf.d/95proxy (best-effort; needs sudo)."
else
    print_substep "${DRY}would write /etc/apt/apt.conf.d/95proxy"
fi

# ---- bundle: regenerate certs/caddy-ca.pem (now includes the proxy CA) -------
print_step "${DRY}Regenerating certs/caddy-ca.pem SUPERSET bundle (adds the proxy CA)"
if [ "$MODE" = "apply" ]; then
    _env_set RAZZFAZZ_CORPORATE_PROXY 1   # so ensure_oidc_ca_superset appends the CA
    if command -v ensure_oidc_ca_superset >/dev/null 2>&1 || declare -F ensure_oidc_ca_superset >/dev/null 2>&1; then
        ensure_oidc_ca_superset || print_warning "ensure_oidc_ca_superset returned non-zero (best-effort)."
    else
        print_warning "ensure_oidc_ca_superset not available from lib.sh — bundle not regenerated."
    fi
else
    print_substep "${DRY}would set RAZZFAZZ_CORPORATE_PROXY=1 and regenerate certs/caddy-ca.pem (superset incl. proxy CA)"
fi

# ---- LEVEL C: container overlay ---------------------------------------------
print_step "${DRY}Level C — generate ${OVERLAY_FILE} + wire COMPOSE_FILE"
_gen_args=( --proxy-url "$PROXY_URL" --egress-set "$EGRESS_SET" --out "$OVERLAY_FILE" )
[ -n "$MAIN_DOMAIN" ]    && _gen_args+=( --main-domain "$MAIN_DOMAIN" )
[ -n "$NO_PROXY_EXTRA" ] && _gen_args+=( --no-proxy "$NO_PROXY_EXTRA" )
[ -n "$PROXY_HOST_PIN" ] && _gen_args+=( --proxy-host-pin "$PROXY_HOST_PIN" )
if [ "$MODE" = "apply" ]; then
    if python3 "${SCRIPT_DIR}/scripts/gen-corporate-proxy-overlay.py" "${_gen_args[@]}"; then
        print_success "Generated ${OVERLAY_FILE}."
    else
        print_error "Overlay generation failed."
        exit 1
    fi
    compose_file_add_overlay
    print_substep "COMPOSE_FILE now includes ${OVERLAY_FILE}: $(_env_get COMPOSE_FILE)"
    # persist capture
    _env_set RAZZFAZZ_PROXY_URL "$PROXY_URL"
    [ -n "$DNS" ]            && _env_set RAZZFAZZ_CORPORATE_DNS "$DNS"
    [ -n "$NO_PROXY_EXTRA" ] && _env_set RAZZFAZZ_PROXY_NOPROXY "$NO_PROXY_EXTRA"
    [ -n "$PROXY_HOST_PIN" ] && _env_set RAZZFAZZ_PROXY_HOST_PIN "$PROXY_HOST_PIN"
    _env_set RAZZFAZZ_PROXY_EGRESS_SET "$EGRESS_SET"
else
    print_substep "${DRY}would run: gen-corporate-proxy-overlay.py ${_gen_args[*]}"
    print_substep "${DRY}would add ${OVERLAY_FILE} to COMPOSE_FILE and write the RAZZFAZZ_* toggle keys"
fi

print_step "${DRY}Done"
if [ "$MODE" = "apply" ]; then
    print_info "Restart docker so daemon proxy/DNS take effect, then recreate the stack:"
    print_info "  sudo systemctl restart docker"
    print_info "  docker compose up -d --force-recreate"
    print_info "Verify:  rzfz setup --corporate-proxy --status"
fi
exit 0
