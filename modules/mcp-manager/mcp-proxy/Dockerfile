# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# razzfazz-mcp-proxy, Google build (#2472/#2086; design record §3, rulings I4/R2-2).
#
# THE PIN (what makes this "the in-repo pinned build"):
#   1. The base is pinned by its registry digest below (python:3.12.14-alpine3.24).
#   2. Nothing else enters the image: server.py uses the standard library only,
#      and this file installs no package.
#   3. The image TAG is content-addressed: google-<first 12 hex of the SHA-256
#      over the build inputs: this Dockerfile and every file it COPYs> (the rule is in
#      tests/unit/mcp-manager/test_2472_mcp_proxy_pinned_build.py). The same tag
#      is written in the compose builder (modules/mcp-manager/compose.yml), the
#      catalogue (core/mcp/personal-mcp-catalog.yaml `google`), the manifest
#      (config/manifests/versions.json custom_built) and the gateway
#      (gateway_credentials.PINNED_PROXY_IMAGES). Changing a byte here without
#      re-tagging turns that test red; the gateway relays only to an instance
#      launched from exactly that tag.
FROM python:3.12.14-alpine3.24@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111

WORKDIR /app
COPY server.py /app/server.py
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)" || exit 1
ENTRYPOINT ["python3", "/app/server.py"]
