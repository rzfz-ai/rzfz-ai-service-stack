# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
from flask import render_template
import os
import json

from razzfazz_common.calver import latest as calver_latest
from razzfazz_common.flask_app import create_base_app

# M026 S05 #1: shared base app provides /healthz, session config, and the
# {razzfazz_version, main_domain, brand_color} template context. Auth is
# enforced by Caddy `forward_auth` upstream of this container (see
# core/Caddy/Caddyfile §7), so require_auth=False here matches the prior
# behaviour of the locally-defined Flask app.
app = create_base_app(__name__, service_name='razzfazz-licenses', require_auth=False)

# #68 license reorg: the component→licence map is now read DYNAMICALLY from the
# stack.yaml SSOT (shipped as stack.json + bind-mounted at /app/stack.json) and
# the rolling BSL Change Dates from license-dates.json — no more hardcoded dicts.
STACK_FILE = os.environ.get("RAZZFAZZ_STACK_JSON", "/app/stack.json")
DATES_FILE = os.environ.get("RAZZFAZZ_LICENSE_DATES", "/app/license-dates.json")
MANIFEST_FILE = "licenses.json"           # name -> downloaded license text file

manifest = {}
if os.path.exists(MANIFEST_FILE):
    with open(MANIFEST_FILE) as f:
        manifest = json.load(f)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def current_change_date():
    """(change_date, tag) for the release THIS BOX runs, or None.

    #524 Two bugs, and fixing either alone left the page wrong.

    1. It answered "newest release in the file" instead of "what this box runs".
       Those diverge the moment the file falls behind — which it had, by a whole
       cycle and twelve patch releases, because nothing in the release path
       appends to it. A ga.12 box was shown ga.9.
    2. It picked that newest release with `sorted(rels)[-1]`, and `"ga.9"` sorts
       above `"ga.12"`. So even once the data was backfilled it would still have
       shown ga.9.

    The second bug makes the first one worse than cosmetic: the BSL Change Date
    is read from the same record, so the page told the customer their code
    converts to Apache-2.0 EARLIER than it actually does for their build. That is
    a licensing statement on a customer-facing page.

    `RAZZFAZZ_VERSION` comes from `.env` via the container's env_file, so it is
    the box's own answer to "what am I running". Falling back to the newest
    parseable release keeps the page working on a box that never set it.
    """
    dates = _load_json(DATES_FILE)
    rels = dates.get("releases", {})
    if not rels:
        return None

    running = (os.environ.get("RAZZFAZZ_VERSION") or "").strip().lstrip("v")
    if running and running in rels:
        return rels[running].get("change_date"), running

    # Either the box does not declare a version, or it declares one this file has
    # never heard of — which is itself the #524 symptom (the file lags releases).
    # Fall back to the newest release we DO know, by CalVer rather than string
    # order, and never to an arbitrary row.
    tag = calver_latest(rels)
    if tag is None:
        return None
    return rels[tag].get("change_date"), tag


# Manifest keys are display names ("Vaultwarden"); stack.json container names are
# lowercase ("vaultwarden"). This maps names to a friendly label + the license
# text file / upstream URL for the "view licence" link.
DISPLAY = {
    "caddy": "Caddy", "postgres": "PostgreSQL / pgvector",
    "authentik-server": "Authentik", "authentik-worker": "Authentik",
    "valkey": "Valkey", "smtp-relay": "SMTP relay (boky/postfix)",
    "backup-service": "offen/docker-volume-backup",
    "docker-socket-proxy": "docker-socket-proxy", "autoheal": "autoheal",
    "openwebui": "Open WebUI", "pipelines": "Pipelines",
    "dify-api": "Dify", "dify-worker": "Dify", "dify-worker-beat": "Dify",
    "dify-web": "Dify", "dify-sandbox": "Dify", "dify-plugin-daemon": "Dify",
    "dify-init-permissions": "Dify",
    "gpustack": "GPUStack", "ollama-proxy": "Ollama-OpenAI proxy",
    "komodo-core": "Komodo", "komodo-periphery": "Komodo", "ferretdb": "FerretDB",
    "postgres-komodo": "FerretDB DocumentDB", "openlit": "OpenLIT",
    "clickhouse": "ClickHouse", "searxng": "SearXNG", "crawl4ai": "Crawl4AI",
    "speaches": "Speaches", "gotenberg": "Gotenberg", "tika": "Apache Tika",
    "docling": "Docling", "docling-rq-worker": "Docling",
    "presidio-analyzer": "Presidio", "presidio-anonymizer": "Presidio",
    "presidio-image-redactor": "Presidio", "stirling-pdf": "Stirling-PDF",
    "paperless-ngx": "paperless-ngx", "gitea": "Gitea", "lightrag": "LightRAG",
    "cognee": "Cognee", "cognee-mcp": "Cognee", "cognee-frontend": "Cognee",
    "onyx-api": "Onyx", "onyx-background": "Onyx", "onyx-web": "Onyx",
    "onyx-model-server": "Onyx", "onyx-model-indexer": "Onyx", "onyx-vespa": "Vespa",
    "synapse": "Synapse", "element-web": "Element Web", "vaultwarden": "Vaultwarden",
    "infisical": "Infisical", "openhands": "OpenHands", "paperclip": "Paperclip",
    # coding-agent split (#36 / PR #84): per-CLI bundled licences.
    "opencode": "opencode", "codex": "OpenAI Codex", "gsd-pi": "gsd-pi",
}


def license_link(display, built_from):
    # prefer a downloaded upstream licence text; else the upstream source URL
    if display in manifest:
        return f"/static/licenses/{manifest[display]}"
    if built_from:
        return built_from
    fn = display.lower().replace(" ", "_").replace("/", "_") + ".txt"
    return f"/static/licenses/{fn}"


def spdx_of(lic):
    if lic and lic.startswith("upstream:"):
        return lic.split(":", 1)[1]
    return lic


@app.route('/')
def index():
    stack = _load_json(STACK_FILE)
    cd = current_change_date()
    change_date, cur_tag = (cd if cd else (None, None))

    modules = list(stack.get("modules", {}).values()) + \
        list(stack.get("per_user_agents", {}).values())

    ee = {}                 # image -> label   (Enterprise / BSL)
    upstream = {}           # category -> {display: (spdx, built_from)}

    for mod in modules:
        cat = mod.get("category", "Other")
        mlic = mod.get("license", "")
        med = mod.get("edition", "")
        mbf = mod.get("built_from")
        for c in mod.get("containers", []):
            if not isinstance(c, dict):
                continue
            name = c.get("name", "")
            lic = c.get("license", mlic)
            ed = c.get("edition", med)
            bf = c.get("built_from", mbf)
            disp = DISPLAY.get(name, name)
            if ed == "enterprise" or lic == "busl-1.1":
                ee[c.get("image", disp)] = disp
            elif ed == "community" or lic == "apache-2.0":
                pass   # our Community code — summarised in its own section below
            else:
                spdx = spdx_of(lic)
                upstream.setdefault(cat, {})[disp] = (spdx, bf)

    sections = []

    # 1. rzfz.ai Subscription tier (source-available, BSL 1.1)
    if change_date:
        ee_lic = f"BUSL-1.1 · source-available · → Apache-2.0 on {change_date}"
    else:
        ee_lic = "BUSL-1.1 · source-available"
    ee_items = [(label, ee_lic, "/static/licenses/busl-1.1.txt")
                for label in sorted(set(ee.values()))]
    if ee_items:
        title = "rzfz.ai Subscription — source-available (BSL 1.1)"
        if cur_tag:
            title += f"  ·  current version {cur_tag}"
        sections.append((title, ee_items))

    # 2. Community tier (open source, Apache-2.0)
    comm_items = [
        ("Compose wiring, config templates, base model wiring, build recipes",
         "Apache-2.0 · open source", "/static/licenses/apache-2.0-community.txt"),
        ("Documentation", "Apache-2.0 · open source",
         "/static/licenses/apache-2.0-community.txt"),
    ]
    sections.append(
        ("razzfazz.ai Community — Open Source (Apache-2.0)", comm_items))

    # 3..N. Bundled & built upstream, grouped by category
    for cat in sorted(upstream):
        items = []
        for disp in sorted(upstream[cat]):
            spdx, bf = upstream[cat][disp]
            label = spdx or "see upstream"
            items.append((disp, label, license_link(disp, bf)))
        sections.append((f"Bundled / built upstream — {cat}", items))

    return render_template('index.html', sections=sections,
                           change_date=change_date, cur_tag=cur_tag)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
