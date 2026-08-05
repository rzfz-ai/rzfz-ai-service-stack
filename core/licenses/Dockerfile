FROM python:3.11-alpine

WORKDIR /app

RUN pip install --no-cache-dir flask requests gunicorn

# M026 S04: build context is now ./core (sibling subdirs are visible),
# so all COPY paths must be prefixed with the per-container subdir.
# M026 S05 #1: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common/

COPY licenses/download_licenses.py .
RUN python download_licenses.py

COPY licenses/app.py .
COPY licenses/templates/ templates/
# M026 S07 #1: container-local CSS overrides for the licenses page.
# /app/static/media is bind-mounted (Authentik branding); this CSS file
# sits alongside it in /app/static/ so Flask's default static endpoint
# serves it at /static/licenses-overrides.css.
COPY licenses/static/ static/

# Run as non-root user
RUN addgroup -S appuser && adduser -S -G appuser appuser \
    && chown -R appuser:appuser /app
USER appuser

# Data provided via bind mount or env vars
EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
