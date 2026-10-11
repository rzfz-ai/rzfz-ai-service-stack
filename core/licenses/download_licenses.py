# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import os
import requests
import json
import sys
import time
import re
import hashlib
import datetime

# #1892: this is now a MAINTAINER --refresh TOOL, not a build step. The
# licence texts are VENDORED in-tree under core/licenses/vendored/ and the
# image COPYs them (see Dockerfile) — the customer upgrade fetches nothing,
# so a GitHub throttle can never roll back an upgrade (that hazard is what
# #1888 exposed and this closes). Run `python download_licenses.py` here to
# refresh the vendored set deliberately (retry/throttle from #1888 applies).
#
#   python download_licenses.py                     # refresh everything (needs GITHUB_TOKEN)
#   python download_licenses.py --only Dify         # one entry; other rows keep their date
#   python download_licenses.py --dry-run [--only X] # report licence-BODY changes, write nothing
#   python download_licenses.py --raw ...           # anonymous raw.githubusercontent.com (opt-in)
# Pinned entries are fetched at the tag the stack ships (`{ref}` below,
# resolved through scripts/component_pins.py).

# Map Component Name -> License URL
LICENSE_URLS = {
    # razzfazz Proprietary (local file, not downloaded - handled separately below)
    # "razzfazz Service Stack": handled in main block via local LICENSE file
    
    # OS & Runtime
    "Ubuntu Desktop 24.04 LTS": "https://ubuntu.com/licensing", 
    "Docker Runtime": "https://raw.githubusercontent.com/moby/moby/master/LICENSE",
    "Docker Compose V2": "https://raw.githubusercontent.com/docker/compose/main/LICENSE",
    "Caddy": "https://raw.githubusercontent.com/caddyserver/caddy/{ref}/LICENSE",
    "Vulkan Driver (AMD)": "https://raw.githubusercontent.com/GPUOpen-Drivers/AMDVLK/master/LICENSE.txt",
    "Python": "https://raw.githubusercontent.com/python/cpython/main/LICENSE",
    
    # DB
    "PostgreSQL": "https://www.postgresql.org/about/licence/", 
    "pgvector": "https://raw.githubusercontent.com/pgvector/pgvector/{ref}/LICENSE",
    "Valkey": "https://raw.githubusercontent.com/valkey-io/valkey/{ref}/COPYING",
    "FerretDB": "https://raw.githubusercontent.com/FerretDB/FerretDB/{ref}/LICENSE",
    
    # Management
    "Komodo": "https://raw.githubusercontent.com/moghtech/komodo/{ref}/LICENSE",
    "GPUStack": "https://raw.githubusercontent.com/gpustack/gpustack/{ref}/LICENSE",
    "Authentik": "https://raw.githubusercontent.com/goauthentik/authentik/{ref}/LICENSE",
    "Gitea": "https://raw.githubusercontent.com/go-gitea/gitea/{ref}/LICENSE", 
    
    # AI
    "Open WebUI": "https://raw.githubusercontent.com/open-webui/open-webui/{ref}/LICENSE",
    "Dify": "https://raw.githubusercontent.com/langgenius/dify/{ref}/LICENSE",
    "Pipelines": "https://raw.githubusercontent.com/open-webui/pipelines/{ref}/LICENSE",
    
    # Components
    "Gotenberg": "https://raw.githubusercontent.com/gotenberg/gotenberg/{ref}/LICENSE",
    "OpenLIT": "https://raw.githubusercontent.com/openlit/openlit/{ref}/LICENSE",
    "ClickHouse": "https://raw.githubusercontent.com/ClickHouse/ClickHouse/{ref}/LICENSE",
    "SearXNG": "https://raw.githubusercontent.com/searxng/searxng/{ref}/LICENSE",
    "Crawl4AI": "https://raw.githubusercontent.com/unclecode/crawl4ai/{ref}/LICENSE",
    # #68: edge-tts removed from the stack (speaches covers STT+TTS).
    "Speaches": "https://raw.githubusercontent.com/speaches-ai/speaches/{ref}/LICENSE",
    "LightRAG": "https://raw.githubusercontent.com/HKUDS/LightRAG/{ref}/LICENSE",
    "Cognee": "https://raw.githubusercontent.com/topoteretes/cognee/{ref}/LICENSE",
    "FalkorDB": "https://raw.githubusercontent.com/FalkorDB/FalkorDB/master/LICENSE.txt",
    "Apache Tika": "https://raw.githubusercontent.com/apache/tika/{ref}/LICENSE.txt",
    "Docling": "https://raw.githubusercontent.com/docling-project/docling-serve/{ref}/LICENSE",
    "Presidio": "https://raw.githubusercontent.com/microsoft/presidio/{ref}/LICENSE",
    "Stirling-PDF": "https://raw.githubusercontent.com/Stirling-Tools/Stirling-PDF/{ref}/LICENSE",
    "Paperclip": "https://raw.githubusercontent.com/paperclipai/paperclip/{ref}/LICENSE",
    "Moltis": "https://raw.githubusercontent.com/moltis-org/moltis/{ref}/LICENSE.md",
    "Hermes Agent": "https://raw.githubusercontent.com/NousResearch/hermes-agent/{ref}/LICENSE",
    "Synapse": "https://raw.githubusercontent.com/element-hq/synapse/{ref}/LICENSE-AGPL-3.0",
    "Element Web": "https://raw.githubusercontent.com/element-hq/element-web/{ref}/LICENSE-AGPL-3.0",
    "paperless-ngx": "https://raw.githubusercontent.com/paperless-ngx/paperless-ngx/{ref}/LICENSE",
    "Vaultwarden": "https://raw.githubusercontent.com/dani-garcia/vaultwarden/{ref}/LICENSE.txt",
    "Infisical": "https://raw.githubusercontent.com/Infisical/infisical/{ref}/LICENSE",
    "Onyx": "https://raw.githubusercontent.com/onyx-dot-app/onyx/{ref}/LICENSE",
    "OpenHands": "https://raw.githubusercontent.com/All-Hands-AI/OpenHands/{ref}/LICENSE",
    # #1075 — one entry for the OpenUEM distribution. Every open-uem repo the
    # module ships (console, worker, cert-manager, ocsp-responder) is Apache-2.0
    # under the same LICENSE text, verified via the GitHub licence API.
    "OpenUEM": "https://raw.githubusercontent.com/open-uem/openuem-console/{ref}/LICENSE",
    # #855 / DECISION-6: the Wazuh distribution is not one licence. The server
    # is GPL-2.0; the indexer and dashboard are OpenSearch / OpenSearch-
    # Dashboards forks under Apache-2.0. Three rows, because one row would
    # under-describe what actually ships.
    "Wazuh": "https://raw.githubusercontent.com/wazuh/wazuh/{ref}/LICENSE",
    "Wazuh Indexer": "https://raw.githubusercontent.com/wazuh/wazuh-indexer/{ref}/LICENSE.txt",
    "Wazuh Dashboard": "https://raw.githubusercontent.com/wazuh/wazuh-dashboard/{ref}/LICENSE.txt",
    # gsd-pi: gsd-build/gsd-pi is private (no public raw LICENSE) — M034 S04.
    # Attribution handled manually; omit from auto-download to avoid a spurious
    # "failed to download" warning on every install.
    "opencode": "https://raw.githubusercontent.com/sst/opencode/{ref}/LICENSE",
    "llama.cpp": "https://raw.githubusercontent.com/ggml-org/llama.cpp/{ref}/LICENSE",
    
    # Models
    "Gemma 4": "https://raw.githubusercontent.com/google-deepmind/gemma/main/LICENSE",
    "Mistral / Mixtral": "https://raw.githubusercontent.com/mistralai/mistral-inference/main/LICENSE",
    "Qwen": "https://raw.githubusercontent.com/QwenLM/Qwen/main/LICENSE",
    "Nomic Embed": "https://raw.githubusercontent.com/nomic-ai/contrastors/main/LICENSE",
    
    # Python Libraries (used by razzfazz containers)
    "Flask": "https://raw.githubusercontent.com/pallets/flask/main/LICENSE.txt",
    "Gunicorn": "https://raw.githubusercontent.com/benoitc/gunicorn/master/LICENSE",
    "Requests": "https://raw.githubusercontent.com/psf/requests/main/LICENSE",
    "psycopg2": "https://raw.githubusercontent.com/psycopg/psycopg2/master/LICENSE",
    "docker-py": "https://raw.githubusercontent.com/docker/docker-py/main/LICENSE",
    "APScheduler": "https://raw.githubusercontent.com/agronholm/apscheduler/master/LICENSE.txt",
    "Jinja2": "https://raw.githubusercontent.com/pallets/jinja/main/LICENSE.txt",
    "Werkzeug": "https://raw.githubusercontent.com/pallets/werkzeug/main/LICENSE.txt",

    # Fonts
    # #2488: the Geist + Geist Mono variable fonts are vendored into the
    # cognee-frontend image (modules/knowledge/cognee/frontend/fonts/, from
    # npm geist@1.7.2) so its build fetches nothing from Google Fonts. SIL
    # OFL-1.1; the package's LICENSE.txt is byte-identical to this file.
    "Geist": "https://raw.githubusercontent.com/vercel/geist-font/main/LICENSE.txt",
    "Geist Mono": "https://raw.githubusercontent.com/vercel/geist-font/main/LICENSE.txt"
}

# Version-pinned licence texts: a `{ref}` in a catalogue URL is the upstream
# TAG of the version the stack ships, resolved through the shared pin table
# scripts/component_pins.py (config/.env.example first; compose image or
# Dockerfile where a component has no .env key). The text is taken at the tag
# that ships, not from a moving branch — and provenance.json records that tag
# as the text's `ref`. Entries without a `{ref}` have no pin in the tree
# (component_pins.UNPINNED_CATALOGUE says why) and stay on their branch.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_component_pins():
    import importlib.util
    path = os.path.join(_REPO_ROOT, "scripts", "component_pins.py")
    spec = importlib.util.spec_from_file_location("_rzfz_component_pins", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


component_pins = _load_component_pins()
LICENSE_URL_TEMPLATES = dict(LICENSE_URLS)
LICENSE_REFS = {}     # catalogue key -> pinned upstream tag


def _pin_catalogue(templates, refs):
    pin_of = component_pins.by_field("catalogue")
    out = {}
    for key, url in templates.items():
        if "{ref}" in url:
            if key not in pin_of:
                raise RuntimeError(f"{key}: catalogue URL has {{ref}} but component_pins has no "
                                   f"row with catalogue={key!r}")
            refs[key] = component_pins.resolve(pin_of[key])["tag"]
            url = url.replace("{ref}", refs[key])
        elif key in pin_of:
            raise RuntimeError(f"{key}: pinned in component_pins but its catalogue URL has no {{ref}}")
        out[key] = url
    return out


LICENSE_URLS = _pin_catalogue(LICENSE_URL_TEMPLATES, LICENSE_REFS)

OUTPUT_DIR = os.environ.get("RAZZFAZZ_LICENSES_OUTPUT_DIR", "vendored")
MANIFEST_FILE = os.environ.get("RAZZFAZZ_LICENSES_MANIFEST", "vendored/licenses.json")
PROVENANCE_FILE = os.environ.get("RAZZFAZZ_LICENSES_PROVENANCE", "vendored/provenance.json")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""

# #1056: written next to the licence texts whenever at least one catalogued
# licence could not be fetched. The build already exits non-zero in that case;
# the marker is what makes a failure that was force-ignored still visible on
# the running box (and greppable from `rzfz verify-*`) instead of invisible.
FAILURE_MARKER = "DOWNLOAD-FAILURES.txt"
THROTTLE_SECONDS = 0.25   # #1888: spacing between raw.githubusercontent.com requests

# #1056 escape hatch for a deliberately offline/air-gapped build: downloads may
# fail, the marker is still written, the manifest still records only what was
# really fetched, and the app degrades to upstream project links. NOT a default.
ALLOW_FAILURES_ENV = "RAZZFAZZ_LICENSES_ALLOW_DOWNLOAD_FAILURES"



# #1892 follow-up: entries whose upstream source is a WEB PAGE, not a licence
# text. Their vendored file is curated by hand once and NOT re-fetched.
#
# Why they cannot be ordinary rows: `--refresh` would overwrite the curated text
# with the rendered page again, and a marketing page's checksum churns on every
# banner and build id. The guard's promise — "a real upstream change shows up as
# a reviewable diff" — dies exactly there: a reviewer who sees that diff every
# week stops reading it.
#
# Adding an entry here is a DECISION, not a convenience. The guard pins this set
# by name for that reason.
CURATED = {
    "PostgreSQL": "postgresql.txt",
    "Ubuntu Desktop 24.04 LTS": "ubuntu_desktop_24.04_lts.txt",
}

def sanitize_filename(key):
    return key.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")

def _github_raw_to_api(url, ref=None):
    """raw.githubusercontent.com/{o}/{r}/{ref}/{path} ->
    api.github.com/repos/{o}/{r}/contents/{path}?ref={ref}. Returns None for a
    URL that is not a github-raw URL (nothing to translate). #1892: the
    maintainer --refresh reads file contents through the authenticated REST API,
    never the anonymous raw-scrape path that GitHub throttles.

    `ref` names the ref when it contains a slash (Authentik's `version/2026.8.3`),
    which a single path segment cannot express."""
    if ref and "/" in ref:
        m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/" + re.escape(ref) + r"/(.+)$", url)
        if m:
            owner, repo, path = m.groups()
            return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", url)
    if not m:
        return None
    owner, repo, ref, path = m.groups()
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"


def _ref_from_url(url, key=None):
    """The ref/version coordinate a text was taken from: the pinned tag when the
    URL was built from one (LICENSE_REFS), else the branch/tag segment of a
    github-raw URL, else the source host."""
    if not url:
        return "n/a"
    pinned = LICENSE_REFS.get(key) if key else None
    if pinned and f"/{pinned}/" in url:
        return pinned
    m = re.match(r"https?://raw\.githubusercontent\.com/[^/]+/[^/]+/([^/]+)/", url)
    if m:
        return m.group(1)
    m = re.match(r"https?://([^/]+)/", url)
    return m.group(1) if m else "n/a"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_with_retry(url, *, attempts=4, headers=None, _get=None, _sleep=None):
    """GET with exponential backoff for TRANSIENT failures — 429 (raw.github
    rate-limits a burst of requests) and 5xx — honoring Retry-After. A genuine
    4xx (a wrong/removed URL) returns immediately so the caller still hard-fails
    on it (#1056 completeness is preserved). Returns the final Response, or None
    on a network error that never recovered.

    #1888: one transient 429 used to fail the whole razzfazz-licenses build and
    roll back an entire upgrade. Absorbing transient rate-limits here is what
    makes an upgrade survive GitHub throttling.
    """
    _get = _get or requests.get
    _sleep = _sleep or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            resp = _get(url, timeout=20, **({"headers": headers} if headers else {}))
        except requests.RequestException as e:
            if attempt == attempts:
                print(f"Error: {e} for {url}")
                return None
            _sleep(min(2 ** attempt, 30))
            continue
        # 200, or a genuine 4xx (not 429) → done; the caller decides.
        if resp.status_code == 200 or (400 <= resp.status_code < 500 and resp.status_code != 429):
            return resp
        # transient: 429 / 5xx
        if attempt == attempts:
            return resp
        after = resp.headers.get("Retry-After")
        wait = int(after) if (after and str(after).isdigit()) else min(2 ** attempt, 30)
        print(f"  transient {resp.status_code} for {url} — retry {attempt}/{attempts - 1} in {wait}s")
        _sleep(wait)
    return None


def download_license(key, url, *, _get=None, _sleep=None, use_api=True, token=None):
    # Sanitize key for filename
    base_name = sanitize_filename(key)
    
    _get = _get or requests.get
    _sleep = _sleep or time.sleep
    if token is None:
        token = GITHUB_TOKEN
    fetch_url, headers = url, None
    if use_api:
        api = _github_raw_to_api(url, LICENSE_REFS.get(key))
        if api:
            if not token:
                print(f"Error: {key}: --refresh needs a GitHub token (set GITHUB_TOKEN) "
                      f"— it must not scrape raw.githubusercontent.com (#1892)", file=sys.stderr)
                return None
            fetch_url = api
            headers = {"Authorization": f"Bearer {token}",
                       "Accept": "application/vnd.github.raw",
                       "X-GitHub-Api-Version": "2022-11-28"}
    print(f"Downloading license for {key} from {fetch_url}...")
    try:
        resp = _fetch_with_retry(fetch_url, headers=headers, _get=_get, _sleep=_sleep)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                print(f"Error: Status {resp.status_code} for {url}")
            return None

        content_type = resp.headers.get('Content-Type', '').lower()
        
        # Determine extension
        if 'html' in content_type:
            ext = ".html"
            mode = "w"
            content = resp.text
            # Add simple style for readability if it's a raw fragment? 
            # But usually it's a full page.
            # If it's a "terms" page, we might want to strip navs? Too complex.
            # Just saving the page is safer for compliance.
        else:
            ext = ".txt"
            mode = "w"
            content = resp.text
        
        filename = base_name + ext
        filepath = os.path.join(OUTPUT_DIR, filename)
        
        with open(filepath, mode, encoding="utf-8") as f:
            # Add header for text files so generic licenses (Apache 2.0) have context
            if ext == ".txt":
                f.write(f"Product: {key}\nSource: {url}\n{'='*50}\n\n")
            
            f.write(content)
            
        print(f"Saved to {filename} ({content_type})")
        return filename

    except Exception as e:
        print(f"Failed to download {key}: {e}")
        return None

def _upstream_display_names(stack, display_map):
    """Every display name the licences page renders an upstream row for.

    Mirrors the filter in `core/licenses/app.py::index()` — first-party BSL /
    Apache tiers get their own hardcoded sections and never reach
    `license_link()`. Returns {display: built_from-or-None}.
    """
    modules = list((stack.get("modules") or {}).values()) + \
        list((stack.get("per_user_agents") or {}).values())
    seen = {}
    for mod in modules:
        mlic = mod.get("license", "")
        med = mod.get("edition", "")
        mbf = mod.get("built_from")
        for c in mod.get("containers", []):
            if not isinstance(c, dict):
                continue
            lic = c.get("license", mlic)
            ed = c.get("edition", med)
            bf = c.get("built_from", mbf)
            if ed == "enterprise" or lic == "busl-1.1":
                continue
            if ed == "community" or lic == "apache-2.0":
                continue
            disp = display_map.get(c.get("name", ""), c.get("name", ""))
            if disp not in seen or not seen[disp]:
                seen[disp] = bf
    return seen


def _has_upstream_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def stack_consistency(stack, display_map, catalogue=None):
    """Reconcile stack.json (the component SSOT) with the download catalogue.

    #1056 class 3 — drift. The catalogue below is keyed by DISPLAY NAME, and
    nothing ever checked those keys against the components the page actually
    renders. So the two sets silently diverged in both directions:

      * uncatalogued — a stack.json component with no catalogue entry AND no
        `built_from` URL. It has no licence link of any kind. Before the
        `license_link()` fix it got a fabricated `/static/licenses/*.txt` href
        that 404'd; now it gets no link, which is honest but still a gap.
      * unreachable — a catalogue entry whose key matches no rendered display
        name ("PostgreSQL" vs the page's "PostgreSQL / pgvector"). Downloaded
        on every build, shipped in the image, and never linked to.

    Returns {"uncatalogued": [...], "unreachable": [...]}, both sorted.

    NOTE it is not called from the build: the licences image builds with
    `./core` as its context (see core/licenses/Dockerfile), and stack.json
    lives at the repo root — it is simply not present at `RUN` time. The
    enforcement point is the unit suite, which has the whole tree.
    """
    catalogue = LICENSE_URLS if catalogue is None else catalogue
    rendered = _upstream_display_names(stack, display_map)
    uncatalogued = sorted(
        d for d, bf in rendered.items()
        if d not in catalogue and not _has_upstream_url(bf)
    )
    unreachable = sorted(k for k in catalogue if k not in rendered)
    return {"uncatalogued": uncatalogued, "unreachable": unreachable}


def write_first_party_licenses(output_dir=None):
    """The two razzfazz.ai first-party summary texts. Returns manifest rows."""
    output_dir = OUTPUT_DIR if output_dir is None else output_dir
    manifest = {}

    # #68 license reorg: generate the two razzfazz.ai first-party licence summary
    # texts locally (the full LICENSE-BSL / LICENSE-APACHE live in the repo root,
    # which is not in this Docker build context). rzfz.ai Subscription
    # tier = source-available (BSL 1.1); Community = Open Source (Apache-2.0).
    with open(os.path.join(output_dir, "busl-1.1.txt"), "w") as f:
        f.write("razzfazz.ai Service Stack — rzfz.ai Subscription tier\n")
        f.write("Business Source License 1.1 (BUSL-1.1) — SOURCE-AVAILABLE\n")
        f.write("Copyright (c) 2024-2026 razzfazz.ai GmbH - Member of SEQIS Group\n")
        f.write("=" * 60 + "\n\n")
        f.write("The source-available components (Config/Help/Licenses/Start\n")
        f.write("portals, the shared runtime library, Model Sync, Backup\n")
        f.write("Management, the Agent Manager and MCP Manager, and the\n")
        f.write("razzfazz.ai orchestration surface) are licensed under the\n")
        f.write("Business Source License 1.1.\n\n")
        f.write("BSL 1.1 is SOURCE-AVAILABLE, NOT an OSI-approved open source\n")
        f.write("licence. You may copy, modify and make NON-PRODUCTION use of it\n")
        f.write("freely. Private, personal and evaluation use is free — no\n")
        f.write("subscription and no registration. COMMERCIAL PRODUCTION use\n")
        f.write("requires a valid rzfz.ai Subscription: a licence (not a\n")
        f.write("service) to run the current, patched Stack commercially —\n")
        f.write("installation, updates and support are separate offerings. A\n")
        f.write("subscription valid when a version was released permanently\n")
        f.write("authorizes commercial production use of that version. No license\n")
        f.write("key is required — the containers run freely; the subscription\n")
        f.write("governs permitted use, not technical function. On the version's\n")
        f.write("Change Date it converts to the Apache License 2.0 regardless.\n\n")
        f.write("Full terms + the rolling Change-Date schedule: LICENSE-BSL and\n")
        f.write("config/manifests/license-dates.json in the repository root.\n\n")
        f.write("Licensing inquiries: licensing@razzfazz.ai\n")
    manifest["razzfazz Subscription (BSL 1.1)"] = "busl-1.1.txt"

    with open(os.path.join(output_dir, "apache-2.0-community.txt"), "w") as f:
        f.write("razzfazz.ai Service Stack — COMMUNITY\n")
        f.write("Apache License 2.0 — OPEN SOURCE\n")
        f.write("Copyright (c) 2024-2026 razzfazz.ai GmbH - Member of SEQIS Group\n")
        f.write("=" * 60 + "\n\n")
        f.write("Our first-party compose wiring, configuration templates,\n")
        f.write("documentation, base model wiring and the build recipes of the\n")
        f.write("build-only containers are Open Source under the Apache License\n")
        f.write("2.0 — free for any use, including production.\n\n")
        f.write("Full text: LICENSE-APACHE in the repository root.\n\n")
        f.write("Licensing inquiries: licensing@razzfazz.ai\n")
    manifest["razzfazz Community (Apache-2.0)"] = "apache-2.0-community.txt"
    print("Created razzfazz.ai BSL (Enterprise) + Apache-2.0 (Community) licence files")
    return manifest


def build_licenses(catalogue=None, downloader=None, output_dir=None, _sleep=None):
    """Fetch every catalogued licence. Returns (manifest, failures).

    `failures` is [(key, url), …] for the ones that did not arrive. A failure
    deliberately produces NO manifest row: the manifest is the record of files
    that really exist, and `app.py::license_link()` reads it as exactly that.
    A missing row makes the page fall back to the upstream project URL, which
    beats both a 404 and the old "LICENSE DOWNLOAD FAILED" placeholder that
    was written into the image and linked to as if it were a licence.
    """
    catalogue = LICENSE_URLS if catalogue is None else catalogue
    downloader = download_license if downloader is None else downloader
    output_dir = OUTPUT_DIR if output_dir is None else output_dir

    manifest = write_first_party_licenses(output_dir)
    failures = []
    _sleep = time.sleep if _sleep is None else _sleep
    # #1888: space the burst so GitHub-raw does not rate-limit us. Counted in
    # requests ACTUALLY ISSUED, not in catalogue position — a curated entry
    # fetches nothing, so spacing after one buys no spacing and only makes the
    # build slower. (The old `if i:` read the enumerate index and paid for the
    # gap after every skipped entry.) The first request is never delayed
    # either: a pause before it costs time and spaces nothing.
    fetched = 0
    for key, url in catalogue.items():
        if key in CURATED:
            # Curated by hand — keep the vendored file, fetch nothing. Printed
            # rather than silent: a refresh that quietly skips entries is how a
            # stale text survives three cycles.
            manifest[key] = CURATED[key]
            print(f"{key}: curated in-tree ({CURATED[key]}) — not fetched")
            continue
        if fetched:
            _sleep(THROTTLE_SECONDS)
        fetched += 1
        fname = downloader(key, url)
        if fname:
            manifest[key] = fname
        else:
            failures.append((key, url))
    return manifest, failures


def build_provenance(manifest, output_dir=None, catalogue=None, previous=None):
    """Provenance sidecar (#1892). For each manifest row: the source it came
    from, the ref/version coordinate, a sha256 of the exact vendored bytes, and
    the refresh date. The guard binds each vendored file to its recorded sha256,
    so a text cannot drift from what was recorded without a visible, intentional
    change — and a real upstream change surfaces as a reviewable diff on the
    next --refresh. This is the "die Version steht daneben" binding for a stack
    whose pins are image tags / moving upstream branches, not a per-component
    version SSOT."""
    output_dir = OUTPUT_DIR if output_dir is None else output_dir
    catalogue = LICENSE_URLS if catalogue is None else catalogue
    today = datetime.date.today().isoformat()
    if previous is None:
        try:
            with open(os.path.join(output_dir, "provenance.json"), encoding="utf-8") as fh:
                previous = json.load(fh)
        except (OSError, ValueError):
            previous = {}
    prov = {}
    for key, fname in manifest.items():
        if not os.path.exists(os.path.join(output_dir, fname)):
            continue
        src = catalogue.get(key, "first-party (repo root LICENSE-BSL / LICENSE-APACHE)")
        row = {
            "file": fname,
            "source": src,
            "ref": _ref_from_url(src, key) if str(src).startswith("http") else "first-party",
            "sha256": _sha256_file(os.path.join(output_dir, fname)),
            "fetched_at": today,
        }
        if key in CURATED:
            # The sha256 is still recomputed from the bytes on disk — the
            # binding holds for a curated file exactly as for a fetched one.
            # What is preserved is the human part: why it is curated, and the
            # date the text was taken, which is NOT today.
            prev = previous.get(key, {})
            row["curated"] = True
            row["fetched_at"] = prev.get("fetched_at", today)
            if prev.get("note"):
                row["note"] = prev["note"]
        prov[key] = row
    return prov


_HEADER_SEP = "=" * 50 + "\n\n"


def licence_body(text):
    """A vendored text without the `Product:/Source:` header download_license()
    writes — the part that is the upstream licence. The header changes with the
    URL (and so the sha256 does); the body is what a refresh must compare."""
    if text.startswith("Product: ") and _HEADER_SEP in text:
        return text.split(_HEADER_SEP, 1)[1]
    return text


def body_changes(before, output_dir):
    """[(key, summary)] for every key whose licence BODY differs from `before`
    ({key: old full text}); summary is a short unified-diff excerpt."""
    import difflib
    out = []
    for key, old in sorted(before.items()):
        fname = sanitize_filename(key) + ".txt"
        path = os.path.join(output_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            new = fh.read()
        a, b = licence_body(old), licence_body(new)
        if a == b:
            continue
        diff = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "before", "at-tag",
                                         n=0, lineterm=""))
        changed = [d for d in diff[2:] if d[:1] in "+-" and d[:3] not in ("+++", "---")]
        out.append((key, f"{len(changed)} changed lines\n" + "\n".join(diff[2:40])))
    return out


def _parse_args(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Refresh the vendored licence texts (maintainer tool).")
    ap.add_argument("--refresh", action="store_true",
                    help="accepted for the documented invocation; a refresh is the default")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="re-fetch only this catalogue entry (repeatable); every other "
                         "manifest/provenance row, and the first-party texts, stay untouched")
    ap.add_argument("--raw", action="store_true",
                    help="fetch github entries anonymously from raw.githubusercontent.com "
                         "instead of the authenticated API (explicit opt-in for a maintainer "
                         "without a GITHUB_TOKEN; never a fallback)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch into a temporary directory and report licence-body changes "
                         "against the vendored texts; write nothing")
    return ap.parse_args(argv)


def refresh_only(names, *, output_dir, manifest_file, prov_path, downloader, dry_run=False,
                 _sleep=None):
    """Re-fetch exactly `names`. Returns (rc, body_changes).

    Only those rows of licenses.json / provenance.json are rewritten, so one
    entry can be refreshed without re-dating the other 60 (`fetched_at`), and
    the first-party texts are not regenerated."""
    import shutil
    import tempfile
    _sleep = time.sleep if _sleep is None else _sleep
    unknown = [n for n in names if n not in LICENSE_URLS]
    curated = [n for n in names if n in CURATED]
    if unknown or curated:
        print(f"ERROR: not refreshable: unknown={unknown} curated={curated}", file=sys.stderr)
        return 2, []
    before = {}
    for n in names:
        p = os.path.join(output_dir, sanitize_filename(n) + ".txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                before[n] = fh.read()
    target = tempfile.mkdtemp(prefix="licences-") if dry_run else output_dir
    global OUTPUT_DIR
    saved, OUTPUT_DIR = OUTPUT_DIR, target
    try:
        fetched, failures = {}, []
        for i, n in enumerate(names):
            if i:
                _sleep(THROTTLE_SECONDS)
            fname = downloader(n, LICENSE_URLS[n])
            if fname:
                fetched[n] = fname
            else:
                failures.append((n, LICENSE_URLS[n]))
        changes = body_changes({k: v for k, v in before.items() if k in fetched}, target)
    finally:
        OUTPUT_DIR = saved
        if dry_run:
            shutil.rmtree(target, ignore_errors=True)
    for key, summary in changes:
        print(f"BODY CHANGED: {key}: {summary}")
    print(f"licence bodies: {len(changes)} changed, "
          f"{len(fetched) - len(changes)} unchanged of {len(fetched)} fetched")
    if failures:
        for key, url in failures:
            print(f"  - FAILED {key}: {url}", file=sys.stderr)
    if dry_run or not fetched:
        return (1 if failures else 0), changes

    with open(manifest_file, encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(prov_path, encoding="utf-8") as fh:
        provenance = json.load(fh)
    manifest.update(fetched)
    provenance.update(build_provenance(fetched, output_dir=output_dir, previous=provenance))
    for path, data in ((manifest_file, manifest), (prov_path, provenance)):
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return (1 if failures else 0), changes


def main(argv=None):
    """Build the offline licence set. Returns a process exit code.

    #1056 class 1 — this used to print "WARNING: Some licenses failed to
    download." and exit 0, so `RUN python download_licenses.py` succeeded and
    the image shipped with licence texts missing. Nothing downstream noticed:
    not the build, not CI, not the page (which fabricated a local href for the
    missing entry). A catalogued licence that did not download is now a hard
    build failure.
    """
    args = _parse_args(list(argv or []))
    output_dir = OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    prov_path = os.environ.get("RAZZFAZZ_LICENSES_PROVENANCE") or \
        os.path.join(os.path.dirname(MANIFEST_FILE) or ".", "provenance.json")
    downloader = None
    if args.raw:
        def downloader(key, url):
            return download_license(key, url, use_api=False)

    if args.only or args.dry_run:
        names = args.only or [k for k in LICENSE_URLS if k not in CURATED]
        rc, _ = refresh_only(names, output_dir=output_dir, manifest_file=MANIFEST_FILE,
                             prov_path=prov_path, downloader=downloader or download_license,
                             dry_run=args.dry_run)
        return rc

    # #1892: the default downloader (download_license) now reads GitHub through
    # the authenticated API by default — never raw. build_licenses keeps calling
    # it as downloader(key, url); the API/token behaviour lives inside it.
    manifest, failures = build_licenses(output_dir=output_dir, downloader=downloader)

    # Save manifest mapping (Key -> Filename)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")

    # #1892: provenance sidecar binds each vendored text to source+ref+sha+date.
    # Written next to the manifest (tracks a monkeypatched MANIFEST_FILE too).
    provenance = build_provenance(manifest, output_dir=output_dir)
    with open(prov_path, "w") as f:
        json.dump(provenance, f, indent=2, sort_keys=True)
        f.write("\n")

    marker = os.path.join(output_dir, FAILURE_MARKER)
    if not failures:
        if os.path.exists(marker):
            os.remove(marker)
        print(f"All {len(LICENSE_URLS)} catalogued licences downloaded.")
        return 0

    with open(marker, "w", encoding="utf-8") as f:
        f.write("These catalogued licences could NOT be downloaded at build time.\n")
        f.write("The licences page falls back to the upstream project URL for them.\n")
        f.write("=" * 60 + "\n\n")
        for key, url in failures:
            f.write(f"{key}\t{url}\n")

    print(f"ERROR: {len(failures)} of {len(LICENSE_URLS)} catalogued licences "
          f"failed to download:", file=sys.stderr)
    for key, url in failures:
        print(f"  - {key}: {url}", file=sys.stderr)
    print(f"Wrote {marker}", file=sys.stderr)

    if os.environ.get(ALLOW_FAILURES_ENV) == "1":
        print(f"{ALLOW_FAILURES_ENV}=1 — continuing with an INCOMPLETE licence "
              f"set.", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

