# ==============================================================================
# OpenWebUI Function/Tool/Pipeline Seeder Sidecar (M019 S01)
# ==============================================================================
# One-shot container that runs alongside the openwebui + pipelines services
# on each `docker compose up`. Seeds:
#   Duty A — Functions and Tools into the openwebui container via its REST API
#   Duty B — Pipelines into the pipelines-data named volume (then triggers a
#            pipelines container restart so the new files are picked up)
#
# Reads source from bind-mounted /app/{functions,tools,pipelines}; targets
# /app/pipelines-target (the pipelines-data volume) for Duty B.
#
# Idempotent: re-runs check version/hash and skip if up-to-date. Set
# RAZZFAZZ_FORCE_RESEED=1 in the environment to overwrite regardless.
# ==============================================================================

# #3343 (2026.11-ga.1): Python 3.12.16 (3.12.15 carried a Critical CVE), on Debian 13.
FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

RUN pip install --no-cache-dir requests==2.32.3

# Docker CLI for triggering the pipelines container restart (Duty B step 3).
# #2327: the seeder's only docker call is `docker restart pipelines`, so only the CLI is
# needed, never the daemon (on Debian 13 `docker.io` IS the daemon, without a CLI).
# #3343 (2026.11-ga.1): Debian's `docker-cli` is built with go1.24 and carried 6 Critical
# Go-stdlib advisories into the 2026.11-ga SBOM. The static CLI binary of the official
# docker:<version>-cli image is built with a current Go; it is copied alone (no plugins).
COPY --from=docker:29.10.0-cli@sha256:34507eb5069d8be82c95dd3b6987d4250fd5fee49b82e2509b7d7a7030740d6d /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

WORKDIR /app
COPY seed.py /app/seed.py

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "/app/seed.py"]
