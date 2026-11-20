FROM python:3.11-alpine@sha256:09d17d5e4c16d0a7a1eea690bcb7c6ab8616b211f8421e429e97c1bef5bd0ed2

# #2336: bring the Alpine base to today's security level.
RUN apk upgrade --no-cache
# #2336: python:3.11-alpine ships pip/setuptools/wheel of its release date; the
# vendored jaraco.context and wheel inside that setuptools carry published
# High advisories (fixed in jaraco.context 6.1.0 / wheel 0.46.2, both bundled by
# setuptools 84.2.1). Same three pins in every python:3.11-alpine portal
# (tests/unit/consistency/test_2336_image_cve_hygiene.py).
RUN pip install --no-cache-dir --upgrade pip==26.2.2 setuptools==84.2.1 wheel==0.48.0

WORKDIR /app

# #54 — the password broker shells out to the rzfz set-password helper, which
# runs `docker exec` (via docker-socket-proxy) and curls the Authentik API.
# bash: the helper + lib.sh are bash scripts. docker-cli: `docker exec`.
# curl: Authentik set_password API.
RUN apk add --no-cache bash docker-cli curl \
    && pip install --no-cache-dir flask==3.1.3 gunicorn==26.2.0 psycopg2-binary==2.9.13 requests==2.34.2 pyyaml==6.0.3 urllib3==2.8.0

# Build context is the REPO ROOT (see compose; #54 changed it from ./core so
# the cli/ helper + scripts/lib.sh can be baked in). Install razzfazz-common
# BEFORE app code so `from razzfazz_common.X import Y` works at import time.
COPY core/common/ /app/common/
RUN pip install --no-cache-dir /app/common/
# #2336: pip 26.2.2 (the newest) vendors urllib3 2.7.0, msgpack 1.1.2 and
# setuptools 70.3.0 and lists them in its own SBOM, so a scanner reports their
# advisories against the image although nothing here imports them. No pin can fix
# that, and nothing at runtime calls pip: drop it after the last install
# (tests/unit/consistency/test_2336_portal_tooling_pins.py).
RUN pip uninstall -y pip

COPY core/start-portal/app.py .
COPY core/start-portal/scenario_templates.py .
COPY core/start-portal/password_broker.py .
# #185 — razzfazz-shortcuts domain logic (imported by app.py at startup).
COPY core/start-portal/shortcuts.py .
# #68 — source-IP anchor (rejects forged X-Authentik-* headers from any peer
# that didn't come through Caddy). Imported by app.py at startup.
COPY core/start-portal/proxy_anchor.py .
COPY core/start-portal/manifest.yaml .
COPY core/start-portal/templates/ templates/
COPY core/start-portal/static/ static/
COPY core/Authentik/media/rzfz-ai-logo.svg static/rzfz-ai-logo.svg

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
