#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #1277: upstream's /entrypoint.sh, minus the run-time download.
#
# Everything below the download is REPRODUCED FROM IT, deliberately verbatim in
# behaviour (Wazuh Docker, GPLv2 — we do not vendor their script, we bake the
# published tool and call it the same way):
#
#   cp /config/certs.yml /config.yml
#   source /wazuh-certs-tool.sh -A
#   … move certificates, set modes, chown indexer/dashboard (1000) and
#     manager (999), duplicate the root CA for the manager …
#
# The tool itself is baked and sha256-verified at build time (see Dockerfile).
# `source` is load-bearing: the tool defines cert_parseYaml, which the node
# loop below uses — the same reason upstream sources instead of executing.
set -euo pipefail

CERT_TOOL=/wazuh-certs-tool.sh
if [ ! -x "$CERT_TOOL" ]; then
    echo "ERROR(#1277): $CERT_TOOL is missing from this image. It is baked at" >&2
    echo "  build time; a box must not fetch it at run time (offline/#184)." >&2
    exit 1
fi

# rev-B, second box finding (0.79, empty volume): the tool derives BOTH its
# config path and its output directory from `$0` —
#   base_path="$(dirname "$(readlink -f "$0")")"      (wazuh-certs-tool.sh:12)
#   readonly config_file="${base_path}/config.yml"    (:14)
# When it is SOURCED, `$0` is the SOURCING script. Upstream's entrypoint lives
# at `/`, so their base_path is `/` and `cp /config/certs.yml /config.yml` is
# exactly right for them. Ours lives at /usr/local/bin, so the tool looked for
# /usr/local/bin/config.yml, found nothing and exited with
# `ERROR: No configuration file found.` — the failure the `-u` crash had been
# masking. Compute the same path the tool will, instead of hard-coding theirs.
BASE_PATH="$(dirname "$(readlink -f "$0")")"
cp /config/certs.yml "${BASE_PATH}/config.yml"

# rev-B (#1277, box proof on 0.79 with an EMPTY certificate volume): the
# upstream tool carries NO `set` line of its own and relies on `$1` being unset
# when its `common_logger()` is called without arguments
# (`if [ -n "${1}" ]` at wazuh-certs-tool.sh:738). `source` runs it in THIS
# shell, so our `-u` applies to it and the first argument-less log line kills
# the process:
#
#   /wazuh-certs-tool.sh: line 738: 1: unbound variable
#   ERROR(#855): /certificates/root-ca.pem missing after generation — refusing
#     to distribute an incomplete PKI.
#
# The #855 entrypoint guard did its job and refused; the result was that every
# FRESH wazuh enable died. An existing PKI masked it: `root-ca.pem exists -
# keeping` returns before the source.
#
# rev-B2, measured with `bash -x` on 0.79: `-u` alone is not enough. The tool
# is not `-e`-clean either. It probes whether a node NAME is an IP with
#
#     isIP=$(echo "${ip}" | grep -P '^[0-9]{1,3}(\.[0-9]{1,3}){3}$')
#
# and a hostname (`wazuh.indexer`) makes that grep exit 1 — under `set -e` the
# assignment inherits that status and the process dies there, silently, with
# the PKI half-built. Upstream never sees any of this because their entrypoint
# runs under plain `bash` with no `set` line at all.
#
# So the FOREIGN tool runs with the shell options it was written for, and ours
# come straight back afterwards — the #382 house standard applies to the code
# we wrote, not to a GPLv2 script we only call. The safety net for the tool is
# the #855 check below (and in the ENTRYPOINT): if the PKI is not complete
# afterwards, we refuse to distribute it. That check is what makes lifting
# `-e` here safe, and it is the check that caught all three failures.
# rev-B3, third box measurement: `cert_parseYaml` is a FOREIGN function too —
# it reads `$2` (a prefix argument upstream never passes either), so calling it
# after restoring our options died with
# `/wazuh-certs-tool.sh: line 214: $2: unbound variable` AFTER the certificates
# had already been generated. The lifted region therefore ends after the LAST
# call into the tool, not after the source.
# shellcheck source=/dev/null
set +euo pipefail
source "$CERT_TOOL" -A
nodes_server=$( cert_parseYaml "${BASE_PATH}/config.yml" | grep -E "nodes[_]+server[_]+[0-9]+=" | sed -e 's/nodes__server__[0-9]=//' | sed 's/"//g' )
set -euo pipefail

node_names=($nodes_server)

echo "Moving created certificates to the destination directory"
# Same `$0` story for the OUTPUT: the tool writes to ${base_path}/wazuh-certificates
# (:617), i.e. /usr/local/bin/wazuh-certificates for us, not /wazuh-certificates.
cp "${BASE_PATH}"/wazuh-certificates/* /certificates/
echo "Changing certificate permissions"
chmod -R 500 /certificates
chmod -R 400 /certificates/*
echo "Setting UID indexer and dashboard"
chown 1000:1000 /certificates/*
echo "Setting UID for wazuh manager and worker"
cp /certificates/root-ca.pem /certificates/root-ca-manager.pem
cp /certificates/root-ca.key /certificates/root-ca-manager.key
chown 999:999 /certificates/root-ca-manager.pem
chown 999:999 /certificates/root-ca-manager.key

for i in "${node_names[@]}"; do
    chown 999:999 "/certificates/${i}.pem"
    chown 999:999 "/certificates/${i}-key.pem"
done
