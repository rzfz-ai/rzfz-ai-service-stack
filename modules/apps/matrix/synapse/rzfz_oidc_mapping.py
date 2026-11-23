# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""OIDC user mapping that refuses to link a case-variant login to an existing
Matrix account (#2578).

Synapse lowercases the localpart it derives from `preferred_username`, and with
`allow_existing_users: true` an unlinked login whose localpart matches an
existing account is attached to that account. Authentik usernames are
case-sensitive, so `AKAdmin` (a different person from `akadmin`) maps onto
`@akadmin` and would inherit it.

Synapse calls the mapping provider only for a subject that is not linked yet,
so every check here applies to NEW logins only. Users already linked to their
subject are never affected.

A login is refused (MappingException, rendered by Synapse as a login error)
when the localpart it maps to already belongs to an account and either

  * the username is not already in its lowercase form (a case variant), or
  * that account is already linked to a different subject of this provider.

A lowercase username that matches an existing, not yet linked account still
links (the grandfathering that allow_existing_users exists for). A username
that maps to no existing account registers as before.

If the account store cannot be read, a case-variant name is refused (fail
closed); a lowercase name behaves as it did before this check.
"""
from __future__ import annotations

import logging

from synapse.handlers.oidc import JinjaOidcMappingProvider
from synapse.handlers.sso import MappingException

logger = logging.getLogger(__name__)


class RzfzOidcMappingProvider(JinjaOidcMappingProvider):
    def __init__(self, config, module_api):
        super().__init__(config, module_api)
        self._module_api = module_api

    async def _existing_accounts(self, localpart: str) -> dict:
        api = self._module_api
        user_id = f"@{localpart}:{api.server_name}"
        return await api._store.get_users_by_id_case_insensitive(user_id)

    async def _linked_to_other_subject(self, user_id: str, subject: str) -> bool:
        ids = await self._module_api._store.get_external_ids_by_user(user_id)
        return any(ext != subject for _provider, ext in ids)

    async def map_user_attributes(self, userinfo, token, failures):
        attributes = await super().map_user_attributes(userinfo, token, failures)
        localpart = attributes.get("localpart")
        if not localpart or failures:
            return attributes

        username = userinfo.get("preferred_username")
        case_variant = isinstance(username, str) and username != username.lower()
        subject = self.get_remote_user_id(userinfo)
        try:
            existing = await self._existing_accounts(localpart)
            takeover = bool(existing) and (
                case_variant
                or any([await self._linked_to_other_subject(uid, subject)
                        for uid in existing]))
        except Exception:  # noqa: BLE001 - fail closed for case variants only
            logger.exception("OIDC case-variant check could not read the account store")
            takeover = case_variant

        if takeover:
            logger.warning(
                "OIDC login refused: %r maps onto an existing Matrix account "
                "that belongs to someone else (case-variant username)", username)
            raise MappingException(
                "This username is the same as an existing Matrix account except "
                "for letter case. Ask an administrator to rename it.")
        return attributes
