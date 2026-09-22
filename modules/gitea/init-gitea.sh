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

# Wait for database to be ready
echo "Waiting for PostgreSQL..."
while ! nc -z postgres 5432 2>/dev/null; do
    sleep 2
done
echo "PostgreSQL is ready!"

# Create a marker file path
ADMIN_CREATED_MARKER="/data/gitea/.admin_created"
OAUTH_CONFIGURED_MARKER="/data/gitea/.google_oauth_configured"

# Start Gitea entrypoint in background
echo "Starting Gitea..."
/usr/bin/entrypoint &
GITEA_PID=$!

# Wait for Gitea to be ready (healthcheck)
echo "Waiting for Gitea to be ready..."
sleep 10
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
    # Restrict to GOOGLE_OAUTH_DOMAIN if set (e.g., seqis.com)
    if su-exec git gitea admin auth add-oauth \
        --name "Google" \
        --provider "openidConnect" \
        --key "${GOOGLE_CLIENT_ID}" \
        --secret "${GOOGLE_CLIENT_SECRET}" \
        --auto-discover-url "https://accounts.google.com/.well-known/openid-configuration" \
        --scopes "openid" --scopes "email" --scopes "profile" \
        --required-claim-name "hd" \
        --required-claim-value "${GOOGLE_OAUTH_DOMAIN:-seqis.com}" \
        --skip-local-2fa 2>&1; then
        touch "$OAUTH_CONFIGURED_MARKER"
        echo "Google OAuth configuration complete (domain: ${GOOGLE_OAUTH_DOMAIN:-seqis.com})"
    else
        echo "ERROR: Failed to configure Google OAuth"
    fi
fi

# Configure Authentik OIDC if enabled and not already configured
AUTHENTIK_OIDC_MARKER="/data/gitea/.authentik_oidc_configured"
# True SSO group->role mapping (#33): map an Authentik GROUP -> Gitea admin.
# Config-driven (SSO_ADMIN_GROUP), never a hardcoded user. The Authentik
# gitea-oidc provider emits a `groups` claim (razzfazz.ai groups scope mapping);
# Gitea reads it via --group-claim-name and promotes members of --admin-group to
# site admin on every OIDC login. A separate marker (.authentik_oidc_grpmap)
# lets us retrofit the mapping onto boxes whose OIDC source predates #33.
GITEA_SSO_ADMIN_GROUP="${SSO_ADMIN_GROUP:-razzfazz.ai Super Admins}"
AUTHENTIK_OIDC_GRPMAP_MARKER="/data/gitea/.authentik_oidc_grpmap"
# #218: without --username, Gitea defaults `userid` to the OIDC `sub` — an
# Authentik hex UUID. The account is then unreachable by its readable name,
# which is how the coding agent mints its token
# (`generate-access-token --username <authentik name>` -> "user does not
# exist" -> empty token -> no .git-credentials -> "Clone from Gitea" fails).
# Its own marker, so a box configured before this change retrofits the
# attribute — same pattern the #33 group mapping uses above.
AUTHENTIK_OIDC_USERNAME_MARKER="/data/gitea/.authentik_oidc_username"
if [ "${ENABLE_GITEA_AUTHENTIK_OIDC:-false}" = "true" ] && [ -n "${GITEA_OIDC_CLIENT_ID:-}" ] && [ ! -f "$AUTHENTIK_OIDC_MARKER" ]; then
    echo "Configuring Authentik OIDC for Gitea (admin group: ${GITEA_SSO_ADMIN_GROUP})..."
    if su-exec git gitea admin auth add-oauth \
        --name "Authentik" \
        --provider "openidConnect" \
        --key "${GITEA_OIDC_CLIENT_ID}" \
        --secret "${GITEA_OIDC_CLIENT_SECRET}" \
        --auto-discover-url "https://${AUTHENTIK_DOMAIN:-auth.localhost}/application/o/gitea-oidc/.well-known/openid-configuration" \
        --scopes "openid" --scopes "email" --scopes "profile" --scopes "groups" \
        --group-claim-name "groups" \
        --admin-group "${GITEA_SSO_ADMIN_GROUP}" \
        --username "preferred_username" \
        --skip-local-2fa 2>&1; then
        touch "$AUTHENTIK_OIDC_MARKER" "$AUTHENTIK_OIDC_GRPMAP_MARKER" "$AUTHENTIK_OIDC_USERNAME_MARKER"
        echo "Authentik OIDC configuration complete (domain: ${AUTHENTIK_DOMAIN:-auth.localhost})"
    else
        echo "ERROR: Failed to configure Authentik OIDC for Gitea"
    fi
elif [ "${ENABLE_GITEA_AUTHENTIK_OIDC:-false}" = "true" ] && [ -n "${GITEA_OIDC_CLIENT_ID:-}" ] && [ -f "$AUTHENTIK_OIDC_MARKER" ] && { [ ! -f "$AUTHENTIK_OIDC_GRPMAP_MARKER" ] || [ ! -f "$AUTHENTIK_OIDC_USERNAME_MARKER" ]; }; then
    # Retrofit the #33 group->admin mapping and the #218 username attribute onto
    # an already-configured source. Both ride ONE update-oauth call and one
    # branch: as separate elif stages, a box missing both markers would only
    # ever fix one per boot, and the second would wait for a restart nobody has
    # a reason to perform.
    OIDC_SOURCE_ID=$(su-exec git gitea admin auth list 2>/dev/null | awk '$2=="Authentik"{print $1; exit}')
    if [ -n "$OIDC_SOURCE_ID" ]; then
        echo "Retrofitting Authentik OIDC attributes (source id ${OIDC_SOURCE_ID}, admin group: ${GITEA_SSO_ADMIN_GROUP}, username: preferred_username)..."
        if su-exec git gitea admin auth update-oauth \
            --id "$OIDC_SOURCE_ID" \
            --scopes "openid" --scopes "email" --scopes "profile" --scopes "groups" \
            --group-claim-name "groups" \
            --admin-group "${GITEA_SSO_ADMIN_GROUP}" \
            --username "preferred_username" 2>&1; then
            touch "$AUTHENTIK_OIDC_GRPMAP_MARKER" "$AUTHENTIK_OIDC_USERNAME_MARKER"
            echo "Authentik OIDC attributes applied (group->admin mapping, preferred_username)"
            echo "NOTE: this changes how FUTURE logins are named. Accounts already created under the hex 'sub' keep that name until an admin renames them — see #218."
        else
            echo "ERROR: Failed to retrofit Authentik OIDC attributes"
        fi
    else
        echo "WARNING: ENABLE_GITEA_AUTHENTIK_OIDC=true but no 'Authentik' OAuth source found to update — skipping attribute retrofit"
    fi
elif [ "${ENABLE_GITEA_AUTHENTIK_OIDC:-false}" = "true" ] && [ -z "${GITEA_OIDC_CLIENT_ID:-}" ]; then
    echo "WARNING: ENABLE_GITEA_AUTHENTIK_OIDC=true but GITEA_OIDC_CLIENT_ID is empty — skipping"
else
    echo "ENABLE_GITEA_AUTHENTIK_OIDC not set or false — skipping Authentik OIDC"
fi

# Wait for Gitea process
wait $GITEA_PID
