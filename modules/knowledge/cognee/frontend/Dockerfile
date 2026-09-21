# ==============================================================================
# COGNEE FRONTEND (Next.js UI) [EXPERIMENTAL]
# ==============================================================================
# Builds the upstream cognee-frontend Next.js app from the same upstream tag
# as the cognee backend image (cognee/cognee:1.0.1 → upstream tag v1.0.1).
#
# Upstream does NOT publish a frontend image at GHCR / Docker Hub. The source
# is shipped under cognee-frontend/ in the topoteretes/cognee monorepo, so we
# git-clone the tag and build it ourselves (mirrors the paperclip pattern).
#
# Strategy:
#   Stage 1: alpine/git — sparse-checkout cognee-frontend/ at COGNEE_TAG.
#   Stage 2: node:22-alpine — npm ci (matches upstream's own Dockerfile),
#            then `next build` for a production bundle and `next start` at
#            runtime. Upstream's reference Dockerfile uses `next dev` with
#            turbopack; we deliberately use `next build` + `next start` for
#            production-grade serving (no on-the-fly compile, no source on
#            disk after build).
#
# Upstream Dockerfile reference (v1.0.1):
#   https://github.com/topoteretes/cognee/blob/v1.0.1/cognee-frontend/Dockerfile
#
# Backend wiring:
#   The frontend reads NEXT_PUBLIC_LOCAL_API_URL at build time (Next.js inlines
#   NEXT_PUBLIC_* into the client bundle during `next build`). The .env.template
#   shipped at v1.0.1 lists NEXT_PUBLIC_BACKEND_API_URL — but that name is NOT
#   wired anywhere in src/; the actual var the code consults is
#   NEXT_PUBLIC_LOCAL_API_URL (verified via `grep -r "process.env.NEXT_PUBLIC"
#   .next/{server,static}` after a build).
#
#   We pass the public HOST URL (no `/api` suffix); source paths construct
#   `${BASE}/api/v1/...` themselves. The cognee FastAPI backend mounts routes
#   under /api/v1/... (cognee/api/client.py), so a browser request to
#   https://<cognee>/api/v1/auth/login lands directly on the right backend
#   handler. Caddy forwards the entire /api/* path through to cognee:8000
#   unchanged (no prefix rewrite needed).
# ==============================================================================

# Pin must match cognee backend tag (modules/knowledge/cognee/Dockerfile: FROM cognee/cognee:1.5.3).
ARG COGNEE_TAG=v1.5.3

# ── Stage 1: sparse-checkout cognee-frontend/ at the pinned tag ───────────────
FROM alpine/git:v2.47.2 AS source
ARG COGNEE_TAG
WORKDIR /src
RUN git clone --depth 1 --branch ${COGNEE_TAG} --filter=blob:none --sparse \
        https://github.com/topoteretes/cognee.git . \
 && git sparse-checkout set cognee-frontend \
 && test -f cognee-frontend/package.json \
 && test -f cognee-frontend/Dockerfile

# ── Stage 2: build + run the Next.js app ──────────────────────────────────────
FROM node:22-alpine AS runtime
WORKDIR /app

# Manifests first for layer caching.
# NOTE: upstream's package-lock.json is out-of-sync with package.json at v1.0.1
# (npm ci fails with "lock file's brace-expansion@2.0.2 does not satisfy ...").
# We use `npm install --no-audit --no-fund` so npm reconciles the tree fresh.
# Trade-off: dependency versions are not byte-stable across builds, but the
# upstream tag IS stable, so semver ranges resolve to the same major.minor.
COPY --from=source /src/cognee-frontend/package.json /src/cognee-frontend/package-lock.json ./
RUN npm install --no-audit --no-fund \
 && npm rebuild lightningcss

# Source
COPY --from=source /src/cognee-frontend/src ./src
COPY --from=source /src/cognee-frontend/public ./public
COPY --from=source /src/cognee-frontend/next.config.mjs ./
COPY --from=source /src/cognee-frontend/postcss.config.mjs ./
COPY --from=source /src/cognee-frontend/tsconfig.json ./
COPY --from=source /src/cognee-frontend/types ./types

# #282: make the CLIENT talk to the SAME ORIGIN it was served from, so this
# image works on ANY customer domain regardless of the build box. Upstream reads
# `process.env.NEXT_PUBLIC_LOCAL_API_URL || "http://localhost:8000"` everywhere
# and NEXT_PUBLIC_* is INLINED AT BUILD — so an offline-package image bakes the
# packaging box's domain into the client bundle → the browser tries the wrong
# host ("Cannot connect to local Cognee backend at https://<build-box>").
# The `||` collapses an empty build value to the localhost fallback, so we can't
# just pass "". Fix: switch the CLIENT-side reads to `??` (empty string survives
# → same-origin relative `/api/v1/...`, which Caddy forwards to cognee:8000) and
# build with NEXT_PUBLIC_LOCAL_API_URL="". Server-side route handlers run in the
# Node process and REQUIRE an absolute URL, so keep their `||` but repoint the
# fallback at the in-cluster backend `cognee:8000`. Re-verify these paths on a
# cognee bump (mirrors the historical case-sensitivity sed below).
RUN set -eu; \
    for f in \
        src/modules/instances/localFetch.ts \
        src/modules/tenant/LocalProvider.tsx \
        "src/app/(auth)/local-login/partials/LocalSignInForm.tsx" \
        "src/app/(app)/api-keys/ApiKeysPage.tsx"; do \
      [ -f "$f" ] && sed -i 's#|| "http://localhost:8000"#?? "http://localhost:8000"#g' "$f" || true; \
    done; \
    for f in \
        src/app/api/visualize/route.ts \
        src/app/api/local-signout/route.ts \
        src/app/api/schema/provenance/route.ts \
        src/app/api/schema/inventory/route.ts \
        src/app/api/schema-provenance/route.ts; do \
      [ -f "$f" ] && sed -i 's#|| "http://localhost:8000"#|| "http://cognee:8000"#g' "$f" || true; \
    done; \
    grep -q '?? "http://localhost:8000"' src/modules/instances/localFetch.ts \
      || { echo "FATAL(#282): client API-base patch did not apply — cognee-frontend layout changed on this tag; re-audit."; exit 1; }

# Null-guard the Agent Activity terminal (upstream defect, present through the
# newest tag v1.5.3): src/ui/elements/AgentActivityTerminal.tsx does
# `r.pipeline_name.toLowerCase()` UNGUARDED while iterating pipeline_runs, but
# cognee itself writes rows with a NULL pipeline_name (memify/system runs), so
# the whole app crashes on render ("Cannot read properties of null (reading
# 'toLowerCase')"). Upstream's own idiom elsewhere is `(x || "").toLowerCase()`;
# we apply the same guard. Re-audit on a cognee bump (fixed upstream → drop).
RUN set -eu; \
    f="src/ui/elements/AgentActivityTerminal.tsx"; \
    [ -f "$f" ] && sed -i 's/const pipelineName = r\.pipeline_name\.toLowerCase();/const pipelineName = (r.pipeline_name || "").toLowerCase();/' "$f"; \
    grep -q '(r.pipeline_name || "").toLowerCase()' "$f" \
      || { echo "FATAL: AgentActivityTerminal null-guard did not apply — cognee-frontend layout changed on this tag; re-audit."; exit 1; }

# Null-hardening at the DATA BOUNDARY (cognee 1.5.3). The pipeline-runs API can
# return a run with pipeline_name=null AND status=null (a placeholder record for
# a freshly-created dataset — observed live on a provisioned box). The frontend
# PipelineRun type declares both non-null, so downstream `.toLowerCase()` /
# `.includes()` crash the whole dashboard after login. Coalesce the two fields
# where useDashboardTelemetry casts the fetch result (ONE place covers every
# consumer), and `.status?.includes(` in the terminal for the onboarding render
# path — the terminal's pipeline_name guard is already applied by the block
# above. Upstream bug; drop when cognee ships defensive types.
RUN set -eu; \
    g="src/app/(app)/dashboard/hooks/useDashboardTelemetry.ts"; \
    [ -f "$g" ] && sed -i 's#(Array.isArray(runData) ? runData : \[\]) as PipelineRun\[\]#(Array.isArray(runData) ? runData : []).map((r) => ({ ...r, pipeline_name: r?.pipeline_name ?? "", status: r?.status ?? "" })) as PipelineRun[]#' "$g"; \
    grep -q 'pipeline_name: r?.pipeline_name ?? ""' "$g" \
      || { echo "FATAL: dashboard runs null-normalize did not apply — useDashboardTelemetry.ts changed on this cognee tag; re-audit."; exit 1; }; \
    [ -f "$g" ] && sed -i 's#sessions: sessionsPage?.sessions ?? \[\]#sessions: (sessionsPage?.sessions ?? []).map((s) => ({ ...s, dataset_id: s?.dataset_id ?? "", last_model: s?.last_model ?? "" }))#' "$g"; \
    grep -q 'dataset_id: s?.dataset_id ?? ""' "$g" \
      || { echo "FATAL: dashboard sessions null-normalize did not apply — useDashboardTelemetry.ts changed on this cognee tag; re-audit."; exit 1; }; \
    f="src/ui/elements/AgentActivityTerminal.tsx"; \
    sed -i 's/\br\.status\.includes(/r.status?.includes(/g' "$f"; \
    grep -q 'r.status?.includes(' "$f" \
      || { echo "FATAL: terminal status optional-chain did not apply — AgentActivityTerminal.tsx changed on this cognee tag; re-audit."; exit 1; }

# NOTE: v1.0.1 had a case-sensitivity bug in src/ui/app/index.ts (imports
# "./logo/…"/"./loading/…" while the on-disk dirs were "Logo/"/"Loading/",
# so `next build` failed on Linux) and we sed-patched the imports to match.
# Upstream FIXED this in a later release: at v1.4.0 the imports AND the on-disk
# dirs are both lowercase ("./logo/"/"./loading/"), i.e. internally consistent
# and already Linux-safe. Re-applying the old sed here would REWRITE the
# correct lowercase imports to "./Logo/"/"./Loading/", which no longer match
# the lowercase dirs → "Module not found: Can't resolve './Logo/TextLogo'".
# So the patch is REMOVED for the 1.4.0 line. Re-verified at v1.5.3 (2026.08
# bump): src/ui/app/logo/ and .../loading/ are still lowercase on-disk via the
# GitHub contents API — no regression. If reverting to <1.0.9, restore the sed
# from git history.

# Force type-check + ESLint failures to be non-fatal for the production build,
# matching the upstream dev posture (`next dev` ignores both). Upstream's own
# next.config.mjs at v1.4.0 (re-verified empty at v1.5.3 too) is empty (`{}`),
# so this overwrite drops nothing. Guards our build against upstream type/lint
# regressions that never fire under `next dev` (e.g. the v1.0.1 ApiKeysList.tsx
# type error — since removed upstream).
RUN cat > next.config.mjs <<'NEXT_EOF'
/** @type {import('next').NextConfig} */
const nextConfig = {
  typescript: { ignoreBuildErrors: true },
  eslint: { ignoreDuringBuilds: true },
};
export default nextConfig;
NEXT_EOF

# Build the production bundle.
# #282: build with an EMPTY base so the (patched, `??`) client bundle emits
# same-origin relative `/api/v1/...` URLs — portable across every customer
# domain. Do NOT pass an absolute host here (it would re-bake a build-box domain
# into the client bundle and re-introduce the cross-domain bug). Server-side
# route handlers fall back to the in-cluster `cognee:8000` (patched above).
ARG NEXT_PUBLIC_LOCAL_API_URL=""
ENV NEXT_PUBLIC_LOCAL_API_URL=${NEXT_PUBLIC_LOCAL_API_URL}
RUN npm run build

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0
EXPOSE 3000

CMD ["npm", "run", "start"]
