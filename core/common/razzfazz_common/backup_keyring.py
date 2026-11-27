# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2132 — BACKUP_ENCRYPTION_PASSWORD keeps its previous generations.

Rotating the backup passphrase used to orphan every earlier archive silently:
the old value was overwritten and nothing kept it (a test box, 2026-09-14: six of
seven archives and 22 env snapshots unreadable). A rotation now shifts the
current value into BACKUP_ENCRYPTION_PASSWORD_PREVIOUS_1 (…_2, …_3 behind it).
Every restore path tries the current value first, then the previous ones,
newest first.

One implementation for every writer. Containers call `rotate()` with their own
in-place .env writer; host scripts call `python3 -m razzfazz_common.backup_keyring
plan` through scripts/lib-backup-keyring.sh, which writes with update_env_value
(inode-preserving, #1224). Stdlib only: the setup CLI runs on the host.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping

CURRENT_KEY = "BACKUP_ENCRYPTION_PASSWORD"
PREVIOUS_KEYS = tuple(f"{CURRENT_KEY}_PREVIOUS_{i}" for i in (1, 2, 3))
KEYRING_KEYS = (CURRENT_KEY, *PREVIOUS_KEYS)


@dataclass(frozen=True)
class Rotation:
    updates: dict = field(default_factory=dict)
    kept: tuple = ()
    dropped: str | None = None
    changed: bool = False


def plan_rotation(env: Mapping[str, str], new: str) -> Rotation:
    """The writes that make `new` current and keep up to three earlier values."""
    if not new:
        raise ValueError("refusing to set BACKUP_ENCRYPTION_PASSWORD to an empty value: "
                         "an empty passphrase means unencrypted archives")
    current = env.get(CURRENT_KEY) or ""
    previous = [env.get(k) or "" for k in PREVIOUS_KEYS]
    if current == new:
        return Rotation(kept=tuple(p for p in previous if p))
    ordered: list[str] = []
    for v in (current, *previous):
        if v and v != new and v not in ordered:
            ordered.append(v)
    kept = tuple(ordered[:len(PREVIOUS_KEYS)])
    dropped = ordered[len(PREVIOUS_KEYS)] if len(ordered) > len(PREVIOUS_KEYS) else None
    # Order matters. The slots shift oldest first (3, 2, 1) so a slot is only
    # overwritten after its value has been copied one slot older, and the current
    # key goes LAST: a failure part-way never loses a passphrase (only the one
    # that falls off the end is overwritten, on purpose).
    updates = {}
    for i in reversed(range(len(PREVIOUS_KEYS))):
        updates[PREVIOUS_KEYS[i]] = kept[i] if i < len(kept) else ""
    updates[CURRENT_KEY] = new
    return Rotation(updates=updates, kept=kept, dropped=dropped, changed=True)


def rotate(env: Mapping[str, str], new: str,
           write: Callable[[dict], None]) -> list[str]:
    """Apply the plan through `write(updates)`; return the operator-facing lines."""
    r = plan_rotation(env, new)
    if not r.changed:
        return ["BACKUP_ENCRYPTION_PASSWORD unchanged (already the requested value)."]
    write(dict(r.updates))
    held = ", ".join(PREVIOUS_KEYS[:len(r.kept)]) or "none"
    lines = [f"BACKUP_ENCRYPTION_PASSWORD rotated; {len(r.kept)} earlier generation(s) "
             f"kept ({held}); every restore tries them."]
    if r.dropped is not None:
        lines.append(
            f"WARNING: the keyring holds {len(PREVIOUS_KEYS)} earlier generations; the oldest "
            "no longer fits. Archives and env snapshots encrypted with it can no longer be "
            "restored by the product — `rzfz backup list` marks them 'NOT HELD'. Keep that "
            "passphrase offline if you still need them.")
    return lines


def keyring_entries(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """[(key, passphrase)]: current first, then previous newest first; no empties,
    no duplicates."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for k in KEYRING_KEYS:
        v = env.get(k) or ""
        if v and v not in seen:
            seen.add(v)
            out.append((k, v))
    return out


def candidate_passphrases(env: Mapping[str, str]) -> list[str]:
    return [v for _, v in keyring_entries(env)]


def _cli(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m razzfazz_common.backup_keyring")
    sub = p.add_subparsers(dest="cmd", required=True)
    plan = sub.add_parser("plan", help="plan a rotation to the passphrase read on stdin")
    plan.add_argument("--env", required=True)
    cand = sub.add_parser("candidates", help="print the passphrases to try, NUL-separated, current first")
    cand.add_argument("--env", required=True)
    a = p.parse_args(argv)
    from razzfazz_common.env_utils import read_env_file
    if a.cmd == "candidates":
        # The restore side reads the keyring through the very reader the backup
        # side uses (#2607): no second parser to disagree on a quoted value.
        for v in candidate_passphrases(read_env_file(a.env)):
            sys.stdout.buffer.write(v.encode("utf-8") + b"\0")
        return 0
    new = sys.stdin.read()
    if new.endswith("\n"):
        new = new[:-1]
    try:
        r = plan_rotation(read_env_file(a.env), new)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    lines = rotate(read_env_file(a.env), new, lambda u: None)
    for k, v in r.updates.items():
        sys.stdout.buffer.write(f"{k}={v}".encode("utf-8") + b"\0")
    for ln in lines:
        print(ln, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
