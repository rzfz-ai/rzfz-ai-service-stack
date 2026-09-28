# F-069: Pre-install runtime dependencies instead of installing them in pre-backup.sh.
# The offen/docker-volume-backup image is Alpine-based. We add docker-cli and openssl
# at build time so the pre-backup script does not need network access or apk at runtime.
# #1969: the default is the pinned release, never latest - a direct `docker build`
# (how an incident gets reproduced) must yield the image the stack runs. Kept
# identical to config/.env.example and core/compose.yml by test_1969.
ARG BACKUP_VERSION=v2.48.2
FROM offen/docker-volume-backup:${BACKUP_VERSION}

RUN apk add --no-cache docker-cli openssl

# BSB-04 / R-DEF-04: install the razzfazz-backup wrapper. Re-reads
# BACKUP_ENCRYPTION_PASSWORD from /scripts/dot-env (the bind-mounted host
# .env) at every invocation, then exec's offen's /usr/bin/backup. Lets
# manually-triggered backups (CLI + UI) pick up a rotated passphrase
# without needing `compose up -d --force-recreate backup-service`. See
# core/backup/razzfazz-backup-wrapper.sh for the full rationale.
COPY razzfazz-backup-wrapper.sh /usr/local/bin/razzfazz-backup
RUN chmod +x /usr/local/bin/razzfazz-backup
