FROM python:3.11-alpine@sha256:09d17d5e4c16d0a7a1eea690bcb7c6ab8616b211f8421e429e97c1bef5bd0ed2

# Install Docker CLI and other utils.
# rc6.7 #7: gnupg added so backup_manager.decrypt_backup_to_temp can decrypt
# .tar.gz.gpg backups (F-A2 encryption, rc2+) before tarfile.open.
RUN apk upgrade --no-cache && apk add --no-cache docker-cli postgresql-client tar curl openssl gnupg
# #2336: python:3.11-alpine ships pip/setuptools/wheel of its release date; the
# vendored jaraco.context and wheel inside that setuptools carry published
# High advisories (fixed in jaraco.context 6.1.0 / wheel 0.46.2, both bundled by
# setuptools 84.2.1). Same three pins in every python:3.11-alpine portal
# (tests/unit/consistency/test_2336_image_cve_hygiene.py).
RUN pip install --no-cache-dir --upgrade pip==26.2.2 setuptools==84.2.1 wheel==0.48.0

WORKDIR /app

# M026 S04: build context is now ./core (sibling subdirs are visible),
# so all COPY paths must be prefixed with the per-container subdir.
# M026 S05 #4: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common/

COPY backup/manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# #2336: pip 26.2.2 (the newest) vendors urllib3 2.7.0, msgpack 1.1.2 and
# setuptools 70.3.0 and lists them in its own SBOM, so a scanner reports their
# advisories against the image although nothing here imports them. No pin can fix
# that, and nothing at runtime calls pip: drop it after the last install
# (tests/unit/consistency/test_2336_portal_tooling_pins.py).
RUN pip uninstall -y pip

# Pre-rc8 the Dockerfile used `COPY . .` against a per-container context.
# With the M026 S04 context shift to ./core, `COPY backup/manager/. .`
# preserves the same set (app.py, backup_manager.py, templates/, etc.)
# while explicitly excluding sibling subdirs like setup/ or config/.
COPY backup/manager/. .

EXPOSE 5000

# F-049: Container binds to 0.0.0.0 inside its network namespace. External access is
# restricted by compose (127.0.0.1:5005:5000) and Caddy reverse proxy with Authentik auth.
# Binding to 127.0.0.1 here would break Docker inter-container networking (Caddy -> this service).
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "1200", "app:app"]
