#!/bin/sh
# ==============================================================================
# Gitea Initialization Script
# ==============================================================================
# This script runs on container startup and:
# 1. Waits for database
# 2. Starts Gitea in background
# 3. Creates admin user if not exists
# 4. Configures Google OAuth if enabled
# 5. Keeps container running
# ==============================================================================

# BEGIN stop-forwarding
# #3059: this script is PID 1 of the container (compose runs it through
# `sh -c`, which execs it). PID 1 ignores every signal it has no handler for,
# so without this trap `docker stop` — the nightly backup's stop-during-backup,
# `rzfz stop`, an upgrade — sent SIGTERM into the void, nothing reached
# s6-svscan or gitea, and the daemon SIGKILLed the container after its timeout:
# no graceful shutdown, every night, on every box. The handler forwards the stop
# the way the image itself does (the same `s6-svscanctl -t` as the #2507
# restart below: svscan TERMs gitea + sshd and exits), waits until the
# supervisor has exited and exits with its status. Further TERMs are ignored
# while it waits; the daemon's own timeout (stop_grace_period) stays the
# backstop. A stop before the server was started just ends the script.
GITEA_PID=""
_gitea_stop() {
    trap '' TERM INT
    if [ -z "$GITEA_PID" ]; then
        echo "Stop signal received before Gitea was started - exiting."
        exit 143
    fi
    echo "Stop signal received - stopping Gitea gracefully (#3059)..."
    s6-svscanctl -t /etc/s6 2>/dev/null || kill -TERM "$GITEA_PID" 2>/dev/null || true
    _gitea_rc=0
    while kill -0 "$GITEA_PID" 2>/dev/null; do
        wait "$GITEA_PID"
        _gitea_rc=$?
    done
    echo "Gitea stopped (exit ${_gitea_rc})."
    exit "$_gitea_rc"
}
trap _gitea_stop TERM INT
# END stop-forwarding

# Wait for database to be ready
echo "Waiting for PostgreSQL..."
while ! nc -z postgres 5432 2>/dev/null; do
    sleep 2
done
echo "PostgreSQL is ready!"

# Create a marker file path
ADMIN_CREATED_MARKER="/data/gitea/.admin_created"
OAUTH_CONFIGURED_MARKER="/data/gitea/.google_oauth_configured"

# BEGIN trusted-proxies
# #2526: Gitea takes the client address from X-Forwarded-For only when the
# request comes from a trusted proxy. `*` (or a wide range) would let any
# client that reaches Gitea directly - a container on the coding-agents
# network, say - forge it, so the list is exactly Gitea's loopback default plus
# the address(es) Caddy has on the stack network. The stack's default network
# has no fixed subnet, so Caddy is resolved here at start. If it cannot be
# resolved, only loopback stays trusted: the access log then shows Caddy's
# address instead of the client's, which is safe.
_trusted="127.0.0.0/8,::1/128"
_caddy_ips=""
_try=0
while [ -z "$_caddy_ips" ] && [ "$_try" -lt 15 ]; do
    _try=$((_try + 1))
    _raw=$(getent ahostsv4 "${GITEA_CADDY_HOST:-caddy}" 2>/dev/null | cut -d' ' -f1)
    [ -n "$_raw" ] || _raw=$(nslookup "${GITEA_CADDY_HOST:-caddy}" 2>/dev/null | grep '^Address' | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}')
    _caddy_ips=$(printf '%s\n' "$_raw" | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' | grep -vE '^(0\.|127\.)' | sort -u)
    [ -n "$_caddy_ips" ] || sleep 2
done
for _ip in $_caddy_ips; do
    _trusted="$_trusted,$_ip/32"
done
export GITEA__security__REVERSE_PROXY_TRUSTED_PROXIES="$_trusted"
echo "Gitea trusted proxies: $_trusted"
# END trusted-proxies

# The image's own entrypoint (s6-svscan over /etc/s6/{gitea,openssh}).
# Overridable only so tests can stub it.
GITEA_ENTRYPOINT="${GITEA_ENTRYPOINT:-/usr/bin/entrypoint}"

# Start Gitea entrypoint in background
echo "Starting Gitea..."
"$GITEA_ENTRYPOINT" &
GITEA_PID=$!

# Wait for Gitea to be ready (healthcheck)
echo "Waiting for Gitea to be ready..."
# Backgrounded + `wait`: a stop that arrives now is handled at once (#3059); a
# foreground sleep would hold the trap back for the full 10 s.
sleep 10 &
wait $!
while ! wget -q -O /dev/null http://localhost:3000/api/healthz 2>/dev/null; do
    sleep 2
done
echo "Gitea is ready!"

# Create admin user if not exists and marker doesn't exist
if [ -n "${GITEA_ADMIN_USER}" ] && [ -n "${GITEA_ADMIN_PASSWORD}" ] && [ ! -f "$ADMIN_CREATED_MARKER" ]; then
    echo "Creating admin user: ${GITEA_ADMIN_USER}"
    # Run as git user to avoid root permission issues
    su-exec git gitea admin user create \
        --username "${GITEA_ADMIN_USER}" \
        --password "${GITEA_ADMIN_PASSWORD}" \
        --email "${GITEA_ADMIN_EMAIL:-admin@localhost}" \
        --admin \
        --must-change-password=false 2>&1 || echo "Admin user might already exist"
    
    # Create marker to avoid re-running on restart
    touch "$ADMIN_CREATED_MARKER"
    echo "Admin user setup complete"
fi

# Configure Google OAuth if enabled and not already configured
if [ "${ENABLE_GOOGLE_OAUTH}" = "true" ] && [ -n "${GOOGLE_CLIENT_ID}" ] && [ ! -f "$OAUTH_CONFIGURED_MARKER" ]; then
    echo "Configuring Google OAuth..."
    # Use openidConnect provider with Google's discovery URL (not "google" provider)
    # Must include openid, email, profile scopes for auto-registration to work
    # Restrict to GOOGLE_OAUTH_DOMAIN (e.g. example.com). The key is a list
    # since #2927 and Gitea's required claim takes ONE value: the first domain.
    # #2927: an empty list admits nobody, as in the Authentik policy. Gitea's
    # required claim with an empty value only checks that the claim exists,
    # which would admit every Workspace account, so no source is added (and no
    # marker written: setting the domain later configures it on the next start).
    _gdom="${GOOGLE_OAUTH_DOMAIN-seqis.com}"; _gdom="${_gdom#"${_gdom%%[!, ]*}"}"; _gdom="${_gdom%%[, ]*}"
    if [ -z "$_gdom" ]; then
        echo "Google OAuth not configured: no allowed Google Workspace domain is set (GOOGLE_OAUTH_DOMAIN)."
    elif su-exec git gitea admin auth add-oauth \
        --name "Google" \
        --provider "openidConnect" \
        --key "${GOOGLE_CLIENT_ID}" \
        --secret "${GOOGLE_CLIENT_SECRET}" \
        --auto-discover-url "https://accounts.google.com/.well-known/openid-configuration" \
        --scopes "openid" --scopes "email" --scopes "profile" \
        --required-claim-name "hd" \
        --required-claim-value "${_gdom}" \
        --skip-local-2fa 2>&1; then
        touch "$OAUTH_CONFIGURED_MARKER"
        echo "Google OAuth configuration complete (domain: ${_gdom})"
    else
        echo "ERROR: Failed to configure Google OAuth"
    fi
fi

# Configure (and on every start, reconcile) the Authentik OIDC source
AUTHENTIK_OIDC_MARKER="/data/gitea/.authentik_oidc_configured"
# True SSO group->role mapping (#33): map an Authentik GROUP -> Gitea admin.
# Config-driven (SSO_ADMIN_GROUP), never a hardcoded user. The Authentik
# gitea-oidc provider emits a `groups` claim (razzfazz.ai groups scope mapping);
# Gitea reads it via --group-claim-name and promotes members of --admin-group to
# site admin on every OIDC login.
GITEA_SSO_ADMIN_GROUP="${SSO_ADMIN_GROUP:-razzfazz.ai Super Admins}"
AUTHENTIK_OIDC_GRPMAP_MARKER="/data/gitea/.authentik_oidc_grpmap"
AUTHENTIK_OIDC_DISCOVERY_URL="https://${AUTHENTIK_DOMAIN:-auth.localhost}/application/o/gitea-oidc/.well-known/openid-configuration"
# Fingerprint of the source config this script last applied (#2507 round 2).
AUTHENTIK_OIDC_APPLIED="/data/gitea/.authentik_oidc_applied"
AUTHENTIK_OIDC_WANT=$(printf '%s\n' "${GITEA_OIDC_CLIENT_ID:-}" "${GITEA_OIDC_CLIENT_SECRET:-}" \
    "$AUTHENTIK_OIDC_DISCOVERY_URL" "openid email profile groups" "groups" "$GITEA_SSO_ADMIN_GROUP" \
    | sha256sum | cut -d' ' -f1)
GITEA_RESTART_NEEDED=false
# #218 / #2507: #218 wanted SSO accounts named after `preferred_username` and
# passed `add-oauth --username preferred_username` — a flag that does NOT exist
# in Gitea 1.27.3 (`gitea admin auth add-oauth --help`, verified on prod
# 2026-09-26): every call died with "flag provided but not defined: -username",
# no source was ever created, and no marker was written, so it retried on every
# start. In Gitea the username source is the GLOBAL app.ini setting
# `[oauth2_client] USERNAME`, rendered by modules/gitea/compose.yml from
# GITEA_OAUTH_USERNAME (default `email`, what every box has run with;
# `preferred_username` is opt-in). Every flag below must exist in the pinned
# Gitea — tests/unit/gitea/test_218_oidc_username.py pins the flag list of the
# shipped version (28.4.0: the same flags as 1.27.3).
#
# #2507 fix round 1: the source is RECONCILED on every start, not only created.
# Caddy sends every browser login straight to /user/oauth2/Authentik when the
# switch is on, so a source whose key/secret/discovery URL differs from the
# Authentik provider (templated from the same .env) breaks EVERY login — prod's
# hand-made source is exactly that risk, and a rotated secret would be too.
# `update-oauth` only changes the fields it is given; re-applying equal values
# is a no-op for Gitea.
if [ "${ENABLE_GITEA_AUTHENTIK_OIDC:-false}" = "true" ] && [ -n "${GITEA_OIDC_CLIENT_ID:-}" ] && [ -n "${GITEA_OIDC_CLIENT_SECRET:-}" ]; then
    OIDC_SOURCE_ID=$(su-exec git gitea admin auth list 2>/dev/null | awk '$2=="Authentik"{print $1; exit}')
    if [ -z "$OIDC_SOURCE_ID" ]; then
        echo "Configuring Authentik OIDC for Gitea (admin group: ${GITEA_SSO_ADMIN_GROUP})..."
        if su-exec git gitea admin auth add-oauth \
            --name "Authentik" \
            --provider "openidConnect" \
            --key "${GITEA_OIDC_CLIENT_ID}" \
            --secret "${GITEA_OIDC_CLIENT_SECRET}" \
            --auto-discover-url "${AUTHENTIK_OIDC_DISCOVERY_URL}" \
            --scopes "openid" --scopes "email" --scopes "profile" --scopes "groups" \
            --group-claim-name "groups" \
            --admin-group "${GITEA_SSO_ADMIN_GROUP}" \
            --skip-local-2fa 2>&1; then
            touch "$AUTHENTIK_OIDC_MARKER" "$AUTHENTIK_OIDC_GRPMAP_MARKER"
            # A NEW source needs no restart: its first Callout fails with "no
            # provider for", which makes the server re-register every source.
            printf '%s\n' "$AUTHENTIK_OIDC_WANT" > "$AUTHENTIK_OIDC_APPLIED"
            echo "Authentik OIDC configuration complete (domain: ${AUTHENTIK_DOMAIN:-auth.localhost})"
        else
            echo "ERROR: Failed to configure Authentik OIDC for Gitea"
        fi
    else
        # An existing source — ours from an earlier start, or adopted (prod's
        # hand-made one, or a lost marker; add-oauth would refuse the duplicate
        # name forever). Credentials, discovery URL and the #33 group mapping
        # are asserted from .env every time.
        echo "Reconciling Authentik OIDC source (id ${OIDC_SOURCE_ID}, admin group: ${GITEA_SSO_ADMIN_GROUP}) (#2507)..."
        if su-exec git gitea admin auth update-oauth \
            --id "$OIDC_SOURCE_ID" \
            --key "${GITEA_OIDC_CLIENT_ID}" \
            --secret "${GITEA_OIDC_CLIENT_SECRET}" \
            --auto-discover-url "${AUTHENTIK_OIDC_DISCOVERY_URL}" \
            --scopes "openid" --scopes "email" --scopes "profile" --scopes "groups" \
            --group-claim-name "groups" \
            --admin-group "${GITEA_SSO_ADMIN_GROUP}" 2>&1; then
            touch "$AUTHENTIK_OIDC_MARKER" "$AUTHENTIK_OIDC_GRPMAP_MARKER"
            echo "Authentik OIDC source reconciled (client, discovery URL, group->admin mapping)"
            # #2507 fix round 2: the RUNNING server registered its goth
            # providers at startup (services/auth/source/oauth2/init.go) and
            # re-registers only when a Callout fails with "no provider for"
            # (routers/web/auth/oauth.go). A stale-but-registered client fails
            # later, at the token exchange — so a CHANGED client reaches the
            # server only through a restart. `gitea admin auth list` does not
            # show a source's key/secret/URL, so "changed" is judged against a
            # fingerprint of what this script last applied (sha256, no secret
            # in clear); absent (prod's hand-made source, first start after
            # the upgrade) counts as changed -> exactly one restart.
            if [ "$(cat "$AUTHENTIK_OIDC_APPLIED" 2>/dev/null)" != "$AUTHENTIK_OIDC_WANT" ]; then
                GITEA_RESTART_NEEDED=true
            fi
            printf '%s\n' "$AUTHENTIK_OIDC_WANT" > "$AUTHENTIK_OIDC_APPLIED"
        else
            echo "ERROR: Failed to reconcile the Authentik OIDC source (id ${OIDC_SOURCE_ID}) — browser logins may fail; the local form is at /user/login?local=1"
        fi
    fi
elif [ "${ENABLE_GITEA_AUTHENTIK_OIDC:-false}" = "true" ]; then
    echo "WARNING: ENABLE_GITEA_AUTHENTIK_OIDC=true but GITEA_OIDC_CLIENT_ID or GITEA_OIDC_CLIENT_SECRET is empty — Authentik OIDC source NOT created/updated ('rzfz upgrade' generates the secret)"
else
    echo "ENABLE_GITEA_AUTHENTIK_OIDC not set or false — skipping Authentik OIDC"
fi

# #2507 round 2: restart the Gitea SERVER once, in place, when the Authentik
# client it holds in memory is stale (see the reconcile above). Why this shape:
#   * a reconcile BEFORE the server starts would run the CLI against a DB the
#     server has not migrated yet after a Gitea upgrade, and against the app.ini
#     of the previous start;
#   * `s6-svc -r /etc/s6/gitea` is no lighter: the image's gitea/finish runs
#     `s6-svscanctl -t /etc/s6`, so any gitea exit tears the whole svscan down;
#   * exiting non-zero leaves the recovery to the restart policy and flaps the
#     container's state during an upgrade.
# So: the same graceful `s6-svscanctl -t` the image uses (gitea + sshd get
# SIGTERM; .s6-svscan/finish is `exit 0`), wait for it, start the entrypoint
# again, wait for healthz. The container stays up; the steady state (nothing
# changed) never gets here.
if [ "$GITEA_RESTART_NEEDED" = "true" ]; then
    echo "Restarting the Gitea server once so it loads the reconciled Authentik client (#2507)..."
    s6-svscanctl -t /etc/s6 2>/dev/null || kill -TERM "$GITEA_PID" 2>/dev/null || true
    wait "$GITEA_PID" 2>/dev/null || true
    "$GITEA_ENTRYPOINT" &
    GITEA_PID=$!
    while ! wget -q -O /dev/null http://localhost:3000/api/healthz 2>/dev/null; do
        sleep 2
    done
    echo "Gitea is ready again (reconciled Authentik client loaded)."
fi

# Wait for Gitea process. A stop signal interrupts this `wait` and runs
# _gitea_stop (#3059), which forwards it and exits with the server's status.
wait "$GITEA_PID"
