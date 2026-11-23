# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2559: read one field of the Mac worker's pins.json — sourced by
# bin/ollama-switch.sh and by the installer. No top-level `set` (a library).
#
# Why not python3: on a stock Mac /usr/bin/python3 is the Command Line Tools
# shim (it opens an install dialog instead of running), and the installer needs
# the uv pin BEFORE any Python exists. pins.json is written only by
# scripts/mac-worker/pin-ollama.py as `json.dumps(indent=2)`: a section header
# `  "<tool>": {` and one `    "<key>": "<value>"` line per field. That layout
# is the contract (tests/unit/scripts/test_2559_mac_packaging.py pins it).
# POSIX awk only — macOS ships BSD awk. Prints nothing when absent.
#
#   rzfz_pin <pins.json> <tool> <key>      e.g. rzfz_pin "$PINS" ollama sha256
rzfz_pin() {
    awk -v tool="$2" -v key="$3" '
        /^  "[^"]*": [{]/ { split($0, a, "\""); section = a[2]; next }
        section == tool && index($0, "    \"" key "\": \"") == 1 {
            split($0, a, "\""); print a[4]; exit
        }' "$1"
}
