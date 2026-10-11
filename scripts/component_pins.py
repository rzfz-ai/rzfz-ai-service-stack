#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""component_pins.py — the ONE table that says where a component's version pin lives.

The licence notices (scripts/generate-notice.py), the Corresponding-Source
mirror (scripts/generate-source-mirror.sh), the per-module "Built from" NOTICE
lines and the vendored licence texts (core/licenses/download_licenses.py) all
need "which upstream version/tag do we ship". Each used to hand-type it, and
they drifted apart (Open WebUI 0.9.5 in the notice vs 0.11.3 pinned; licence
texts fetched from `main`). This table records only WHERE the pin is read and
HOW the upstream git tag is spelled — never a version.

Pin sources, in order of preference (decided 2026-10-07):
  ("env", KEY)                    config/.env.example `KEY=value` — the SSOT
  ("compose", path, image)        a literal `image: <image>:<tag>` in a compose
                                  file, for images that have no .env key
  ("dockerfile", path, regex)     a Dockerfile ARG/FROM, group 1 is the version

Tag spelling (`tag`): a format string over
  {ver}   the pin verbatim               (Dify 1.17.1 -> "1.17.1")
  {base}  the pin's `base` regex group 1 (pgvector 0.8.2-pg17 -> "0.8.2")
Upstreams differ (v-prefix or not, `version/…`, `rust-v…`, `-lts`); every tag
here was checked against the upstream repo when the row was added.

Row fields besides the pin:
  repo           GitHub owner/name
  notice         display name in NOTICE / THIRD-PARTY-NOTICES.md (generate-notice.py ENRICH key)
  catalogue      key in download_licenses.LICENSE_URLS (licence text fetched at the tag)
  mirror         {"name", "spdx"} — a Corresponding-Source mirror row
  module_notices [(path, label)] — per-module NOTICE "Built from: <label> <version>" lines
                 that generate-notice.py rewrites (and --check verifies)

    python3 scripts/component_pins.py            # print every pin (key version tag source)
    python3 scripts/component_pins.py --mirror   # name|repo|tag|SPDX rows for the source mirror
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_EXAMPLE = os.path.join("config", ".env.example")


class PinError(Exception):
    pass


PINS = {
    # --- copyleft: Corresponding Source mirrored at the pinned tag ------------
    "searxng": dict(src=("env", "SEARXNG_VERSION"), repo="searxng/searxng",
                    # SearXNG publishes no git tags; the image version is
                    # <date>-<commit>, and the commit is the source coordinate.
                    base=r"-([0-9a-f]{7,40})$", tag="{base}",
                    notice="SearXNG", catalogue="SearXNG",
                    mirror={"name": "searxng", "spdx": "AGPL-3.0"}),
    "synapse": dict(src=("compose", "modules/apps/matrix/compose.yml", "matrixdotorg/synapse"),
                    repo="element-hq/synapse", tag="{ver}",
                    notice="Synapse", catalogue="Synapse",
                    mirror={"name": "synapse", "spdx": "AGPL-3.0"}),
    "element-web": dict(src=("compose", "modules/apps/matrix/compose.yml", "vectorim/element-web"),
                        repo="element-hq/element-web", tag="{ver}",
                        notice="Element Web", catalogue="Element Web",
                        mirror={"name": "element-web", "spdx": "AGPL-3.0"}),
    "vaultwarden": dict(src=("compose", "modules/apps/vaultwarden/compose.yml", "vaultwarden/server"),
                        repo="dani-garcia/vaultwarden", tag="{ver}",
                        notice="Vaultwarden", catalogue="Vaultwarden",
                        mirror={"name": "vaultwarden", "spdx": "AGPL-3.0"}),
    "komodo": dict(src=("env", "KOMODO_VERSION"), repo="moghtech/komodo", tag="v{ver}",
                   notice="Komodo", catalogue="Komodo",
                   mirror={"name": "komodo", "spdx": "GPL-3.0"}),
    "paperless-ngx": dict(src=("compose", "modules/doc-processing/paperless-ngx/compose.yml",
                               "ghcr.io/paperless-ngx/paperless-ngx"),
                          repo="paperless-ngx/paperless-ngx", tag="v{ver}",
                          notice="paperless-ngx", catalogue="paperless-ngx",
                          mirror={"name": "paperless-ngx", "spdx": "GPL-3.0"}),
    # --- weak copyleft --------------------------------------------------------
    "backup": dict(src=("env", "BACKUP_VERSION"), repo="offen/docker-volume-backup", tag="{ver}",
                   notice="offen/docker-volume-backup",
                   module_notices=[("core/backup/NOTICE", "offen/docker-volume-backup")]),
    # --- permissive with notice enrichment ------------------------------------
    "dify": dict(src=("env", "DIFY_VERSION"), repo="langgenius/dify", tag="{ver}",
                 notice="Dify", catalogue="Dify",
                 module_notices=[("modules/dify/NOTICE", "Dify (web)")]),
    "openwebui": dict(src=("env", "OPENWEBUI_VERSION"), repo="open-webui/open-webui", tag="v{ver}",
                      notice="Open WebUI", catalogue="Open WebUI"),
    "authentik": dict(src=("env", "AUTHENTIK_VERSION"), repo="goauthentik/authentik",
                      tag="version/{ver}", notice="Authentik", catalogue="Authentik"),
    # Every shipped GPUStack build is 0.7.1: razzfazz-gpustack:cuda is FROM
    # gpustack/gpustack:v0.7.1, :vulkan clones --branch v0.7.1, and the CPU
    # overlay runs the upstream v0.7.1-cpu image.
    "gpustack": dict(src=("dockerfile", "modules/llm/gpustack/Dockerfile.cuda",
                          r"^FROM gpustack/gpustack:(\S+)\s*$"),
                     repo="gpustack/gpustack", tag="{ver}",
                     notice="GPUStack", catalogue="GPUStack",
                     module_notices=[("modules/llm/gpustack/NOTICE", "GPUStack")]),
    "caddy": dict(src=("dockerfile", "core/Caddy/Dockerfile", r"^FROM caddy:([0-9][0-9.]*)\s*$"),
                  repo="caddyserver/caddy", tag="v{ver}",
                  notice="Caddy", catalogue="Caddy",
                  module_notices=[("core/Caddy/NOTICE", "Caddy")]),
    "opencode": dict(src=("env", "OPENCODE_VERSION"), repo="sst/opencode", tag="v{ver}",
                     notice="opencode", catalogue="opencode"),
    "codex": dict(src=("env", "CODEX_VERSION"), repo="openai/codex", tag="rust-v{ver}",
                  notice="OpenAI Codex"),
    "pgvector": dict(src=("compose", "core/compose.yml", "pgvector/pgvector"),
                     repo="pgvector/pgvector", base=r"^(\d+\.\d+\.\d+)", tag="v{base}",
                     notice="PostgreSQL / pgvector", catalogue="pgvector"),
    # --- licence text only (no notice enrichment) -----------------------------
    "valkey": dict(src=("env", "VALKEY_VERSION"), repo="valkey-io/valkey", tag="{ver}",
                   catalogue="Valkey"),
    "ferretdb": dict(src=("env", "FERRETDB_VERSION"), repo="FerretDB/FerretDB", tag="v{ver}",
                     catalogue="FerretDB"),
    "gitea": dict(src=("env", "GITEA_VERSION"), repo="go-gitea/gitea", tag="v{ver}",
                  catalogue="Gitea"),
    "pipelines": dict(src=("env", "PIPELINES_VERSION"), repo="open-webui/pipelines",
                      base=r"^git-([0-9a-f]{7,40})$", tag="{base}", catalogue="Pipelines"),
    "gotenberg": dict(src=("env", "GOTENBERG_VERSION"), repo="gotenberg/gotenberg", tag="v{ver}",
                      catalogue="Gotenberg"),
    "openlit": dict(src=("env", "OBSERVABILITY_VERSION"), repo="openlit/openlit",
                    tag="openlit-{ver}", catalogue="OpenLIT"),
    "clickhouse": dict(src=("env", "CLICKHOUSE_VERSION"), repo="ClickHouse/ClickHouse",
                       tag="v{ver}-lts", catalogue="ClickHouse"),
    "crawl4ai": dict(src=("env", "CRAWL4AI_VERSION"), repo="unclecode/crawl4ai", tag="v{ver}",
                     catalogue="Crawl4AI"),
    "speaches": dict(src=("env", "SPEACHES_VERSION"), repo="speaches-ai/speaches",
                     base=r"^(\d+\.\d+\.\d+)", tag="v{base}", catalogue="Speaches"),
    "lightrag": dict(src=("env", "LIGHTRAG_VERSION"), repo="HKUDS/LightRAG", tag="{ver}",
                     catalogue="LightRAG"),
    "cognee": dict(src=("dockerfile", "modules/knowledge/cognee/Dockerfile",
                        r"^FROM cognee/cognee:(\S+)\s*$"),
                   repo="topoteretes/cognee", tag="v{ver}", catalogue="Cognee",
                   module_notices=[("modules/knowledge/cognee/NOTICE", "Cognee")]),
    # apache/tika image tags are <tika version>.<image revision>[-full].
    "tika": dict(src=("compose", "modules/doc-processing/tika/compose.yml", "apache/tika"),
                 repo="apache/tika", base=r"^(\d+\.\d+\.\d+)\.\d+", tag="{base}",
                 catalogue="Apache Tika"),
    "docling": dict(src=("compose", "modules/doc-processing/docling/compose.yml",
                         "ghcr.io/docling-project/docling-serve-cpu"),
                    repo="docling-project/docling-serve", tag="{ver}", catalogue="Docling"),
    "presidio": dict(src=("compose", "modules/doc-processing/presidio/compose.yml",
                          "mcr.microsoft.com/presidio-analyzer"),
                     repo="microsoft/presidio", tag="{ver}", catalogue="Presidio"),
    "stirling-pdf": dict(src=("compose", "modules/doc-processing/stirling-pdf/compose.yml",
                              "stirlingtools/stirling-pdf"),
                         repo="Stirling-Tools/Stirling-PDF", tag="v{ver}", catalogue="Stirling-PDF"),
    "paperclip": dict(src=("env", "PAPERCLIP_VERSION"), repo="paperclipai/paperclip", tag="{ver}",
                      catalogue="Paperclip",
                      module_notices=[("modules/apps/paperclip/NOTICE", "Paperclip")]),
    "moltis": dict(src=("env", "MOLTIS_VERSION"), repo="moltis-org/moltis", tag="{ver}",
                   catalogue="Moltis",
                   module_notices=[("modules/agents/moltis/NOTICE", "Moltis")]),
    "hermes-agent": dict(src=("env", "HERMES_AGENT_VERSION"), repo="NousResearch/hermes-agent",
                         tag="{ver}", catalogue="Hermes Agent",
                         module_notices=[("modules/agents/hermes-agent/NOTICE", "NousResearch Hermes")]),
    "infisical": dict(src=("compose", "modules/apps/infisical/compose.yml", "infisical/infisical"),
                      repo="Infisical/infisical", tag="{ver}", catalogue="Infisical"),
    "onyx": dict(src=("compose", "modules/apps/onyx/compose.yml", "onyxdotapp/onyx-backend"),
                 repo="onyx-dot-app/onyx", tag="{ver}", catalogue="Onyx"),
    "openhands": dict(src=("env", "OPENHANDS_VERSION"), repo="All-Hands-AI/OpenHands", tag="{ver}",
                      catalogue="OpenHands"),
    "openuem": dict(src=("env", "OPENUEM_VERSION"), repo="open-uem/openuem-console", tag="v{ver}",
                    catalogue="OpenUEM"),
    "wazuh": dict(src=("env", "WAZUH_VERSION"), repo="wazuh/wazuh", tag="v{ver}", catalogue="Wazuh"),
    "wazuh-indexer": dict(src=("env", "WAZUH_VERSION"), repo="wazuh/wazuh-indexer", tag="v{ver}",
                          catalogue="Wazuh Indexer"),
    "wazuh-dashboard": dict(src=("env", "WAZUH_VERSION"), repo="wazuh/wazuh-dashboard", tag="v{ver}",
                            catalogue="Wazuh Dashboard"),
    # The llama.cpp runners and razzfazz-gpustack:cuda build b9851; the legacy
    # razzfazz-gpustack:vulkan builds b9112 (same MIT licence text).
    "llama.cpp": dict(src=("dockerfile", "modules/llm/runners/llama-vulkan/Dockerfile",
                           r"^ARG LLAMA_CPP_TAG=(\S+)\s*$"),
                      repo="ggml-org/llama.cpp", tag="{ver}", catalogue="llama.cpp"),
}

# A mirrored copyleft component with NO exact pin in the tree. Kept as a named
# exception (not a silent hand-typed row): requirements files pin
# `psycopg2-binary==2.9.*`, a range, so there is no single tag to derive.
# An LGPL library we bundle (dynamically linked, unmodified) — mirrored for
# completeness; drop the row if counsel does not require it.
MIRROR_EXTRA = {
    "psycopg2": dict(repo="psycopg/psycopg2", tag="2_9_10", spdx="LGPL-3.0",
                     reason="requirements pin psycopg2-binary==2.9.* (a range); no exact pin in the tree"),
}

# Catalogue entries (download_licenses.LICENSE_URLS) that have no version pin
# in the tree, with the reason. Their licence text stays on the upstream
# branch named in the catalogue.
UNPINNED_CATALOGUE = {
    "Docker Runtime": "host-installed; the stack only sets a minimum engine API",
    "Docker Compose V2": "host-installed; scripts/lib.sh sets only a minimum version",
    "Vulkan Driver (AMD)": "host driver, not shipped in an image",
    "Python": "no single pin; many base images with different Python versions",
    "FalkorDB": "no FalkorDB image is shipped any more; no pin in the tree",
    "Gemma 4": "model weights; the catalogued repo is code with no shipped pin",
    "Mistral / Mixtral": "model weights; the catalogued repo is code with no shipped pin",
    "Qwen": "model weights; the catalogued repo is code with no shipped pin",
    "Nomic Embed": "model weights; the catalogued repo is code with no shipped pin",
    "Flask": "requirements pin a range (flask==3.1.*)",
    "Gunicorn": "requirements pin a range (gunicorn==23.*)",
    "Requests": "requirements pin a range (requests==2.*)",
    "psycopg2": "requirements pin a range (psycopg2-binary==2.9.*)",
    "docker-py": "requirements pin a range (docker==7.*)",
    "APScheduler": "requirements pin a range (apscheduler==3.*)",
    "Jinja2": "transitive (via Flask); no pin",
    "Werkzeug": "transitive (via Flask); no pin",
    "Geist": "vendored .woff2 files; no version pin in the tree",
    "Geist Mono": "vendored .woff2 files; no version pin in the tree",
}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _from_env(key):
    for line in _read(ENV_EXAMPLE).splitlines():
        m = re.match(rf"^{re.escape(key)}=(\S*)", line)
        if m:
            val = m.group(1).strip("\"'")
            if not val:
                raise PinError(f"{ENV_EXAMPLE}: {key} is empty")
            return val, f"{ENV_EXAMPLE}:{key}"
    raise PinError(f"{ENV_EXAMPLE} has no {key}=")


def _from_compose(path, image):
    m = re.search(rf"^\s*image:\s*[\"']?{re.escape(image)}:([^\"'\s]+)", _read(path), re.M)
    if not m:
        raise PinError(f"{path}: no `image: {image}:<tag>`")
    val = m.group(1)
    if "$" in val:
        raise PinError(f"{path}: {image} is pinned through a variable ({val}); use its .env key")
    return val, f"{path} ({image})"


def _from_dockerfile(path, regex):
    m = re.search(regex, _read(path), re.M)
    if not m:
        raise PinError(f"{path}: no match for {regex}")
    return m.group(1), path


def resolve(key):
    """{'version': pin verbatim, 'tag': upstream git tag, 'source': where it was read}."""
    p = PINS[key]
    kind, *args = p["src"]
    reader = {"env": _from_env, "compose": _from_compose, "dockerfile": _from_dockerfile}[kind]
    ver, where = reader(*args)
    fields = {"ver": ver}
    if p.get("base"):
        m = re.search(p["base"], ver)
        if not m:
            raise PinError(f"{key}: pin {ver!r} does not match base {p['base']}")
        fields["base"] = m.group(1)
    return {"version": ver, "tag": p["tag"].format(**fields), "source": where}


def by_field(field):
    """{row[field]: key} for every row that sets `field` (notice / catalogue)."""
    return {p[field]: k for k, p in PINS.items() if p.get(field)}


def mirror_rows():
    """[(name, repo_url, tag, spdx)] — the Corresponding-Source mirror matrix."""
    rows = []
    for key, p in PINS.items():
        if p.get("mirror"):
            rows.append((p["mirror"]["name"], f"https://github.com/{p['repo']}",
                         resolve(key)["tag"], p["mirror"]["spdx"]))
    for name, x in MIRROR_EXTRA.items():
        rows.append((name, f"https://github.com/{x['repo']}", x["tag"], x["spdx"]))
    return rows


def main(argv):
    if "--mirror" in argv:
        for row in mirror_rows():
            print("|".join(row))
        return 0
    rc = 0
    for key in PINS:
        try:
            r = resolve(key)
            print(f"{key:16} {r['version']:24} {r['tag']:24} {r['source']}")
        except PinError as e:
            print(f"{key:16} UNRESOLVED: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
