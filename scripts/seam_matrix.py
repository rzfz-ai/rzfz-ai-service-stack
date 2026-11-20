#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2001 — read the seam matrix in core/config/profiles.yaml.

`siem-containers --profiles <csv>` prints the Wazuh agent's container list for
the active profiles: every profile whose SIEM seam is `wired` contributes its
declared containers; `core` is always active."""
from __future__ import annotations

import argparse
import sys

import yaml


def siem_containers(profiles_yaml: str, active_profiles: set[str]) -> list[str]:
    with open(profiles_yaml, encoding="utf-8") as f:
        profiles = yaml.safe_load(f)["profiles"]
    active = set(active_profiles) | {"core"}
    out = set()
    for pid, p in profiles.items():
        cell = ((p.get("seams") or {}).get("siem") or {})
        if pid in active and cell.get("state") == "wired":
            out.update(cell.get("containers") or [])
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("siem-containers")
    s.add_argument("--profiles-yaml", required=True)
    s.add_argument("--profiles", default="")
    a = ap.parse_args(argv)
    print(",".join(siem_containers(a.profiles_yaml, {x.strip() for x in a.profiles.split(",") if x.strip()})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
