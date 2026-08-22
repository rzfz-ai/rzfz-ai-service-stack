# ==============================================================================
# CUSTOM COGNEE IMAGE (1.4.x line)
# ==============================================================================
# Extends the official Cognee image with litellm/tiktoken runtime patches for
# non-OpenAI embedding models served through GPUStack, and vendors the ladybug
# graph-extension so cognee needs NO network egress at build OR startup (#79).
#
# Base: cognee/cognee:1.4.0
# Graph backend: cognee swapped Kuzu for ladybug in 1.0.9 (kuzu==0.11.3 →
# ladybug); GRAPH_DATABASE_PROVIDER=kuzu still routes through ladybug. The
# ladybug version tracks the cognee base: 1.1.2 → ladybug 0.16.x, 1.2.2 →
# ladybug 0.17.x, 1.4.0 → ladybug 0.17.1 (uv.lock). Existing on-disk Kuzu data
# isn't documented as compat — base bumps are paired with a wipe of the
# cognee-data volume (no prod data) OR the #186 migration below.
#
# 2026.08 BUMP 1.2.2 → 1.4.0 (#79 coupling — VERIFIED, NOT a blocker):
#   cognee 1.4.0 pins `ladybug>=0.16.0,<0.18` — IDENTICAL to 1.2.2 — and its
#   uv.lock resolves ladybug 0.17.1, whose extension-release dir is /0.17.0/
#   (both 0.17.0 and 0.17.1 map to the 0.17.0 extension). So LBUG_EXT_VER=0.17.0
#   below is CORRECT and UNCHANGED for 1.4.0 — no re-fetch. The #186 block also
#   vendors the 0.16.0 old-engine extension, so the WHOLE >=0.16.0,<0.18 range
#   is present on-disk offline → no egress → no #79 deadlock at boot OR during
#   the migration's export leg. Migration API (cognee_db_workers.ladybug_migrate:
#   ladybug_migration / read_ladybug_storage_version) confirmed present at cognee
#   v1.4.0. STILL DO at cut: a real build + live test (not run on this host) and
#   verify/wipe cognee-data for the 1.2→1.4 pgvector schema churn.
# ==============================================================================

FROM cognee/cognee:1.4.0

# Bundle the ladybug "json" graph-extension so cognee needs NO egress at build OR startup.
# cognee's ladybug backend (GRAPH_DATABASE_PROVIDER=kuzu → ladybug) downloads
# libjson.lbug_extension from extension.ladybugdb.com the first time it starts; on an
# egress-restricted / hardened / airgapped box that download is blocked (prod 8.246 got 403) →
# the ladybug worker wedges → cognee's gunicorn worker DEADLOCKS on startup and never serves
# (#79). We vendor the binary (modules/knowledge/cognee/vendor/) and COPY it to ladybug's local
# extension cache so no download ever happens. LBUG_EXT_VER MUST match the ladybug version in
# the cognee base image (cognee 1.2.2 AND 1.4.0 → ladybug 0.17.x → extension v0.17.0). To refresh:
#   curl -fsSLO https://extension.ladybugdb.com/v0.17.0/linux_amd64/json/libjson.lbug_extension
ARG LBUG_EXT_VER=0.17.0
COPY vendor/libjson.lbug_extension \
     /root/.lbdb/extension/${LBUG_EXT_VER}/linux_amd64/json/libjson.lbug_extension

# #186: ALSO vendor the json extension for the OLD ladybug 0.16.0 engine. A box
# whose cognee graph crossed the 0.16 -> 0.17 upgrade needs a one-time on-disk
# format migration (kuzu-migrate.py below), whose EXPORT leg runs under a 0.16.0
# engine. ladybug resolves the extension cache dir from the ENGINE's own
# extension-release version (0.16.0 -> .../0.16.0/..., 0.17.1 -> .../0.17.0/...),
# so the export leg looks under /root/.lbdb/extension/0.16.0/ — a DIFFERENT
# binary from 0.17.0 (same 827 KB, distinct ABI). Vendoring it keeps the
# migration fully offline for the extension (the engine wheels themselves still
# need PyPI during the one-time migration — see kuzu-migrate.py). To refresh:
#   curl -fsSLO https://extension.ladybugdb.com/v0.16.0/linux_amd64/json/libjson.lbug_extension
ARG LBUG_EXT_VER_OLD=0.16.0
COPY vendor/libjson-0.16.0.lbug_extension \
     /root/.lbdb/extension/${LBUG_EXT_VER_OLD}/linux_amd64/json/libjson.lbug_extension

# .pth hook: disable aiohttp transport (avoids some SSL issues in litellm),
# drop unsupported params so non-OpenAI embeddings accept `dimensions`, and
# register tiktoken encodings for the embedding models we serve via GPUStack.
RUN PATCH='import litellm; litellm.disable_aiohttp_transport = True; litellm.drop_params = True; import tiktoken; tiktoken.model.MODEL_TO_ENCODING.update({"nomic-embed-text": "cl100k_base", "qwen3-embedding": "cl100k_base", "gemma3": "cl100k_base"})' && \
    echo "$PATCH" > /app/.venv/lib/python3.12/site-packages/razzfazz_cognee_patch.pth

# rc6.7 #70 patch RETIRED in 1.0.9. Upstream now sets
# self.max_completion_tokens and self.tokenizer natively in the
# OpenAICompatibleEmbeddingEngine constructor (verified against v1.0.9
# source 2026-05-09). Re-applying our old sed would corrupt the
# upstream's get_tokenizer() selection logic. If reverting to <1.0.9,
# restore the patch from git history.

# #186: proactive ladybug 0.16 -> 0.17 graph-format migration, run by the
# entrypoint wrapper before the cognee server boots (idempotent + non-fatal).
COPY kuzu-migrate.py /app/kuzu-migrate.py

# Custom entrypoint that wraps the original
COPY entrypoint-wrapper.sh /app/entrypoint-wrapper.sh
RUN chmod +x /app/entrypoint-wrapper.sh
ENTRYPOINT ["/app/entrypoint-wrapper.sh"]
