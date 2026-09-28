# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# Wazuh certificate generator — with the cert tool BAKED IN (#1277)
# ==============================================================================
# Upstream's `wazuh/wazuh-certs-generator` image does not contain the tool it
# runs. Its /entrypoint.sh HEAD-checks two Wazuh S3 buckets at CONTAINER START
# and curls `wazuh-certs-tool.sh` from whichever answers 200:
#
#     PACKAGES_URL=https://packages.wazuh.com/4.8/
#     CERT_TOOL=wazuh-certs-tool.sh
#     curl -o $CERT_TOOL $PACKAGES_URL$CERT_TOOL -s
#
# Three consequences we do not accept:
#
#  1. An OFFLINE box (#184, NETWORK_MODE=offline) can never enable `wazuh`.
#     The one-shot needs egress on its FIRST run, and an offline package cannot
#     carry something that is fetched at run time. That is exactly the class
#     #184 WS2a removed for builds ("no runtime build"), one layer down.
#  2. A PROXY box (#181) hands that curl to the corporate proxy from inside the
#     container, without our proxy environment.
#  3. The script is UNPINNED. #1259 pinned our SAN format against the
#     validation rule of the 4.8 tool; if Wazuh changes the script under the
#     same URL, that rule changes with no diff in this repo.
#
# So we bake it at BUILD time and verify it. The build happens where every
# other custom image is built (install / package / upgrade), so the offline
# package carries the finished image.
#
# RE-PIN RULE: URL and SHA256 move together, and the guard
# tests/unit/consistency/test_1277_wazuh_certs_generator_offline.py fails when
# one of them is missing. Verified 2026-09-04: 36 313 bytes,
# sha256 acd4fb48c9646904069d81ed9c67f849cf910ab5eb127e051fe9db1dae068874.
# ==============================================================================
# 2026.09: base 0.0.2 -> 0.0.4 (0.0.3 of 2025-09 and 0.0.4 of 2025-12 lay between;
# we had stood on a 2024-03 build). VERIFIED against the base image on
# 2026-09-14 (DevBox, docker pull wazuh/wazuh-certs-generator:0.0.4 ->
# sha256:369b4d58509aab074b188596870c81584f7120e653d9ef83c591f0f785dcf325):
#   * `ls -l /wazuh-certs-tool.sh` -> No such file or directory. 0.0.4 still
#     does NOT ship the tool, so the pinned build-time download above is
#     necessary, not redundant.
#   * The base is Amazon Linux 2023 and ships NO `find` and NO `xargs`
#     (#2128: distribute-certs.sh's `find … -exec rm` died with exit 127 and
#     the whole wazuh profile could not be installed in 2026.09-rc1/rc2).
#     Present: cp chmod chown mkdir rm dirname readlink grep sed cat bash awk tr.
#     Every script that runs in this image must stay within that set;
#     tests/unit/consistency/test_2128_* runs publish() under exactly that PATH.
ARG WAZUH_CERTS_GENERATOR_VERSION=0.0.4
FROM wazuh/wazuh-certs-generator:${WAZUH_CERTS_GENERATOR_VERSION}

ARG WAZUH_CERTS_TOOL_URL=https://packages.wazuh.com/4.8/wazuh-certs-tool.sh
ARG WAZUH_CERTS_TOOL_SHA256=acd4fb48c9646904069d81ed9c67f849cf910ab5eb127e051fe9db1dae068874

# `sha256sum -c` FAILS the build on a mismatch — a silently different tool is
# the thing this file exists to prevent.
RUN curl -fsSL -o /wazuh-certs-tool.sh "${WAZUH_CERTS_TOOL_URL}" \
 && printf '%s  /wazuh-certs-tool.sh\n' "${WAZUH_CERTS_TOOL_SHA256}" | sha256sum -c - \
 && chmod 700 /wazuh-certs-tool.sh

COPY generate-certs.sh /usr/local/bin/generate-certs.sh
RUN chmod 755 /usr/local/bin/generate-certs.sh

# Not /entrypoint.sh: that one downloads. compose.yml calls this script
# explicitly (its idempotency wrapper), the ENTRYPOINT is the safety net for
# anyone who runs the image bare.
ENTRYPOINT ["/usr/local/bin/generate-certs.sh"]

LABEL ai.razzfazz.wazuh.certs-tool-sha256="${WAZUH_CERTS_TOOL_SHA256}"
