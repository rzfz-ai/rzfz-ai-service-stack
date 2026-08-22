FROM python:3.11-alpine

# Docker CLI: control the stack (apply flow runs `docker compose up -d`).
# git: check the upstream manifest via the smart-HTTP protocol — works
# through Authentik's proxy on git.razzfazz.ai whereas /raw/ paths just
# get the SSO login page.
# bash + curl: the M029-S04 LLM Runtime toggle invokes
# razzfazz-post-install.sh --refresh after a successful flip to re-mint
# the API key + re-register custom backends. The script's shebang is
# #!/bin/bash and it uses curl + ensure_gpustack_api_key helpers (rc6.9).
RUN apk add --no-cache docker-cli docker-cli-compose git bash curl

WORKDIR /app

# M026 S04: build context is now ./core (sibling subdirs are visible),
# so all COPY paths must be prefixed with the per-container subdir.
# M026 S05 #5: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common/

# Install Python dependencies
COPY config/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy profile manifest
COPY config/profiles.yaml .

# #184 P1 / WS7d — the model-sideload service reuses core/llm/model_source.py
# (the one place that builds the offline source=local_path registration spec).
# Build context is ./core, so COPY it in at /app so `import model_source` works
# at runtime (WORKDIR /app is on sys.path — same shape the model_source.py
# docstring documents). Single source of truth stays in core/llm.
COPY llm/model_source.py /app/model_source.py

# Copy application code
COPY config/app/ app/

# Copy documentation files
COPY config/own_docs/ own_docs/

# Create audit log directory
RUN mkdir -p /var/log/razzfazz

EXPOSE 5000

# Using Gunicorn for production — single worker to avoid Docker API contention
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "300", "app:create_app()"]
