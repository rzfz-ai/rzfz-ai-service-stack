# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #2476 — raft storage on a volume; plain HTTP on the dedicated openbao-net
# network only (no host port), TLS terminates at Caddy for bao.<domain>.
# Static seal (decision record D9): the key file is written once by
# openbao-keygen into the openbao-keys volume, which this server mounts
# read-only. There is no `disable_mlock`: 2.6.3 no longer knows the field
# (measured with the pinned binary, it only logs "unknown or unsupported").
ui           = true
api_addr     = "http://openbao:8200"
cluster_addr = "http://openbao:8201"

storage "raft" {
  path    = "/openbao/data"
  node_id = "openbao-1"
}

seal "static" {
  current_key_id = "rzfz-1"
  current_key    = "file:///openbao/keys/seal.key"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
  # An explicit pin of the 2.x default (CHANGELOG GH-2125): rekeys need a
  # token. The unauthenticated generate-root endpoints stay off as well
  # (2.x default since CVE-2026-5807, GH-2912); do not re-enable them.
  disable_unauthed_rekey_endpoints = true
}

# Declarative: 2.x refuses audit devices created through the API
# (unsafe_allow_api_audit_creation defaults to false). openbao-bootstrap
# checks it is there before it sets anything up.
audit "file" "file" {
  description = "audit log to the container's stdout (docker logs openbao)"
  options {
    file_path = "stdout"
  }
}
