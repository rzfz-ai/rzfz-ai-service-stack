# SPDX-License-Identifier: BUSL-1.1
# Multi-stage: build the React/Vite SPA, serve the static bundle from nginx.
FROM node:24-alpine AS build
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
FROM nginx:1.30.5-alpine3.24
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
