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
    "dify-api":         {"CHECK_UPDATE_URL": "", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "API_SENTRY_DSN": ""},
    "dify-worker":      {"CHECK_UPDATE_URL": "", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "API_SENTRY_DSN": ""},
    "dify-worker-beat": {"CHECK_UPDATE_URL": "", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "API_SENTRY_DSN": ""},
    # Dify web (Next.js): Next telemetry off + Scarf + web Sentry empty.
    "dify-web":         {"NEXT_TELEMETRY_DISABLED": "1", "SCARF_NO_ANALYTICS": "true", "SENTRY_DSN": "", "WEB_SENTRY_DSN": ""},
    # Dify plugin daemon: Sentry off + Scarf (marketplace fetches are user-driven,
    # not boot-time, and fail closed offline anyway — left as a feature, not here).
    "dify-plugin-daemon": {"PLUGIN_SENTRY_ENABLED": "false", "PLUGIN_SENTRY_DSN": "", "SCARF_NO_ANALYTICS": "true"},
    # GPUStack (gpustack/gpustack, Python + huggingface_hub): the model-catalog /
    # download path is the WAN talker. HF_HUB_OFFLINE + the HF telemetry disable
    # stop huggingface_hub reaching out; offline registers models from local GGUFs
    # anyway (source=local_path), so this changes nothing about offline operation.
    # All three profile variants (llm / llm-legacy / llm-cpu) pull from HF.
    "gpustack":        {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    "gpustack-legacy": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
    "gpustack-cpu":    {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"},
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
    "cognee": {"COGNEE_KUZU_AUTO_MIGRATE": "false"},
}

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
    lines.append("")
    return "\n".join(lines)


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

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
        print("Wrote %s (%d service(s), pull_policy: never)"
              % (args.out, len(services)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
