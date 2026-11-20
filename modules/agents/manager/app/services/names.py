# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""2026.09-ga.4 (#2517 §3): an agent's name is required and unique per user.

ONE definition of "the same name" (`name_key`), used by every runtime check.
Migration 15 approximates it in SQL (lower + whitespace collapse) once; after
that, only this function decides."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

NAME_MAX = 64   # == provisioner.INSTANCE_NAME_MAX


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name or "")).strip()


def name_key(name: str) -> str:
    return _clean(name).casefold()


def suggest_name(base: str, taken: Iterable[str]) -> str:
    keys = {name_key(t) for t in taken if t}
    base = _clean(base)[:NAME_MAX] or "Agent"
    if name_key(base) not in keys:
        return base
    n = 2
    while True:
        sfx = f" {n}"
        cand = base[:NAME_MAX - len(sfx)].rstrip() + sfx
        if name_key(cand) not in keys:
            return cand
        n += 1


class NameRequired(Exception):
    def __init__(self, suggested: str):
        super().__init__("Give the agent a name.")
        self.suggested = suggested


class NameTaken(Exception):
    def __init__(self, name: str, suggested: str):
        super().__init__(f"You already have an agent named “{_clean(name)}”.")
        self.name = name
        self.suggested = suggested
