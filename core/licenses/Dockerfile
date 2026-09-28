FROM python:3.11-alpine

WORKDIR /app

RUN pip install --no-cache-dir flask requests gunicorn

# M026 S04: build context is now ./core (sibling subdirs are visible),
# so all COPY paths must be prefixed with the per-container subdir.
# M026 S05 #1: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common/

# #1892: licence texts are VENDORED in-tree (core/licenses/vendored/) and the
# build COPYs them — no fetch at build time, so a GitHub throttle can never
# fail the build or roll back an upgrade (the hazard #1888 exposed). Refreshing
# the vendored set is a deliberate maintainer step (`download_licenses.py`),
# guarded by tests/unit/razzfazz-licenses/test_1892_vendored_licenses.py.
COPY licenses/vendored/licenses.json ./licenses.json
COPY licenses/vendored/ ./static/licenses/

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
