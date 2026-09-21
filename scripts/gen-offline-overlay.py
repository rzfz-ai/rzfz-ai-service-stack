#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/gen-offline-overlay.py   (#184 — 2026.08 offline / air-gap)
# =============================================================================
# Emit `compose.offline.yml` — the offline-mode container overlay. It sets
# `pull_policy: never` on EVERY service in the active compose model, so that on
# an air-gapped box `docker compose up` / module enable / disable never reach out
# to a registry: a present image is used, a MISSING image FAILS CLEAR (compose
# errors "image not found") instead of tripping the corporate firewall on a pull.
#
# WHY an overlay on top of the baked-in defaults:
#   - WS2a already bakes `pull_policy: never` into the 25 custom-BUILD services in
#     their module compose files (universal, all modes). This overlay is the
#     OFFLINE belt that ALSO covers the pinned-IMAGE services (postgres, valkey,
#     gpustack upstream, …): in offline mode those are loaded from the package,
#     never pulled — `never` makes a missing one fail clear rather than pull.
#   - It is the visible, gitignored artifact that says "offline is active",
#     added to COMPOSE_FILE exactly like the corporate-proxy overlay (#181) and
#     the hardware device overlays (modules/llm/compose.devices.*).
#
# Added to / removed from COMPOSE_FILE by scripts/lib.sh::ensure_network_mode_overlay
# (driven by RAZZFAZZ_NETWORK_MODE=offline). Box-local — never committed.
#
# Usage (normally invoked by ensure_network_mode_overlay / apply-network-mode.sh):
#   gen-offline-overlay.py [--services caddy,postgres,gpustack,…] \
#                          [--out compose.offline.yml]
# With no --services it asks `docker compose config --services` (honours the
# box's real COMPOSE_PROFILES / COMPOSE_FILE, so only active services are set —
# which is what we want to overlay).
#
# ── Talker telemetry suppression (#184 offline-hardening) ────────────────────
# The offline live-acceptance (2026-07-21) found a SECOND egress class distinct
# from the upgrade logic: the talker apps phone home on boot (reverse-DNS'd to
# GitHub / Fastly / GitHub-Pages / CloudFront on :443, FORWARD chain). On a truly
# air-gapped box a customer firewall LOGS that as noise ("nothing that triggers
# the firewall"). So — ONLY in offline mode, via THIS overlay — each talker gets
# its vendor-documented telemetry / update-check / analytics off-switch. Online /
# proxied boxes never compose this overlay, so they are unchanged (conservative +
# reversible). Every value is a real, confirmed switch (see the per-service note);
# values are quoted STRINGS so `docker compose config` stays warning-free (WS4).
# The env MERGES additively onto each service's base env (compose overrides by
# key), so nothing else the service defines is disturbed.
TELEMETRY_OFF = {
    # Open WebUI (ghcr.io/open-webui/open-webui). OFFLINE_MODE is the master
    # switch: env.py sets HF_HUB_OFFLINE=1 AND forces the GitHub version-update
    # check off. The remaining flags are the individually-documented switches
    # (Chroma ANONYMIZED_TELEMETRY, Scarf, universal DO_NOT_TRACK, transformers).
    "openwebui": {
        "OFFLINE_MODE": "true",
        "ENABLE_VERSION_UPDATE_CHECK": "false",
        "ANONYMIZED_TELEMETRY": "false",
        "SCARF_NO_ANALYTICS": "true",
        "DO_NOT_TRACK": "true",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    },
    # Dify Python services (api / worker / worker-beat). CHECK_UPDATE_URL="" is
    # Dify's documented way to disable the updates.dify.ai version check (empty →
    # api/controllers/console/version.py returns local version, no fetch). Sentry
    # DSNs pinned empty (already the default) so nothing reports out.
    "dify-api":         {"CHECK_UPDATE_URL": "", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "API_SENTRY_DSN": "",
                         "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    "dify-worker":      {"CHECK_UPDATE_URL": "", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "API_SENTRY_DSN": "",
                         "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    "dify-worker-beat": {"CHECK_UPDATE_URL": "", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "API_SENTRY_DSN": "",
                         "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    # Help Center (#2223): its post-install documentation MIRROR (`wget --mirror`
    # of the upstream doc sites, hours) is the largest source of blocked egress
    # on an idle air-gapped box — 362 packets in 10 minutes on 0.175 under a
    # proven cut, from a service that was outside this belt. HELP_AUTOWARM=0 is
    # the module's own switch (core/help/app.py): the mirror thread does not
    # start and the pages serve the baked/local documentation. This stops the
    # BACKGROUND thread; an operator's explicit refresh (/api/refresh,
    # /admin/cache) may still try and fail visibly. app.py compares the string
    # explicitly (`!= "0"`) — "0" is truthy in Python, a bare truth test would
    # have ENABLED the mirror. gotenberg was the other talker (33 packets in
    # 600 s to five Google addresses incl. the 5228 push channel) and its
    # Chromium already runs with --disable-background-networking,
    # --safebrowsing-disable-auto-update, --disable-sync and --disable-pings;
    # the channel opens anyway, so there is no flag to set and it is NOT
    # belted here — the network layer is the answer, stated rather than faked.
    "razzfazz-help":    {"HELP_AUTOWARM": "0", "DO_NOT_TRACK": "1"},
    # #2267: FerretDB (monitor profile) reported 9 packets to its telemetry
    # beacon from an air-gapped box; the compose default is already off, the
    # belt names it so the overlay is the second, explicit line.
    "ferretdb":         {"FERRETDB_TELEMETRY": "disable", "DO_NOT_TRACK": "1"},
    # Dify web (Next.js): Next telemetry off + Scarf + web Sentry empty.
    "dify-web":         {"NEXT_TELEMETRY_DISABLED": "1", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "WEB_SENTRY_DSN": ""},
    # Dify plugin daemon: Sentry off + Scarf (marketplace fetches are user-driven,
    # not boot-time, and fail closed offline anyway — left as a feature, not here).
    # #2261: the daemon builds each plugin's Python environment with uv, and uv
    # RESOLVES against the index even with a warm cache unless it is told it is
    # offline — journey D on 2026.09-rc10 (air-gapped, cut armed) measured the
    # bundled cache staged, locked and used, and two of three plugins still
    # failing on `https://pypi.org/simple/<name>/` after 3×45 s, 428 packets
    # from the daemon to PyPI. UV_OFFLINE makes uv resolve from the seeded cache
    # only and fail fast, by name, on a genuine miss — for the `uv venv` step,
    # which inherits the container environment. The `uv pip install` step that
    # RESOLVES runs in an environment the daemon builds from scratch
    # (UV_CACHE_DIR + VIRTUAL_ENV only; measured on 0.175 from the spawned uv's
    # own /proc/<pid>/environ; 208 packets to PyPI with the env line alone), so
    # no container variable reaches it. What does reach it is uv's user config
    # file, searched at /root/.config/uv/uv.toml regardless of environment:
    # CONFIG_FILES below binds one carrying `offline = true` (written beside
    # this overlay, present only in offline mode, part of the compose config so
    # it survives container recreation; the image is upstream's, not ours).
    "dify-plugin-daemon": {"PLUGIN_SENTRY_ENABLED": "false", "PLUGIN_SENTRY_DSN": "", "SCARF_NO_ANALYTICS": "true",
                           "UV_OFFLINE": "1"},
    # #2264: the LLM Manager's router IS LiteLLM and was belted nowhere — it
    # fetched its model-cost map from raw.githubusercontent.com once a minute
    # under journey B's egress cut (80 packets, its own log naming the URL).
    # The cost-map belt every LiteLLM loader gets, plus the HF belt in case a
    # tokenizer is ever pulled for token counting. (The manager itself needs no
    # belt: it reads RAZZFAZZ_NETWORK_MODE, now forwarded by its compose.)
    "llm-manager-router": {"LITELLM_LOCAL_MODEL_COST_MAP": "True", "HF_HUB_OFFLINE": "1",
                           "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    # GPUStack (gpustack/gpustack, Python + huggingface_hub): the model-catalog /
    # download path is the WAN talker. HF_HUB_OFFLINE + the HF telemetry disable
    # stop huggingface_hub reaching out; offline registers models from local GGUFs
    # anyway (source=local_path), so this changes nothing about offline operation.
    # All three profile variants (llm / llm-legacy / llm-cpu) pull from HF.
    "gpustack":        {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    "gpustack-legacy": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    "gpustack-cpu":    {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    # docling-serve (ghcr.io/docling-project/docling-serve-cpu) + its RQ worker.
    # docling loads its layout / OCR / figure-classifier / granite-docling VLM
    # models through huggingface_hub, which does an online revision-check on EVERY
    # convert unless HF_HUB_OFFLINE=1. That check SYN-hangs behind the egress
    # firewall (CloudFront :443) and stalls EVERY conversion until the TCP timeout —
    # the whole doc→JSON extraction times out before it ever reaches a model. The
    # models are pre-cached in the docling-models volume, so the flags simply make
    # docling use them directly. BOTH the API and the rq-worker (which runs the
    # actual convert) need it. (#184 offline gap — found on the PSA air-gapped box,
    # 2026-08-02: docling omitted from this list while gpustack/openwebui had it.)
    "docling":           {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    "docling-rq-worker": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    # Authentik (server + worker, ghcr.io/goauthentik/server): error reporting →
    # sentry.io (pinned off), the built-in update-check (off), and AVATARS=initials
    # so per-user avatar lookups do NOT fetch gravatar.com. GeoIP is NOT auto-
    # updated in this stack (no geoipupdate sidecar; the bundled DB is loaded as-is),
    # so there is no GeoIP fetch to disable here.
    "authentik-server": {"AUTHENTIK_ERROR_REPORTING__ENABLED": "false", "AUTHENTIK_DISABLE_UPDATE_CHECK": "true", "AUTHENTIK_AVATARS": "initials"},
    "authentik-worker": {"AUTHENTIK_ERROR_REPORTING__ENABLED": "false", "AUTHENTIK_DISABLE_UPDATE_CHECK": "true", "AUTHENTIK_AVATARS": "initials"},
    # cognee (#186): offline behaviour override (not telemetry) — do NOT auto-run
    # the ladybug/Kuzu 0.16->0.17 graph migration, which pip-installs the old+new
    # engines from PyPI. In offline mode it degrades to detect-and-warn with the
    # documented manual-recovery command, so an air-gapped box makes no egress.
    # #2143 (Journey B, 0.175, 2026-09-15): cognee loads its embedding tokenizer
    # through huggingface_hub, whose revision check does an HTTP HEAD for
    # `<model>/config.json` — even from /health. Behind the egress firewall that
    # SYN-hangs to the TCP timeout: /health measured 159.5 s as shipped vs 0.1 s
    # with HF_HUB_OFFLINE=1, against a 10 s healthcheck — so cognee was `unhealthy`
    # forever and the offline install failed with a registry-reach count of 0.
    # The docling entry above describes the identical class; cognee was the one
    # HuggingFace-loading service without the belt.
    "cognee": {"COGNEE_KUZU_AUTO_MIGRATE": "false",
               "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
               "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    # cognee-mcp runs the same code with the same imports; it was outside the
    # belt only because nobody had listed it.
    "cognee-mcp": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                   "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                   "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    # crawl4ai imports litellm for its extraction strategies.
    "crawl4ai": {"DO_NOT_TRACK": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
}

#: Every service that loads models or tokenizers through huggingface_hub. The
#: guard tests/unit/consistency/test_2143_* asserts each of these carries the
#: offline pair above — cognee was missing from the belt while this list lived
#: only in people's heads (#184 docling 2026-08-02, #2143 cognee 2026-09-15).
# cognee-mcp added #2173: it runs cognee's codebase with cognee's imports
# (huggingface_hub and transformers both present in the image, measured), so it
# loads the same way and was outside the belt only because nobody listed it.
# Whether crawl4ai and the Dify Python services belong here too is a wider
# question — they carry huggingface_hub but may never load a model through it,
# and this list means "loads models or tokenizers", not "has the package".
# Measured and filed separately rather than expanded on a guess.
HF_LOADERS = ("openwebui", "gpustack", "gpustack-legacy", "gpustack-cpu",
              "docling", "docling-rq-worker", "cognee", "cognee-mcp",
              # #2264: the router (LiteLLM) — its cost-map fetch is the measured reach;
              # listed here so the #2143 guard keeps its HF belt too.
              "llm-manager-router")

#: #2261: bind-mounted config files. {service: [(host_file_beside_the_overlay,
#: container_path, content)]}. The generator WRITES the host file before the
#: overlay names it — a bind whose source is missing makes docker create an
#: empty DIRECTORY at the target, which would break uv's config read.
UV_TOML_SIDECAR = "compose.offline.uv.toml"
CONFIG_FILES = {
    "dify-plugin-daemon": [(UV_TOML_SIDECAR, "/root/.config/uv/uv.toml",
                            "# GENERATED by scripts/gen-offline-overlay.py (#2261) - offline mode only.\n"
                            "# uv's user config: the plugin daemon's `uv pip install` runs in an\n"
                            "# environment it builds from scratch, so only this file can tell uv the\n"
                            "# box is offline (#2261). #2272: resolution comes from the wheelhouse the\n"
                            "# package carries and post-install stages, and from NOTHING else - the\n"
                            "# daemon's index (an auto-detected mirror by default) is never consulted,\n"
                            "# so uv's per-index cache identity cannot hide the package's contents.\n"
                            "offline = true\n"
                            "\n"
                            "[pip]\n"
                            "no-index = true\n"
                            "find-links = [\"/app/storage/cwd/.wheelhouse\"]\n")],
}

#: Every service that imports `litellm`. LiteLLM fetches its model-cost map from
#: raw.githubusercontent.com at import unless LITELLM_LOCAL_MODEL_COST_MAP is
#: set, and falls back to a bundled copy — instantly on a REJECTing firewall,
#: after the full TCP timeout on a real blackhole.
#:
#: That is NOT a HuggingFace host, so HF_HUB_OFFLINE cannot reach it and the
#: #2143 belt could never have caught it. Measured on 0.175 against a true
#: blackhole (journey B, #2126), cognee /health steady state:
#:
#:     as shipped                 >= 200 s   unhealthy
#:     #2143 HF belt only          ~22 s     unhealthy (healthcheck timeout 10 s)
#:     HF belt + this variable     ~1.7 s    healthy in 15 s
#:
#: The membership is MEASURED, not assumed: `python -c "import litellm"` inside
#: every running container on a full box. It is wider than cognee — Dify's three
#: Python services and crawl4ai import it too — which is exactly why this list
#: exists instead of one more variable on one more service.
LITELLM_LOADERS = ("cognee", "cognee-mcp", "crawl4ai",
                   "dify-api", "dify-worker", "dify-worker-beat",
                   # #2264: the router IS LiteLLM; it was belted nowhere.
                   "llm-manager-router")

# Pure stdlib (no PyYAML) — emits deterministic YAML by hand for a fixed shape.
# =============================================================================
import argparse
import subprocess
import sys


def _yq(value):
    """Quote a scalar for YAML double-quoted flow so every emitted env value is a
    STRING (unquoted `false`/`1`/`` would be parsed as bool/int/null and make
    `docker compose config` warn). Mirrors gen-corporate-proxy-overlay.py::_yq."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(cmd):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def discover_services(explicit):
    """Services defined in the CURRENT compose model.

    Prefers caller-supplied --services (test-friendly, no docker); else asks
    `docker compose config --services`.
    """
    if explicit:
        return [s.strip() for s in explicit.split(",") if s.strip()]
    out = _run(["docker", "compose", "config", "--services"])
    if out is None:
        return []
    return [s.strip() for s in out.splitlines() if s.strip()]


def render(services):
    lines = []
    lines.append("# SPDX-License-Identifier: BUSL-1.1")
    lines.append("# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.")
    lines.append("# ==============================================================================")
    lines.append("# compose.offline.yml  —  GENERATED, DO NOT EDIT BY HAND (#184)")
    lines.append("# ==============================================================================")
    lines.append("# Regenerate with:  rzfz setup --network-mode --mode offline")
    lines.append("# Added to COMPOSE_FILE like the hardware device overlays. Present only when")
    lines.append("# RAZZFAZZ_NETWORK_MODE=offline. Box-local — never committed (.gitignore).")
    lines.append("#")
    lines.append("# `pull_policy: never` on every service: an air-gapped box uses the images it")
    lines.append("# already has (built/pulled once at install, or loaded from the package) and")
    lines.append("# NEVER reaches a registry at runtime. A missing image fails clear (compose")
    lines.append("# 'image not found') — surfaced up-front by `rzfz verify-images` — instead of")
    lines.append("# attempting a pull that would trip the customer firewall.")
    lines.append("#")
    lines.append("# Talker services ALSO get their vendor-documented telemetry / update-check /")
    lines.append("# analytics off-switch (offline-hardening): apps phone home on boot (GitHub /")
    lines.append("# CDN :443) which a customer firewall logs as noise. Offline-only + additive.")
    lines.append("# ==============================================================================")
    lines.append("")
    lines.append("services:")
    for svc in services:
        lines.append("  %s:" % svc)
        lines.append("    pull_policy: never")
        env = TELEMETRY_OFF.get(svc)
        if env:
            lines.append("    environment:")
            for key, val in env.items():
                lines.append("      %s: %s" % (key, _yq(val)))
        files = CONFIG_FILES.get(svc)
        if files:
            lines.append("    volumes:")
            for host_file, container_path, _content in files:
                # relative to the compose project root, where the overlay lives
                lines.append("      - %s" % _yq("./%s:%s:ro" % (host_file, container_path)))
    lines.append("")
    return "\n".join(lines)


def write_sidecars(out_path, services):
    """Write every bind-mounted config file next to the overlay, BEFORE the
    overlay that names it exists (#2261). Returns the paths written."""
    import os
    outdir = os.path.dirname(os.path.abspath(out_path)) if out_path != "-" else os.getcwd()
    written = []
    for svc in services:
        for host_file, _container_path, content in CONFIG_FILES.get(svc, []):
            path = os.path.join(outdir, host_file)
            with open(path, "w") as f:
                f.write(content)
            written.append(path)
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate compose.offline.yml (#184)")
    ap.add_argument("--services", default="",
                    help="CSV of active service names (else `docker compose config --services`)")
    ap.add_argument("--out", default="compose.offline.yml")
    args = ap.parse_args(argv)

    services = discover_services(args.services)
    if not services:
        print("gen-offline-overlay: no services discovered "
              "(pass --services or run where `docker compose config` works)",
              file=sys.stderr)
        return 3

    text = render(services)
    sidecars = write_sidecars(args.out, services)   # before the overlay names them (#2261)

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
        print("Wrote %s (%d service(s), pull_policy: never)"
              % (args.out, len(services)), file=sys.stderr)
        for p_ in sidecars:
            print("Wrote %s (bind-mounted config, #2261)" % p_, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
