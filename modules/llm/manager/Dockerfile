# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# LLM Manager (manager) — FastAPI control plane (#254 Phase 1).

# ── #1195: Swagger UI assets for the self-hosted /docs page ───────────────────
# The manager domain's CSP is `script-src 'self'` (#347) and the box must work
# air-gapped, so /docs serves Swagger UI SAME-ORIGIN from this image. The assets
# are fetched at IMAGE BUILD time from the npm registry (exact pin below) and
# copied into app/static/vendor/swagger-ui/ — never committed (`vendor/` is
# gitignored) and never fetched by a browser or a running box. Offline packages
# ship the built image, so an air-gapped box needs nothing from the internet.
#
# WHY BUILD-TIME VENDORING and not a committed copy like the four `.gitignore`
# re-includes (start-portal Sortable, help Mermaid, agent-manager xterm, cognee
# ladybug): this is the DECISION-7c pattern the help image uses for `monolith`
# (core/help/Dockerfile, tests/unit/consistency/test_824_vendored_monolith.py) —
# a 1.5 MB minified bundle in git is unreviewable and un-diffable, whereas a
# version + sha256 pin IS reviewable, and the offline package already carries
# the built image. The pin lives in ONE place (the two ARGs below) and is
# mirrored in config/manifests/versions.json (custom_built.razzfazz-llm-manager)
# so check-and-bump-versions / VERSIONS.md see it; tests/unit/llm-manager/
# test_1195_docs_selfhosted.py fails when the two drift apart.
#
# `npm pack` on its own is VERSION-pinned, not CONTENT-pinned (no lockfile, so
# the only integrity check is the registry's own packument). The tarball is
# therefore verified against SWAGGER_UI_DIST_SHA256 before anything is
# extracted — computed 2026-09-03 from the downloaded
# swagger-ui-dist-5.32.14.tgz (npm integrity of the same file:
# sha512-nOA2pSQhcmODMUQZpJHYKNuwniDUqcOWGNaSCOoZv12FdOSJ9JxV95HtyRGNMqEBj6h6lCNTy20TgZDYTSuUIg==).
# A version bump moves BOTH ARGs together, plus the manifest entry.
#
# Audited at 5.32.14: swagger-ui.css uses data: URIs only (no @import, no remote
# url()); swagger-ui-bundle.js has no CDN loader; its single `new Function` is
# webpack's globalThis polyfill inside a try that modern browsers never reach,
# so no 'unsafe-eval' is needed. Bump deliberately: change the ARGs, rebuild,
# re-run tests/unit/llm-manager/test_1195_docs_selfhosted.py and open /docs on
# a box with the CSP in place (the audit above is per-version).
ARG SWAGGER_UI_DIST_VERSION=5.32.14
ARG SWAGGER_UI_DIST_SHA256=609702d791d8d3cdcbc3a52632f6be2f9b743eadf6ba49ca9737dac2a6e0b2a3
FROM node:24-alpine AS swagger-ui
ARG SWAGGER_UI_DIST_VERSION
ARG SWAGGER_UI_DIST_SHA256
WORKDIR /swagger
# EVERY failure here is LOUD: `set -eu`, `sha256sum -c` on the tarball before
# it is unpacked, and no `|| true` anywhere — a checksum mismatch fails the
# image build, it never yields an image with an unverified bundle inside.
RUN set -eu; \
    npm pack "swagger-ui-dist@${SWAGGER_UI_DIST_VERSION}" --pack-destination /swagger --no-audit --no-fund >/dev/null; \
    printf '%s  %s\n' "${SWAGGER_UI_DIST_SHA256}" "/swagger/swagger-ui-dist-${SWAGGER_UI_DIST_VERSION}.tgz" > /swagger/swagger-ui-dist.tgz.sha256; \
    sha256sum -c /swagger/swagger-ui-dist.tgz.sha256; \
    tar -xzf "swagger-ui-dist-${SWAGGER_UI_DIST_VERSION}.tgz"; \
    mkdir -p /out; \
    cp package/swagger-ui-bundle.js package/swagger-ui.css package/favicon-32x32.png \
       package/LICENSE package/NOTICE package/swagger-ui-bundle.js.LICENSE.txt /out/; \
    printf 'swagger-ui-dist %s (Apache-2.0) — fetched at image build from https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-%s.tgz, tarball sha256 %s (#1195)\n' \
       "${SWAGGER_UI_DIST_VERSION}" "${SWAGGER_UI_DIST_VERSION}" "${SWAGGER_UI_DIST_SHA256}" > /out/VERSION

FROM python:3.12-slim

# psql client (entrypoint waits for postgres); build tools for psycopg2 are
# avoided by using the -binary wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# #1195: the build-time-fetched Swagger UI (see the swagger-ui stage above).
# Served by app/docs.py at /docs/static/vendor/swagger-ui/*.
COPY --from=swagger-ui /out/ /app/app/static/vendor/swagger-ui/

# Run migrations then serve. entrypoint.sh runs `alembic upgrade head`
# before launching uvicorn (idempotent; safe on every start).
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# SINGLE WORKER — deliberate, and load-bearing (#359).
#
# Correctness-relevant state lives in module-level Python objects, so a second
# uvicorn worker degrades behaviour SILENTLY rather than loudly:
#
#   app/api/workers.py  _undeploy_tombstones  an undeploy handled by worker A
#                                             does not suppress the re-registration
#                                             handled by worker B, so #298b's
#                                             resurrection bug returns
#                                             non-deterministically. This is the
#                                             one that is outright WRONG.
#   app/proxy.py        _metering_mode_cache  a PATCH clears only the worker that
#                                             served it; the others keep the old
#                                             mode until the 3s TTL lapses.
#   app/metrics.py      _STATE                /metrics reports whichever worker
#                                             answered the scrape, so token totals
#                                             under-report by ~1/N and jump between
#                                             scrapes.
#
# `--workers 4` is the obvious first move if the manager is ever a throughput
# bottleneck — which #322 makes plausible, since "add workers" is exactly the fix
# someone reaches for when the hot path is slow. Do NOT add it here until the
# tombstones (at minimum) live in Valkey; they already have a natural TTL.
#
# This is not merely documented: tests/unit/llm-manager/test_single_worker.py
# FAILS if a worker flag appears here while that state is still process-local.
#
# --ws-max-size 33554432 (32 MiB): defense-in-depth so the master's inbound
# WS frame cap matches `relay_protocol.MAX_FRAME_SIZE` and the node's own
# `websockets.connect(..., max_size=...)` (#262 pre-ship review, I1) — a
# `req` frame carries the whole request body, and uvicorn's/websockets'
# 1 MiB default would otherwise drop a large RAG/long-context/embedding
# request before it ever reaches this side. Response chunks are
# streaming-small so this rarely bites in practice; kept in sync anyway.
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--ws-max-size", "33554432"]
