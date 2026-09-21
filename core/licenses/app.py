# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
from flask import render_template
import os
import json
import re as _re
from urllib.parse import urlparse as _urlparse

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


# HLP-12: `built_from` is free-form text out of stack.json and lands in an
# `href` on a customer-facing page. Only absolute http(s) URLs may become a
# link; anything else (a `javascript:` scheme, a bare path, a typo) is dropped
# rather than being rendered as-is.
_SAFE_LINK_SCHEMES = ("http", "https")


def _is_safe_upstream_url(url) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = _urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in _SAFE_LINK_SCHEMES and bool(parsed.netloc)


def license_link(display, built_from):
    """Resolve the "view licence" target for one component, or None.

    #1056. The offline licence texts are NOT in the repo — `download_licenses.py`
    fetches them at image-build time and records what it actually wrote in
    `licenses.json`. **The manifest is therefore the only record of which
    `/static/licenses/*.txt` files exist.**

    The old last branch ignored that and CONSTRUCTED
    `/static/licenses/<slugged display>.txt` for any display name the manifest
    did not know. Nothing ever writes such a file, so every one of those links
    was a guaranteed 404 in the side pane — and it fired for real components:
    a display name that simply differs from the download catalogue's key
    ("PostgreSQL / pgvector" vs "PostgreSQL"), a component with no catalogue
    entry at all (autoheal, docker-socket-proxy, Vespa, …), and every component
    whose download had failed, because the build did not stop on that either.

    Resolution order is now, strictly:
      1. the downloaded local text, when the manifest says it was written;
      2. else the upstream `built_from` project URL (rendered as an external
         link — the template marks it `data-upstream` and licenses.js lets the
         native `target="_blank"` open it instead of the side pane);
      3. else None — the caller omits the link. A missing link is honest; a
         dead one is not.
    """
    if display in manifest:
        return f"/static/licenses/{manifest[display]}"
    if _is_safe_upstream_url(built_from):
        return built_from
    return None


def spdx_of(lic):
    if lic and lic.startswith("upstream:"):
        return lic.split(":", 1)[1]
    return lic


# license-family classification for the redesign chips (#983). Presentation
# only: derived from the SPDX already in `sections`; no data change.
#
# HLP-8: this was a chain of SUBSTRING checks, and the chip is a licensing
# statement on a customer-facing page, so a wrong bucket is not cosmetic:
#   * `"gpl" in s` swallowed LGPL-2.1/LGPL-3.0 into the AGPL/GPL `copyleft`
#     bucket — weak copyleft filed under strong copyleft, materially different
#     obligations, under a rail legend that literally reads "AGPL / GPL".
#   * SSPL-1.0, Elastic-2.0, CC-BY-*, CC0 and Unlicense all fell through to
#     `other`: an uncoloured chip with no legend row at all.
#   * `"mit" in s` fired on any label merely CONTAINING that substring.
# Now: the label is split into tokens and each token is matched by ANCHORED
# prefix against normalized SPDX ids, so "lgpl-2.1" can no longer be read as
# "gpl". First token that classifies wins, which is what makes compound labels
# ("MIT AND BSD-3-Clause") deterministic — and keeps the BUSL change-note
# ("BUSL-1.1 · … · → Apache-2.0 on <date>") a BUSL chip rather than an Apache
# one, since its own id is the first token.
_FAMILY_PREFIXES = (
    ("busl", "busl"),
    ("bsl", "busl"),
    ("lgpl", "lgpl"),          # MUST stay ahead of "gpl" for readability; the
    ("agpl", "copyleft"),      # anchored match is what actually separates them
    ("gpl", "copyleft"),
    ("mpl", "mpl"),
    ("sspl", "sourceavail"),
    ("elastic", "sourceavail"),
    ("elv2", "sourceavail"),
    ("cc0", "publicdomain"),
    ("unlicense", "publicdomain"),
    ("cc", "cc"),
    ("mit", "mit"),
    ("bsd", "bsd"),
    ("postgresql", "bsd"),
    ("isc", "bsd"),
    ("apache", "apache"),
)

# The families a chip/legend swatch exists for. `other` is the explicit
# unclassified bucket and now has a legend row of its own (HLP-8) — an
# uncoloured chip nobody can look up is worse than an honest "unclassified".
LICENSE_FAMILIES = ("busl", "apache", "mit", "bsd", "copyleft", "lgpl", "mpl",
                    "sourceavail", "cc", "publicdomain", "other")

_FAMILY_TOKEN_RE = _re.compile(r"[a-z0-9][a-z0-9.]*(?:-[a-z0-9.]+)*")


def family_of(spdx: str) -> str:
    s = (spdx or "").lower()
    if not s:
        return "other"
    # Multi-word names that never appear as a single SPDX token.
    if "business source" in s:
        return "busl"
    if "mozilla public" in s:
        return "mpl"
    if "creative commons" in s:
        return "cc"
    if "public domain" in s:
        return "publicdomain"
    for token in _FAMILY_TOKEN_RE.findall(s):
        family = _token_family(token)
        if family:
            return family
    return "other"


def _token_family(token: str):
    """Family for ONE token, or None. Anchored: the prefix must be the whole
    token or be followed by a version separator — so "mit" classifies but
    "mitigation" does not, and "lgpl-2.1" never reads as "gpl"."""
    for prefix, family in _FAMILY_PREFIXES:
        if not token.startswith(prefix):
            continue
        rest = token[len(prefix):]
        if not rest or rest[0] in "-._" or rest[0].isdigit():
            return family
    return None


def _slug(title):
    """Slug a section title into a rail/anchor id (#983 category nav)."""
    return _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _dedupe_section_ids(sections):
    """Make every section id unique in place (HLP-9).

    `_slug()` collapses each non-alphanumeric run to '-', so two stack.yaml
    categories differing only in punctuation ("AI & Agents" vs "AI Agents")
    slugged to the SAME id — and a category literally named "Subscription" or
    "Community" collided with the two hardcoded tier sections. Both then
    emitted `id="sec-<slug>"` twice plus two rail links with the same
    `data-j`, so the jump-to-section always scrolled to the first one and the
    second category was unreachable from the rail. Nothing detected it.
    Collisions now get a `-2`, `-3`, … suffix, first occurrence unsuffixed.
    """
    used = set()
    for s in sections:
        base = s["id"] or "section"
        candidate, n = base, 1
        while candidate in used:
            n += 1
            candidate = f"{base}-{n}"
        used.add(candidate)
        s["id"] = candidate
    return sections


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
    # #983 follow-up (operator): a section is {nav,label,kind,kind_class,items}.
    # `nav` is the SHORT rail label; `kind` is a chip on the card header (the
    # "Bundled upstream" / tier identity), NOT a long title prefix.
    def _section(nav, label, kind, kind_class, items, note=None):
        # key is "rows" not "items" — in Jinja `s.items` resolves to the dict's
        # .items() METHOD, not the value, and iterating it TypeErrors.
        return {"id": _slug(nav), "nav": nav, "label": label, "kind": kind,
                "kind_class": kind_class, "rows": items, "count": len(items),
                "note": note}

    ee_items = [(label, ee_lic, "/static/licenses/busl-1.1.txt", "busl", True)
                for label in sorted(set(ee.values()))]
    if ee_items:
        sections.append(_section(
            "Subscription", "rzfz.ai Subscription", "Source-available · BSL 1.1",
            "busl", ee_items, note=(f"current version {cur_tag}" if cur_tag else None)))

    # 2. Community tier (open source, Apache-2.0)
    comm_items = [
        ("Compose wiring, config templates, base model wiring, build recipes",
         "Apache-2.0 · open source", "/static/licenses/apache-2.0-community.txt",
         "apache", True),
        ("Documentation", "Apache-2.0 · open source",
         "/static/licenses/apache-2.0-community.txt", "apache", True),
    ]
    sections.append(_section(
        "Community", "razzfazz.ai Community", "Open source · Apache-2.0",
        "apache", comm_items))

    # 3..N. Bundled & built upstream, grouped by category. "Bundled upstream"
    # is a chip on each card, and the rail shows just the category name.
    for cat in sorted(upstream):
        items = []
        for disp in sorted(upstream[cat]):
            spdx, bf = upstream[cat][disp]
            label = spdx or "see upstream"
            link = license_link(disp, bf)
            fam = family_of(label)
            # #1056: `link` may be None (no local text, no safe upstream URL).
            # The template renders no anchor at all in that case.
            local = bool(link) and link.startswith("/static/licenses/")
            items.append((disp, label, link, fam, local))
        sections.append(_section(cat, cat, "Bundled upstream", "up", items))

    # HLP-9: ids feed both `id="sec-<id>"` on the card and `data-j`/`href` on
    # the rail link, so they must be unique before either is rendered.
    _dedupe_section_ids(sections)

    nav_categories = [{"id": s["id"], "label": s["nav"], "count": s["count"]}
                      for s in sections]
    tiers = {"subscription_change_date": change_date, "cur_tag": cur_tag}

    return render_template('index.html', sections=sections,
                           change_date=change_date, cur_tag=cur_tag,
                           nav_categories=nav_categories, tiers=tiers)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
