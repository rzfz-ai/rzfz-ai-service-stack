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

# Pin must match cognee backend tag (modules/knowledge/cognee/Dockerfile: FROM cognee/cognee:1.6.5).
ARG COGNEE_TAG=v1.6.5

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
FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS runtime
# #2336: bring the Alpine base to today's security level.
RUN apk upgrade --no-cache
WORKDIR /app

# Manifests first for layer caching.
# NOTE: upstream's package-lock.json is out-of-sync with package.json at v1.0.1
# (npm ci fails with "lock file's brace-expansion@2.0.2 does not satisfy ...").
# We use `npm install --no-audit --no-fund` so npm reconciles the tree fresh.
# Trade-off: dependency versions are not byte-stable across builds, but the
# upstream tag IS stable, so semver ranges resolve to the same major.minor.
COPY --from=source /src/cognee-frontend/package.json /src/cognee-frontend/package-lock.json ./
# #2336: upstream's lockfile pins next 16.1.6 (2 Critical, 13 High) and a tree with
# further Critical/High advisories (postcss, sharp, nanoid, source-map-js,
# browserslist, flatted, handlebars; brace-expansion, js-yaml, minimatch in the
# lint/test tooling). security-overrides.json carries OUR exact versions, each the
# first release that closes the advisories published for the locked one: `next` as
# a direct dependency, the rest as npm `overrides` (version-keyed where the tree
# holds several majors; postcss only below next, because it is also a direct
# devDependency). They are merged into package.json BEFORE the install, so npm
# re-resolves just those subtrees against upstream's lock. The check after the
# install fails the build if the resolved next is not the pinned one.
COPY security-overrides.json /tmp/security-overrides.json
RUN node -e 'const fs=require("fs");const o=JSON.parse(fs.readFileSync("/tmp/security-overrides.json"));const p=JSON.parse(fs.readFileSync("package.json"));Object.assign(p.dependencies,o.dependencies);p.overrides=Object.assign({},p.overrides,o.overrides);fs.writeFileSync("package.json",JSON.stringify(p,null,2)+"\n")' \
 && rm -f /tmp/security-overrides.json
RUN npm install --no-audit --no-fund \
 && npm rebuild lightningcss \
 && node -e 'const v=require("next/package.json").version;if(v!=="16.3.8"){console.error("FATAL(#2336): next resolved to "+v+", not the pinned 16.3.8");process.exit(1)}' \
 && node -e 'const cp=require("child_process");let o;try{o=cp.execSync("npm ls minimatch --all --json",{encoding:"utf8",stdio:["ignore","pipe","ignore"],maxBuffer:1<<28})}catch(e){o=e.stdout}const bad=[];(function w(d){for(const [k,v] of Object.entries(d||{})){if(k==="minimatch"&&v.version){const [a,b,c]=v.version.split(".").map(Number);if(a===9&&(b===0&&c<7))bad.push(v.version)}w(v.dependencies)}})(JSON.parse(o).dependencies);if(bad.length){console.error("FATAL(#2336): minimatch 9.x below 9.0.7 in the tree: "+bad);process.exit(1)}'

# Source
COPY --from=source /src/cognee-frontend/src ./src
COPY --from=source /src/cognee-frontend/public ./public
COPY --from=source /src/cognee-frontend/postcss.config.mjs ./
COPY --from=source /src/cognee-frontend/tsconfig.json ./
COPY --from=source /src/cognee-frontend/types ./types

# #282: make the CLIENT talk to the SAME ORIGIN it was served from, so this
# image works on ANY customer domain regardless of the build box. NEXT_PUBLIC_*
# is INLINED AT BUILD, so an offline-package image that bakes the packaging
# box's domain into the client bundle sends the browser to the wrong host
# ("Cannot connect to local Cognee backend at https://<build-box>").
# cognee 1.6 reads the backend from one module per side (replacing the
# `|| "http://localhost:8000"` literals 1.5.x scattered over ten files):
#   - src/modules/users/getLocalApiUrl.ts (browser): runtime config
#     (COGNEE_BACKEND_URL, empty on this stack) > NEXT_PUBLIC_LOCAL_API_URL if
#     TRUTHY > `http(s)://<window host>:8000`. We build with an EMPTY
#     NEXT_PUBLIC_LOCAL_API_URL, which the truthiness test discards, so the
#     browser would call <host>:8000 (nothing listens there; Caddy serves
#     /api/* on 443). `!== undefined` keeps the empty string -> same-origin
#     relative `/api/v1/...`, which Caddy forwards to cognee:8000.
#   - src/modules/config/serverRuntimeConfig.ts (route handlers, run inside
#     the Node process and REQUIRE an absolute URL): falls back to
#     `http://localhost:8000`; the backend is `cognee:8000` in-cluster.
# Re-verify both files on a cognee bump.
RUN set -eu; \
    f=src/modules/users/getLocalApiUrl.ts; \
    grep -q 'if (configuredUrl) return stripTrailingSlash(configuredUrl);' "$f" \
      || { echo "FATAL(#282): getLocalApiUrl.ts changed on this tag; re-audit the client API-base patch."; exit 1; }; \
    sed -i 's#if (configuredUrl) return stripTrailingSlash(configuredUrl);#if (configuredUrl !== undefined) return stripTrailingSlash(configuredUrl);#' "$f"; \
    grep -q 'configuredUrl !== undefined' "$f" \
      || { echo "FATAL(#282): client API-base patch did not apply."; exit 1; }; \
    g=src/modules/config/serverRuntimeConfig.ts; \
    grep -q 'const DEFAULT_BACKEND_URL = "http://localhost:8000";' "$g" \
      || { echo "FATAL(#282): serverRuntimeConfig.ts changed on this tag; re-audit the server backend default."; exit 1; }; \
    sed -i 's#const DEFAULT_BACKEND_URL = "http://localhost:8000";#const DEFAULT_BACKEND_URL = "http://cognee:8000";#' "$g"; \
    grep -q 'DEFAULT_BACKEND_URL = "http://cognee:8000"' "$g" \
      || { echo "FATAL(#282): server backend default patch did not apply."; exit 1; }

# #2404: the upstream local sign-in form pre-fills its DEVELOPMENT account
# (`default_user@example.com` / `default_password`, hard-coded constants in
# src/app/(auth)/local-login/partials/LocalSignInForm.tsx, no flag) and says so
# in a caption. On a delivered box that is a working superuser credential handed
# to whoever opens the page (measured on 0.208). Blank both constants and the
# caption; fail the build if the layout moved so the pre-fill cannot creep back
# silently on a cognee bump (the account itself is deactivated by post-install,
# cli/post-install.sh::cognee_reconcile_default_account).
RUN f='src/app/(auth)/local-login/partials/LocalSignInForm.tsx'; \
    grep -q 'const DEFAULT_EMAIL = "default_user@example.com"' "$f" \
      && grep -q 'const DEFAULT_PASSWORD = "default_password"' "$f" \
      || { echo "FATAL(#2404): local-login pre-fill constants not found — cognee-frontend layout changed on this tag; re-audit."; exit 1; }; \
    sed -i 's#const DEFAULT_EMAIL = "default_user@example.com"#const DEFAULT_EMAIL = ""#; s#const DEFAULT_PASSWORD = "default_password"#const DEFAULT_PASSWORD = ""#; s#Default credentials are pre-filled for local development##' "$f"; \
    ! grep -q 'default_user@example.com\|default_password\|pre-filled for local development' "$f" \
      || { echo "FATAL(#2404): the pre-fill survived the patch."; exit 1; }

# Null-hardening (#1006). cognee <= 1.5.3 crashed the whole app on render:
# AgentActivityTerminal did `r.pipeline_name.toLowerCase()` unguarded while
# cognee itself writes pipeline-run rows with a NULL pipeline_name/status.
# Re-audited at v1.6.5: upstream now types the field `string | null`, skips
# every row that is not `kind === "pipeline"` with a name, and the dashboard
# only keeps rows that carry the `kind` discriminator, so the pipeline_name
# guard and the runs normaliser are DROPPED (their sed targets no longer
# exist). Kept as cheap defence where the target text still exists: the
# status optional chain in the terminal and the sessions normaliser in
# useDashboardTelemetry.ts (observed null `dataset_id` / `last_model`).
RUN set -eu; \
    g="src/app/(app)/dashboard/hooks/useDashboardTelemetry.ts"; \
    [ -f "$g" ] && sed -i 's#sessions: sessionsPage?.sessions ?? \[\]#sessions: (sessionsPage?.sessions ?? []).map((s) => ({ ...s, dataset_id: s?.dataset_id ?? "", last_model: s?.last_model ?? "" }))#' "$g"; \
    grep -q 'dataset_id: s?.dataset_id ?? ""' "$g" \
      || { echo "FATAL: dashboard sessions null-normalize did not apply — useDashboardTelemetry.ts changed on this cognee tag; re-audit."; exit 1; }; \
    f="src/ui/elements/AgentActivityTerminal.tsx"; \
    sed -i 's/\br\.status\.includes(/r.status?.includes(/g' "$f"; \
    grep -q 'r.status?.includes(' "$f" \
      || { echo "FATAL: terminal status optional-chain did not apply — AgentActivityTerminal.tsx changed on this cognee tag; re-audit."; exit 1; }; \
    ! grep -q '[^?]\.pipeline_name\.toLowerCase' "$f" || grep -q '!r.pipeline_name' "$f" \
      || { echo "FATAL(#1006): AgentActivityTerminal pipeline_name is unguarded again on this cognee tag; re-audit."; exit 1; }

# #2488: vendor Geist + Geist Mono (SIL OFL 1.1, see fonts/OFL.txt) and switch
# layout.tsx from next/font/google to next/font/local, so `npm run build`
# makes NO network request to fonts.googleapis.com / fonts.gstatic.com. A
# single unreachable-DNS blip against that one host used to abort the whole
# `rzfz init` bake (measured on 0.91, 2026-09-25, #2488) — and cancelling the
# other in-flight bake targets left an unrelated image (caddy) "No such image"
# at compose-up, which is what the operator actually saw. Vendoring removes
# the fetch outright (also required for an air-gapped build). The patch
# script FATAL-guards both ends the same way the sed patches above do: refuses
# to touch the file if upstream's next/font/google usage moved, and refuses to
# finish if next/font/google survived.
COPY fonts ./fonts
COPY patches/apply-local-fonts.js ./patches/apply-local-fonts.js
RUN node patches/apply-local-fonts.js && rm -rf patches

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
