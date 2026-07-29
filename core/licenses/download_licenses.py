# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import os
import requests
import json
import sys

# Map Component Name -> License URL
LICENSE_URLS = {
    # razzfazz Proprietary (local file, not downloaded - handled separately below)
    # "razzfazz Service Stack": handled in main block via local LICENSE file
    
    # OS & Runtime
    "Ubuntu Desktop 24.04 LTS": "https://ubuntu.com/licensing", 
    "Docker Runtime": "https://raw.githubusercontent.com/moby/moby/master/LICENSE",
    "Docker Compose V2": "https://raw.githubusercontent.com/docker/compose/main/LICENSE",
    "Caddy": "https://raw.githubusercontent.com/caddyserver/caddy/master/LICENSE",
    "Vulkan Driver (AMD)": "https://raw.githubusercontent.com/GPUOpen-Drivers/AMDVLK/master/LICENSE.txt",
    "Python": "https://raw.githubusercontent.com/python/cpython/main/LICENSE",
    
    # DB
    "PostgreSQL": "https://www.postgresql.org/about/licence/", 
    "pgvector": "https://raw.githubusercontent.com/pgvector/pgvector/master/LICENSE",
    "Valkey": "https://raw.githubusercontent.com/valkey-io/valkey/unstable/COPYING",
    "FerretDB": "https://raw.githubusercontent.com/FerretDB/FerretDB/main/LICENSE",
    
    # Management
    "Komodo": "https://raw.githubusercontent.com/mbecker20/komodo/main/LICENSE",
    "GPUStack": "https://raw.githubusercontent.com/gpustack/gpustack/main/LICENSE",
    "Authentik": "https://raw.githubusercontent.com/goauthentik/authentik/main/LICENSE",
    "Gitea": "https://raw.githubusercontent.com/go-gitea/gitea/main/LICENSE", 
    
    # AI
    "Open WebUI": "https://raw.githubusercontent.com/open-webui/open-webui/main/LICENSE",
    "Dify": "https://raw.githubusercontent.com/langgenius/dify/main/LICENSE",
    "Pipelines": "https://raw.githubusercontent.com/open-webui/pipelines/main/LICENSE",
    
    # Components
    "Gotenberg": "https://raw.githubusercontent.com/gotenberg/gotenberg/main/LICENSE",
    "OpenLIT": "https://raw.githubusercontent.com/openlit/openlit/main/LICENSE",
    "ClickHouse": "https://raw.githubusercontent.com/ClickHouse/ClickHouse/master/LICENSE",
    "SearXNG": "https://raw.githubusercontent.com/searxng/searxng/master/LICENSE",
    "Crawl4AI": "https://raw.githubusercontent.com/unclecode/crawl4ai/main/LICENSE",
    # #68: edge-tts removed from the stack (speaches covers STT+TTS).
    "Speaches": "https://raw.githubusercontent.com/speaches-ai/speaches/master/LICENSE",
    "LightRAG": "https://raw.githubusercontent.com/HKUDS/LightRAG/main/LICENSE",
    "Cognee": "https://raw.githubusercontent.com/topoteretes/cognee/main/LICENSE",
    "FalkorDB": "https://raw.githubusercontent.com/FalkorDB/FalkorDB/master/LICENSE.txt",
    "Apache Tika": "https://raw.githubusercontent.com/apache/tika/main/LICENSE.txt",
    "Docling": "https://raw.githubusercontent.com/docling-project/docling-serve/main/LICENSE",
    "Presidio": "https://raw.githubusercontent.com/microsoft/presidio/main/LICENSE",
    "Stirling-PDF": "https://raw.githubusercontent.com/Stirling-Tools/Stirling-PDF/main/LICENSE",
    "Paperclip": "https://raw.githubusercontent.com/paperclipai/paperclip/master/LICENSE",
    "Moltis": "https://raw.githubusercontent.com/moltis-org/moltis/main/LICENSE.md",
    "Hermes Agent": "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/LICENSE",
    "Synapse": "https://raw.githubusercontent.com/element-hq/synapse/master/LICENSE-AGPL-3.0",
    "Element Web": "https://raw.githubusercontent.com/element-hq/element-web/develop/LICENSE-AGPL-3.0",
    "paperless-ngx": "https://raw.githubusercontent.com/paperless-ngx/paperless-ngx/main/LICENSE",
    "Vaultwarden": "https://raw.githubusercontent.com/dani-garcia/vaultwarden/main/LICENSE.txt",
    "Infisical": "https://raw.githubusercontent.com/Infisical/infisical/main/LICENSE",
    "Onyx": "https://raw.githubusercontent.com/onyx-dot-app/onyx/main/LICENSE",
    "OpenHands": "https://raw.githubusercontent.com/All-Hands-AI/OpenHands/main/LICENSE",
    # gsd-pi: gsd-build/gsd-pi is private (no public raw LICENSE) — M034 S04.
    # Attribution handled manually; omit from auto-download to avoid a spurious
    # "failed to download" warning on every install.
    "opencode": "https://raw.githubusercontent.com/sst/opencode/dev/LICENSE",
    "llama.cpp": "https://raw.githubusercontent.com/ggml-org/llama.cpp/master/LICENSE",
    
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
    "Werkzeug": "https://raw.githubusercontent.com/pallets/werkzeug/main/LICENSE.txt"
}

OUTPUT_DIR = "static/licenses"
MANIFEST_FILE = "licenses.json"
os.makedirs(OUTPUT_DIR, exist_ok=True)

manifest = {}

def sanitize_filename(key):
    return key.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")

def download_license(key, url):
    # Sanitize key for filename
    base_name = sanitize_filename(key)
    
    print(f"Downloading license for {key} from {url}...")
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
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

if __name__ == "__main__":
    success = True
    
    # #68 license reorg: generate the two razzfazz.ai first-party licence summary
    # texts locally (the full LICENSE-BSL / LICENSE-APACHE live in the repo root,
    # which is not in this Docker build context). rzfz.ai Subscription
    # tier = source-available (BSL 1.1); Community = Open Source (Apache-2.0).
    with open(os.path.join(OUTPUT_DIR, "busl-1.1.txt"), "w") as f:
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

    with open(os.path.join(OUTPUT_DIR, "apache-2.0-community.txt"), "w") as f:
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
    
    for key, url in LICENSE_URLS.items():
        fname = download_license(key, url)
        if fname:
            manifest[key] = fname
        else:
            success = False
            # Create a placeholder so the app doesn't crash on lookup
            # But mark as failed in content
            fname = sanitize_filename(key) + ".txt"
            with open(os.path.join(OUTPUT_DIR, fname), "w") as f:
                f.write(f"LICENSE DOWNLOAD FAILED.\nURL: {url}")
            manifest[key] = fname

    # Save manifest mapping (Key -> Filename)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f)
        
    if not success:
        print("WARNING: Some licenses failed to download.")

