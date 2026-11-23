# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Which upstream, with which credential, for this caller (#2472).

One identity per request: a person's credential only ever serves that person,
a technical user's only that technical user; a per-user proxy is never lent
to a technical caller. A missing credential raises NotConnected, whose message
names what to connect and where — never a secret.

Keyed by the caller's principal ALONE (a person's OIDC sub, or `~svc-<name>`):
the gateway never passes a `legacy_slug`, so an undecided pre-2026.11 row
(NULL key, served by its slug only in the portal's no-token window, B6
`legacy_slug_for`) is never served through the gateway — with or without the
read-only Authentik token. A new surface has no ga.4 behaviour to keep.

Per-user and shared proxies (B9): `per_user_proxy:<id>` relays to the
PERSON's own running instance, looked up by their sub; `shared_proxy:<id>`
relays a TECHNICAL caller to the instance an admin provisioned for exactly
that `~svc-<name>` principal, and only for a catalogue entry whose tenancy is
`shared` (the cognee company brain). Neither ever falls back to another
principal's instance, and neither is served while the catalogue governance
marks the integration unavailable (a governance row that cannot be read is a
503, never an admission).

The company brain (a catalogue entry with `cognee_autoprovision: company`)
follows A1 D6 at call time (B9 fix round 1): a person is served only while
they are a CURRENT member of a `KB: …` scope group targeting the company
dataset, read from B8's cached single-flight directory listing (a directory
that cannot be read is a 503); a technical user only through an instance an
administrator provisioned for it, whose recorded grant (`_kb_grant`) names
the current company dataset, plus its `mcp-integration:<server id>` binding
(checked by the gateway before this module). cognee's own dataset ACL
(A14) stays the last line behind both.

`razzfazz-mcp-proxy` (design record §3, rulings I4 and R2-2): an instance of
that image family is reached through the gateway only when the catalogue
names B9's in-repo pinned build (PINNED_PROXY_IMAGES, a content-addressed
tag of modules/mcp-manager/mcp-proxy) AND the instance itself was launched
from it (its recorded `_image`). `razzfazz-mcp-proxy:latest`, a digest of
unknown origin, or an instance launched before B9 is never reached."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: The image family whose providers get gateway exposure only on the in-repo
#: pinned build (rulings I4, R2-2).
PROXY_IMAGE_FAMILY = ("razzfazz-mcp-proxy",)
#: B9's in-repo pinned build: the content-addressed tag of
#: modules/mcp-manager/mcp-proxy (Dockerfile header; the tag rule and its
#: agreement with compose, catalogue and manifest are held by
#: tests/unit/mcp-manager/test_2472_mcp_proxy_pinned_build.py).
PINNED_PROXY_IMAGES = frozenset({"razzfazz-mcp-proxy:google-5d25133e55b3"})
#: The user name the gateway presents to a relay sidecar (docling-egress's
#: basic_auth, modules/doc-processing/docling/egress/Caddyfile).
RELAY_USER = "mcp-gateway"

# One refresh per (principal, integration) at a time: the gateway runs threads
# (gunicorn gthread), and two concurrent refreshes of a rotating refresh token
# would leave one of them holding a token the provider has already retired.
_REFRESH_GUARD = threading.Lock()
_REFRESH_LOCKS: dict = {}


class NotConnected(Exception):
    pass


class NotConfigured(Exception):
    """The upstream's relay secret is not set: nothing is dialled (503)."""


class Unavailable(Exception):
    """What decides access could not be read (the directory, the governance
    row): nothing is decided and nothing is dialled (503, fail closed)."""


@dataclass(frozen=True)
class Upstream:
    url: str
    headers: dict = field(default_factory=dict)
    host: str | None = None
    #: #2554: the catalogue id of the caller's OAuth grant whose access token
    #: is injected, or None (a PAT, an API key, a relay secret, a proxy). Only
    #: then does an upstream rejection mean "the grant is dead".
    oauth_ref: str | None = None


def _hint(ref: str, mcp_domain: str) -> str:
    return f"Not connected: open https://{mcp_domain}/ and connect '{ref}' first."


def reconnect_message(ref: str, mcp_domain: str) -> str:
    """#2554: what a caller reads when the provider no longer accepts its
    OAuth grant (revoked by the user at the provider, or expired for good)."""
    return (f"'{ref}' authorization expired or was revoked; connect it again at "
            f"https://{mcp_domain}/.")


class GrantRejected(NotConnected):
    """#2554: the caller's OAuth grant is marked as rejected by the provider:
    nothing is dialled until the user authorizes again."""


def _refresh_lock(principal: str, ref: str) -> threading.Lock:
    with _REFRESH_GUARD:
        return _REFRESH_LOCKS.setdefault((principal, ref), threading.Lock())


#: The longest lifetime a provider's `expires_in` is believed for (a day).
MAX_TOKEN_LIFETIME = 86400


def _lifetime(value, default):
    """A provider's `expires_in` as whole seconds in 1..MAX_TOKEN_LIFETIME,
    or `default` when it is missing or not a number (a malformed answer is
    never a 500, and never an unbounded cache)."""
    if value is None or isinstance(value, bool):
        return default
    try:
        secs = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(secs, 1), MAX_TOKEN_LIFETIME)


def _pair_fingerprint(client_id: str, client_secret: str) -> str:
    """Which client pair a cached machine token was fetched with."""
    return hashlib.sha256(f"{client_id}\0{client_secret}".encode()).hexdigest()


def _expiring(cred_store, principal, ref) -> bool:
    # #2472 B22: the expiry is the store's (Postgres or OpenBao), never a row.
    exp = cred_store.oauth_expiry(principal, ref)
    return isinstance(exp, datetime) and exp <= datetime.now(timezone.utc) + timedelta(seconds=60)


def _fresh_access(caller, ref, creds, *, db, cred_store, catalog, oauth_flow):
    """The access token, refreshed when it expires within 60 s and a refresh
    token exists. A failed refresh is NotConnected, not a stale token."""
    access = creds.get("oauth_access")
    if not access:
        return creds.get("api_key") or creds.get("pat")
    if not _expiring(cred_store, caller.principal, ref):
        return access
    with _refresh_lock(caller.principal, ref):
        # Another thread may have refreshed while this one waited.
        creds = cred_store.get_decrypted(caller.principal, ref)
        if creds.get("oauth_access") and not _expiring(cred_store, caller.principal, ref):
            return creds["oauth_access"]
        refresh = creds.get("oauth_refresh")
        provider = catalog.oauth_provider(ref) if catalog else None
        if not (refresh and provider and oauth_flow):
            raise NotConnected(f"'{ref}' authorization expired; connect it again.")
        try:
            tok = oauth_flow.refresh(provider, refresh)
        except Exception as e:  # noqa: BLE001
            raise NotConnected(f"'{ref}' authorization could not be renewed; connect it again.") from e
        if not tok.get("access_token"):
            raise NotConnected(f"'{ref}' authorization could not be renewed; connect it again.")
        new_exp = None
        secs = _lifetime(tok.get("expires_in"), None)
        if secs:
            new_exp = datetime.now(timezone.utc) + timedelta(seconds=secs)
        from app.services.credential_store import CredentialStoreError
        try:
            cred_store.store_oauth_tokens(caller.principal, ref, access=tok["access_token"],
                                          refresh=tok.get("refresh_token") or refresh, expires_at=new_exp)
        except CredentialStoreError as e:
            # #2472 B22: a renewed grant that cannot be kept where it must
            # live is not used (the provider may have rotated the refresh
            # token: serving it would strand the connection silently).
            raise NotConnected(f"'{ref}' authorization could not be saved; try again later.") from e
        return tok["access_token"]


def _proxy_family(image: str) -> bool:
    name = str(image or "").rsplit("/", 1)[-1]
    return name.split("@", 1)[0].split(":", 1)[0] in PROXY_IMAGE_FAMILY


def _governance_refusal(db, ref: str, *, person=False, person_sub="", directory=None,
                        cfg=None) -> str:
    """'' while the catalogue governance admits `ref`, else the reason.
    `available: false` stops every principal at once. For a person, a
    `min_tier` above the base tier is checked (B9 fix rounds 2 and 3):
      * with the read-only directory configured, against the person's
        CURRENT tier — raising `min_tier` stops a running proxy on the next
        call; a person the directory does not list (NOT_CURRENT) is refused
        (403); a tier that cannot be read raises Unavailable (503);
      * in B6's no-token window (no directory: 2026.09-ga.4 parity), against
        the tier recorded at provisioning (`cfg["_tier"]`); an instance that
        recorded none is not re-checked, as on ga.4.
    A governance row that cannot be read raises Unavailable (the portal's
    permissive default is not an admission here)."""
    from app.services.tier import TIER_BASIC, VALID_MIN_TIERS, tier_allows
    try:
        row = db.get_governance(ref)
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"'{ref}' cannot be checked right now; try again shortly.") from e
    if not isinstance(row, dict):
        return ""
    if not bool(row.get("available", True)):
        return f"'{ref}' is not available on this box."
    min_tier = row.get("min_tier") if row.get("min_tier") in VALID_MIN_TIERS else TIER_BASIC
    if not person or min_tier == TIER_BASIC:
        return ""
    if directory is None:
        have = (cfg or {}).get("_tier")
        if have not in VALID_MIN_TIERS:
            return ""
    else:
        from app.services.gateway_identity import NOT_CURRENT
        ask = getattr(directory, "current_tier", None)
        have = ask(person_sub) if (ask and person_sub) else "unavailable"
        if have == NOT_CURRENT:
            return (f"'{ref}' requires the '{min_tier}' tier; this account is not a current "
                    "user of this box.")
        if have not in VALID_MIN_TIERS:
            raise Unavailable(f"'{ref}' cannot be checked right now (the directory is unavailable); "
                              "try again shortly.")
    if not tier_allows(have, min_tier):
        return f"'{ref}' requires the '{min_tier}' tier; your tier is '{have}'."
    return ""


def _company_dataset(db, ref: str) -> str:
    from app.services.governance import company_dataset
    try:
        return company_dataset(db, ref)
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"'{ref}' cannot be checked right now; try again shortly.") from e


def company_brain_refusal(db, directory, ref: str, owner: str, cfg=None) -> str:
    """A1 D6/D7 for one principal on the company brain, shared by the gateway
    and the agents' wildcard path (B9 fix round 2): '' when `owner` may read
    it now, else the reason; raises Unavailable when that cannot be decided.
    * a technical principal (`~svc-…`): the administrator's recorded grant
      (`cfg["_kb_grant"]`) must name the CURRENT company dataset;
    * a person (their sub): a CURRENT member of a `KB: …` scope targeting it;
    * no owner (an unattributed row): refused."""
    if str(owner or "").startswith("~"):
        if ((cfg or {}).get("_kb_grant") or {}).get("dataset") != _company_dataset(db, ref):
            return (f"'{ref}' has not been granted to this technical user for the current "
                    "company brain; an administrator must provision it again.")
        return ""
    if directory is None:
        # B6's no-token window: 2026.09-ga.4 parity, no KB check (fix round 3).
        return ""
    if not owner:
        return f"'{ref}' is not attributed to a person; start it again from the MCP portal."
    return _kb_scope_refusal(db, directory, ref, owner)


def instance_refusal(db, directory, integ: dict, owner: str, cfg=None) -> str:
    """The call-time checks for one instance on the agents' wildcard path
    (B9 fix round 3), the same as the gateway's: the governance (available;
    for a person, min_tier) and, for a company brain, company_brain_refusal.
    '' to relay, else the reason; raises Unavailable when undecidable."""
    ref = integ.get("id") or ""
    technical = str(owner or "").startswith("~")
    refusal = _governance_refusal(db, ref, person=not technical, person_sub=owner or "",
                                  directory=directory, cfg=cfg)
    if refusal:
        return refusal
    if integ.get("cognee_autoprovision") == "company":
        return company_brain_refusal(db, directory, ref, owner, cfg)
    return ""


def _kb_scope_refusal(db, directory, ref: str, sub: str) -> str:
    """A1 D6 for a person on the company brain: '' while they are a current
    member of a KB scope targeting its dataset; the reason otherwise; raises
    Unavailable when the directory cannot answer."""
    if directory is None:
        # B6's no-token window: 2026.09-ga.4 parity, no KB check (fix round 3).
        return ""
    dataset = _company_dataset(db, ref)
    ask = getattr(directory, "kb_dataset_state", None)
    state = ask(sub, dataset) if ask else "unavailable"
    if state == "member":
        return ""
    if state == "not_member":
        return (f"'{ref}' is readable only by members of its knowledge scope (a knowledge "
                f"scope group for the '{dataset}' dataset); ask an administrator.")
    raise Unavailable(f"'{ref}' cannot be checked right now (the directory is unavailable); "
                      "try again shortly.")


def _instance_upstream(db, catalog, ref, principal, mcp_domain, *, grant_dataset=None,
                       person=False, directory=None) -> Upstream:
    """The running instance of `ref` owned by exactly `principal` (a person's
    sub or `~svc-<name>`), with its route secret. Never another principal's:
    the lookup is keyed by the principal alone, and nothing else is tried."""
    if not principal:
        raise NotConnected(_hint(ref, mcp_domain))
    integ = (catalog.get(ref) if catalog else None) or {}
    inst = db.get_instance_by_mcp_and_user(ref, principal)
    if not inst or inst.get("state") != "running":
        raise NotConnected(_hint(ref, mcp_domain))
    cfg = inst.get("config") or {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg or "{}")
    if _proxy_family(integ.get("image", "")) and not (
            integ.get("image") in PINNED_PROXY_IMAGES and cfg.get("_image") == integ.get("image")):
        # Rulings I4/R2-2: only an instance launched from the in-repo pinned
        # build; an older instance must be started again first.
        raise NotConnected(f"'{ref}' is not available through the gateway on this proxy build; "
                           f"start it again from https://{mcp_domain}/ first.")
    refusal = _governance_refusal(db, ref, person=person, person_sub=principal,
                                  directory=directory, cfg=cfg)
    if refusal:
        raise NotConnected(refusal)
    if grant_dataset is not None and (cfg.get("_kb_grant") or {}).get("dataset") != grant_dataset:
        # A1 D6/D7: a technical user reads the company brain only through an
        # administrator's grant for the CURRENT company dataset.
        raise NotConnected(f"'{ref}' has not been granted to this technical user for the current "
                           "company brain; an administrator must provision it again.")
    secret = cfg.get("_route_secret") or ""
    if not secret or not integ.get("container_port"):
        raise NotConnected(_hint(ref, mcp_domain))
    port = int(integ["container_port"])
    host = f"localhost:{port}" if integ.get("rewrite_upstream_host") else None
    return Upstream(f"http://{inst['container_name']}:{port}{integ.get('mcp_path', '/mcp')}",
                    {"Authorization": f"Bearer {secret}"}, host)


def _client_credentials_upstream(server, caller, ref, creds, *, db, cred_store, catalog,
                                 oauth_flow) -> Upstream:
    """#2472: a TECHNICAL caller whose vaulted credential for `ref` is a
    client-credentials pair (an Atlassian service account, stored through the
    technical-user credential form) is served a machine token for that pair.
    The token is cached in the same principal's vault row until 60 s before
    it expires; one fetch per (principal, ref) at a time. A person never
    reaches this branch (the caller check is the caller's, not the row's).
    The upstream carries no oauth_ref: a rejected machine token is not a
    person's grant to mark — the next fetch after expiry takes a new one.
    The cache is served only while its stored fingerprint names the client
    pair in the vault NOW: a pair rotated between a fetch and its store (the
    rotation's own cache drop then runs first) never serves the old token."""
    def cached():
        # #2472 B22: a technical user's machine token is an OAuth token and
        # lives in OpenBao when the profile is on.
        exp = cred_store.oauth_expiry(caller.principal, ref)
        live = isinstance(exp, datetime) and exp > datetime.now(timezone.utc) + timedelta(seconds=60)
        same_pair = creds.get("oauth_cc_fp") == _pair_fingerprint(
            creds.get("client_id") or "", creds.get("client_secret") or "")
        return creds.get("oauth_access") if live and same_pair else None

    token = cached()
    if not token:
        with _refresh_lock(caller.principal, ref):
            # Another thread may have fetched one while this one waited.
            creds = cred_store.get_decrypted(caller.principal, ref)
            if not (creds.get("client_id") and creds.get("client_secret")):
                raise NotConnected(f"'{ref}' service account has no client credentials stored.")
            token = cached()
            if not token:
                provider = catalog.oauth_provider(ref) if catalog else None
                if not (provider and oauth_flow):
                    raise NotConnected(f"'{ref}' has no client-credentials provider configured.")
                try:
                    got = oauth_flow.client_credentials(provider, creds["client_id"],
                                                        creds["client_secret"])
                except Exception as e:  # noqa: BLE001
                    raise NotConnected(f"'{ref}' service account could not sign in.") from e
                token = (got or {}).get("access_token")
                if not token:
                    raise NotConnected(f"'{ref}' service account could not sign in.")
                lifetime = _lifetime((got or {}).get("expires_in"), 300)
                # The token first, then the pair it belongs to: a reader
                # between the two sees a mismatch and waits for this lock.
                from app.services.credential_store import CredentialStoreError
                try:
                    cred_store.store_oauth_tokens(
                        caller.principal, ref, access=token,
                        expires_at=datetime.now(timezone.utc) + timedelta(seconds=lifetime))
                except CredentialStoreError as e:
                    # #2472 B22: OpenBao unreachable. The machine token is
                    # fetched for this call only and cached nowhere else.
                    logger.warning("machine token for %s/%s not cached: %s", caller.principal, ref, e)
                else:
                    cred_store.store_machine_fingerprint(
                        caller.principal, ref,
                        _pair_fingerprint(creds["client_id"], creds["client_secret"]))
    return Upstream(server.url, {"Authorization": server.auth_header.format(token=token)})


def _reauth_required(cred_store, principal: str, ref: str) -> bool:
    """Only an explicit True counts: a store without the mark (an older
    double, a failed read) never refuses on its own."""
    ask = getattr(cred_store, "reauth_required", None)
    return callable(ask) and ask(principal, ref) is True


def upstream_for(server, caller, *, db, cred_store, catalog, oauth_flow=None, mcp_domain="",
                 directory=None) -> Upstream:
    kind, ref = server.credential_kind, server.credential_ref
    if kind == "none":
        env = getattr(server, "relay_secret_env", "")
        if not env:
            return Upstream(server.url, {})
        # The relay secret of a stateless upstream's sidecar (Docling's
        # docling-egress admits only this header, design record §7). It is
        # the gateway's, never the caller's; empty = never dial without it.
        secret = (os.environ.get(env) or "").strip()
        if not secret:
            raise NotConfigured(f"'{server.id}' is not configured on this box yet.")
        # HTTP Basic, so the sidecar checks it with basic_auth against a bcrypt
        # hash (a comparison that leaks nothing about a partial guess).
        cred = base64.b64encode(f"{RELAY_USER}:{secret}".encode()).decode()
        return Upstream(server.url, {"Authorization": f"Basic {cred}"})
    if kind == "per_user_proxy":
        if caller.kind != "user":
            raise NotConnected(f"'{server.id}' holds personal data and is never lent to a technical user.")
        integ = (catalog.get(ref) if catalog else None) or {}
        if integ.get("cognee_autoprovision") == "company" and caller.principal:
            refusal = _kb_scope_refusal(db, directory, ref, caller.sub or caller.principal)
            if refusal:
                raise NotConnected(refusal)
        return _instance_upstream(db, catalog, ref, caller.principal, mcp_domain,
                                  person=True, directory=directory)
    if kind == "shared_proxy":
        # #2472: the technical users' entry for a SHARED integration. Only a
        # technical caller, only a catalogue entry that is shared, and only
        # the instance provisioned for this very `~svc-<name>` principal.
        if caller.kind != "technical" or not str(caller.principal).startswith("~svc-"):
            raise NotConnected(f"'{server.id}' is the technical users' entry; use your own tool instead.")
        integ = (catalog.get(ref) if catalog else None) or {}
        if integ.get("tenancy") != "shared":
            raise NotConnected(f"'{ref}' is not a shared integration.")
        grant = _company_dataset(db, ref) if integ.get("cognee_autoprovision") == "company" else None
        return _instance_upstream(db, catalog, ref, caller.principal, mcp_domain, grant_dataset=grant)
    # catalog:<mcp_id>
    if caller.kind == "technical" and not server.technical:
        raise NotConnected(f"'{server.id}' does not accept technical users.")
    if not caller.principal:
        raise NotConnected(_hint(ref, mcp_domain))
    creds = cred_store.get_decrypted(caller.principal, ref)
    if caller.kind == "technical" and creds.get("client_id") and creds.get("client_secret"):
        return _client_credentials_upstream(server, caller, ref, creds, db=db,
                                            cred_store=cred_store, catalog=catalog,
                                            oauth_flow=oauth_flow)
    oauth = bool(creds.get("oauth_access"))
    if oauth and _reauth_required(cred_store, caller.principal, ref):
        raise GrantRejected(reconnect_message(ref, mcp_domain))
    token = _fresh_access(caller, ref, creds, db=db, cred_store=cred_store,
                          catalog=catalog, oauth_flow=oauth_flow)
    if not token:
        raise NotConnected(_hint(ref, mcp_domain))
    return Upstream(server.url, {"Authorization": server.auth_header.format(token=token)},
                    oauth_ref=ref if oauth else None)
