# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# authentik-init tools image (#253)
# ------------------------------------------------------------------------------
# The Authentik init one-shot (core/init-authentik.sh) needs `envsubst` (from
# gettext, to template blueprints) and `docker` (docker-cli, to reconcile
# embedded-outpost bindings through the docker-socket-proxy). Previously
# ensure_init_tools() `apk add`ed these AT RUNTIME on every run — which FAILS on
# air-gapped boxes (#253 / #147: "apk add gettext/docker-cli FAILED — restricted
# network?"), silently skipping the ALWAYS-RUN outpost-binding reconcile and
# 404'ing SSO-gated apps. Baking the toolchain at BUILD time makes the init
# container self-contained: zero runtime network egress on offline boxes.
#
# The init script itself stays bind-mounted at runtime (see core/compose.yml
# authentik-init `volumes:`), so this image carries ONLY the toolchain — no app
# code, nothing to keep in sync.
# ==============================================================================
FROM alpine:3.24
RUN apk add --no-cache gettext docker-cli
