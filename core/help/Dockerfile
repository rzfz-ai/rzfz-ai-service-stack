# #824: the Alpine BRANCH is pinned, not floating (`python:3.11-alpine` today
# resolves to alpine3.24 — identical digest, verified 2026-09-02). The vendored
# `monolith` binary below is a byte-pinned artifact built for ONE Alpine branch
# (musl + libssl.so.3 from that branch); a floating base would silently pair it
# with a different libc/OpenSSL generation on the next Alpine release. Bumping
# this tag and the monolith pins is one deliberate step, tracked in
# config/manifests/versions.json (custom_built.razzfazz-help).
FROM python:3.11-alpine3.24

# wget is needed for mirroring documentation; su-exec lets the entrypoint
# fix /data ownership as root before dropping to appuser (see entrypoint.sh).
# git (#1196): the `git-markdown` capture path clones documentation that only
# exists as markdown in a repository (the Vaultwarden GitHub wiki, paperless'
# docs/ directory — its rendered site sits behind a Cloudflare JS challenge).
# cache_manager.git_available() reports its absence as a per-module failure.
RUN apk add --no-cache wget su-exec git

# ── #824: vendor the `monolith` archiver into the image ──────────────────────
# cache_manager.CAPTURE_MONOLITH is the capture path for the six SPA/CDN doc
# sites (dify, cognee, openhands, gotenberg, komodo, lightrag). `monolith_
# available()` is `shutil.which('monolith')` — with no binary in the image
# EVERY capture on EVERY box records MIRROR_FAILED and the module falls back to
# its local_doc for ever (#824 PR #1118's box-verify tail). This RUN is what
# makes the archiver actually present; it follows the DECISION-7c pattern
# (docs/superpowers/plans/2026-08-29-855-wazuh-module-integration.md): fetch the
# third-party artifact at image-BUILD time against a pinned sha256, so air-gap
# boxes get it inside the image (which ships in the offline package) and never
# reach the internet for it at run time.
#
# WHY NOT THE GITHUB RELEASE ASSET (the shape the ticket first assumed).
# Upstream's release binaries — monolith-gnu-linux-{x86_64,aarch64,armhf} at
# v2.10.1 — are **glibc-dynamic**, not static: verified 2026-09-02 by reading
# the ELF program headers of the downloaded x86_64 asset
# (sha256 663ca914b078e91d5a854b4a07e913c613bbbcfe8fb11a24da1a6ab23c9205df):
# `PT_INTERP = /lib64/ld-linux-x86-64.so.2`, GLIBC_2.* version refs, no musl.
# On a musl base that binary cannot execute at all — "not found" from the
# loader. Y2Z publishes NO musl/static asset for any architecture (all releases
# back to v2.3.1 carry only `-gnu-linux-*` + `monolith.exe`); upstream's own
# Dockerfile builds a musl binary from source rather than shipping one.
#
# WHAT IS VENDORED INSTEAD: the *musl* build of the SAME upstream version,
# 2.10.1, from Alpine's community repository for this image's own branch,
# fetched as a plain .apk over https and verified TWICE — the package file
# against MONOLITH_APK_SHA256_*, and the extracted `usr/bin/monolith` against
# MONOLITH_BIN_SHA256_*. Both were computed from the actually-downloaded files
# on 2026-09-02 (tests/unit/consistency/test_824_vendored_monolith.py pins the
# url/sha256 pairs). The package is unpacked directly instead of `apk add`ed so
# the build never depends on the community repo being enabled or on apk-tools 3
# local-file semantics; the three shared libs its ELF actually needs
# (libssl.so.3, libcrypto.so.3, libgcc_s.so.1 — read out of DT_NEEDED) are
# installed explicitly.
#
# EVERY failure here is LOUD: `set -eu`, `sha256sum -c` on both artifacts, a
# hard exit on an unpinned architecture, a hard exit when the base image has
# moved off the pinned Alpine branch, and a `monolith --version` smoke run that
# proves the binary and its libs actually execute inside the image.
ARG TARGETARCH
ARG ALPINE_BRANCH=v3.24
ARG MONOLITH_VERSION=2.10.1
ARG MONOLITH_APK_REV=r0
ARG MONOLITH_APK_SHA256_AMD64=c49865c57faab6614c97f24c913dd7eb5c3f0f64de6b4d04a4418c4365a324c9
ARG MONOLITH_APK_SHA256_ARM64=df7c985210ebce311b62d6a95b22d1549d3d65bc911e4e587d0f7c34b467f233
ARG MONOLITH_BIN_SHA256_AMD64=f5feac97e49f10d5f65aa3f10b0f5fd4aa2129d4fa036e72eff8614eb0332226
ARG MONOLITH_BIN_SHA256_ARM64=c57919771a51ed796a03742113c322daf1f0dd7e118f66019ef4bdee8112ee32
RUN set -eu; \
    branch="v$(cut -d. -f1,2 /etc/alpine-release)"; \
    if [ "$branch" != "${ALPINE_BRANCH}" ]; then \
        echo "FATAL (#824): base image is Alpine ${branch}, but monolith is pinned for ${ALPINE_BRANCH}." >&2; \
        echo "  A musl binary from one branch must not be paired with another branch's libc/OpenSSL." >&2; \
        echo "  Re-pin ALPINE_BRANCH + MONOLITH_APK_SHA256_* + MONOLITH_BIN_SHA256_* in this file" >&2; \
        echo "  and in config/manifests/versions.json (custom_built.razzfazz-help), then rebuild." >&2; \
        exit 1; \
    fi; \
    arch="${TARGETARCH:-}"; \
    if [ -z "$arch" ]; then \
        case "$(uname -m)" in \
            x86_64)  arch=amd64 ;; \
            aarch64) arch=arm64 ;; \
            *)       arch="$(uname -m)" ;; \
        esac; \
    fi; \
    case "$arch" in \
        amd64) apk_arch=x86_64;  apk_sha="${MONOLITH_APK_SHA256_AMD64}"; bin_sha="${MONOLITH_BIN_SHA256_AMD64}" ;; \
        arm64) apk_arch=aarch64; apk_sha="${MONOLITH_APK_SHA256_ARM64}"; bin_sha="${MONOLITH_BIN_SHA256_ARM64}" ;; \
        *) \
            echo "FATAL (#824): no monolith sha256 pin for architecture '${arch}'." >&2; \
            echo "  Only linux/amd64 and linux/arm64 are pinned. Add the branch's apk +" >&2; \
            echo "  binary sha256 for this arch rather than installing an unverified build." >&2; \
            exit 1 ;; \
    esac; \
    url="https://dl-cdn.alpinelinux.org/alpine/${ALPINE_BRANCH}/community/${apk_arch}/monolith-${MONOLITH_VERSION}-${MONOLITH_APK_REV}.apk"; \
    echo "[help-build] #824 vendoring monolith ${MONOLITH_VERSION}-${MONOLITH_APK_REV} (${apk_arch}) from ${url}"; \
    wget -q -O /tmp/monolith.apk "$url"; \
    printf '%s  %s\n' "$apk_sha" /tmp/monolith.apk > /tmp/monolith.apk.sha256; \
    sha256sum -c /tmp/monolith.apk.sha256; \
    apk add --no-cache --virtual .monolith-unpack tar; \
    mkdir -p /tmp/monolith-unpack; \
    tar -xzf /tmp/monolith.apk -C /tmp/monolith-unpack usr/bin/monolith; \
    apk del .monolith-unpack; \
    printf '%s  %s\n' "$bin_sha" /tmp/monolith-unpack/usr/bin/monolith > /tmp/monolith.bin.sha256; \
    sha256sum -c /tmp/monolith.bin.sha256; \
    install -m 0755 /tmp/monolith-unpack/usr/bin/monolith /usr/local/bin/monolith; \
    rm -rf /tmp/monolith.apk /tmp/monolith.apk.sha256 /tmp/monolith.bin.sha256 /tmp/monolith-unpack; \
    apk add --no-cache libgcc libssl3 libcrypto3; \
    monolith --version; \
    monolith --version | grep -q "${MONOLITH_VERSION}"; \
    test "$(command -v monolith)" = /usr/local/bin/monolith

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
# #986: app.py does `from search_index import build_index, collect_corpus` for the
# Help Center's local full-text search — the module MUST be in the image or the
# gunicorn worker fails to boot ("No module named 'search_index'").
COPY core/help/search_index.py .
# #1196: the mirror sweep modules cache_manager imports — the content-quality
# probe, the markdown-tree format (llms.txt / git sources), the bounded HTTP
# fetcher and the post-wget asset completion. All four MUST ship or the
# worker fails at import exactly like the #986 search_index omission did.
COPY core/help/mirror_quality.py .
COPY core/help/markdown_tree.py .
COPY core/help/mirror_fetch.py .
COPY core/help/mirror_assets.py .
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
