FROM python:3.11-alpine

# Install Docker CLI and other utils.
# rc6.7 #7: gnupg added so backup_manager.decrypt_backup_to_temp can decrypt
# .tar.gz.gpg backups (F-A2 encryption, rc2+) before tarfile.open.
RUN apk add --no-cache docker-cli postgresql-client tar curl openssl gnupg

WORKDIR /app

# M026 S04: build context is now ./core (sibling subdirs are visible),
# so all COPY paths must be prefixed with the per-container subdir.
# M026 S05 #4: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common/

COPY backup/manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
