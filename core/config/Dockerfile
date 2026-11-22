FROM python:3.11-alpine@sha256:09d17d5e4c16d0a7a1eea690bcb7c6ab8616b211f8421e429e97c1bef5bd0ed2

# Docker CLI: control the stack (apply flow runs `docker compose up -d`).
# git: check the upstream manifest via the smart-HTTP protocol — works
# through Authentik's proxy on git.razzfazz.ai whereas /raw/ paths just
# get the SSO login page.
# bash + curl: the M029-S04 LLM Runtime toggle invokes
# razzfazz-post-install.sh --refresh after a successful flip to re-mint
# the API key + re-register custom backends. The script's shebang is
# #!/bin/bash and it uses curl + ensure_gpustack_api_key helpers (rc6.9).
RUN apk upgrade --no-cache && apk add --no-cache docker-cli docker-cli-compose git bash curl
# #2336: python:3.11-alpine ships pip/setuptools/wheel of its release date; the
# vendored jaraco.context and wheel inside that setuptools carry published
# High advisories (fixed in jaraco.context 6.1.0 / wheel 0.46.2, both bundled by
# setuptools 84.2.1). Same three pins in every python:3.11-alpine portal
# (tests/unit/consistency/test_2336_image_cve_hygiene.py).
RUN pip install --no-cache-dir --upgrade pip==26.2.2 setuptools==84.2.1 wheel==0.48.0

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
# #2336: pip 26.2.2 (the newest) vendors urllib3 2.7.0, msgpack 1.1.2 and
# setuptools 70.3.0 and lists them in its own SBOM, so a scanner reports their
# advisories against the image although nothing here imports them. No pin can fix
# that, and nothing at runtime calls pip: drop it after the last install
# (tests/unit/consistency/test_2336_portal_tooling_pins.py).
RUN pip uninstall -y pip

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
# #1307: `--timeout` is what gunicorn allows a SYNC worker to spend inside one
# request, and the SSE apply-stream is exactly that. 300 s killed the worker in
# the middle of a `wazuh` enable (measured on 0.91: `[CRITICAL] WORKER TIMEOUT`
# at 19:08:01, stream dead at 19:08:33) while the apply thread ran on to the
# end. The rotation's own budget for the heaviest module is 900 s
# (`health_timeout_for(5120 MB)`), and an apply also pulls images before that,
# so the worker has to be allowed to outlive it. Pinned against that budget by
# tests/unit/razzfazz-config/test_1307_apply_stream_budget.py.
#
# This alone is not the fix: a stream can still be cut by a proxy or a client.
# The result is therefore also retrievable at GET /api/apply/<action_id>, so a
# broken stream costs a request, not the answer.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "1800", "app:create_app()"]
