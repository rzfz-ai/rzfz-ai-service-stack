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
import json
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
    "razzfazz-config",   # #275: version-manifest fetch (GitHub raw on public boxes) — needs the WSA CA
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
    "llm-worker-agent",    # #283: spawned engines pull models (HF egress)
    # #1064 item 1 (decided 2026-09-05, after #1418): the LLM Manager egresses —
    # HuggingFace (catalog browser + hf_pull), external OpenAI-compatible
    # backends over WAN (#307 endpoint probe), the hub/registry — so it needs
    # the intercept CA and the proxy. Safe to hand it HTTP(S)_PROXY only because
    # every INTERNAL httpx client in it (router, node-agents, engines, zot,
    # playground) declares trust_env=False since #1418; before that, the proxy
    # env sent in-network calls to the corporate proxy (#276 class: /v1/rerank
    # 502, catalog "Connection refused" — reproduced on 0.91). NO_PROXY cannot
    # cover it: engine-* container names are dynamic and CIDRs never match.
    # tests/unit/llm-manager/test_1409_httpx_clients_declare_trust_env.py keeps
    # that precondition true; test_1064_llm_manager_egress.py keeps this line.
    "llm-manager",
    # #1075: the OpenUEM console checks https://releases.openuem.eu every 6h for
    # new agent/server releases and downloads agent installers from there. It is
    # the module's ONLY egressing container, and upstream offers no switch to
    # disable the check — so it needs the corporate CA, and `rzfz status` needs
    # to be able to see it.
    "openuem-console",
    # #855 — Wazuh. The certs generator downloads wazuh-certs-tool.sh from
    # packages.wazuh.com on FIRST start (DECISION-7); the manager egresses only
    # when the operator enables <vulnerability-detection> (off by default), but
    # it is listed so `rzfz status` can flag a missing intercept CA if they do —
    # an UNLISTED egress container is invisible to that check.
    "wazuh-certs-generator",
    "wazuh-manager",
]

# #276 — CA yes, proxy NO. These services egress (so they need the corporate
# TLS-intercept CA), but they also carry an INTERNAL control plane that talks to
# other containers by IP. Giving them HTTP(S)_PROXY breaks that control plane:
# GPUStack's server reaches its worker at a 172.16.x bridge address, and its
# HTTP client honours NO_PROXY only by exact host or suffix — never by CIDR — so
# `NO_PROXY=172.16.0.0/12` does not exempt it and the internal call is sent to
# the corporate proxy, which cannot route into the docker bridge. On the Care
# Solutions box that showed as worker "Unreachable" and EVERY model stuck
# "Pending": the box was effectively dead while every container was healthy.
# Adding the service NAME to NO_PROXY does not help either, because the worker
# is addressed by IP.
#
# The cost of this choice, stated plainly: GPUStack cannot pull models from
# HuggingFace THROUGH the proxy any more. That is the right trade for these
# boxes -- they run sideloaded/offline model files, and a box whose scheduler
# cannot reach its own worker cannot serve anything at all. A site that really
# wants proxied HF pulls should re-add the proxy env for gpustack in a
# box-local overlay, accepting that server->worker must then be reachable some
# other way.
CA_ONLY_NO_PROXY = {
    "gpustack",
    "gpustack-legacy",
    "gpustack-cpu",
}

# Never proxy/CA these even under --egress-set all (pure internal datastores;
# adding the mount/env is pointless and, for scratch/distroless images, risky).
NEVER_LIST = {
    "postgres", "valkey", "postgres-komodo", "komodo-postgres", "ferretdb",
    "onyx-vespa", "vespa", "clickhouse", "dify-init-permissions",
    # #1075: the rest of OpenUEM never leaves the box — the three workers speak
    # only NATS + postgres, the broker is a broker, the OCSP responder answers
    # queries rather than making them, and the cert bootstrap is provably
    # offline (it only invokes local binaries and writes to the shared postgres).
    "openuem-nats", "openuem-ocsp-responder", "openuem-worker-agents",
    "openuem-worker-cert-manager", "openuem-worker-notification", "openuem-certs",
    # #855 — pure internal: these address each other by container name over the
    # docker bridge. Handing them HTTP(S)_PROXY breaks that control plane the
    # same way it broke GPUStack server->worker (#276).
    "wazuh-indexer", "wazuh-dashboard", "wazuh-securityconfig-init",
    "wazuh-securityadmin", "wazuh-dashboard-config-init",
}


def _run(cmd, env=None):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False,
            env=env,
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _all_profiles_env():
    """Env with COMPOSE_PROFILES set to EVERY defined profile.

    #280: NO_PROXY (and the egress overlay) must cover every service the customer
    COULD enable later — not just the box's currently-active profiles — because
    httpx bypasses NO_PROXY only on EXACT-host matches (never the CIDR ranges),
    and an already-running egress container keeps its NO_PROXY even after a new
    profile is turned on. So we enumerate the model with all profiles active.
    Returns None if the profile list can't be obtained (caller falls back to the
    ambient env / active profiles).
    """
    profs = _run(["docker", "compose", "config", "--profiles"])
    if not profs:
        return None
    names = [p.strip() for p in profs.splitlines() if p.strip()]
    if not names:
        return None
    env = dict(os.environ)
    env["COMPOSE_PROFILES"] = ",".join(names)
    return env


def discover_model(explicit):
    """Return (service_names, hostnames) across ALL profiles.

    - service_names → egress selection (the allow-list is keyed by service name).
    - hostnames → NO_PROXY: service names ∪ container_names ∪ host.docker.internal
      (Docker DNS answers to BOTH the service name and the container_name; where
      they differ — e.g. backup→backup-service, *-image→*-image-builder — both
      must be exact-host entries).

    Prefers the caller-supplied --services (test-friendly, no docker). Else asks
    `docker compose config` with EVERY profile enabled (#280).
    """
    if explicit:
        svcs = [s.strip() for s in explicit.split(",") if s.strip()]
        return svcs, set(svcs)
    env = _all_profiles_env()
    # Full model (incl. container_names) with all profiles active.
    out = _run(["docker", "compose", "config", "--format", "json"], env=env)
    if out:
        try:
            model = json.loads(out)
        except ValueError:
            model = None
        if model and isinstance(model.get("services"), dict):
            svcs = sorted(model["services"].keys())
            hosts = set(svcs)
            for spec in model["services"].values():
                cn = (spec or {}).get("container_name")
                if cn:
                    hosts.add(cn)
            return svcs, hosts
    # Fallback: service names only, all profiles.
    out = _run(["docker", "compose", "config", "--services"], env=env)
    svcs = [s.strip() for s in (out or "").splitlines() if s.strip()]
    return svcs, set(svcs)


def select_egress(all_services, egress_set):
    """Which services get the CA + proxy overlay."""
    present = set(all_services)
    if egress_set == "all":
        return [s for s in all_services if s not in NEVER_LIST]
    # standard: allow-list ∩ services actually present, order-preserving.
    return [s for s in EGRESS_ALLOWLIST if s in present and s not in NEVER_LIST]


def build_no_proxy(hostnames, main_domain, extra, proxy_host):
    """NO_PROXY must cover every path that stays ON-box so inter-container
    traffic NEVER hits the corporate proxy (that would break Docker-DNS
    service-name calls and same-box loopback)."""
    # host.docker.internal: the v2.x gpustack embedded worker advertises it, and
    # onyx/openhands dial callbacks through it — must bypass the proxy (#280).
    parts = ["localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"]
    # Every service name + container_name (Docker DNS), across ALL profiles —
    # not just active/egress ones (#280). httpx matches NO_PROXY by exact host,
    # never CIDR, so each internal hostname must be listed explicitly.
    parts += sorted(hostnames)
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
           proxy_host_pin, extra_env=None, chain=None):
    extra_env = extra_env or {}
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
        svc_extra = extra_env.get(svc, {})
        # #276: CA env above, but no HTTP(S)_PROXY for a service whose internal
        # control plane addresses containers by IP.
        if proxy_url and svc in CA_ONLY_NO_PROXY:
            lines.append("      # #276: deliberately NO HTTP(S)_PROXY — this service's server↔worker")
            lines.append("      # traffic is by IP on the docker bridge, and its client ignores CIDR")
            lines.append("      # NO_PROXY, so a proxy here makes the worker Unreachable.")
        if proxy_url and svc not in CA_ONLY_NO_PROXY:
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                if var not in svc_extra:
                    lines.append("      %s: %s" % (var, _yq(proxy_url)))
            if "NO_PROXY" not in svc_extra:
                lines.append("      NO_PROXY: %s" % _yq(no_proxy))
            if "no_proxy" not in svc_extra:
                lines.append("      no_proxy: %s" % _yq(no_proxy))
        # #277: per-service env overrides (repoint Dify SSRF egress at the chain).
        # Emitted after the defaults; the skip-guards above avoid duplicate keys.
        for _k, _v in svc_extra.items():
            lines.append("      %s: %s" % (_k, _yq(_v)))
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
    # #277: the Dify SSRF chaining sidecar (squid). Chains PUBLIC egress through
    # the corporate WSA while keeping the doc-tools direct + blocking other
    # internal targets — the one thing caddy:8195's forward_proxy can't do
    # (it refuses to chain to a plain-HTTP upstream). Only emitted on a proxied
    # box with Dify present. profiles:[dify] so it starts iff Dify is active.
    if chain:
        lines.append("  %s:" % chain["name"])
        lines.append("    image: %s" % _yq(chain["image"]))
        lines.append("    container_name: %s" % chain["name"])
        lines.append('    profiles: ["dify"]')
        lines.append("    restart: unless-stopped")
        lines.append("    networks:")
        lines.append("      - ssrf_proxy_network")
        lines.append("      - default")
        lines.append("    volumes:")
        lines.append("      - %s:%s:ro" % (chain["conf_host_path"], chain["conf_path"]))
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
    ap.add_argument("--print-no-proxy", action="store_true",
                    help="print ONLY the computed NO_PROXY value and exit — the "
                         "single source of truth for the daemon/docker-client "
                         "NO_PROXY in apply-corporate-proxy.sh (#280)")
    ap.add_argument("--dify-ssrf-chain", action="store_true",
                    help="emit the dify-ssrf-chain squid sidecar + repoint Dify "
                         "SSRF egress at it (#277); needs --proxy-url + Dify present")
    ap.add_argument("--dify-ssrf-chain-image",
                    # F-06: digest-pinned, no floating :latest (supply-chain). Resolved
                    # 2026-08-13 from docker.io/ubuntu/squid:latest (Ubuntu 24.04 base).
                    # Operator override: RAZZFAZZ_DIFY_SSRF_CHAIN_IMAGE. Re-resolve on a
                    # squid CVE / base-image bump.
                    default=os.environ.get(
                        "RAZZFAZZ_DIFY_SSRF_CHAIN_IMAGE",
                        "ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029"),
                    help="image for the dify-ssrf-chain sidecar (#277); digest-pinned (F-06)")
    ap.add_argument("--squid-conf-host-path",
                    default="./certs/dify-ssrf-chain-squid.conf",
                    help="host bind source for the rendered squid.conf (#277)")
    ap.add_argument("--squid-conf-path", default="/etc/squid/squid.conf",
                    help="in-container path for the squid.conf (#277)")
    ap.add_argument("--out", default="compose.corporate-proxy.yml")
    args = ap.parse_args(argv)

    all_services, all_hostnames = discover_model(args.services)
    if not all_services:
        print("gen-corporate-proxy-overlay: no services discovered "
              "(pass --services or run where `docker compose config` works)",
              file=sys.stderr)
        return 3

    proxy_host = ""
    if args.proxy_url:
        # Extract host for NO_PROXY (strip scheme + :port + creds).
        h = args.proxy_url.split("://", 1)[-1]
        h = h.split("@", 1)[-1]
        proxy_host = h.split(":", 1)[0].split("/", 1)[0]
    if args.proxy_host_pin:
        proxy_host = args.proxy_host_pin.split(":", 1)[0] or proxy_host

    no_proxy = build_no_proxy(all_hostnames, args.main_domain, args.no_proxy, proxy_host)

    # --print-no-proxy: emit ONLY the computed NO_PROXY and exit. This is the
    # single source of truth apply-corporate-proxy.sh uses for the daemon
    # drop-in + ~/.docker/config.json, so EVERY container (not just the overlaid
    # egress ones) bypasses the proxy for on-box hostnames (#280).
    if args.print_no_proxy:
        sys.stdout.write(no_proxy)
        return 0

    egress = select_egress(all_services, args.egress_set)
    if not egress:
        print("gen-corporate-proxy-overlay: no egress services matched "
              "(egress-set=%s); nothing to overlay" % args.egress_set,
              file=sys.stderr)
        return 4

    # #277: Dify SSRF chaining sidecar + repoint Dify's SSRF egress at it.
    DIFY_SSRF_SVCS = ("dify-api", "dify-worker", "dify-worker-beat")
    DIFY_SANDBOX = "dify-sandbox"
    chain_url = "http://dify-ssrf-chain:3128"
    extra_env = {}
    chain = None
    if args.dify_ssrf_chain and args.proxy_url:
        eg = set(egress)
        if any(s in eg for s in DIFY_SSRF_SVCS) or DIFY_SANDBOX in eg:
            for s in DIFY_SSRF_SVCS:
                if s in eg:
                    extra_env[s] = {"SSRF_PROXY_HTTP_URL": chain_url,
                                    "SSRF_PROXY_HTTPS_URL": chain_url}
            if DIFY_SANDBOX in eg:
                # sandbox uses HTTP(S)_PROXY (not SSRF_PROXY_*) — repoint those so
                # user-code egress keeps the SSRF ACL via the chain instead of the
                # overlay's direct-to-WSA (which would lose RFC1918 protection).
                extra_env[DIFY_SANDBOX] = {"HTTP_PROXY": chain_url,
                                           "HTTPS_PROXY": chain_url,
                                           "http_proxy": chain_url,
                                           "https_proxy": chain_url}
            chain = {"name": "dify-ssrf-chain",
                     "image": args.dify_ssrf_chain_image,
                     "conf_host_path": args.squid_conf_host_path,
                     "conf_path": args.squid_conf_path}

    text = render(egress, all_services, args.ca_path, args.ca_host_path,
                  args.proxy_url, no_proxy, args.proxy_host_pin,
                  extra_env=extra_env, chain=chain)

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
