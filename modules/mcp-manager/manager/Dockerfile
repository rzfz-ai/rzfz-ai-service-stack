FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

# postgresql-client + curl for the healthcheck; build deps only needed if a
# cryptography wheel isn't available for the platform (alpine/musl) — kept
# minimal and in the same layer.
RUN apk upgrade --no-cache \
    && apk add --no-cache postgresql-client curl \
    && apk add --no-cache --virtual .build gcc musl-dev libffi-dev openssl-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apk del .build

COPY . .

# #3433: the audit file's directory (the mcp-manager-audit volume), owned by
# the uid the service runs as, so a fresh volume is writable for it.
RUN mkdir -p /var/log/razzfazz && chown 100:1000 /var/log/razzfazz && chmod 0750 /var/log/razzfazz

# NOTE: runs as root — talks to docker-socket-proxy (not the raw socket) with
# the SAME constrained grants as agent-manager. SECURITY: all routes are behind
# Authentik SSO (Caddy forward_auth); credentials are AES-GCM encrypted at rest.

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl --noproxy '*' -f http://localhost:5000/healthz || exit 1

# #2472 (B4 decision record §3(g)): ONE gthread worker with 64 threads, so a
# held-open MCP gateway call never blocks other callers or the portal. Why one
# process and how the timeout is derived: gunicorn.conf.py. (The boot
# route-reconcile scheduler the old single-worker comment named is gone, #619.)
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
