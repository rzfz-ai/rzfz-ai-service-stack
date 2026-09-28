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

FROM python:3.12-slim

RUN pip install --no-cache-dir requests==2.32.3

# Docker CLI for triggering the pipelines container restart (Duty B step 3)
RUN apt-get update \
    # #2327: the seeder's only docker call is `docker restart pipelines`. On Debian 13
    # `docker.io` ships the DAEMON only (dockerd, docker-proxy, docker-init); the CLI
    # moved to `docker-cli`. With docker.io the binary was absent, FileNotFoundError
    # was downgraded to a WARN, and Duty B's pipelines never took effect until an
    # unrelated restart — on every install since the base moved. docker-cli is also
    # ~230 MB smaller and drops ~60 High CVEs (runc, containerd, Go stdlib) from the image.
    && apt-get install -y --no-install-recommends docker-cli ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY seed.py /app/seed.py

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "/app/seed.py"]
