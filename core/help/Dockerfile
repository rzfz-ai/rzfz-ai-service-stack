FROM python:3.11-alpine

# wget is needed for mirroring documentation; su-exec lets the entrypoint
# fix /data ownership as root before dropping to appuser (see entrypoint.sh).
RUN apk add --no-cache wget su-exec

WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir flask gunicorn markdown requests bleach

# 2026.07 docs rework: the build context is now the REPO ROOT (was ./core) so
# the Dockerfile can COPY the canonical customer docs from docs/enterprise/ into
# the image. All other COPY paths therefore gain the `core/` prefix.
#
# M026 S05 #2: install razzfazz-common (shared Flask helpers) BEFORE app code
# so `from razzfazz_common.X import Y` works at import time.
COPY core/common/ /app/common/
RUN pip install --no-cache-dir /app/common/

# Copy application code
COPY core/help/app.py .
COPY core/help/cache_manager.py .
COPY core/help/mirror_config.json .
COPY core/help/templates/ templates/
# M026 S07 #2: help-overrides.css (paired with shared razzfazz-base.css
# served by razzfazz_common at /static/razzfazz-common/). The /app/static/
# directory also receives the Authentik media bind-mount at runtime
# (see core/compose.yml razzfazz-help volumes). static/vendor/ holds the
# air-gapped mermaid.min.js the viewer loads for ```mermaid diagrams.
COPY core/help/static/ static/

# #125 P1: the Help-UI BASE own_docs are the COMMUNITY docs (docs/community/**.md),
# present in BOTH the internal tree AND the Codeberg export — so this image builds
# on a public/Codeberg box too (the old `COPY docs/enterprise/` FAILED there because
# docs/enterprise/ is not shipped publicly). The gated Enterprise docs are NOT baked;
# they are RUNTIME-MOUNTED as a box-local, gitignored overlay at /app/own_docs/
# enterprise (see core/compose.yml razzfazz-help volumes + scripts/sync-enterprise-
# overlay.sh). When the overlay is empty/absent the hub is community-only (no crash).
# The own_docs nav/index is AUTO-DISCOVERED from the whole /app/own_docs tree (see
# cache_manager.discover_own_docs); there is no hand-maintained own_docs manifest.
# Override with HELP_OWN_DOCS_DIR to point at a live bind-mount during authoring.
COPY docs/community/ own_docs/

# #149: box-local MODULE help pages — curated own-docs that REPLACE the upstream
# doc mirror for modules whose SPA/CDN docs can't be reliably wget-mirrored
# (dify, cognee, openhands, gotenberg, komodo, lightrag). Kept in a SEPARATE
# /app/module_docs tree (NOT /app/own_docs) so they render as their normal
# "Application Help" cards, not as auto-discovered rzfz.ai Guides. An app opts in
# via a "local_doc" field in mirror_config.json; cache_manager.render_local_doc
# reads them from LOCAL_DOCS_DIR (default /app/module_docs).
COPY core/help/module_docs/ module_docs/

# Run as non-root user. The entrypoint stays as root so it can fix /data
# ownership on existing volumes (see help/entrypoint.sh + commit message),
# then immediately drops to appuser via su-exec.
RUN addgroup -S appuser && adduser -S -G appuser appuser \
    && mkdir -p /data && chown -R appuser:appuser /app /data

# Data volume for cached documentation
VOLUME /data

COPY core/help/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

# Using Gunicorn for production
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "300", "app:app"]
