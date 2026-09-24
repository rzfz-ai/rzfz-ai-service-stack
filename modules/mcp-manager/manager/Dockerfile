FROM python:3.12-alpine

# postgresql-client + curl for the healthcheck; build deps only needed if a
# cryptography wheel isn't available for the platform (alpine/musl) — kept
# minimal and in the same layer.
RUN apk add --no-cache postgresql-client curl \
    && apk add --no-cache --virtual .build gcc musl-dev libffi-dev openssl-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apk del .build

COPY . .

# NOTE: runs as root — talks to docker-socket-proxy (not the raw socket) with
# the SAME constrained grants as agent-manager. SECURITY: all routes are behind
# Authentik SSO (Caddy forward_auth); credentials are AES-GCM encrypted at rest.

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5000/healthz || exit 1

# Single worker: the boot route-reconcile scheduler runs in-process.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "app:create_app()"]
