# SPDX-License-Identifier: BUSL-1.1
# Multi-stage: build the React/Vite SPA, serve the static bundle from nginx.
FROM node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# #2467: pinned to a patch release on the current stable line AND an Alpine
# release. The floating `nginx:1.27-alpine` sat on an end-of-line branch with an
# old Alpine and carried 3 Critical / 14 High (libxml2 CVE-2025-49794/-49796,
# CVE-2025-48174) into the 2026.09-ga.2 SBOM. A floating tag also makes two
# builds of one release differ. Bump deliberately; the SBOM diff is the check.
# #3343 (2026.11-ga.1): 1.30.5 -> 1.30.7. nginx 1.30.5-r1 carried 3 Critical CVEs
# (fixed in 1.30.6). The nginx package comes from nginx.org, not from Alpine, so the
# `apk upgrade` below cannot lift it; only this tag can.
FROM nginx:1.30.7-alpine3.24
# #2336: bring the Alpine runtime base to today's security level (libexpat, libx11,
# pcre2, tiff ... fixed after the base image was cut).
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
# #3524: busybox wget (nginx-alpine) reads http_proxy but ignores no_proxy, and
# has no --no-proxy flag. On a corporate-proxy box the container gets the proxy
# env, so a bare loopback probe went to the proxy (502), the container stayed
# unhealthy, and the upgrade's final health gate ended INCOMPLETE. Clear the
# proxy variables for the probe (same remedy as #273).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
