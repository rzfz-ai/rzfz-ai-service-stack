#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Assert that changed .env defaults carry a migration entry for THIS release.

Background (#198). `prepare-release.sh` Check 1 diffs `.env.example` against the
previous tag and sorts the keys into three buckets: added, removed, and
modified. It then asks the migration manifest about the first two and drops the
third on the floor. That is how the 2026.08 cut shipped with five `*_VERSION`
defaults raised in `.env.example` and no `change_default` behind them: new
installs got authentik 2026.5.5, dify 1.16.0, gitea 1.27.0 and the rest, while
every UPGRADED box kept its old pins and never pulled the images. The check
printed `~ AUTHENTIK_VERSION` and reported success in the same breath.

A second, quieter problem is why this script exists at all instead of one more
`grep` in the shell: the existing coverage test is `grep -q "\\"$key\\""` across
the whole manifest. For a key like `GITEA_VERSION`, which appears in a dozen
historical entries, that grep can never fail. It answers "was this key ever
mentioned", and the question worth asking is "does THIS release carry an entry
that moves it". Those differ precisely when it matters.

Usage:
    check-env-migration-coverage.py --manifest config/migrations/env-changes.json \\
        --version 2026.09-rc1 --action change_default --file .env KEY [KEY ...]

Exits 0 when every key is covered, 1 when any is not (uncovered keys on stdout),
2 on a usage or manifest error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def load_entries(manifest: Path, version: str) -> list[dict]:
    """Every env_changes entry recorded under `version`.

    The manifest may legitimately carry more than one block for a version
    (a release amended after the fact), so all of them are collected rather
    than the first match.
    """
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {manifest}: {exc}", file=sys.stderr)
        raise SystemExit(2)

    versions = doc.get("versions")
    if not isinstance(versions, list):
        print(f"{manifest}: no 'versions' list — schema changed?", file=sys.stderr)
        raise SystemExit(2)

    # Callers pass whatever they have: `prepare-release.sh` is invoked both as
    # `2026.09-rc1` and as `v2026.09-rc1`, while the manifest stores the bare
    # form. A version mismatch here would report EVERY key as uncovered, which
    # reads like a real finding — so normalise instead of trusting the caller.
    want = version.strip().lstrip("v")

    entries: list[dict] = []
    for block in versions:
        if not isinstance(block, dict):
            continue
        if str(block.get("version", "")).strip().lstrip("v") != want:
            continue
        for change in block.get("env_changes") or []:
            if isinstance(change, dict):
                entries.append(change)
    return entries


_VERSION_PIN = re.compile(r"[A-Z0-9_]+_VERSION")


def uncovered(entries: list[dict], keys: list[str], action: str,
              env_file: str | None) -> list[str]:
    """Keys with no entry that covers them.

    `action` names the one action that counts — or ``any``: an entry of ANY
    action for the key under this version counts as "considered", including a
    `note` that says why nothing is written (#2037: the question the release
    check asks is *forgotten vs. considered*, not *which action*). A `rename`
    covers both its old and its new key.
    """
    covered = set()
    for c in entries:
        if env_file is not None and str(c.get("file", "")) != env_file:
            continue
        if action != "any" and str(c.get("action", "")) != action:
            continue
        for field in ("key", "old_key", "new_key"):
            if c.get(field):
                covered.add(str(c[field]))
    return [k for k in keys if k not in covered]


def version_pin_syncs_present(upgrade_script: Path) -> tuple[bool, str]:
    """Do the two mechanisms that deliver *_VERSION pins WITHOUT a manifest rule
    still exist on the upgrade path? (#169: the .env.example-derived force-sync
    inside migrate_env; #177: reconcile_version_pins_from_manifest, called at top
    level.) The exemption below is only honest while both hold — the same test
    test_2037 makes before exempting a pin."""
    try:
        text = upgrade_script.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read {upgrade_script}: {exc}"
    m = re.search(r"^migrate_env\(\) \{(.*?)^\}", text, re.S | re.M)
    if not m or "grep -oE '^[A-Z0-9_]+_VERSION='" not in m.group(1):
        return False, "the #169 *_VERSION sync loop is not inside migrate_env()"
    for fn in ("migrate_env", "reconcile_version_pins_from_manifest"):
        if not re.search(rf"^{fn}\s*$", text, re.M):
            return False, f"{fn} is not called at top level"
    return True, "both pin syncs present"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--version", required=True,
                    help="the release being cut, e.g. 2026.09-rc1")
    ap.add_argument("--action", default="change_default",
                    help="the action that counts as coverage, or 'any': an entry of any "
                         "action (a note included) means the key was considered (#2037)")
    ap.add_argument("--file", dest="env_file", default=None,
                    help="restrict to entries for this env file, e.g. .env")
    ap.add_argument("--exempt-version-pins", action="store_true",
                    help="skip *_VERSION keys — they are force-synced on upgrade by the #169 "
                         "and #177 mechanisms in cli/upgrade.sh; the exemption is granted only "
                         "if both are still present there")
    ap.add_argument("--upgrade-script", type=Path, default=None,
                    help="cli/upgrade.sh to verify the pin syncs in (default: beside the manifest's repo)")
    ap.add_argument("keys", nargs="*")
    args = ap.parse_args(argv)

    keys = [k.strip() for k in args.keys if k.strip()]
    if not keys:
        return 0

    exempted: list[str] = []
    if args.exempt_version_pins:
        script = args.upgrade_script or (args.manifest.resolve().parent.parent.parent / "cli" / "upgrade.sh")
        ok, why = version_pin_syncs_present(script)
        if ok:
            exempted = [k for k in keys if _VERSION_PIN.fullmatch(k)]
            keys = [k for k in keys if k not in exempted]
            if exempted:
                print(f"{len(exempted)} *_VERSION pin(s) exempt — delivered on upgrade by the #169/#177 "
                      f"syncs in {script}: {' '.join(exempted)}", file=sys.stderr)
        else:
            print(f"NOT exempting *_VERSION pins — {why}; a pin without a rule would now "
                  "strand upgraded boxes", file=sys.stderr)

    entries = load_entries(args.manifest, args.version)
    missing = uncovered(entries, keys, args.action, args.env_file)
    if not missing:
        return 0

    what = "entry of any action" if args.action == "any" else f"'{args.action}' entry"
    print(
        f"{len(missing)} .env default(s) changed with no {what} "
        f"under version {args.version}:", file=sys.stderr)
    for key in missing:
        print(f"  {key}", file=sys.stderr)
    print(
        "An upgraded box keeps its old value for these — only fresh installs "
        "get the new default. That is #198: five image pins shipped that way "
        "and no box in the field moved. Add a change_default — or, if nothing "
        "must be written on upgrade, a `note` entry that says why (#2037).", file=sys.stderr)
    for key in missing:
        print(key)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
