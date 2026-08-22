#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# scripts/gen-nobuild-overlay.py   (#184 WS2a — 2026.08 no-runtime-build)
# =============================================================================
# Emit `compose.no-build.yml` — the UNIVERSAL (all-modes) no-runtime-build
# overlay. It NEUTRALISES the `build:` context of every custom-build service via
# Compose's `build: !reset null` merge directive, leaving only `image:` +
# `pull_policy: never`. Composed into COMPOSE_FILE on every installed box, so:
#
#   * `docker compose up -d`, module ENABLE, and module DISABLE can NEVER build —
#     `up` auto-builds a `build:` service whenever its image is MISSING, and
#     `pull_policy: never` does NOT stop that (pull_policy governs pulling, not
#     building). Dropping the `build:` section is the ONLY thing that does.
#   * a MISSING custom image FAILS CLEAR ("… required image is missing" /
#     "pull access denied", surfaced up-front by `rzfz verify-images`) instead of
#     silently running a full internet-reaching build (the #184 P0 breaker:
#     offline `up -d` git-cloned dify.git + ran `pnpm build`).
#
# The ONE place a build runs is the explicit install/package/upgrade build step
# (`docker compose build`), which is invoked with the no-build overlay STRIPPED
# from COMPOSE_FILE (scripts/lib.sh::compose_file_strip_overlay) so the build:
# contexts are still present there. See cli/init.sh, cli/package.sh,
# cli/post-install.sh, cli/upgrade.sh.
#
# DETERMINISTIC + docker-free: the project name is pinned (`name: razzfazz-stack`
# in compose.yml), so a build-only service's image is exactly
# `<project>-<service>` — the SAME derivation core/config/.../build_preflight.py
# `_resolved_image()` uses. We parse the raw module compose files with PyYAML
# (no `docker compose config` render → no llm-runtime container_name collision,
# works on any dev box), so the emitted overlay covers EVERY build service across
# ALL modules regardless of which profiles are active.
#
# The output is COMMITTED (unlike compose.offline.yml which is per-box generated)
# — it is deterministic and is the correctness/security guarantee, so it must not
# depend on a runtime generation step that could silently fail. Regenerate after
# adding/removing a build service:
#
#   python3 scripts/gen-nobuild-overlay.py --out compose.no-build.yml
#
# tests/test-network-mode.sh re-runs this and diffs against the committed file,
# so a new custom-build module that forgets the overlay fails CI (drift guard).
# =============================================================================
import argparse
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - dev tool; PyYAML ships in the repo venv
    sys.stderr.write("gen-nobuild-overlay: PyYAML required\n")
    sys.exit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_yaml(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _included_files(root):
    """Every compose file referenced by the top-level compose.yml `include:`
    list (which itself includes ./core/compose.yml + every module file). Returns
    absolute paths that exist, plus compose.yml itself for completeness."""
    top = _load_yaml(os.path.join(root, "compose.yml"))
    files = [os.path.join(root, "compose.yml")]
    for inc in top.get("include", []) or []:
        path = inc.get("path") if isinstance(inc, dict) else inc
        if not path:
            continue
        ap = os.path.normpath(os.path.join(root, path))
        if os.path.isfile(ap):
            files.append(ap)
    return files


def _project_name(root):
    """The pinned compose project name (`name:` in compose.yml). Build-only
    services derive image `<project>-<service>` from it."""
    return _load_yaml(os.path.join(root, "compose.yml")).get("name") or "razzfazz-stack"


def collect_build_services(root):
    """`{service_name: resolved_image}` for every service carrying a `build:`
    section, across all module compose files. Explicit `image:` wins; otherwise
    the compose-derived `<project>-<service>`."""
    project = _project_name(root)
    out = {}
    for path in _included_files(root):
        doc = _load_yaml(path)
        for name, svc in (doc.get("services") or {}).items():
            if not isinstance(svc, dict) or "build" not in svc:
                continue
            image = svc.get("image") or f"{project}-{name}"
            out[name] = image
    return out


def render(services):
    lines = [
        "# SPDX-License-Identifier: BUSL-1.1",
        "# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.",
        "# ==============================================================================",
        "# compose.no-build.yml  —  GENERATED, DO NOT EDIT BY HAND (#184 WS2a)",
        "# ==============================================================================",
        "# Regenerate:  python3 scripts/gen-nobuild-overlay.py --out compose.no-build.yml",
        "#",
        "# UNIVERSAL no-runtime-build overlay (composed in EVERY network mode:",
        "# online / proxied / offline). `build: !reset null` DROPS each custom-build",
        "# service's build context, so `docker compose up -d` / module enable / disable",
        "# can never build — a missing image fails clear (surfaced by `rzfz",
        "# verify-images`) instead of a silent internet-reaching build. `pull_policy:",
        "# never` keeps it from pulling too. Building happens ONLY at the explicit",
        "# install/package/upgrade `docker compose build` step, which strips this",
        "# overlay from COMPOSE_FILE (scripts/lib.sh::compose_file_strip_overlay).",
        "#",
        "# Added to COMPOSE_FILE by scripts/lib.sh::ensure_nobuild_overlay on every",
        "# init/upgrade. `!reset` needs Docker Compose >= 2.24 (fleet ships 2.40+).",
        "# ==============================================================================",
        "",
        "services:",
    ]
    for name in sorted(services):
        lines.append("  %s:" % name)
        # Drop the build section entirely: `up` then has nothing to build.
        lines.append("    build: !reset null")
        # Pin the image compose would have built/tagged, so a build-only service
        # (no explicit `image:` in its base) still has a concrete ref to run.
        lines.append("    image: %s" % services[name])
        lines.append("    pull_policy: never")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate compose.no-build.yml (#184 WS2a)")
    ap.add_argument("--root", default=REPO_ROOT, help="stack checkout root")
    ap.add_argument("--out", default="-", help="output file (default: stdout)")
    args = ap.parse_args(argv)

    services = collect_build_services(args.root)
    if not services:
        sys.stderr.write(
            "gen-nobuild-overlay: no build services found "
            "(wrong --root, or compose.yml has no includes)\n")
        return 3

    text = render(services)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as f:
            f.write(text)
        sys.stderr.write(
            "Wrote %s (%d build service(s), build neutralised)\n"
            % (args.out, len(services)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
