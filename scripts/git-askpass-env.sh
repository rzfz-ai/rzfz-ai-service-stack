#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2706: GIT_ASKPASS helper for a token typed in for one `rzfz upgrade` run.
# Holds no secret: it prints what razzfazz_git_origin (scripts/lib.sh) put
# into the environment of that one git command.
set -euo pipefail
case "${1:-}" in
    Username*) printf '%s\n' "${RZFZ_GIT_ASKPASS_USER:-}" ;;
    *) printf '%s\n' "${RZFZ_GIT_ASKPASS_PAT:-}" ;;
esac
