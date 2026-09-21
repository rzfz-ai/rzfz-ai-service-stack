# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# llm-worker-agent (#254 Phase-2 skeleton). The real engine drivers + GPU
# exporter + atomic pull land as box work; this image serves the §7 contract.
FROM python:3.12-slim

# `git` (#817): the #291 dev-version stamp shells out to `git describe` against
# the read-only `.git` bind from compose.yml. Without the binary the call raises
# FileNotFoundError, the best-effort `except` swallows it, and the console keeps
# showing the stale GA tag with no diagnostic anywhere — the bind mount alone
# does not fix the issue.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*

# #1932: the image says what it IS. A thin node has no repo, so neither leg of
# `resolve_stack_version` can answer there — `git describe` has nothing to read
# and `RAZZFAZZ_VERSION` is not in `config/node.env.example`. Measured on 0.91
# (2026-09-10): `gb10-191` reported `ready` with `stack_version: None`, which
# makes "must I raise this worker for fix X?" unanswerable for exactly the node
# shape the 2026.09 cutover targets.
#
# Operator decision (2026-09-10): the agent reports its OWN image version
# rather than a stack version it cannot see. The value is stamped HERE, at build
# time, because that is what makes it un-stale-able: the only way to change it
# is to build a new image, and a new image is exactly what the value describes.
# An `.env` number goes stale on the next upgrade — the objection #291 was built
# for, and the reason the full node prefers `git describe`.
#
# `unknown` and not empty: a thin node whose builder passed nothing must say
# "I do not know" rather than look like a node that answered. The tag is
# `:latest` (floating), so the tag itself carries nothing.
ARG RAZZFAZZ_IMAGE_VERSION=unknown
ENV RAZZFAZZ_IMAGE_VERSION=${RAZZFAZZ_IMAGE_VERSION}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# NODE-18: `.dockerignore` keeps the compose files / README / caches out of the
# context, so this copies the code and nothing else.
COPY . .

# NODE-18 (deferred, #1037): still no USER — uvicorn runs as root. The agent
# needs no host device access (it drives docker over TCP through the scoped
# socket-proxy), but it DOES write weights into the /models volume, which is
# root-owned on every existing box; de-rooting therefore needs a volume chown
# migration verified on a live node, exactly like modules/llm/compose.nonroot.yml
# documents for gpustack. Not bundled with the off-box fixes.

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8090/health || exit 1

CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090"]
