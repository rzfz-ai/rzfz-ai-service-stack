#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# Blank internal dev-history prose in files that MUST ship to the public mirror
# for functional reasons but whose prose is not customer-facing.
#
# Currently: config/migrations/env-changes.json. It ships because the customer's
# `rzfz upgrade` reads it to migrate .env — but its `notes`/`comment` fields are
# internal development history (milestone IDs, CVE-exploit specifics, .gsd refs,
# per-commit narrative). This clears ONLY those prose fields; the migration LOGIC
# (key / action / default / version) is untouched, so upgrades behave identically.
# The internal Gitea repo keeps the full annotations — this runs on the export
# copy only.
#
# Usage: sanitize-public-export.sh <stage-dir>
# Exit:  0 done (or nothing to do) · 1 transform failed · 2 usage/deps.

set -uo pipefail

STAGE="${1:?usage: sanitize-public-export.sh <stage-dir>}"
ECJ="$STAGE/config/migrations/env-changes.json"

# ── Pass 1: env-changes.json prose ────────────────────────────────────────────
# SCOPED to this pass. The file-level `[ -f "$ECJ" ] || exit 0` guard that used
# to sit here preceded the Markdown neutralizer below, so a staged tree without
# env-changes.json exited 0 and skipped the WHOLE *.md internal-ref pass —
# while publish-public.sh printed "sanitized public-export prose". The two
# passes have unrelated preconditions and must not gate each other.
if [ -f "$ECJ" ]; then
    command -v jq >/dev/null 2>&1 || { echo "sanitize-public-export: jq required" >&2; exit 2; }

    tmp="$(mktemp)"
    # walk every object; blank the prose keys where present. reduce keeps the
    # object structure intact and touches nothing else.
    if jq 'walk(if type == "object"
               then reduce ("notes", "comment", "note", "description_internal") as $k
                    (.; if has($k) then .[$k] = "" else . end)
               else . end)' "$ECJ" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
        mv "$tmp" "$ECJ"
    else
        rm -f "$tmp"
        echo "sanitize-public-export: jq transform of env-changes.json failed" >&2
        exit 1
    fi
else
    echo "sanitize-public-export: no config/migrations/env-changes.json in the stage — JSON pass skipped (the Markdown pass still runs)"
fi

# --- Markdown neutralizer -----------------------------------------------------
# Customer-facing Markdown that ships (module READMEs, docs/community, cycle
# notes) sometimes carries internal engineering detail that the pre-push
# deliverable-review gate (review-public-deliverables.sh) rejects: internal
# doc paths (.gsd/ .claude/ security-run/), milestone IDs (M0NN / M0NN-SNN),
# and RFC1918/RFC6598 addresses. This pass is the mechanical inverse of that
# gate's internal-refs class — it neutralises exactly those token classes in
# the STAGED copies (the internal Gitea repo keeps the originals). Conservative
# by construction: it only rewrites those three token shapes, never prose.
python3 - "$STAGE" <<'PYEOF' || { echo "sanitize-public-export: markdown neutralizer failed" >&2; exit 1; }
import re, sys, pathlib
stage = pathlib.Path(sys.argv[1])
# RFC1918 (10/8, 172.16/12, 192.168/16) + RFC6598 CGNAT (100.64/10) dotted-quads
IPRE = re.compile(r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
                  r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}'
                  r'|192\.168\.\d{1,3}\.\d{1,3}'
                  r'|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b')
# internal doc path token (optionally with ../ prefixes); stops at whitespace,
# a closing paren/bracket, a backtick or a quote — i.e. the markdown delimiters
PATHRE = re.compile(r'(?:\.{1,2}/)*(?:\.gsd|\.claude|security-run)/[^\s)\]`\'"]*')
MIDRE = re.compile(r'\bM0\d{2}(?:-S\d{1,2})?\b')   # M031, M031-S5, M008, …
# a whole markdown link [label](target) whose TARGET is an internal doc path —
# drop the broken link, keep readable prose (handled before the bare-path rule)
LINKRE = re.compile(r'\[[^\]]*\]\((?:\.{1,2}/)*(?:\.gsd|\.claude|security-run)/[^)]*\)')
changed = 0
for md in stage.rglob('*.md'):
    t = md.read_text(encoding='utf-8', errors='replace')
    # internal-path links first, then bare path tokens, then milestone IDs, then IPs
    n = IPRE.sub('192.0.2.10',
        MIDRE.sub('internal-tracking',
        PATHRE.sub('internal-docs',
        LINKRE.sub('internal docs', t))))
    if n != t:
        md.write_text(n, encoding='utf-8')
        changed += 1
        print(f'sanitize-public-export: neutralized internal refs in {md.relative_to(stage)}')
print(f'sanitize-public-export: markdown neutralizer touched {changed} file(s)')
PYEOF
