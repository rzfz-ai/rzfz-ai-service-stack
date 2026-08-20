FROM python:3.11-alpine

WORKDIR /app

# #54 — the password broker shells out to the rzfz set-password helper, which
# runs `docker exec` (via docker-socket-proxy) and curls the Authentik API.
# bash: the helper + lib.sh are bash scripts. docker-cli: `docker exec`.
# curl: Authentik set_password API.
RUN apk add --no-cache bash docker-cli curl \
    && pip install --no-cache-dir flask gunicorn psycopg2-binary requests pyyaml

# Build context is the REPO ROOT (see compose; #54 changed it from ./core so
# the cli/ helper + scripts/lib.sh can be baked in). Install razzfazz-common
# BEFORE app code so `from razzfazz_common.X import Y` works at import time.
COPY core/common/ /app/common/
RUN pip install --no-cache-dir /app/common/

COPY core/start-portal/app.py .
COPY core/start-portal/password_broker.py .
# #185 — razzfazz-shortcuts domain logic (imported by app.py at startup).
COPY core/start-portal/shortcuts.py .
# #68 — source-IP anchor (rejects forged X-Authentik-* headers from any peer
# that didn't come through Caddy). Imported by app.py at startup.
COPY core/start-portal/proxy_anchor.py .
COPY core/start-portal/manifest.yaml .
COPY core/start-portal/templates/ templates/
COPY core/start-portal/static/ static/

# #54 — the shared set-password helper + its bash deps. set-password.sh sources
# scripts/lib.sh and cli/lib-set-password.sh — the SAME implementation the
# operator CLI (cli/set-admin-password.sh) uses, so there's one fan-out path.
COPY scripts/lib.sh /app/scripts/lib.sh
COPY cli/lib-set-password.sh /app/cli/lib-set-password.sh
COPY cli/set-password.sh /app/cli/set-password.sh
RUN chmod +x /app/cli/set-password.sh

# Run as non-root user
RUN addgroup -S appuser && adduser -S -G appuser appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# 2 workers — same as the other razzfazz-* Flask UIs.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
