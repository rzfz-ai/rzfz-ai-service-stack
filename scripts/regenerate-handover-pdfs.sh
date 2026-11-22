#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Regenerate the customer handover PDFs (getting-started.pdf, quickstart.pdf).
#
# They embed MAIN_DOMAIN and the live model list, so they go stale when the
# box is re-domained or refreshed. Callers: scripts/redomain-reconcile.sh
# (after Caddy serves the new domain) and `rzfz post-install --refresh`.
#
# Idempotent (both generators overwrite their output) and NON-FATAL: the exit
# status is always 0; a failure only prints a WARN line naming the command to
# re-run. The generators themselves are not changed here.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^gotenberg$'; then
    echo "  WARN: gotenberg is not running: getting-started.pdf and quickstart.pdf were not regenerated and may still name the previous domain. Once gotenberg is up, run: ./scripts/regenerate-handover-pdfs.sh"
    exit 0
fi

for gen in generate-getting-started-pdf.sh generate-quickstart-pdf.sh; do
    if [ ! -x "scripts/${gen}" ]; then
        echo "  WARN: scripts/${gen} is missing or not executable: its PDF was not regenerated."
        continue
    fi
    if "scripts/${gen}" >/dev/null 2>&1; then
        echo "  regenerated ${gen#generate-}" | sed 's/-pdf\.sh$/.pdf/'
    else
        echo "  WARN: scripts/${gen} failed: the PDF may still name the previous domain. Re-run: ./scripts/${gen}"
    fi
done
exit 0
