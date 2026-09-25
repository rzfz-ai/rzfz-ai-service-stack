# ==============================================================================
# CUSTOM COGNEE IMAGE (1.5.x line)
# ==============================================================================
# Extends the official Cognee image with litellm/tiktoken runtime patches for
# non-OpenAI embedding models served through GPUStack, and vendors the ladybug
# graph-extension so cognee needs NO network egress at build OR startup (#79).
#
# Base: cognee/cognee:1.5.3
# Graph backend: cognee swapped Kuzu for ladybug in 1.0.9 (kuzu==0.11.3 →
# ladybug); GRAPH_DATABASE_PROVIDER=kuzu still routes through ladybug. The
# ladybug version tracks the cognee base: 1.1.2 → ladybug 0.16.x, 1.2.2/1.4.0 →
# ladybug 0.17.x, 1.5.0+ (incl. 1.5.3) → ladybug ==0.19.0 (Linux; macOS 13/14
# stays on 0.17.x upstream — N/A, we ship Linux containers). Existing on-disk
# Kuzu/ladybug data isn't documented as compat across a minor bump — base bumps
# are paired with a wipe of the cognee-data volume (no prod data) OR the #186
# migration below.
#
# 2026.08 BUMP 1.4.0 → 1.5.3 (operator "cognee muss rauf" — unfreezes the prior
# deliberate 1.4.0 pin; #79 coupling VERIFIED, NOT a build blocker, but DOES
# cross a ladybug on-disk format boundary — see #186 note):
#   Verified directly against upstream pyproject.toml + release notes (GitHub
#   API, not summarized) for every tag v1.4.1..v1.5.3:
#     - v1.4.1, v1.4.2: "no breaking changes"; ladybug pin unchanged
#       (>=0.16.0,<=0.18.2).
#     - v1.5.0 ("Large-scale migration & graph reliability"): ladybug pin
#       bumped >=0.16.0,<=0.18.2 -> ==0.19.0 (Linux). Release notes say "no
#       user-facing breaking changes" but this IS an on-disk graph-format
#       crossing: cognee_db_workers.ladybug_migrate.ladybug_version_mapping
#       maps 0.17.1 -> storage code 41 and 0.19.0 -> storage code 43 (verified
#       by fetching that module at v1.5.3) — codes differ, so #186's
#       kuzu-migrate.py major.minor check (0.17 vs 0.19) correctly detects
#       "migrate" and drives cognee's OWN ladybug_migration() API (unchanged
#       code here — that function already does a single EXPORT(old)/
#       IMPORT(new) hop regardless of version gap, so it covers 0.16->0.19 and
#       0.17->0.19 boxes alike). NOT a schema wipe: pgvector client pin
#       (`pgvector>=0.3.5,<0.4`) is UNCHANGED 1.4.0->1.5.3 and no release note
#       in 1.4.1..1.5.3 mentions a vector/embedding-dim change — contrast the
#       1.2->1.4 jump, which DID require wiping cognee-data. No re-cognify
#       needed; only the graph-format migration applies.
#     - v1.5.1, v1.5.2, v1.5.3: "no breaking changes"; ladybug stays ==0.19.0.
#   Extension re-vendor (REQUIRED, done this bump): extension.ladybugdb.com
#   publishes a v0.19.0 JSON extension (verified 200, live-fetched) that is a
#   DISTINCT binary from the old 0.17.0 one despite the same 827 KB size
#   (sha256 39c51fa9…97426 vs 8a5eb3c6…5114) — reusing the old file would have
#   silently served the wrong ABI. The previous "current" vendor/libjson.
#   lbug_extension (0.17.0) is kept on disk as vendor/libjson-0.17.0.
#   lbug_extension so the migration's EXPORT leg still works offline for any
#   box still at on-disk format 0.17.x (the common case, since most boxes
#   already crossed 0.16->0.17 via #186). vendor/libjson-0.16.0.lbug_extension
#   is ALSO kept (defensive: a box that never completed that earlier migration,
#   e.g. COGNEE_KUZU_AUTO_MIGRATE=false, still needs it for its own EXPORT
#   leg). cognee-frontend patch surface re-verified at v1.5.3 (see
#   frontend/Dockerfile) — all 9 patched files still present with the expected
#   fallback string; Next.js/React pins unchanged (^16.0.8 / ^19.1.2).
#   STILL DO at cut: a real build + live migration test (not run on this
#   host/sandbox — no live Cognee here) per the acceptance criteria in the
#   bump commit/PR.
# ==============================================================================

FROM cognee/cognee:1.5.3

# Bundle the ladybug "json" graph-extension so cognee needs NO egress at build OR startup.
# cognee's ladybug backend (GRAPH_DATABASE_PROVIDER=kuzu → ladybug) downloads
# libjson.lbug_extension from extension.ladybugdb.com the first time it starts; on an
# egress-restricted / hardened / airgapped box that download is blocked (prod 8.246 got 403) →
# the ladybug worker wedges → cognee's gunicorn worker DEADLOCKS on startup and never serves
# (#79). We vendor the binary (modules/knowledge/cognee/vendor/) and COPY it to ladybug's local
# extension cache so no download ever happens. LBUG_EXT_VER MUST match the ladybug version in
# the cognee base image (cognee 1.5.0+ -> ladybug ==0.19.0 -> extension v0.19.0, a DISTINCT
# binary from the 0.17.0 one despite the identical 827 KB size — sha256-verified). To refresh:
#   curl -fsSLO https://extension.ladybugdb.com/v0.19.0/linux_amd64/json/libjson.lbug_extension
ARG LBUG_EXT_VER=0.19.0
COPY vendor/libjson.lbug_extension \
     /root/.lbdb/extension/${LBUG_EXT_VER}/linux_amd64/json/libjson.lbug_extension

# #186: ALSO vendor the json extension for OLDER ladybug engines that a box's
# on-disk graph store may still be in when it crosses this bump. Two tiers are
# kept: 0.17.0 (the PRIOR "current" — the common case, since most boxes already
# migrated 0.16->0.17 via this same mechanism) and 0.16.0 (defensive — a box
# that never completed that earlier migration, e.g. COGNEE_KUZU_AUTO_MIGRATE=
# false, still needs it for its own EXPORT leg). ladybug resolves the extension
# cache dir from the ENGINE's own extension-release version (0.16.0 ->
# .../0.16.0/..., 0.17.x -> .../0.17.0/..., 0.19.0 -> .../0.19.0/...), and
# cognee's ladybug_migration() spins up a throwaway venv for whichever old
# engine version the on-disk format needs, so the export leg looks under the
# matching /root/.lbdb/extension/<ver>/ dir — a DIFFERENT binary per tier
# (same ~827 KB, distinct ABI). Vendoring both keeps the migration fully
# offline for the extension (the engine wheels themselves still need PyPI
# during the one-time migration — see kuzu-migrate.py). To refresh:
#   curl -fsSLO https://extension.ladybugdb.com/v0.17.0/linux_amd64/json/libjson.lbug_extension
#   curl -fsSLO https://extension.ladybugdb.com/v0.16.0/linux_amd64/json/libjson.lbug_extension
ARG LBUG_EXT_VER_OLD=0.17.0
COPY vendor/libjson-0.17.0.lbug_extension \
     /root/.lbdb/extension/${LBUG_EXT_VER_OLD}/linux_amd64/json/libjson.lbug_extension
ARG LBUG_EXT_VER_OLDEST=0.16.0
COPY vendor/libjson-0.16.0.lbug_extension \
     /root/.lbdb/extension/${LBUG_EXT_VER_OLDEST}/linux_amd64/json/libjson.lbug_extension

# .pth hook: disable aiohttp transport (avoids some SSL issues in litellm),
# drop unsupported params so non-OpenAI embeddings accept `dimensions`, and
# register tiktoken encodings for the embedding models we serve via GPUStack.
RUN PATCH='import litellm; litellm.disable_aiohttp_transport = True; litellm.drop_params = True; import tiktoken; tiktoken.model.MODEL_TO_ENCODING.update({"nomic-embed-text": "cl100k_base", "qwen3-embedding": "cl100k_base", "gemma3": "cl100k_base"})' && \
    echo "$PATCH" > /app/.venv/lib/python3.12/site-packages/razzfazz_cognee_patch.pth

# #2091: the stack admin's identity is `razzfazz-ai-admin@<MAIN_DOMAIN>` in a
# dozen places (post-install's cognee login and key minting, set-admin-password,
# mcp-manager, the chat pipes, Authentik, Gitea). cognee validates it as a
# DELIVERABLE mailbox (pydantic EmailStr -> email-validator 2.3.0), which
# rejects every special-use TLD in email_validator.SPECIAL_USE_DOMAIN_NAMES:
# arpa, invalid, local, localhost, onion, test. So `rzfz init --domain myai.local`
# — CLAUDE.md's own example — made cognee die in its lifespan creating the
# default user, and the default presets exited 1 (rc1 Phase 3, scenarios 19-22
# on `test-1292-*.local`; 0.79/0.91 have routable TLDs and never saw it).
# The address is an identity, not a mailbox. The library's TEST_ENVIRONMENT
# switch does NOT lift this check (measured on 0.175 in the rebuilt image:
# the global was True and `.local` was still rejected — syntax.py exempts only
# `test` under it). What works, measured in the same interpreter: taking
# `local` out of SPECIAL_USE_DOMAIN_NAMES (syntax.py imports the list at call
# time, so the in-place edit is what every later validation sees). Only
# `local` is removed — it is the TLD our own docs tell operators to use;
# `invalid`, `onion`, `arpa`, `localhost`, `test` keep rejecting. Done from the
# same .pth hook, before cognee imports anything. An operator can restore the
# strict check with RZFZ_ADMIN_EMAIL_ALLOW_SPECIAL_USE=0 on a box whose domain
# is routable.
RUN echo 'import os, email_validator; email_validator.SPECIAL_USE_DOMAIN_NAMES[:] = [d for d in email_validator.SPECIAL_USE_DOMAIN_NAMES if d != "local" or os.environ.get("RZFZ_ADMIN_EMAIL_ALLOW_SPECIAL_USE", "1") == "0"]' \
        >> /app/.venv/lib/python3.12/site-packages/razzfazz_cognee_patch.pth

# rc6.7 #70 patch RETIRED in 1.0.9. Upstream now sets
# self.max_completion_tokens and self.tokenizer natively in the
# OpenAICompatibleEmbeddingEngine constructor (verified against v1.0.9
# source 2026-05-09). Re-applying our old sed would corrupt the
# upstream's get_tokenizer() selection logic. If reverting to <1.0.9,
# restore the patch from git history.

# #186: proactive ladybug on-disk graph-format migration (generically handles
# any crossing detected by storage-format major.minor mismatch — 0.16->0.17,
# and now also 0.17->0.19 for this 1.5.3 bump), run by the entrypoint wrapper
# before the cognee server boots (idempotent + non-fatal).
COPY kuzu-migrate.py /app/kuzu-migrate.py

# Custom entrypoint that wraps the original
# base bumped to cognee/cognee:1.5.3 (non-root default USER) → a runtime
# `RUN chmod` on the root-owned copied file fails EPERM. Set the exec bit at
# COPY time instead (source is already +x); no runtime chmod needed.
COPY --chmod=0755 entrypoint-wrapper.sh /app/entrypoint-wrapper.sh
ENTRYPOINT ["/app/entrypoint-wrapper.sh"]

# #2477 (2026.09-ga.3): the storage roots must EXIST in the image, owned by the
# image user. #2433 pointed SYSTEM_ROOT_DIRECTORY / DATA_ROOT_DIRECTORY at the
# mounted paths below; Docker seeds a NEW named volume from the image's content
# and ownership only when the path exists in the image. Without these two
# directories a fresh volume came up empty and root-owned, cognee (non-root)
# could not create databases/ in it, stayed unhealthy, and every fresh
# `rzfz init` failed (Jira RZFZAI-2007). Upgrades are handled separately by
# upgrade.sh::align_cognee_volume_ownership (#2455). The owner is read from the
# image's own user, and the build fails if it did not take.
USER root
RUN set -e; \
    u="$(id -u cognee)"; g="$(id -g cognee)"; \
    mkdir -p /app/cognee/.cognee_system /app/cognee/.data_storage; \
    chown "$u:$g" /app/cognee/.cognee_system /app/cognee/.data_storage; \
    for d in /app/cognee/.cognee_system /app/cognee/.data_storage; do \
        [ "$(stat -c %u:%g "$d")" = "$u:$g" ] || { echo "#2477: $d not owned by cognee" >&2; exit 1; }; \
    done
USER cognee
