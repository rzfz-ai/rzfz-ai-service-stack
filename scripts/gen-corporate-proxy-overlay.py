#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/gen-corporate-proxy-overlay.py  (#181 — 2026.08 corporate-proxy)
# =============================================================================
# Emit `compose.corporate-proxy.yml` — the Level-C container overlay that makes
# every egress container trust the corporate TLS-intercept CA and route through
# the corporate HTTP(S) proxy. Added to COMPOSE_FILE alongside compose.yml (the
# same precedent as the hardware device overlays modules/llm/compose.devices.*).
#
# WHY an env-var overlay (not per-image baking):
#   - certifi (Python: GPUStack / Dify / cognee via requests+httpx) and Node
#     (dify-web, coding-tools) IGNORE the system trust store. A mounted CA
#     bundle alone is NOT enough — REQUESTS_CA_BUNDLE / SSL_CERT_FILE /
#     NODE_EXTRA_CA_CERTS must point them at it explicitly.
#   - SSL_CERT_FILE / REQUESTS_CA_BUNDLE *REPLACE* the whole trust store (not
#     append), so they must point at a SUPERSET bundle (public roots + Caddy
#     internal CA + corporate proxy CA). That superset is exactly what
#     certs/caddy-ca.pem already is once the host CA is installed and the bundle
#     regenerated (scripts/lib.sh ensure_oidc_ca_superset). We mount THAT file.
#   - EMPTY-DIR x509 TRAP: a bare SSL_CERT_FILE=/path with no matching mount
#     makes Docker auto-create /path as an empty *directory* → x509 error. So
#     this generator ALWAYS emits the mount AND the env together, per service.
#
# The proxy CA is customer/box-local: it is NOT baked into any shared image and
# the generated overlay carries only paths + the proxy URL, never the CA bytes.
#
# Usage (normally invoked by scripts/apply-corporate-proxy.sh):
#   gen-corporate-proxy-overlay.py \
#       --proxy-url http://proxy.corp:3128 \
#       --main-domain example.com \
#       [--services gpustack,model-sync,dify-api]   # else: docker compose config --services
#       [--egress-set standard|all] \
#       [--ca-path /certs/caddy-ca.pem] \
#       [--ca-host-path ./certs/caddy-ca.pem] \
#       [--no-proxy "extra,names"] \
#       [--proxy-host-pin host:IP] \
#       [--out compose.corporate-proxy.yml]
#
# Pure stdlib (no PyYAML) — emits deterministic YAML by hand for a fixed shape.
# =============================================================================
import argparse
import ipaddress
import os
import subprocess
import sys

# The CA-trust env vars every egress container gets. All point at the SAME
# mounted superset bundle. Covering the six ecosystems the stack actually uses:
#   SSL_CERT_FILE        — OpenSSL default (curl, git, most C/Go tools)
#   REQUESTS_CA_BUNDLE   — Python requests / httpx / certifi consumers
#   CURL_CA_BUNDLE       — libcurl explicit
#   GIT_SSL_CAINFO       — git over HTTPS (coding-agents checkout)
#   PIP_CERT             — pip (coding-agents / plugin installs)
#   NODE_EXTRA_CA_CERTS  — Node.js (APPENDS to Node's bundled roots — the one
#                          var that is additive, which is why Node also works)
CA_ENV_VARS = [
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "PIP_CERT",
    "NODE_EXTRA_CA_CERTS",
]

# Maintained egress allow-list (design §6). These are the services that make
# OUTBOUND internet calls and therefore MUST trust the intercept CA + route via
# the proxy. Internal-only services (postgres, valkey, vespa, …) never egress,
# so they are left untouched under the default `standard` egress-set. Use
# `--egress-set all` to blanket every defined service (the env is harmless to a
# service that never egresses; use it if a new egressing module is not yet
# listed here). `rzfz status` flags an egress container missing the CA env.
EGRESS_ALLOWLIST = [
    # Always (core + LLM):
    "caddy",             # ACME (Let's Encrypt) + SSRF proxy outbound
    "gpustack",          # HuggingFace model pulls (llm profile, v2.x)
    "gpustack-legacy",   # HuggingFace model pulls (llm-legacy profile)
    "gpustack-cpu",      # HuggingFace model pulls (llm-cpu profile)
    "model-sync",
    "model-sync-legacy",
    "model-sync-cpu",
    "razzfazz-help",     # documentation mirror fetches
    "backup",            # offsite backup upload (if a remote target is set)
    # When their profile is enabled:
    "dify-api",
    "dify-worker",
    "dify-worker-beat",
    "dify-plugin-daemon",  # Dify plugin marketplace
    "dify-sandbox",
    "cognee",
    "cognee-mcp",
    "onyx-api",
    "onyx-background",
    "onyx-web",
    "openhands",
    "crawl4ai",
    "searxng",
    "openlit",
    "paperclip",
    "coding-agents",     # per-user coding tools (npm/pip/git egress)
    "agent-manager",     # provisions per-user agent images (image pulls)
    "mcp-manager",
]

# Never proxy/CA these even under --egress-set all (pure internal datastores;
# adding the mount/env is pointless and, for scratch/distroless images, risky).
NEVER_LIST = {
    "postgres", "valkey", "postgres-komodo", "komodo-postgres", "ferretdb",
    "onyx-vespa", "vespa", "clickhouse", "dify-init-permissions",
}


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
    """Full list of services defined in the CURRENT compose model.

    Prefers the caller-supplied --services (test-friendly, no docker); else
    asks `docker compose config --services` (honours the box's real
    COMPOSE_PROFILES / COMPOSE_FILE, so only active services are returned —
    which is exactly what we want to overlay).
    """
    if explicit:
        return [s.strip() for s in explicit.split(",") if s.strip()]
    out = _run(["docker", "compose", "config", "--services"])
    if out is None:
        return []
    return [s.strip() for s in out.splitlines() if s.strip()]


def select_egress(all_services, egress_set):
    """Which services get the CA + proxy overlay."""
    present = set(all_services)
    if egress_set == "all":
        return [s for s in all_services if s not in NEVER_LIST]
    # standard: allow-list ∩ services actually present, order-preserving.
    return [s for s in EGRESS_ALLOWLIST if s in present and s not in NEVER_LIST]


def build_no_proxy(all_services, main_domain, extra, proxy_host):
    """NO_PROXY must cover every path that stays ON-box so inter-container
    traffic NEVER hits the corporate proxy (that would break Docker-DNS
    service-name calls and same-box loopback)."""
    parts = ["localhost", "127.0.0.1", "::1", "0.0.0.0"]
    # Every service name (Docker DNS) — not just egress ones.
    parts += sorted(all_services)
    # Docker embedded resolver + private ranges (bridge networks live here).
    parts += ["172.16.0.0/12", "10.0.0.0/8", "192.168.0.0/16"]
    # Internal / same-box hostnames.
    parts += [".internal", ".svc", ".local"]
    if main_domain:
        parts += [main_domain, "." + main_domain]
    # The proxy host itself must be reachable directly, not via itself.
    if proxy_host:
        parts.append(proxy_host)
    if extra:
        parts += [p.strip() for p in extra.split(",") if p.strip()]
    # De-dup, order-preserving.
    seen = set()
    out = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ",".join(out)


def _yq(value):
    """Quote a scalar for YAML double-quoted flow (values may contain : , /)."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(services, all_services, ca_path, ca_host_path, proxy_url, no_proxy,
           proxy_host_pin):
    lines = []
    lines.append("# SPDX-License-Identifier: BUSL-1.1")
    lines.append("# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.")
    lines.append("# ==============================================================================")
    lines.append("# compose.corporate-proxy.yml  —  GENERATED, DO NOT EDIT BY HAND (#181)")
    lines.append("# ==============================================================================")
    lines.append("# Regenerate with:  rzfz setup --corporate-proxy   (or scripts/apply-corporate-proxy.sh)")
    lines.append("# Added to COMPOSE_FILE like the hardware device overlays. Off by default:")
    lines.append("# generated only when RAZZFAZZ_CORPORATE_PROXY=1. Box-local — never committed.")
    lines.append("#")
    lines.append("# Each egress service below gets BOTH the CA bundle mount AND the CA env vars")
    lines.append("# (the two MUST go together — a bare SSL_CERT_FILE with no mounted file makes")
    lines.append("# Docker create an empty directory → x509 error), plus HTTP(S)_PROXY / NO_PROXY.")
    lines.append("# The mounted bundle (%s) is a SUPERSET: public roots + Caddy internal CA +" % ca_path)
    lines.append("# corporate proxy CA — because SSL_CERT_FILE/REQUESTS_CA_BUNDLE REPLACE the store.")
    lines.append("# ==============================================================================")
    lines.append("")
    lines.append("services:")
    for svc in services:
        lines.append("  %s:" % svc)
        lines.append("    environment:")
        for var in CA_ENV_VARS:
            lines.append("      %s: %s" % (var, _yq(ca_path)))
        if proxy_url:
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                lines.append("      %s: %s" % (var, _yq(proxy_url)))
            lines.append("      NO_PROXY: %s" % _yq(no_proxy))
            lines.append("      no_proxy: %s" % _yq(no_proxy))
        # Mount the superset bundle read-only. This line is REQUIRED whenever the
        # CA env is set (empty-dir x509 trap guard). ca_host_path resolves
        # relative to THIS overlay file's directory (repo root).
        lines.append("    volumes:")
        lines.append("      - %s:%s:ro" % (ca_host_path, ca_path))
        if proxy_host_pin:
            host, _, ip = proxy_host_pin.partition(":")
            if host and ip:
                lines.append("    extra_hosts:")
                lines.append('      - "%s:%s"' % (host, ip))
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate compose.corporate-proxy.yml (#181)")
    ap.add_argument("--proxy-url", default="")
    ap.add_argument("--main-domain", default=os.environ.get("MAIN_DOMAIN", ""))
    ap.add_argument("--services", default="",
                    help="CSV of active service names (else `docker compose config --services`)")
    ap.add_argument("--egress-set", choices=["standard", "all"], default="standard")
    ap.add_argument("--ca-path", default="/certs/caddy-ca.pem",
                    help="in-container path of the mounted CA bundle")
    ap.add_argument("--ca-host-path", default="./certs/caddy-ca.pem",
                    help="host bind source, relative to the overlay file (repo root)")
    ap.add_argument("--no-proxy", default="", help="extra NO_PROXY entries (CSV)")
    ap.add_argument("--proxy-host-pin", default="",
                    help="host:IP to pin as extra_hosts (locked-down-DNS fallback)")
    ap.add_argument("--out", default="compose.corporate-proxy.yml")
    args = ap.parse_args(argv)

    all_services = discover_services(args.services)
    if not all_services:
        print("gen-corporate-proxy-overlay: no services discovered "
              "(pass --services or run where `docker compose config` works)",
              file=sys.stderr)
        return 3

    egress = select_egress(all_services, args.egress_set)
    if not egress:
        print("gen-corporate-proxy-overlay: no egress services matched "
              "(egress-set=%s); nothing to overlay" % args.egress_set,
              file=sys.stderr)
        return 4

    proxy_host = ""
    if args.proxy_url:
        # Extract host for NO_PROXY (strip scheme + :port + creds).
        h = args.proxy_url.split("://", 1)[-1]
        h = h.split("@", 1)[-1]
        proxy_host = h.split(":", 1)[0].split("/", 1)[0]
    if args.proxy_host_pin:
        proxy_host = args.proxy_host_pin.split(":", 1)[0] or proxy_host

    no_proxy = build_no_proxy(all_services, args.main_domain, args.no_proxy, proxy_host)

    text = render(egress, all_services, args.ca_path, args.ca_host_path,
                  args.proxy_url, no_proxy, args.proxy_host_pin)

    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
        print("Wrote %s (%d egress service(s), egress-set=%s)"
              % (args.out, len(egress), args.egress_set), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
