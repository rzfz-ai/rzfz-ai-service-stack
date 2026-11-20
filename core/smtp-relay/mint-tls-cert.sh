#!/bin/sh
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #2004 — give the internal mail relay a certificate its clients can verify.
#
# The relay inherits Debian's snakeoil certificate (CN=localhost, SAN
# DNS:localhost) and offers STARTTLS with it. Every consumer that wanted TLS has
# so far worked around that ONE fact, each in its own way:
#
#   Authentik, Dify, Open WebUI, Onyx, paperless-ngx, Vaultwarden  TLS off
#   Infisical                        NODE_TLS_REJECT_UNAUTHORIZED=0  (#2004)
#   OpenUEM                          cannot: go-mail verifies, always (#1992)
#
# Measured on 0.91 (2026-09-13):
#
#   dial failed: tls: failed to verify certificate:
#     x509: certificate is valid for localhost, not smtp-relay
#   relay: disconnect ... ehlo=1 starttls=0/1 commands=1/2
#
# Two gates, not one. The NAME check fires first (CN/SAN say localhost, the
# client dialled smtp-relay); behind it sits the CHAIN check, which a snakeoil
# certificate fails too. A fix that adds `smtp-relay` as a SAN clears the first
# and stops at the second — so this mints from a CA, and the CA goes into the
# trust bundle the stack already maintains.
#
# WHY ITS OWN CA and not Caddy's internal one: Caddy only mints a local CA when
# it is asked for an internal certificate. On a Let's Encrypt box (TLS_MODE
# empty — the default) it never is, so `/data/caddy/pki/authorities/local/
# root.crt` does not exist there and a design resting on it would work on
# TLS_MODE=internal boxes and silently not on the others. This CA exists on
# every box, for exactly one purpose, and is named so in its subject.
#
# Idempotent by construction: it re-mints only when something is actually
# wrong — missing files, an expiry inside the renewal window, or a name a
# consumer now dials that the certificate does not carry.
set -eu

CERT_DIR="${CERT_DIR:-/certs}"
CA_CRT="${CERT_DIR}/ca.crt"
CA_KEY="${CERT_DIR}/ca.key"
CRT="${CERT_DIR}/smtp-relay.crt"
KEY="${CERT_DIR}/smtp-relay.key"

# The names a consumer may dial. `smtp-relay` is the container name every
# consumer uses today; the rest are configurable so an operator who reaches the
# relay under another name does not have to patch a script.
NAMES="${SMTP_TLS_NAMES:-smtp-relay}"
# Re-mint when the leaf has less than this left. 30 days on a 2-year leaf means
# a box that is restarted even once a year renews in good time.
RENEW_DAYS="${SMTP_TLS_RENEW_DAYS:-30}"
CA_DAYS="${SMTP_TLS_CA_DAYS:-3650}"
LEAF_DAYS="${SMTP_TLS_LEAF_DAYS:-825}"

log() { echo "[smtp-relay-certs] $*"; }

mkdir -p "$CERT_DIR"

# ---------------------------------------------------------------------------
# What names must the leaf carry?
# ---------------------------------------------------------------------------
# Accept commas or whitespace: an operator writing a list will use whichever
# separator the neighbouring variables use, and both are one `tr` away.
_names=$(printf '%s' "$NAMES" | tr ',' ' ' | tr -s ' ')
[ -n "$(printf '%s' "$_names" | tr -d ' ')" ] || {
    log "ERROR: SMTP_TLS_NAMES is empty — refusing to mint a certificate with no name."
    exit 1
}

_san=""
for n in $_names; do
    [ -n "$n" ] || continue
    _san="${_san:+${_san},}DNS:${n}"
done
log "names: ${_names}"

# ---------------------------------------------------------------------------
# Is the existing leaf still good?
# ---------------------------------------------------------------------------
_needs_mint="no"
if [ ! -s "$CRT" ] || [ ! -s "$KEY" ] || [ ! -s "$CA_CRT" ]; then
    _needs_mint="missing"
elif ! openssl x509 -in "$CRT" -noout -checkend $((RENEW_DAYS * 86400)) >/dev/null 2>&1; then
    _needs_mint="expiring"
else
    # Every requested name must be IN the certificate. This is the check that
    # makes the names configurable rather than decorative: add a name to
    # SMTP_TLS_NAMES, restart, and the certificate follows.
    _have=$(openssl x509 -in "$CRT" -noout -ext subjectAltName 2>/dev/null || true)
    for n in $_names; do
        case "$_have" in
            *"DNS:${n}"*) ;;
            *) _needs_mint="name:${n}"; break ;;
        esac
    done
fi

if [ "$_needs_mint" = "no" ]; then
    log "certificate is current and covers every requested name — nothing to do."
    exit 0
fi
log "re-minting (${_needs_mint})"

# ---------------------------------------------------------------------------
# The CA. Kept across re-mints: replacing it would invalidate every client that
# has already been handed the bundle.
# ---------------------------------------------------------------------------
if [ ! -s "$CA_CRT" ] || [ ! -s "$CA_KEY" ]; then
    log "minting the internal-services CA (${CA_DAYS} days)"
    openssl req -x509 -newkey rsa:4096 -sha256 -days "$CA_DAYS" -nodes \
        -keyout "$CA_KEY" -out "$CA_CRT" \
        -subj "/O=razzfazz.ai/CN=razzfazz.ai internal services CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
    chmod 0600 "$CA_KEY"
    chmod 0644 "$CA_CRT"
fi

# ---------------------------------------------------------------------------
# The leaf.
# ---------------------------------------------------------------------------
_tmp=$(mktemp -d)
trap 'rm -rf "$_tmp"' EXIT
cat > "${_tmp}/ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${_san}
EXT

openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "${_tmp}/leaf.key" -out "${_tmp}/leaf.csr" \
    -subj "/O=razzfazz.ai/CN=$(printf '%s' "$_names" | cut -d' ' -f1)" >/dev/null 2>&1
openssl x509 -req -in "${_tmp}/leaf.csr" -CA "$CA_CRT" -CAkey "$CA_KEY" \
    -CAcreateserial -sha256 -days "$LEAF_DAYS" \
    -extfile "${_tmp}/ext" -out "${_tmp}/leaf.crt" >/dev/null 2>&1

# Move into place only once BOTH halves exist: a half-written pair would leave
# postfix with a key that does not match its certificate, which fails at
# handshake time rather than at startup.
mv "${_tmp}/leaf.key" "$KEY"
mv "${_tmp}/leaf.crt" "$CRT"
chmod 0600 "$KEY"
chmod 0644 "$CRT"

# Prove what was produced rather than announcing it.
log "issued: $(openssl x509 -in "$CRT" -noout -subject -enddate | tr '\n' ' ')"
log "names:  $(openssl x509 -in "$CRT" -noout -ext subjectAltName | tail -n +2 | tr -d ' ')"
