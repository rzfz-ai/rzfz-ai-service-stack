#!/usr/bin/env bash
# Classify a list of changed files into the CI scopes they can possibly break.
#
# WHY THIS EXISTS (#444). CI was running all three jobs for every push. Measured
# over 32 merges to main on 2026-08-18: 96 jobs / 304 runner-minutes, of which
# eight merges changed ONE markdown file each and still span up ephemeral
# postgres containers for the full 7.5-minute unit+api suite. One runner carries
# the `razzfazz-ci` label, so that is queue depth for the next person's PR.
#
# The obvious fix — per-workflow `paths:` filters — was tried first and REVERTED.
# Splitting ci.yml into three filtered workflows left the branch with *zero* runs
# on Gitea 1.27.1, for both a workflow-only commit and a docs-only commit: the
# gate silently switched off. A CI change that disables CI is worse than the
# waste it fixes, so scope selection moved here, where it is testable and where
# an unmatched path can only ever mean "run more", never "run nothing".
#
# Usage:
#   scripts/ci-changed-scopes.sh <file>...     # classify these paths
#   git diff --name-only A B | scripts/ci-changed-scopes.sh
#
# Prints, on stdout, one `key=value` per line (GITHUB_OUTPUT format):
#   code=true|false   unit+api tier must run
#   docs=true|false   docs-structure + release-docs lint must run
#   ui=true|false     manager-ui type-check + build must run
#   caddy=true|false  the Caddyfile must be validated with the real binary
#
# FAIL OPEN. An empty file list, or an unrecognised path, yields `true`. The only
# way to skip a job is for EVERY changed file to be positively recognised as
# irrelevant to it. Silence must cost runner minutes, never coverage.
set -euo pipefail

files=()
if [ "$#" -gt 0 ]; then
    files=("$@")
elif [ ! -t 0 ]; then
    while IFS= read -r line; do
        [ -n "$line" ] && files+=("$line")
    done
fi

# No file list at all → we could not determine the diff. Run everything.
if [ "${#files[@]}" -eq 0 ]; then
    printf 'code=true\ndocs=true\nui=true\ncaddy=true\n'
    exit 0
fi

code=false
docs=false
ui=false
caddy=false

for f in "${files[@]}"; do
    # --- manager-ui: the SPA only ---------------------------------------
    case "$f" in
        modules/llm/manager-ui/*) ui=true ;;
    esac

    # --- caddy: the single external entry point (#455). One malformed
    # directive and NO domain on the box answers, so any change to the
    # ingress gets validated against the real plugin-carrying binary.
    case "$f" in
        core/Caddy/*) caddy=true ;;
    esac

    # --- docs: ALWAYS. Not scope-selected, deliberately.
    #
    # This started as an allow-list (prose + cli/** + rzfz, because
    # test-docs-structure.sh runs generate-command-reference.py --check). That
    # list was WRONG within a day: #399 added a --network-mode flag to
    # core/llm/expected_models.py, which the command reference documents but the
    # list did not name, so the docs gate was skipped and main went red with a
    # stale commands.md.
    #
    # The lesson is that the generator's input set cannot be reliably enumerated
    # by hand — it introspects whatever the CLI happens to reach. And the docs
    # gate costs ~20-30 seconds against a 7.5-minute unit+api suite, so the trade
    # is lopsided: selecting it saves almost nothing and can turn main red.
    #
    # Scope selection stays where the money is (unit+api, manager-ui).
    docs=true

    # --- code: an IGNORE-list, not an allow-list. Anything not positively
    # recognised as documentation counts as code and runs the tests. A new
    # top-level directory must fail towards running the suite.
    #
    # #1855: markdown that a TEST READS is code. `tests/unit/consistency/
    # test_1447_doc_tables_name_live_profiles.py` asserts over `CLAUDE.md`,
    # `README.md`, `docs/enterprise/**` and — since #1851 — `wiki/**`: a change
    # to one of those files can turn the unit tier red all by itself. Ignoring
    # them here would skip exactly the tier that reads them, which is the same
    # shape of hole the docs allow-list had in #399 (see the note above), only
    # pointing the other way.
    #
    # The ordering matters: this case runs BEFORE the markdown ignore-list, so
    # a test-read document is code even though it ends in `.md`.
    case "$f" in
        CLAUDE.md|README.md|docs/enterprise/*|wiki/*) code=true ;;
        *.md|.gsd/*|docs/*|releases/*|security-run/*) : ;;
        *) code=true ;;
    esac
done

printf 'code=%s\ndocs=%s\nui=%s\ncaddy=%s\n' "$code" "$docs" "$ui" "$caddy"
