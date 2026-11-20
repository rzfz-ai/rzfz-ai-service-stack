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

RUN pip install --no-cache-dir flask==3.1.3 requests==2.34.2 gunicorn==26.2.0 urllib3==2.8.0

# M026 S04: build context is now ./core (sibling subdirs are visible),
# so all COPY paths must be prefixed with the per-container subdir.
# M026 S05 #1: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common/
# #2336: pip 26.2.2 (the newest) vendors urllib3 2.7.0, msgpack 1.1.2 and
# setuptools 70.3.0 and lists them in its own SBOM, so a scanner reports their
# advisories against the image although nothing here imports them. No pin can fix
# that, and nothing at runtime calls pip: drop it after the last install
# (tests/unit/consistency/test_2336_portal_tooling_pins.py).
RUN pip uninstall -y pip

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
