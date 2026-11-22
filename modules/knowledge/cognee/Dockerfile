# ==============================================================================
# CUSTOM COGNEE IMAGE (1.6.x line)
# ==============================================================================
# Extends the official Cognee image with litellm/tiktoken runtime patches for
# non-OpenAI embedding models served through GPUStack, and vendors the ladybug
# graph-extension so cognee needs NO network egress at build OR startup (#79).
#
# Base: cognee/cognee:1.6.5
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
#
# 2026.11-rc2 BUMP 1.5.3 -> 1.6.5 (stable; 1.6.6rc1 is a release candidate and
# was not taken). Read from the upstream tree at v1.5.3 and v1.6.5 (diff of the
# code, not only the release notes):
#   - ladybug stays ==0.19.0, so NO graph-format migration: #186's
#     kuzu-migrate.py finds matching storage codes and does nothing. The image now
#     also bundles the ladybug JSON extension for every released ladybug (its own
#     `ladybug-extensions` stage) under /app/cognee_db_workers/, a layout ladybug does
#     not read; the vendored copies below stay, and since #2865 they are installed under
#     the runtime user's $HOME, which is the only place ladybug looks.
#   - Relational schema: the Alembic chain was re-based on a frozen initial
#     schema and one reconcile revision (1c22e6cb5aec) that only ADDS missing
#     tables/columns/indexes; a database stamped at head by the old create_all
#     path passes through. Additive, but it does not downgrade: a rollback to
#     1.5.3 meets an unknown head. The entrypoint now runs cognee's own
#     `run_migrations()` (relational chain, then per-dataset data chain).
#   - Auth: access tokens are bound to the password hash (a password change
#     signs every session out; tokens issued before the upgrade stop verifying
#     once), and an unset/blank token secret becomes a random per-process one
#     (we always set FASTAPI_USERS_JWT_SECRET). `DEFAULT_USER_PASSWORD` is
#     honoured as before; the old public default password is gone.
#   - `fastembed` + `onnxruntime` are core dependencies, `dlt` too (pinned
#     <1.31); `GLiNER` runtime is installed into the image; `git` is in the
#     runtime image; `lancedb` 0.39 / `pylance` 12 (we run pgvector, LanceDB is
#     not used). litellm 1.105 (>=1.102.2).
#   - Security pins below: re-derived against the 1.6.5 lock (uv.lock).
# ==============================================================================

FROM cognee/cognee:1.6.5
# #2336: security floor over the base image. (1) every OS package of the base to
# today's security level; (2) the Python pins in requirements-security-pins.txt
# over the base's own virtualenv. The base's final stage is python:3.12-slim-bookworm with pip, and
# its venv has none, so the system pip installs into it (`--python`). Wheels
# only: a pin without a wheel for the platform fails the build instead of
# compiling in an image that has no compiler. The check afterwards fails the build
# if a pin did not take (pip keeps an already-satisfied requirement silently).
# This layer reads the Debian and PyPI indexes at BUILD time (the image is built on
# a connected host; offline boxes load it from the package). Startup still needs
# no egress: the ladybug extension below is vendored (#79).
USER root
# #3343 (2026.11-ga.1): the interpreter the base ships carries 2 Critical CVEs, fixed in
# Python 3.12.16. The base's runtime is python:3.12-slim-bookworm, so the same image at the
# fixed patch release is laid over /usr/local (same ABI, same Debian; the venv links to
# /usr/local/bin/python3 and keeps its packages). The check fails the build if it did not take.
COPY --from=python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df /usr/local/ /usr/local/
RUN /app/.venv/bin/python -c "import sys; sys.exit('python %s, want 3.12.16+' % sys.version.split()[0] if sys.version_info[:3] < (3, 12, 16) else 0)"
# The base ships openssh-client, which nothing in cognee uses (no ssh/scp/git-over-ssh
# at runtime); it carries a critical CVE without a Debian fix, so it is removed.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && if dpkg -s openssh-client >/dev/null 2>&1; then apt-get purge -y openssh-client; fi \
    && ! command -v ssh \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-security-pins.txt /tmp/requirements-security-pins.txt
RUN /usr/local/bin/python -m pip --python /app/.venv/bin/python install \
        --no-cache-dir --disable-pip-version-check --only-binary=:all: -r /tmp/requirements-security-pins.txt \
    && /app/.venv/bin/python -c "import importlib.metadata as m, sys; bad=[l for l in open('/tmp/requirements-security-pins.txt').read().splitlines() if l and not l.startswith('#') and m.version(l.split('==')[0])!=l.split('==')[1]]; sys.exit('pins did not take: %s' % bad if bad else 0)" \
    && rm -f /tmp/requirements-security-pins.txt
USER cognee

# Bundle the ladybug "json" graph-extension so cognee needs NO egress at build OR startup.
# cognee's ladybug backend (GRAPH_DATABASE_PROVIDER=kuzu -> ladybug) LOADs the "json"
# extension when a graph database is opened (cognee 1.6 does it on its health path too);
# a missing file is downloaded from extension.ladybugdb.com, which an egress-restricted /
# hardened / airgapped box blocks (prod 8.246 got 403) and which refuses Python's default
# User-Agent anyway (#2865). We vendor the binaries (modules/knowledge/cognee/vendor/).
#
# #2865: WHERE they go. ladybug resolves its extension cache from the HOME of the user the
# server runs as: cognee 1.5+ runs as `cognee` with HOME=/app, so the cache is
# $HOME/.lbdb/extension/<ver>/linux_amd64/json/libjson.lbug_extension. Up to 2026.11-rc3
# the copies sat under /root/.lbdb (0700, a user the server never runs as): dead weight
# that only looked like a guarantee, and cognee 1.6.5 failed to open its graph database
# (/health 503, cognee-frontend and cognee-mcp never started). The /root copies are DROPPED:
# nothing in this image runs ladybug as root (the entrypoint, the migration and the server
# all run as `cognee`). The files are staged root-owned, then installed by a `cognee` RUN
# into "$HOME", which is the HOME that user has at runtime, so the path is never written
# out a second time and the files are owned by the user that reads them.
#
# LBUG_EXT_VER MUST match the ladybug version in the cognee base image (cognee 1.5.0+ ->
# ladybug ==0.19.0 -> extension v0.19.0, a DISTINCT binary from the 0.17.0 one despite the
# identical 827 KB size - sha256-verified); the smoke check below fails the build when it
# does not. To refresh:
#   curl -fsSLO https://extension.ladybugdb.com/v0.19.0/linux_amd64/json/libjson.lbug_extension
#
# #186: ALSO vendor the json extension for OLDER ladybug engines that a box's on-disk graph
# store may still be in when it crosses this bump. Two tiers are kept: 0.17.0 (the PRIOR
# "current" - the common case, since most boxes already migrated 0.16->0.17 via this same
# mechanism) and 0.16.0 (defensive - a box that never completed that earlier migration, e.g.
# COGNEE_KUZU_AUTO_MIGRATE=false, still needs it for its own EXPORT leg). ladybug resolves
# the extension cache dir from the ENGINE's own extension-release version (0.16.0 ->
# .../0.16.0/..., 0.17.x -> .../0.17.0/..., 0.19.0 -> .../0.19.0/...), and cognee's
# ladybug_migration() spins up a throwaway venv for whichever old engine version the on-disk
# format needs, so the export leg looks under the matching $HOME/.lbdb/extension/<ver>/ dir -
# a DIFFERENT binary per tier (same ~827 KB, distinct ABI). Vendoring both keeps the
# migration fully offline for the extension (the engine wheels themselves still need PyPI
# during the one-time migration - see kuzu-migrate.py). To refresh:
#   curl -fsSLO https://extension.ladybugdb.com/v0.17.0/linux_amd64/json/libjson.lbug_extension
#   curl -fsSLO https://extension.ladybugdb.com/v0.16.0/linux_amd64/json/libjson.lbug_extension
ARG LBUG_EXT_VER=0.19.0
ARG LBUG_EXT_VER_OLD=0.17.0
ARG LBUG_EXT_VER_OLDEST=0.16.0
COPY vendor/libjson.lbug_extension /opt/rzfz/lbug-ext/libjson-${LBUG_EXT_VER}.lbug_extension
COPY vendor/libjson-0.17.0.lbug_extension /opt/rzfz/lbug-ext/libjson-${LBUG_EXT_VER_OLD}.lbug_extension
COPY vendor/libjson-0.16.0.lbug_extension /opt/rzfz/lbug-ext/libjson-${LBUG_EXT_VER_OLDEST}.lbug_extension
# Install as `cognee` into "$HOME". The vendored binaries are linux_amd64. On any other
# architecture (linux/arm64 boxes exist: cli/init.sh sets DOCKER_PLATFORM) the base image's
# own bundle (/app/cognee_db_workers/ladybug_extensions/v<ver>/<arch>/) is seeded into the
# same layout, never over a vendored file. Whatever is missing is caught by the smoke check.
RUN set -eu; \
    root="$HOME/.lbdb/extension"; \
    for v in "${LBUG_EXT_VER}" "${LBUG_EXT_VER_OLD}" "${LBUG_EXT_VER_OLDEST}"; do \
        install -D -m 0644 "/opt/rzfz/lbug-ext/libjson-$v.lbug_extension" \
            "$root/$v/linux_amd64/json/libjson.lbug_extension"; \
    done; \
    for f in /app/cognee_db_workers/ladybug_extensions/v*/linux_*/libjson.lbug_extension; do \
        [ -f "$f" ] || continue; \
        arch="$(basename "$(dirname "$f")")"; \
        ver="$(basename "$(dirname "$(dirname "$f")")")"; ver="${ver#v}"; \
        dest="$root/$ver/$arch/json/libjson.lbug_extension"; \
        [ -e "$dest" ] || install -D -m 0644 "$f" "$dest"; \
    done

# #2865: BUILD-TIME smoke check, as the user the server runs as, with no network. It opens
# a throwaway ladybug database and LOADs the json extension through the real engine, then
# calls a json function: a wrong cache path, a wrong owner, a wrong binary ABI or a ladybug
# bump away from LBUG_EXT_VER fails the build here instead of the box at /health. The proxy
# variables point at a closed port so a silent download fallback cannot make it pass.
RUN HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 \
    https_proxy=http://127.0.0.1:9 NO_PROXY= no_proxy= \
    /app/.venv/bin/python -c "import os, sys, tempfile, ladybug; want = os.environ['LBUG_EXT_VER']; sys.exit('#2865: ladybug %s in the base image, vendored extension is for %s' % (ladybug.__version__, want)) if ladybug.__version__ != want else None; c = ladybug.Connection(ladybug.Database(os.path.join(tempfile.mkdtemp(), 'smoke'))); c.execute('LOAD EXTENSION json'); r = c.execute(\"RETURN to_json({a: 1}) AS j\").get_next(); sys.exit('#2865: json extension loaded but to_json returned %r' % (r,)) if r != ['{\"a\":1}'] else print('ladybug json extension OK for', want, 'under', os.environ['HOME'])"

# .pth hook: disable aiohttp transport (avoids some SSL issues in litellm),
# drop unsupported params so non-OpenAI embeddings accept `dimensions`, and
# register tiktoken encodings for the embedding models we serve via GPUStack.
RUN PATCH='import litellm; litellm.disable_aiohttp_transport = True; litellm.drop_params = True; import tiktoken; tiktoken.model.MODEL_TO_ENCODING.update({"nomic-embed-text": "cl100k_base", "qwen3-embedding": "cl100k_base", "gemma3": "cl100k_base"})' && \
    echo "$PATCH" > /app/.venv/lib/python3.12/site-packages/razzfazz_cognee_patch.pth

# #2871: bake the tiktoken encodings cognee can request, so an OFFLINE box never reaches for
# openaipublic.blob.core.windows.net. cognee 1.x builds a TikToken tokenizer for the embedding
# model at runtime (tokenizer/TikToken/adapter.py: encoding_for_model(<model>), or
# get_encoding("cl100k_base") without a model; the .pth hook above maps our embedding models to
# cl100k_base). tiktoken downloads the encoding unless it sits in TIKTOKEN_CACHE_DIR, and on an
# offline box each attempt ran into a connect timeout (648 of them), so /health never answered.
# Encodings baked: cl100k_base (every embedding/LLM name mapped to the default, gpt-4 and
# gpt-3.5 names) and o200k_base (gpt-4o / gpt-5 / o-series names, and the encoding cognee's
# eval code falls back to). The older r50k/p50k/gpt2 encodings belong to models nothing here
# serves. Fetched at BUILD time (a connected host; offline boxes load the image), sha256-checked
# against tiktoken's own published values, stored under the sha1 of the URL like tiktoken does.
# The directory is created by `cognee`, so it owns it.
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken_cache
# --chown: the RUN below is `cognee`, and /tmp is sticky, so a root-owned copy
# could not be removed afterwards (the build failed on the rm).
COPY --chown=cognee:cognee fetch-tiktoken-encodings.py /tmp/fetch-tiktoken-encodings.py
RUN /app/.venv/bin/python /tmp/fetch-tiktoken-encodings.py "$TIKTOKEN_CACHE_DIR" \
    && rm -f /tmp/fetch-tiktoken-encodings.py

# #2871: BUILD-TIME smoke check, as `cognee`, proxies cut (same recipe as the #2865 check): the
# baked encodings load by name AND through the model-name mapping our .pth hook registers. A
# wrong cache file name, owner or hash fails the build here instead of the box at /health.
RUN HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 \
    https_proxy=http://127.0.0.1:9 NO_PROXY= no_proxy= \
    /app/.venv/bin/python -c "import tiktoken; [tiktoken.get_encoding(n).encode('smoke') for n in ('cl100k_base', 'o200k_base')]; [tiktoken.encoding_for_model(m).encode('smoke') for m in ('qwen3-embedding', 'nomic-embed-text', 'gpt-4o')]; print('tiktoken encodings OK offline')"

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
# #3022: a CONCURRENCY bound for cognee's LLM calls (cognee 1.6.5 only paces by a
# rate, so ~40 s extraction calls pile up past the LLM Manager's box-wide cap and
# datasets end ERRORED). The .pth loads rzfz_llm_bound at interpreter start; it
# patches cognee.shared.rate_limiting when cognee imports it. The check fails the
# build when the patch does not take on THIS cognee (a moved seam must not
# silently drop the bound).
COPY rzfz_llm_bound.py rzfz_llm_bound.pth /tmp/rzfz-llm-bound/
USER root
RUN SP="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && cp /tmp/rzfz-llm-bound/rzfz_llm_bound.py /tmp/rzfz-llm-bound/rzfz_llm_bound.pth "$SP/" \
    && rm -rf /tmp/rzfz-llm-bound \
    && COGNEE_LLM_MAX_CONCURRENT=2 /app/.venv/bin/python -c "import cognee.shared.rate_limiting as r; f = r._governed_llm_dispatch; assert getattr(f, '__rzfz_bound__', False) and f.__rzfz_limit__ == 2, 'rzfz_llm_bound did not patch cognee (#3022)'"
USER cognee
# #3033: a busy dataset graph ends in a bounded wait and a 503 + Retry-After, never in a
# long stall or a 500 naming the server path. rzfz_dataset_lock gates every graph open (it
# closes a worker only an idle holder kept alive, then waits without spawning anything),
# runs a search once and scrubs paths from its errors. Same .pth mechanism; the check fails
# the build when a seam the gate needs moved in THIS cognee. Importing the route's module is
# not needed: GraphBusy is an HTTPException, which the stock handler answers with its headers.
COPY rzfz_dataset_lock.py rzfz_dataset_lock.pth /tmp/rzfz-dataset-lock/
USER root
RUN SP="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && cp /tmp/rzfz-dataset-lock/rzfz_dataset_lock.py /tmp/rzfz-dataset-lock/rzfz_dataset_lock.pth "$SP/" \
    && rm -rf /tmp/rzfz-dataset-lock \
    && /app/.venv/bin/python -c "import sys, cognee.api.v1.search as a, cognee.infrastructure.databases.graph.get_graph_engine, rzfz_dataset_lock as L; g = sys.modules['cognee.infrastructure.databases.graph.get_graph_engine']; assert getattr(g.acreate_graph_engine, '__rzfz_dataset_lock__', False), 'graph open not gated (#3033)'; assert callable(g._resolve_graph_engine_args), 'open key moved (#3033)'; c = g._create_graph_engine.cache_info.__self__; assert all(callable(getattr(c, n)) for n in ('_detach_entry', '_snapshot_pending_closes', '_track_close', 'contains')), 'cache seams moved (#3033)'; assert getattr(a.search, '__rzfz_dataset_lock__', False) and getattr(sys.modules['cognee.api.v1.search.search'].search, '__rzfz_dataset_lock__', False), 'search API not wrapped (#3033)'; e = L.graph_busy(); assert e.status_code == 503 and e.headers.get('Retry-After') and type(e).__mro__[1].__name__ == 'HTTPException', 'busy answer wrong (#3033)'"
USER cognee
# Deleting a dataset stops its running knowledge graph build: the delete authorizes as
# before, then cancels the dataset's tasks in LLM/embedding calls and refuses its later
# ones, so the pipeline rolls back and releases the dataset lock at once. The request waits
# a bounded time (COGNEE_DELETE_WAIT_S, 20 s) and answers 202 when the delete still runs.
# After cognee's delete it purges the dataset's cached session answers (all users).
# Before, the delete waited for the whole cognify (29 min measured). Same .pth mechanism;
# the check fails the build when a seam moved in THIS cognee.
COPY rzfz_dataset_delete.py rzfz_dataset_delete.pth /tmp/rzfz-dataset-delete/
USER root
RUN SP="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && cp /tmp/rzfz-dataset-delete/rzfz_dataset_delete.py /tmp/rzfz-dataset-delete/rzfz_dataset_delete.pth "$SP/" \
    && rm -rf /tmp/rzfz-dataset-delete \
    && /app/.venv/bin/python -c "import sys, inspect, cognee.shared.rate_limiting as r, cognee.modules.pipelines.operations.pipeline, cognee.modules.pipelines.operations.run_tasks, cognee.api.v1.datasets.datasets, cognee.context_global_variables as cv, rzfz_dataset_delete as D; p = sys.modules['cognee.modules.pipelines.operations.pipeline']; a = sys.modules['cognee.api.v1.datasets.datasets']; assert all(getattr(getattr(r, n), '__rzfz_dataset_delete__', False) for n in ('llm_rate_limiter_context_manager', 'embedding_rate_limiter_context_manager')), 'LLM/embedding seams not guarded'; assert getattr(p.run_tasks, '__rzfz_dataset_delete__', False) and 'run_tasks(' in inspect.getsource(p.run_pipeline_per_dataset), 'pipeline run not wrapped'; assert getattr(a.datasets.empty_dataset, '__rzfz_dataset_delete__', False) and callable(a.get_authorized_dataset) and callable(a.get_default_user), 'dataset delete not wrapped'; assert cv.current_dataset_id.get() is None, 'dataset context variable moved'; assert all('llm_rate_limiter_context_manager' in inspect.getsource(__import__('importlib').import_module(m)) for m in ('cognee.infrastructure.llm.structured_output_framework.litellm_native.native_adapter', 'cognee.infrastructure.llm.structured_output_framework.litellm_instructor.llm.generic_llm_api.adapter')), 'adapters no longer use the guarded seam'; e = D.deletion_accepted(); assert e.status_code == 202; import cognee.infrastructure.databases.cache as C, cognee.infrastructure.databases.cache.sql.tables as T; from cognee.infrastructure.databases.cache.sql.SqlCacheAdapter import SqlCacheAdapter; assert callable(C.get_cache_engine) and callable(SqlCacheAdapter._ensure_initialized), 'session cache seams moved'; assert all('session_id' in t.c for t in (T.cache_qa_entries, T.cache_trace_entries, T.cache_session_context)), 'session cache tables moved'"
USER cognee
# The brain overviews (/visualize/brains, /visualize/brains-summary, /datasets/graph-summary)
# do bounded work per request: /brains answers pages of datasets (max_datasets, offset,
# X-Total-Count, X-Next-Offset), the count views open a bounded number of graphs per request,
# and a request is cancelled when its client leaves or after a time limit (503 + Retry-After).
# Before, one user who could read hundreds of datasets kept cognee's single worker busy for
# minutes, also after the client had gone. Same .pth mechanism; the check fails the build
# when a seam moved in THIS cognee.
COPY rzfz_brain_views.py rzfz_brain_views.pth /tmp/rzfz-brain-views/
USER root
RUN SP="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && cp /tmp/rzfz-brain-views/rzfz_brain_views.py /tmp/rzfz-brain-views/rzfz_brain_views.pth "$SP/" \
    && rm -rf /tmp/rzfz-brain-views \
    && /app/.venv/bin/python -c "import sys, cognee.api.client as c, cognee.api.v1.visualize, cognee.modules.data.methods.get_datasets_graph_counts, rzfz_brain_views as B; v = sys.modules['cognee.api.v1.visualize']; vis = sys.modules['cognee.api.v1.visualize.visualize']; g = sys.modules['cognee.modules.data.methods.get_datasets_graph_counts']; assert getattr(c.app, '__rzfz_brain_views__', False) and any(m.cls is B.ViewGuard for m in c.app.user_middleware), 'view guard not installed'; assert getattr(v.build_brains_payload, '__rzfz_brain_views__', False) and vis.build_brains_payload is v.build_brains_payload, 'brains overview not paged'; assert all(callable(getattr(vis, n)) for n in ('get_all_user_permission_datasets', 'fetch_dataset_graph_data', 'build_brain_summary_payload', 'get_default_user')), 'brains seams moved'; assert getattr(g._count_and_cache, '__rzfz_brain_views__', False) and isinstance(g._inflight_counts, dict) and g.DatasetGraphCounts(pipeline_run_id=None).computed_at is None, 'graph counts not bounded'; paths = set(c.app.openapi()['paths']); assert set(B.GUARDED_PATHS) <= paths, 'guarded view paths moved: %s' % (set(B.GUARDED_PATHS) - paths)"
USER cognee

# Custom entrypoint that wraps the original
# base bumped to cognee/cognee:1.5.3 (non-root default USER) → a runtime
# `RUN chmod` on the root-owned copied file fails EPERM. Set the exec bit at
# COPY time instead (source is already +x); no runtime chmod needed.
COPY --chmod=0755 entrypoint-wrapper.sh /app/entrypoint-wrapper.sh
# The in-container liveness watchdog the wrapper starts (stdlib only; see the file).
COPY --chmod=0644 rzfz_liveness.py /app/rzfz-liveness.py
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
