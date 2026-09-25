# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Read the fingerprint of Caddy's live root CA (#419 P0).

A joining node pins this so first contact on a `TLS_MODE=internal` fleet — the
default — is VERIFIED rather than trusted. Without it the node's only options are
to disable TLS verification or to trust whatever answers the advertised address,
and a box that will subsequently accept code rollouts from that answerer must not
adopt it on faith.

Deliberately a standalone, single-job module with no manager imports:

* The manager container does NOT mount `caddy-data`. Adding that mount to reach
  the root would couple two services and trip the security-review wire on new
  container mounts, for a value the host can simply pass in. The host supplies
  the path via ``LLM_MANAGER_CA_PEM``.
* Best-effort by contract. A master whose Caddy has not yet issued a root must
  still mint enrollment tokens; the absence of a pin degrades to an unverified
  join (which the CLI warns about), never to a failed one.

stdlib only — no cryptography dependency for what is a base64 decode and a hash.
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Optional

# The FIRST certificate in the file. `caddy-ca.pem` may carry a chain; the root
# leads and is what a node pins — pinning an intermediate would break on renewal.
_PEM = re.compile(
    rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


#: The ONE default path to the master's CA (#419, #499).
#
# Caddy writes its internal root here when TLS_MODE=internal, and
# modules/llm/manager/compose.yml mounts caddy_data read-only so the manager can
# read it. Both the compose default and every in-app fallback MUST be this
# value — `tests/unit/llm-manager/test_ca_pem_single_default.py` fails if they
# drift apart.
#
# Why this constant exists: #490 set the compose default while `enroll.py` kept
# an older literal (`/certs/caddy-ca.pem`) that nothing mounts. In-container the
# compose value always wins, so it was latent — but running the manager OUTSIDE
# compose (a developer, a harness, any future deployment) resolved the pin to a
# path that does not exist, and an inert pin is precisely the defect #419 fixed.
DEFAULT_CA_PEM = "/caddy-data/caddy/pki/authorities/local/root.crt"


def read_ca_fingerprint(pem_path: str) -> Optional[str]:
    """``'sha256:<64 hex>'`` over the DER body of the first cert, or None.

    Returns None — never raises — when the file is absent, unreadable, contains
    no complete certificate, or fails to base64-decode. Every one of those is a
    legitimate state on a box mid-provision, and an exception here would take
    down token minting for a value that is optional by design.

    The digest is over the DER encoding, which is what a TLS peer sees and what
    ``openssl x509 -outform DER | sha256sum`` produces — so the node can compute
    the same value from the handshake without access to this file.
    """
    try:
        with open(pem_path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None

    m = _PEM.search(blob)
    if not m:
        return None  # absent, or a truncated PEM with no END line

    try:
        der = base64.b64decode(re.sub(rb"\s+", b"", m.group(1)), validate=True)
    except Exception:
        return None
    if not der:
        return None

    return "sha256:" + hashlib.sha256(der).hexdigest()
