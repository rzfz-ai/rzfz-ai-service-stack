# F-069: Pre-install runtime dependencies instead of installing them in pre-backup.sh.
# The offen/docker-volume-backup image is Alpine-based. We add docker-cli and openssl
# at build time so the pre-backup script does not need network access or apk at runtime.
# #1969: the default is the pinned release, never latest - a direct `docker build`
# (how an incident gets reproduced) must yield the image the stack runs. Kept
# identical to config/.env.example and core/compose.yml by test_1969.
ARG BACKUP_VERSION=v2.50.0

# #2336: the upstream release binary is built with the Go toolchain and module set
# of its release day; the Go standard library, grpc, x/crypto, x/text and
# klauspost/compress in it carry published High advisories (18 findings, trivy
# 0.71.2). So the `backup` binary is rebuilt here from the SAME upstream tag on a
# current Go toolchain, with those modules raised to the releases that close them
# (versions from the OSV/Go vulnerability data, each present on proxy.golang.org;
# x/net 0.60 pulls x/crypto 0.57 and x/text 0.42 with it; the one x/crypto
# advisory without a fixed release stays). The result replaces /usr/bin/backup of
# the upstream image below; everything else in that image (alpine,
# ca-certificates, tzdata) stays. `go build` of the unmodified source is what
# upstream's own Dockerfile does.
# v2.49 builds with Go 1.27 and already requires grpc v1.84.0 and
# klauspost/compress v1.20.1; `go get` at a LOWER version would downgrade them,
# so the pins below follow upstream's go.mod (x/crypto v0.57.0, x/text v0.42.0)
# and raise only x/net. Re-read go.mod of the new tag on every BACKUP_VERSION bump.
FROM golang:1.27.3-alpine3.24@sha256:f2c872bcee949b7be9c490efd02b48a8a2fbf947cdf533f1c58525ec9d8170fd AS backup-build
ARG BACKUP_VERSION
RUN apk add --no-cache git
RUN test -n "${BACKUP_VERSION}" || { echo "ERROR: BACKUP_VERSION is empty; refusing to clone a floating ref" >&2; exit 1; }
RUN git clone --depth 1 --branch "${BACKUP_VERSION}" https://github.com/offen/docker-volume-backup /src
WORKDIR /src
RUN set -eu; \
    go get golang.org/x/crypto@v0.57.0 golang.org/x/net@v0.60.0 golang.org/x/text@v0.42.0 google.golang.org/grpc@v1.84.0 github.com/klauspost/compress@v1.20.1; \
    (cd cmd/backup && go build -o /out/backup .); \
    go version -m /out/backup > /tmp/backup.mods; \
    head -n1 /tmp/backup.mods | grep -qx '/out/backup: go1.27.3' \
        || { echo "FATAL(#2336): backup was not built with go1.27.3" >&2; head -n1 /tmp/backup.mods >&2; exit 1; }; \
    for m in golang.org/x/crypto:v0.57.0 golang.org/x/net:v0.60.0 golang.org/x/text:v0.42.0 google.golang.org/grpc:v1.84.0 github.com/klauspost/compress:v1.20.1; do \
        awk -v n="${m%%:*}" -v v="${m##*:}" '($1=="dep"||$1=="mod") && $2==n && $3==v {f=1} END{exit !f}' /tmp/backup.mods \
            || { echo "FATAL(#2336): backup was built without $m" >&2; exit 1; }; \
    done

FROM offen/docker-volume-backup:${BACKUP_VERSION}

RUN apk upgrade --no-cache && apk add --no-cache docker-cli openssl

# BSB-04 / R-DEF-04: install the razzfazz-backup wrapper. Re-reads
# BACKUP_ENCRYPTION_PASSWORD from /scripts/dot-env (the bind-mounted host
# .env) at every invocation, then exec's offen's /usr/bin/backup. Lets
# manually-triggered backups (CLI + UI) pick up a rotated passphrase
# without needing `compose up -d --force-recreate backup-service`. See
# core/backup/razzfazz-backup-wrapper.sh for the full rationale.
COPY razzfazz-backup-wrapper.sh /usr/local/bin/razzfazz-backup
RUN chmod +x /usr/local/bin/razzfazz-backup

# #2336: the rebuilt binary (see the backup-build stage above).
COPY --from=backup-build /out/backup /usr/bin/backup
