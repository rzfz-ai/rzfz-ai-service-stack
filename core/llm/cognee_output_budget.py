# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""cognee's per-call output budget (``max_tokens`` inside the ``LLM_ARGS`` JSON).

``core/llm/sync.py`` writes ``max_tokens = min(per_slot_context / 4, CAP)`` into
``LLM_ARGS`` on a fresh install. Before the cap it wrote ``per_slot_context / 4``,
which is 65536 for the default chat model: one looping extraction call then held
an engine slot for 55 minutes. The largest extraction output measured over a
500-document ingest was 8,915 tokens.

An upgraded box never runs sync.py (``post-install --refresh`` does not reach it,
and its other writes, model names and embedding dimension, belong to the refresh's
own measured reconciles), and the env migration format compares whole values, so
it cannot change one key inside a JSON value. This module is the upgrade's step:

* ``max_tokens`` equal to the old default (65536) becomes the cap,
* any other ``max_tokens`` is an operator's choice and is kept,
* no ``max_tokens``, an unparsable value or no ``LLM_ARGS`` line: nothing changes,
* the rest of ``LLM_ARGS`` and every other line of .env stay byte for byte.

CLI: ``python3 cognee_output_budget.py <env-file>`` rewrites the file in place when
needed and prints one line saying what changed (nothing when nothing did).
"""
from __future__ import annotations

import json
import re
import sys

#: The most output tokens one cognee LLM call may generate (LLM_ARGS max_tokens).
COGNEE_MAX_OUTPUT_TOKENS = 16384
#: What sync.py wrote before the cap for the default chat model (262144 / 4).
OLD_DEFAULT_MAX_TOKENS = 65536

_LINE = re.compile(r"^(?P<lead>[ \t]*(?:export[ \t]+)?LLM_ARGS=)(?P<value>.*)$")
_OLD = re.compile(r'("max_tokens"\s*:\s*)%d(?![0-9.eE])' % OLD_DEFAULT_MAX_TOKENS)


def migrate_value(raw: str) -> str | None:
    """Return the new LLM_ARGS value, or None when it stays as it is."""
    value = raw.strip()
    quote = value[0] if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1 else ""
    body = value[1:-1] if quote else value
    try:
        args = json.loads(body)
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    mt = args.get("max_tokens")
    if isinstance(mt, bool) or mt != OLD_DEFAULT_MAX_TOKENS:
        return None
    # Change only the number, so the operator's spelling of the rest is kept. A
    # nested max_tokens (inside extra_body) with the same number makes the text
    # ambiguous: then the parsed object is written back compactly instead.
    if len(_OLD.findall(body)) == 1:
        new_body = _OLD.sub(r"\g<1>%d" % COGNEE_MAX_OUTPUT_TOKENS, body)
        try:
            if json.loads(new_body) == {**args, "max_tokens": COGNEE_MAX_OUTPUT_TOKENS}:
                return quote + new_body + quote
        except ValueError:
            pass
    args["max_tokens"] = COGNEE_MAX_OUTPUT_TOKENS
    return quote + json.dumps(args, separators=(",", ":")) + quote


def migrate_env_text(text: str) -> tuple[str, bool]:
    """Return (.env text, changed). Only the last LLM_ARGS line counts, as in a shell."""
    lines = text.splitlines(keepends=True)
    idx = None
    for i, line in enumerate(lines):
        if _LINE.match(line.rstrip("\r\n")):
            idx = i
    if idx is None:
        return text, False
    line = lines[idx]
    body = line.rstrip("\r\n")
    m = _LINE.match(body)
    new_value = migrate_value(m.group("value"))
    if new_value is None:
        return text, False
    lines[idx] = m.group("lead") + new_value + line[len(body):]
    return "".join(lines), True


def migrate_env_file(path: str) -> bool:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new, changed = migrate_env_text(text)
    if changed:
        # In place: the file keeps its owner, mode and inode.
        with open(path, "r+", encoding="utf-8") as fh:
            fh.seek(0)
            fh.write(new)
            fh.truncate()
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: cognee_output_budget.py <env-file>", file=sys.stderr)
        return 2
    if migrate_env_file(argv[1]):
        print("cognee: LLM_ARGS max_tokens %d -> %d (the per-call output cap)"
              % (OLD_DEFAULT_MAX_TOKENS, COGNEE_MAX_OUTPUT_TOKENS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
