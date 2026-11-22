# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Network Policy inspector — read-only view of the ACTUAL per-container network
policy (who may reach whom inside the stack + internet-egress posture), introspected
LIVE from docker. Sibling of offline_status.py; NEVER mutates.

Two layers:
  * PURE derivation (egress_posture / internal_reach / module_for / build_model) —
    plain dicts, no docker, unit-tested on fixtures;
  * a thin ``collect()`` that fetches live docker data through an injectable runner
    (the socket-proxy ``docker`` CLI) and calls build_model.

'Allowed to connect' means network-level reachability (shared docker network), NOT
observed traffic. Editing + the 4th ``userdefined`` mode are a later release.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

# NOTE: import ONLY stdlib at module top so this file loads standalone (the unit
# tests exec it via importlib, where the `app.services` package isn't importable).
# offline_status is imported lazily inside _box_mode() (Task 2), fail-open.

SSRF_PROXY = "caddy:8195"
PROJECT_PREFIX = "razzfazz-stack_"
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
# The flat all-services bridge every container shares by default. On a stock
# stack it is WAN-capable, so "attached to default" alone is the low-signal
# majority — the inspector highlights everything that DEVIATES from it.
DEFAULT_NET = "default"


def short_net(name, prefix=PROJECT_PREFIX):
    """Drop the compose project prefix so 'razzfazz-stack_default' -> 'default'.
    Explicit-name networks (coding-agents, mcp-network) have no prefix and pass through."""
    return name[len(prefix):] if name.startswith(prefix) else name


def egress_posture(container_networks, networks_by_name, env):
    """(posture, why) with posture in {NONE, DIRECT, PROXY}.
    Rule order (see spec): PROXY > DIRECT > NONE."""
    for k in _PROXY_ENV_KEYS:
        if SSRF_PROXY in (env.get(k) or ""):
            return ("PROXY", f"routed via SSRF proxy ({SSRF_PROXY})")
    wan = [n for n in container_networks
           if not networks_by_name.get(n, {}).get("internal", False)]
    if wan:
        return ("DIRECT", f"on '{wan[0]}' (WAN-capable), no proxy env")
    return ("NONE", "attached only to internal network(s)")


def internal_reach(name, container_networks, members_by_network):
    """Sorted [{peer, via}] for every OTHER container sharing >=1 network with `name`.
    First (stable) network is recorded as the 'via'."""
    seen = {}
    for net in container_networks:
        for peer in members_by_network.get(net, ()):
            if peer != name:
                seen.setdefault(peer, net)
    return [{"peer": p, "via": v} for p, v in sorted(seen.items())]


def module_for(container_name, profile_containers):
    """profile_containers: profile_id -> set(container_name). 'core' when unmapped."""
    for prof, names in profile_containers.items():
        if container_name in names:
            return prof
    return "core"


def notable_flags(posture, container_networks):
    """(notable: bool, reasons: list[str]) — is this container worth the admin's
    attention for egress/segmentation review, vs. the flat default-bridge majority?

    A container is notable when it DEVIATES from 'DIRECT via the shared default
    bridge, nothing else': it routes egress via the SSRF proxy, is network-isolated
    (no direct internet path), or is attached to a network beyond `default`.
    A plain DIRECT-only-on-`default` container is NOT notable (that's the majority
    the summary collapses)."""
    reasons = []
    if posture == "PROXY":
        reasons.append("routes egress via the SSRF proxy")
    elif posture == "NONE":
        reasons.append("network-isolated — no direct internet path")
    for n in sorted({net for net in container_networks if net != DEFAULT_NET}):
        reasons.append(f"on segmented network '{n}'")
    return (bool(reasons), reasons)


def summarize(ctr_out):
    """Roll up the per-container postures so the page can collapse the flat
    default-bridge majority and lead with the exceptions. PURE over build_model's
    container dicts (needs `internet`, `networks`, `notable`)."""
    return {
        "total": len(ctr_out),
        "direct_default": sum(1 for c in ctr_out
                              if c["internet"] == "DIRECT" and not c["notable"]),
        "proxy_routed": sum(1 for c in ctr_out if c["internet"] == "PROXY"),
        "internal_only": sum(1 for c in ctr_out if c["internet"] == "NONE"),
        "segmented": sum(1 for c in ctr_out
                         if any(n != DEFAULT_NET for n in c["networks"])),
        "notable": sum(1 for c in ctr_out if c["notable"]),
    }


def build_model(networks, containers, box_mode, profile_containers, generated_at):
    """Assemble the NetworkPolicyModel from parsed docker data. PURE."""
    networks_by_name = {n["short_name"]: n for n in networks}
    members_by_network = {n["short_name"]: n["members"] for n in networks}

    ctr_out = []
    for c in containers:
        posture, why = egress_posture(c["networks"], networks_by_name, c["env"])
        reach = internal_reach(c["name"], c["networks"], members_by_network)
        notable, notable_reasons = notable_flags(posture, c["networks"])
        ctr_out.append({
            "name": c["name"],
            "compose_service": c.get("compose_service", ""),
            "module": module_for(c["name"], profile_containers),
            "networks": sorted(c["networks"]),
            "internet": posture,
            "internet_why": why,
            "internal_reach": reach,
            "internal_reach_count": len(reach),
            "notable": notable,
            "notable_reasons": notable_reasons,
        })

    net_out = []
    for n in networks:
        net_out.append({
            "name": n["name"],
            "short_name": n["short_name"],
            "driver": n["driver"],
            "internal": bool(n["internal"]),
            "wan_capable": not n["internal"],
            "members": sorted(n["members"]),
            "meaning": ("members reach each other; internet only via caddy:8195"
                        if n["internal"]
                        else "members reach each other + direct internet"),
        })

    return {
        "box_mode": box_mode,
        "networks": sorted(net_out, key=lambda d: d["short_name"]),
        "containers": sorted(ctr_out, key=lambda d: (d["module"], d["name"])),
        "summary": summarize(ctr_out),
        "generated_at": generated_at,
    }


# --------------------------------------------------------------------------- #
# Live docker fetch (the ONLY docker-touching code). runner is injectable.    #
# --------------------------------------------------------------------------- #

def _default_runner(cmd, cwd, timeout):
    """Run cmd (docker via the socket-proxy) -> combined stdout+stderr, '' on failure."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _json_lines(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _fetch_networks(runner, cwd, timeout, prefix):
    names = [l.strip() for l in
             runner(["docker", "network", "ls", "--format", "{{.Name}}"], cwd, timeout).splitlines()
             if l.strip()]
    if not names:
        return []
    raw = runner(["docker", "network", "inspect", *names, "--format", "{{json .}}"], cwd, timeout)
    out = []
    for d in _json_lines(raw):
        members = [v.get("Name") for v in (d.get("Containers") or {}).values() if v.get("Name")]
        out.append({
            "name": d.get("Name", ""),
            "short_name": short_net(d.get("Name", ""), prefix),
            "driver": d.get("Driver", ""),
            "internal": bool(d.get("Internal")),
            "members": members,
        })
    return out


def _fetch_containers(runner, cwd, timeout, prefix):
    names = [l.strip() for l in
             runner(["docker", "ps", "--format", "{{.Names}}"], cwd, timeout).splitlines()
             if l.strip()]
    if not names:
        return []
    raw = runner(["docker", "inspect", *names, "--format", "{{json .}}"], cwd, timeout)
    out = []
    for d in _json_lines(raw):
        cfg = d.get("Config") or {}
        env = {}
        for e in (cfg.get("Env") or []):
            if "=" in e:
                k, v = e.split("=", 1)
                env[k] = v
        nets = [short_net(k, prefix)
                for k in (d.get("NetworkSettings", {}).get("Networks") or {}).keys()]
        out.append({
            "name": (d.get("Name", "") or "").lstrip("/"),
            "compose_service": (cfg.get("Labels") or {}).get("com.docker.compose.service", ""),
            "networks": nets,
            "env": env,
        })
    return out


def _load_profile_containers(stack_root):
    """profiles.yaml -> {profile_id: set(container_name)}. Fail-open to {} (=> 'core')."""
    path = os.path.join(stack_root, "core", "config", "profiles.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    profiles = data.get("profiles", data) if isinstance(data, dict) else {}
    out = {}
    for pid, prof in (profiles or {}).items():
        if not isinstance(prof, dict):
            continue
        out[pid] = {c.get("name") for c in (prof.get("containers") or []) if c.get("name")}
    return out


def _box_mode(stack_root):
    """Reuse offline_status.resolve_network_mode (DRY — one source for the box mode).
    Lazy import + fail-open to 'online' so this module still loads/collects in a
    standalone/unit context where the `app.services` package isn't importable."""
    try:
        from app.services import offline_status
        return offline_status.resolve_network_mode(os.path.join(stack_root, ".env"))
    except Exception:
        return "online"


def collect(stack_root, runner=_default_runner, timeout=20, prefix=PROJECT_PREFIX):
    """The full read-only NetworkPolicyModel. runner injectable for tests."""
    networks = _fetch_networks(runner, stack_root, timeout, prefix)
    containers = _fetch_containers(runner, stack_root, timeout, prefix)
    profile_containers = _load_profile_containers(stack_root)
    box_mode = _box_mode(stack_root)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return build_model(networks, containers, box_mode, profile_containers, generated_at)


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="network_policy",
                                 description="Read-only network policy inspector.")
    ap.add_argument("--stack-root", default=os.getcwd())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    model = collect(args.stack_root)
    if args.json:
        print(json.dumps(model, indent=2))
    else:
        s = model["summary"]
        print(f"box mode: {model['box_mode']}  networks: {len(model['networks'])}  "
              f"containers: {s['total']}")
        print(f"  summary: {s['direct_default']} DIRECT via default bridge · "
              f"{s['proxy_routed']} proxy-routed · {s['internal_only']} isolated · "
              f"{s['segmented']} on segmented nets · {s['notable']} notable")
        for c in model["containers"]:
            mark = "*" if c["notable"] else " "
            print(f" {mark}{c['module']:<14} {c['name']:<28} internet={c['internet']:<7} "
                  f"reaches={c['internal_reach_count']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
