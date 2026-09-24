#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Backward-compatibility shim (#34 / #26 cli/ move; relocated to legacy/).
# The implementation lives in cli/security-check.sh; this delegates to the repo-root
# `rzfz` dispatcher so existing `legacy/razzfazz-security-check.sh …` invocations keep working
# unchanged. From legacy/ the dispatcher is one directory up, hence the
# `$(dirname "$0")/..` resolution.
exec "$(cd "$(dirname "$0")/.." && pwd)/rzfz" security-check "$@"
