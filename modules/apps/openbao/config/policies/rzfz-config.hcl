# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz-config — the host's management role (#2476). ONE source, written by the
# first-boot bootstrap (openbao-init.sh) and again by every `rzfz post-install
# --refresh` (scripts/render-openbao-identity.py), so a change here reaches
# installed boxes. It can write any policy, so it is root-equivalent; the
# Configuration Portal never uses it (rzfz-portal).
#
# sys/audit* needs "sudo": OpenBao lists sys/audit and sys/audit/* as
# root-protected paths (vault/logical_system.go PathsSpecial.Root, v2.6.3), so
# read/list alone answers 403. Read and list only: audit devices are
# declarative (openbao.hcl) and the role never creates or deletes one.
# sys/storage/raft/snapshot-force restores a backup (scripts/openbao-restore.sh;
# measured on 2.6.3 to need no sudo).
path "sys/auth*" { capabilities = ["create","read","update","delete","list","sudo"] }
path "sys/policies/acl/*" { capabilities = ["create","read","update","delete","list"] }
path "auth/*" { capabilities = ["create","read","update","delete","list"] }
path "identity/*" { capabilities = ["create","read","update","delete","list"] }
path "sys/audit*" { capabilities = ["read","list","sudo"] }
path "sys/storage/raft/snapshot-force" { capabilities = ["update"] }
