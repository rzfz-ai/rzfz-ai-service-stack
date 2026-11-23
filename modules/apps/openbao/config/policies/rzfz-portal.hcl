# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# rzfz-portal — the Configuration Portal's everyday OpenBao role (#2476).
# ONE source, written by the first-boot bootstrap (openbao-init.sh) and again
# by every `rzfz post-install --refresh` (scripts/render-openbao-identity.py),
# so a tightening here reaches installed boxes.
#
# Only what the Portal's features need: the technical users' AppRoles, on
# their own mount approle-apps/ and nowhere else (every role there carries
# rzfz-app, the fixed limits and Caddy's fixed address, all REQUIRED on every
# write, so a role is never recreated looser), and the deleted-user purge (KV
# users/ and purged/, the entity, the token accessors). Nothing on approle/,
# where the stack's roles are; no sys/auth, no sys/policies.
path "auth/approle-apps/role/+" {
  capabilities = ["create","update","delete"]
  required_parameters = ["token_policies","token_ttl","token_max_ttl","secret_id_ttl","secret_id_num_uses","token_bound_cidrs","secret_id_bound_cidrs"]
  allowed_parameters = {
    "token_policies" = ["rzfz-app"]
    "token_ttl" = ["5m"]
    "token_max_ttl" = ["5m"]
    "secret_id_ttl" = ["24h"]
    "secret_id_num_uses" = ["1000"]
    "token_bound_cidrs" = ["10.213.76.2/32"]
    "secret_id_bound_cidrs" = ["10.213.76.2/32"]
  }
}
path "auth/approle-apps/role/+/*" {
  capabilities = ["create","read","update"]
  allowed_parameters = { "secret_id_accessor" = [] }
}
path "secret/data/users/*" { capabilities = ["read"] }
path "secret/metadata/users/*" { capabilities = ["list","read","delete"] }
path "secret/data/purged/*" { capabilities = ["create","read","update"] }
path "secret/metadata/purged/*" { capabilities = ["list","read","delete"] }
path "identity/entity-alias/id" { capabilities = ["list"] }
path "identity/entity-alias/id/*" { capabilities = ["read","list"] }
path "identity/entity/id/*" {
  capabilities = ["read","update","delete"]
  allowed_parameters = { "disabled" = [true] }
}
path "auth/token/accessors" { capabilities = ["list","sudo"] }
path "auth/token/lookup-accessor" { capabilities = ["update"] }
path "auth/token/revoke-accessor" { capabilities = ["update"] }
