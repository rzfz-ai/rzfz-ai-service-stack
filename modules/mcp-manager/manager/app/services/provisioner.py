# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Provisioning engine for per-user MCP proxies (#36).

Mirrors agent-manager's Provisioner. For a given (user, mcp_id) it:
  1. decrypts ONLY that user's credentials (via CredentialStore)
  2. renders the catalog env_map (injecting creds + user identity + settings)
  3. launches a per-user proxy container holding those creds in env/tmpfs
     (no docker socket, no host-mounted disk for creds)
  4. records the instance. #619: NO route registration and NO boot
     reconcile anymore — the static *.MCP_DOMAIN wildcard proxies every
     instance host to the manager's bearer proxy (wildcard_proxy.py),
     which enforces the per-instance bearer from the DB row per request.

SECURITY: each proxy receives ONLY its owner's creds. Decrypted plaintext lives
only in this process during launch and in the proxy container's memory. Nothing
here logs credential values.
"""

import json
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

# #61 NEW-1: SINGLE source of truth for the user slug, shared with agent-manager.
# Both services bind-mount core/common/razzfazz_common at /app/common and set
# PYTHONPATH=/app/common. The api blueprint re-exports from here. The two
# services' derivations MUST agree byte-for-byte (``<=14>-<6hex>``, fits
# user_slug VARCHAR(24)) or agent-manager's /internal/agent-wiring/<slug> lookup
# misses these instances and the per-proxy bearer never reaches the agent.
from razzfazz_common.user_slug import make_user_slug  # noqa: F401
# The cognee account addresses, shared with the role reconcile, which must
# recognise these accounts and leave them alone.
from razzfazz_common.cognee_accounts import slug_account_email, sub_account_email

logger = logging.getLogger(__name__)

# #36 follow-up: catalog `reach_backend` -> the SCOPED docker network(s) a proxy
# joins (besides mcp-network) so it can dial a shared backend by container name.
# Only backends that live on a non-`default`, non-`mcp-network` net qualify — so
# the proxy never gains a path to mcp-manager:5000/internal.
# #36 BLOCKER-1 fix: cognee proxies join a DEDICATED `cognee-backend` net (cognee
# is dual-homed on it) — NOT the shared `coding-agents` bridge. Sandboxes are on
# `coding-agents` and CANNOT reach a per-user proxy by container name any more
# (defence-in-depth on top of the per-instance auth-shim bearer gate).
_BACKEND_NETWORKS = {
    "cognee": ["cognee-backend"],
}

#: #3453: the box settings a catalogue `env_map` may hand a proxy as
#: {{policy:<KEY>}}, read from mcp-manager's own environment at launch (an
#: unset key renders empty, which the proxy reads as its default). Only these:
#: mcp-manager's environment also holds its secrets, which never reach a
#: per-user container.
POLICY_ENV_KEYS = frozenset({
    "GOOGLE_MCP_MODE", "GOOGLE_MCP_ALLOW_SEND", "GOOGLE_MCP_SEND_DELETE_AGENTS", "MCP_DOMAIN",
})


#: B9 fix round 1 (I2): an OAuth proxy holds the access token it was started
#: with (the per-user proxy injects one user's credential at start, design
#: record §3). A launch therefore renews the token when less than this much
#: of its lifetime is left, so a started proxy has (about) a full token
#: lifetime ahead of it.
LAUNCH_MIN_TOKEN_LIFETIME = timedelta(minutes=45)
#: A running OAuth proxy whose token expires within this margin (or whose
#: expiry was never recorded) counts as stale: "Start" restarts it.
STALE_TOKEN_MARGIN = timedelta(minutes=2)


#: B9 fix round 2: one lock per (owner key, integration) around launch, stop
#: and delete, so a double click, or a Start racing a reconnect's restart,
#: cannot interleave two container lifecycles for the same instance
#: (mcp-manager serves on the threads of ONE process, gunicorn.conf.py).
_INSTANCE_LOCKS: dict = {}
_INSTANCE_LOCKS_GUARD = threading.Lock()


def _instance_lock(owner_key: str, mcp_id: str) -> threading.RLock:
    with _INSTANCE_LOCKS_GUARD:
        return _INSTANCE_LOCKS.setdefault((owner_key or "", mcp_id), threading.RLock())


def _row_lock_key(row: dict) -> str:
    """The one lock key of an instance row (B9 fix round 4): its owner_sub,
    else, for an undecided pre-2026.11 row, its historical user_id. launch,
    stop and delete all lock on it, so a Start and a Stop of the same legacy
    row cannot interleave."""
    return str(row.get("owner_sub") or row.get("user_id") or "")


#: cognee token renewal (see MCPProvisioner.renew_cognee_token).
RENEW_PATH = "/instance-auth/cognee-token"
RENEW_ENV_URL = "RZFZ_COGNEE_RENEW_URL"
RENEW_ENV_SECRET = "RZFZ_COGNEE_RENEW_SECRET"
RENEW_ENV_INSTANCE = "RZFZ_INSTANCE_ID"
#: At most one renewal per instance in this interval (the shim renews about
#: five minutes before expiry and once on a 401).
RENEW_MIN_INTERVAL = timedelta(seconds=10)
_RENEW_LAST: dict = {}
_RENEW_GUARD = threading.Lock()


class _GrantUncheckable(Exception):
    """The company dataset could not be read while deciding whether a running
    technical instance's grant is current (B9 fix round 4, M4)."""



def launch_failure_reason(exc: Exception, image: str) -> str:
    """#3427: the reason a failed launch records and shows. A missing image
    says what to do about it; anything else keeps the error's own text."""
    text = str(exc)
    if type(exc).__name__ == "ImageNotFound" or "No such image" in text:
        return (f"the image {image} is missing on this box; an administrator runs "
                f"'rzfz post-install --refresh', then start it again")
    return text

class MCPProvisioner:
    def __init__(self, db, docker_client, caddy_client, catalog, cred_store, config,
                 oauth_flow=None):
        self._db = db
        self._docker = docker_client
        self._caddy = caddy_client
        self._catalog = catalog
        self._creds = cred_store
        self._config = config
        #: app.services.oauth.OAuthFlow (create_app sets it); None = no renewal.
        self.oauth_flow = oauth_flow

    # ------------------------------------------------------------------
    # OAuth tokens at launch (B9 fix round 1, I2)
    # ------------------------------------------------------------------

    def _launch_oauth_token(self, user_id: str, mcp_id: str, decrypted: dict) -> tuple:
        """(decrypted, expires_at, error). Renews the caller's OWN access token
        (vault key = `user_id`) with the stored refresh token when less than
        LAUNCH_MIN_TOKEN_LIFETIME is left or its expiry is unknown, under the
        gateway's per-(principal, integration) refresh lock. A token that is
        still valid is used when renewal is impossible; an expired one that
        cannot be renewed refuses the launch with a recovery that works."""
        from app.services.gateway_credentials import _refresh_lock
        now = datetime.now(timezone.utc)

        def _expiry():
            # #2472 B22: the store's expiry (Postgres or OpenBao)
            exp = self._creds.oauth_expiry(user_id, mcp_id)
            if not isinstance(exp, datetime):
                return None
            return exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)

        exp = _expiry()
        if exp is not None and exp - now > LAUNCH_MIN_TOKEN_LIFETIME:
            return decrypted, exp, ""
        with _refresh_lock(user_id, mcp_id):
            # Another thread (the gateway, another launch) may have renewed it.
            exp = _expiry()
            if exp is not None and exp - now > LAUNCH_MIN_TOKEN_LIFETIME:
                return self._creds.get_decrypted(user_id, mcp_id), exp, ""
            refresh = decrypted.get("oauth_refresh")
            provider = self._catalog.oauth_provider(mcp_id)
            if refresh and provider and self.oauth_flow is not None:
                try:
                    tok = self.oauth_flow.refresh(provider, refresh)
                except Exception as e:  # noqa: BLE001 — never log a token
                    logger.warning("launch: %s token renewal failed for %s: %s",
                                   mcp_id, user_id, type(e).__name__)
                    tok = {}
                if tok.get("access_token"):
                    new_exp = (now + timedelta(seconds=int(tok["expires_in"]))
                               if tok.get("expires_in") else None)
                    from app.services.credential_store import CredentialStoreError
                    try:
                        self._creds.store_oauth_tokens(
                            user_id, mcp_id, access=tok["access_token"],
                            refresh=tok.get("refresh_token") or refresh, expires_at=new_exp)
                    except CredentialStoreError as e:
                        # #2472 B22: a renewal that cannot be kept is not used.
                        logger.error("launch: renewed %s token for %s not stored: %s", mcp_id, user_id, e)
                        return None, None, ("Your connection could not be saved right now. "
                                            "Try again in a few minutes.")
                    out = dict(decrypted, oauth_access=tok["access_token"])
                    return out, new_exp, ""
        if exp is None or exp > now:
            return decrypted, exp, ""
        name = (self._catalog.get(mcp_id) or {}).get("display_name", mcp_id)
        return None, None, (f"Your {name} authorization has expired and could not be renewed. "
                            f"Connect {name} again in the MCP portal; that restarts it.")

    @staticmethod
    def _token_stale(instance: dict) -> bool:
        """A running OAuth proxy started with a token that has expired (or
        expires within STALE_TOKEN_MARGIN), or whose expiry was not recorded
        (started before B9's fix round 1)."""
        cfg = instance.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg or "{}")
            except ValueError:
                cfg = {}
        raw = cfg.get("_token_expires_at") if isinstance(cfg, dict) else None
        if not raw:
            return True
        try:
            exp = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= datetime.now(timezone.utc) + STALE_TOKEN_MARGIN

    # ------------------------------------------------------------------

    def _render_env(self, integ: dict, user_slug: str, user_email: str,
                    decrypted_creds: dict, settings: dict,
                    extra: dict | None = None, user_id: str = "") -> dict:
        """Resolve the catalog env_map placeholders to concrete values.

        Placeholders:
          {{cred:<key>}}          -> decrypted_creds[<key>]
          {{oauth_access_token}}  -> decrypted_creds['oauth_access']
          {{user_slug}}           -> user_slug
          {{user_email}}          -> user_email
          {{user_id}}             -> the owner's key (a person's sub; #3453 audit)
          {{policy:<KEY>}}        -> mcp-manager's env KEY, POLICY_ENV_KEYS only
          {{setting:<key>}}       -> settings[<key>]
          {{cognee_user_api_key}} -> extra['cognee_user_api_key'] (auto-provisioned)
          {{cognee_company_dataset}} -> extra['cognee_company_dataset']
        Unresolved placeholders resolve to '' (never left literal in the env).
        """
        extra = extra or {}
        env_map = integ.get("env_map", {}) or {}
        out: dict[str, str] = {}
        for var, template in env_map.items():
            val = str(template)
            # {{cred:<key>}}
            for m in set(re.findall(r"\{\{cred:([a-zA-Z0-9_]+)\}\}", val)):
                val = val.replace(f"{{{{cred:{m}}}}}", str(decrypted_creds.get(m, "")))
            # {{setting:<key>}}
            for m in set(re.findall(r"\{\{setting:([a-zA-Z0-9_]+)\}\}", val)):
                val = val.replace(f"{{{{setting:{m}}}}}", str(settings.get(m, "")))
            val = val.replace("{{oauth_access_token}}",
                              str(decrypted_creds.get("oauth_access", "")))
            val = val.replace("{{user_slug}}", user_slug)
            val = val.replace("{{user_email}}", user_email or "")
            val = val.replace("{{user_id}}", user_id or "")
            # {{policy:<KEY>}} (#3453): an allow-listed box setting; any other
            # key renders empty, never a value of mcp-manager's environment.
            for m in set(re.findall(r"\{\{policy:([A-Za-z0-9_]+)\}\}", val)):
                got = os.environ.get(m, "") if m in POLICY_ENV_KEYS else ""
                val = val.replace(f"{{{{policy:{m}}}}}", got)
            val = val.replace("{{cognee_user_api_key}}",
                              str(extra.get("cognee_user_api_key", "")))
            val = val.replace("{{cognee_company_dataset}}",
                              str(extra.get("cognee_company_dataset", "")))
            out[var] = val
        return out

    # ------------------------------------------------------------------

    def _cognee_user_email(self, user_slug: str, user_email: str = "") -> str:
        """LEGACY (2026.09, slug-derived; #2572): used only in the no-token
        legacy window. The per-user cognee account email is ALWAYS the
        deterministic slug-based address under the box's real MAIN_DOMAIN
        (cognee's email validator rejects reserved/special-use domains like
        `.local`, so we namespace under a real domain the box owns).

        #2574 item 4: `user_email` is accepted for the old signature and
        ignored. The mapped path (`cognee-<legacy_slug>@domain`) never used
        an email, and a legacy account must resolve to the same address
        whether or not a caller happens to pass one."""
        return slug_account_email(user_slug, self._domain())

    def _cognee_user_password(self, user_slug: str) -> str:
        """LEGACY (2026.09, slug-derived; #2572): the password of a legacy
        account (HMAC of the slug under the manager master key). Never logged,
        never returned to the user."""
        return self._hmac_password(user_slug)

    def _domain(self) -> str:
        import os as _os
        return (self._config.get("MAIN_DOMAIN")
                or _os.environ.get("MAIN_DOMAIN", "example.com"))

    def _hmac_password(self, key: str) -> str:
        import hashlib
        import hmac as _hmac
        secret = (self._config.get("MCP_MANAGER_SECRET_KEY")
                  or __import__("os").environ.get("MCP_MANAGER_SECRET_KEY"))
        if not secret:  # the app refuses to start without it (crypto.py)
            raise RuntimeError("MCP_MANAGER_SECRET_KEY is not set")
        if isinstance(secret, str):
            secret = secret.encode()
        return "Cg1!" + _hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()[:32]

    def cognee_account(self, sub: str, user_slug: str, user_email: str = "",
                       legacy_slug: str | None = None) -> tuple:
        """(email, password, error); see cognee_login."""
        email, key, err = self.cognee_login(sub, user_slug, user_email, legacy_slug)
        if err:
            return None, None, err
        return email, self._hmac_password(key), ""

    def cognee_login(self, sub: str, user_slug: str, user_email: str = "",
                     legacy_slug: str | None = None) -> tuple:
        """(email, key, error) of the caller's cognee account (#2572). The
        password is HMAC(master key, key); the key is not a secret.

        * No read-only token (`legacy_slug` given): the 2026.09 slug-derived
          account, like the rest of the legacy window.
        * A legacy account the directory-confirmed instance run MAPPED to this
          sub: that account (cognee has no measured rename, so it is kept).
        * An undecided legacy account under the caller's slug: refused until
          the run decides it — it may be someone else's.
        * Otherwise the sub-derived account: cognee-<sha256(sub)[:16]>@domain,
          password HMAC(master key, sub). Never the slug, never the email.
        """
        if legacy_slug:
            # The window serves the slug account, but never one a run already
            # decided against this caller: a quarantine (or a mapping to
            # another sub) holds even if the token is emptied later (B6 fix
            # round 4). With no sub, any decided account is refused.
            decided = self._db.cognee_account_state(user_slug)
            if decided and (decided[0] == "quarantined"
                            or (decided[1] and decided[1] != sub)):
                logger.warning("cognee: slug account %s is %s and not the caller's; "
                               "refused in the no-token window", user_slug, decided[0])
                return None, None, ("Your personal memory cannot be provisioned: the "
                                    "memory under this username belongs to another "
                                    "account. Ask an administrator.")
            return self._cognee_user_email(user_slug, user_email), user_slug, ""
        if not sub:
            return None, None, "No Authentik uid on this request; cannot provision memory."
        mapped = self._db.cognee_account_for_sub(sub)
        if mapped:
            return slug_account_email(mapped, self._domain()), mapped, ""
        if self._db.cognee_account_pending(user_slug):
            return None, None, ("Personal memory saved before the upgrade is not verified "
                                "yet; ask an administrator.")
        return sub_account_email(sub, self._domain()), sub, ""

    def _provision_cognee_identity(self, integ: dict, user_slug: str,
                                   username: str, user_email: str,
                                   groups: list, sub: str = "",
                                   legacy_slug: str | None = None,
                                   technical: bool = False) -> tuple:
        """Ensure the per-user cognee identity + key (fail-closed). Returns
        (ok, extra_dict, error_msg).

        `technical` (B9 fix round 1, A1 D6/D7): an admin provisioning the
        company brain for a technical user is the explicit grant. The
        technical user's OWN cognee account gets read on the company dataset
        (never a KB: group, never a person's account); any failure refuses
        the launch. The grant is recorded as `_kb_grant` and the gateway
        serves the instance only while it names the current dataset."""
        import os as _os
        from app.services import cognee_identity
        base = _os.environ.get("COGNEE_BASE_URL", "http://cognee:8000")
        admin_email = _os.environ.get("COGNEE_ADMIN_EMAIL", "")
        admin_pw = _os.environ.get("COGNEE_ADMIN_PASSWORD", "")
        if not admin_email or not admin_pw:
            return False, {}, ("Cognee admin credentials are not configured on "
                               "this box; cannot provision personal memory.")
        # FAIL-CLOSED: require real backend access control for the private tier.
        if integ.get("requires_backend_access_control") and \
                not cognee_identity.backend_access_control_on(base, admin_email, admin_pw):
            return False, {}, (
                "Cognee backend access control is OFF — personal memory would "
                "not be isolated, so provisioning is refused (no masquerade).")
        cu_email, cu_key, err = self.cognee_login(sub, user_slug, user_email, legacy_slug)
        if err:
            return False, {}, err
        cu_pw = self._hmac_password(cu_key)
        try:
            res = cognee_identity.ensure_user(base, admin_email, admin_pw,
                                              cu_email, cu_pw)
        except Exception as e:
            logger.exception("cognee identity provisioning failed for %s", user_slug)
            return False, {}, f"Cognee identity provisioning failed: {e}"
        # cognee-mcp presents API_TOKEN as `Authorization: Bearer` for self-hosted;
        # cognee accepts a per-user JWT there (an api-key is rejected on that
        # header). That JWT lives one hour: the instance renews it through
        # renew_cognee_token with the account recorded here (`_cognee_login`,
        # the address and the HMAC key, no password) and its own renewal secret.
        token = res.get("token") or res.get("api_key", "")
        if not token:
            return False, {}, "Cognee per-user token could not be obtained."
        # A slug account used in the no-token window is registered like
        # migration 4's so the next directory-confirmed run decides it, but
        # NOT here (#2574 item 1): _launch registers it once the instance row
        # exists. A launch that fails before the row (instance limit,
        # create_instance error) must leave no pending account without
        # evidence, which the next run would quarantine as no_evidence.
        if not legacy_slug and not technical and sub and not self._db.cognee_account_for_sub(sub):
            # #2574 item 2: this person now has a memory under their current
            # identity; a legacy account mapped to them later does not displace it.
            self._db.note_cognee_sub_used(sub)
        extra = {"cognee_user_api_key": token,
                 "cognee_user_id": res.get("user_id", ""),
                 "_cognee_login": {"email": cu_email, "key": cu_key}}
        # Company-brain tier: resolve which shared dataset this user is scoped to.
        if integ.get("cognee_autoprovision") == "company":
            from app.services.governance import DEFAULT_COMPANY_DATASET, company_dataset
            try:
                ds = company_dataset(self._db, integ["id"])
            except Exception as e:  # noqa: BLE001
                if technical:
                    # B9 fix round 4 (M4): a technical grant names the dataset
                    # it was made for; never grant on a guess.
                    return False, {}, ("The company brain could not be looked up right now: "
                                       f"{type(e).__name__}; try again shortly.")
                ds = DEFAULT_COMPANY_DATASET
            extra["cognee_company_dataset"] = ds
            if technical:
                user_id = res.get("user_id") or ""
                try:
                    ds_id = cognee_identity.find_dataset_id(base, admin_email, admin_pw, ds)
                except Exception as e:  # noqa: BLE001
                    return False, {}, f"The company brain could not be looked up: {type(e).__name__}"
                if not ds_id:
                    return False, {}, (f"The company brain '{ds}' does not exist yet; create it "
                                       "first, then grant it.")
                if not user_id or not cognee_identity.grant_principal_read(
                        base, admin_email, admin_pw, user_id, ds_id, permission="read"):
                    return False, {}, "Read access to the company brain could not be granted."
                extra["_kb_grant"] = {"dataset": ds, "principal": sub}
        return True, extra, ""

    # ------------------------------------------------------------------
    # cognee token renewal for a running instance
    # ------------------------------------------------------------------
    #
    # cognee's JWT lives one hour (JWT_LIFETIME_SECONDS), and an instance used
    # to hold the one it was launched with, so personal and company memory over
    # MCP answered 401 an hour after a start. The instance now asks the manager
    # for a fresh one: it presents a renewal secret that only its own container
    # holds (not the route bearer the user's agents present), the manager keeps
    # only its sha256, and the answer is a one-hour token for the account the
    # instance was launched with. The account password never leaves the manager.

    def _arm_cognee_renewal(self, instance_config: dict, login: dict) -> str:
        """Record the account and a fresh renewal secret's hash in the instance
        config; return the secret (for the container's env only)."""
        import hashlib
        secret = secrets.token_urlsafe(32)
        instance_config["_cognee_login"] = {"email": login["email"], "key": login["key"]}
        instance_config["_cognee_renew_sha256"] = hashlib.sha256(secret.encode()).hexdigest()
        return secret

    def cognee_renewal_env(self, instance_id, secret: str) -> dict:
        base = (self._config.get("MCP_MANAGER_INTERNAL_URL")
                or "http://mcp-manager:5000").rstrip("/")
        return {RENEW_ENV_URL: base + RENEW_PATH,
                RENEW_ENV_SECRET: secret,
                RENEW_ENV_INSTANCE: str(instance_id)}

    def renew_cognee_token(self, instance_id: str, secret: str) -> tuple:
        """(token, status, error). Only for a RUNNING cognee instance whose
        recorded renewal hash matches `secret`, for the account it was launched
        with, and never for an account decided against its owner since."""
        import hashlib
        import hmac as _hmac
        import os as _os
        refused = (None, 403, "refused")
        try:
            iid = uuid.UUID(str(instance_id))
        except ValueError:
            return refused
        row = self._db.get_instance(iid)
        if not row or not secret:
            return refused
        cfg = row.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg or "{}")
            except ValueError:
                cfg = {}
        want = str(cfg.get("_cognee_renew_sha256") or "")
        got = hashlib.sha256(str(secret).encode()).hexdigest()
        if not want or not _hmac.compare_digest(want.encode(), got.encode()):
            return refused
        if row.get("state") != "running":
            return None, 409, "the instance is not running"
        integ = self._catalog.get(row.get("mcp_id")) or {}
        login = cfg.get("_cognee_login") or {}
        if not integ.get("cognee_autoprovision") or not login.get("email") or not login.get("key"):
            return refused
        owner = str(row.get("owner_sub") or "")
        if login["key"] != owner:
            # a slug (legacy) account: never once a run decided it against the owner
            decided = self._db.cognee_account_state(login["key"])
            if decided and (decided[0] == "quarantined"
                            or (decided[1] and decided[1] != owner)):
                logger.warning("cognee renewal for instance %s refused: its memory account "
                               "is %s and not its owner's", iid, decided[0])
                return None, 403, "the memory account is no longer this instance's"
        if owner.startswith("~"):
            from app.services.gateway_identity import TECH_PREFIX
            tu = self._db.get_technical_user(owner[len(TECH_PREFIX):])
            if not tu or tu.get("revoked_at"):
                return None, 403, "the technical user is revoked"
        now = datetime.now(timezone.utc)
        with _RENEW_GUARD:
            last = _RENEW_LAST.get(str(iid))
            if last and now - last < RENEW_MIN_INTERVAL:
                return None, 429, "renewed a moment ago"
            _RENEW_LAST[str(iid)] = now
        from app.services import cognee_identity
        base = _os.environ.get("COGNEE_BASE_URL", "http://cognee:8000")
        try:
            res = cognee_identity.user_token(
                base, _os.environ.get("COGNEE_ADMIN_EMAIL", ""),
                _os.environ.get("COGNEE_ADMIN_PASSWORD", ""),
                login["email"], self._hmac_password(login["key"]))
        except cognee_identity.CogneeAccountDeactivated as e:
            # Credential-free by construction: the reason may be shown and
            # logged. Never re-activated from here.
            logger.warning("cognee renewal for instance %s failed: %s", iid, e)
            with _RENEW_GUARD:
                _RENEW_LAST.pop(str(iid), None)
            return None, 409, str(e)
        except Exception as e:  # noqa: BLE001 — never a credential in the log
            logger.warning("cognee renewal for instance %s failed: %s", iid, type(e).__name__)
            with _RENEW_GUARD:
                _RENEW_LAST.pop(str(iid), None)
            return None, 502, f"cognee sign-in failed ({type(e).__name__})"
        return res["token"], 200, ""

    # A start that does not use the memory account the user had before.
    #
    # Measured on a test box: with the read-only directory token set, a
    # relaunch gave the user a new, empty sub-derived account while their
    # grants lived on the slug-derived one. That is the #2572 rule working, not
    # a lost migration: a slug account is served to a sub only when the
    # directory-confirmed run attributed it to that sub (cognee_login's
    # `mapped`; then nothing switches), and it is quarantined when the
    # evidence names several people (there: a deleted earlier holder of the
    # same username), was made before the owner joined, or names nobody
    # active. Carrying a quarantined account's grants or data to the new
    # account would hand the new person exactly what could not be proven
    # theirs, so nothing is carried. What was missing is that it happened
    # silently: the start now says so, the log names the slug and the audit
    # keeps it, and an administrator can review the account (identity page:
    # erase it, or restore a legacy memory that was attributed).

    SWITCH_NOTE = (" Your memory starts empty here: memory saved under this username "
                   "before could not be confirmed as yours, so it is not used. An "
                   "administrator can review it.")
    AUDIT_SWITCH = "cognee_account_not_carried_over"

    def _cognee_switch_note(self, existing, extra, sub, user_slug, legacy_slug, mcp_id) -> str:
        login = (extra or {}).get("_cognee_login") or {}
        if not login or legacy_slug or not sub or sub.startswith("~"):
            return ""
        prev_cfg = (existing or {}).get("config") or {}
        if isinstance(prev_cfg, str):
            try:
                prev_cfg = json.loads(prev_cfg or "{}")
            except ValueError:
                prev_cfg = {}
        prev = ((prev_cfg if isinstance(prev_cfg, dict) else {}).get("_cognee_login") or {}).get("key")
        decided = None
        if login.get("key") == sub:     # the sub-derived account
            try:
                decided = self._db.cognee_account_state(user_slug)
            except Exception:  # noqa: BLE001 — a note, never a reason to fail the start
                decided = None
        withheld = bool(decided) and (decided[0] == "quarantined"
                                      or (decided[1] and decided[1] != sub))
        # Once: a row that already records this account was told before.
        if not ((prev and prev != login.get("key")) or (not prev and withheld)):
            return ""
        logger.warning("cognee: %s starts on its own sub account; the slug account %s is %s "
                       "and is not carried over", mcp_id, user_slug,
                       decided[0] if decided else "a different one")
        try:
            self._db.log_audit(sub, self.AUDIT_SWITCH, mcp_id,
                               {"legacy_slug": user_slug,
                                "legacy_state": decided[0] if decided else None})
        except Exception:  # noqa: BLE001
            logger.warning("could not audit the cognee account switch for %s", user_slug)
        return self.SWITCH_NOTE

    def _technical_launch_refusal(self, integ: dict, user_id: str, principal: str) -> str:
        """'' when `principal` may get an admin-provisioned instance of
        `integ`, else the reason (#2472, B9). Only a live, unrevoked technical
        user from the mirror, only its own principal as the key, and only a
        catalogue entry whose tenancy is `shared` — a per-user entry holds one
        person's data and is never provisioned for a technical user."""
        from app.services.gateway_identity import TECH_PREFIX, technical_principal
        name = principal[len(TECH_PREFIX):] if principal.startswith(TECH_PREFIX) else ""
        try:
            if technical_principal(name) != principal:
                return "Unknown or revoked technical user."
        except ValueError:
            return "Unknown or revoked technical user."
        if user_id != principal:
            return "A technical user's instance is keyed by its own principal only."
        if integ.get("tenancy") != "shared":
            return "Only shared integrations can be provisioned for a technical user."
        tu = self._db.get_technical_user(name)
        if not tu or tu.get("revoked_at"):
            return "Unknown or revoked technical user."
        return ""

    def launch(self, mcp_id: str, user_id: str, username: str,
               groups: list, user_email: str = "", legacy_slug: str | None = None,
               principal: str | None = None, restart: bool = False) -> tuple:
        """Launch (see _launch) under the lock of the row it will act on:
        an existing row's own key (owner_sub, else a legacy row's user_id,
        the key stop and delete take), or the caller's sub for a new row."""
        key = user_id
        if user_id:
            try:
                existing = self._db.get_instance_by_mcp_and_user(mcp_id, user_id,
                                                                 legacy_slug=legacy_slug)
            except Exception:  # noqa: BLE001 — _launch reads it again and reports
                existing = None
            if existing:
                key = _row_lock_key(existing) or user_id
        with _instance_lock(key, mcp_id):
            return self._launch(mcp_id, user_id, username, groups, user_email=user_email,
                                legacy_slug=legacy_slug, principal=principal, restart=restart)

    def _grant_stale(self, instance: dict, integ: dict) -> bool:
        """B9 fix round 2: a technical user's running company instance whose
        recorded grant does not name the CURRENT company dataset (or cannot
        be checked) — the administrator's provisioning restarts it, which
        grants again, instead of answering "Already running"."""
        if integ.get("cognee_autoprovision") != "company":
            return False
        from app.services.governance import company_dataset
        cfg = instance.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg or "{}")
            except ValueError:
                cfg = {}
        try:
            current = company_dataset(self._db, integ["id"])
        except Exception as e:  # noqa: BLE001
            # B9 fix round 4 (M4): a failed lookup is reported, and a healthy
            # instance is not restarted on a guess.
            raise _GrantUncheckable(type(e).__name__) from e
        return ((cfg.get("_kb_grant") if isinstance(cfg, dict) else None) or {}).get("dataset") != current

    def _launch(self, mcp_id: str, user_id: str, username: str,
                groups: list, user_email: str = "", legacy_slug: str | None = None,
                principal: str | None = None, restart: bool = False) -> tuple:
        """Launch the caller's own per-user MCP proxy. Returns (instance_id, msg).

        #2472: `user_id` is the caller's sub (X-Authentik-Uid). It keys the
        instance and the credentials; the slug only names the container.

        `principal` (B9): an admin-provisioned instance for a TECHNICAL user
        (`~svc-<name>`, live in the mirror) of a SHARED integration only. It
        must equal `user_id`, so the instance, its credentials and its cognee
        account are keyed by the technical principal alone: no legacy slug, no
        email, no person's anything."""
        integ = self._catalog.get(mcp_id)
        if not integ:
            return None, f"Unknown integration: {mcp_id}"
        # #3427: an entry no box can run (no image ships for it) is refused
        # here, the one place every launch passes — never a create that fails.
        from app.services.card_state import not_available
        gone = not_available(self._catalog, mcp_id)
        if gone:
            return None, gone
        if integ.get("runtime") == "gateway":
            # #2554: connect only — the MCP gateway's shared upstream uses the
            # caller's own authorization per request; there is no container.
            return None, ("Nothing to provision: connect it, and the MCP gateway uses "
                          "your authorization.")
        if not user_id:
            return None, "No Authentik uid on this request; cannot provision."

        if principal is not None:
            refusal = self._technical_launch_refusal(integ, user_id, principal)
            if refusal:
                return None, refusal
            # Never the legacy window, never a person's email or groups; the
            # container name part is derived from the principal (a docker
            # name cannot hold `~`, and user_slug is VARCHAR(24)).
            legacy_slug, user_email, groups = None, "", []
            user_slug = make_user_slug(principal)
        elif user_id.startswith("~"):
            # A person's key is their sub; a `~` key is a technical principal
            # and comes only through the `principal` path above.
            return None, "A technical principal is provisioned only by an administrator."
        else:
            user_slug = make_user_slug(username)

        # dedupe / relaunch. There is at most ONE non-destroyed instance per
        # (mcp_id, user_slug) — enforced by the partial-unique index
        # idx_mcp_instance_user. If it's already running, no-op. If it exists in
        # ANY other live state (stopped / error / provisioning), we must REUSE
        # that same row on relaunch (#66): a fresh create_instance() INSERT would
        # collide with that index and 500. `relaunch_of` carries the existing id
        # through to the reuse path below.
        # Legacy window (B6 fix rounds 1+2): while no read-only token is set,
        # the caller passes `legacy_slug` and an undecided pre-2026.11
        # instance is found by the slug, as on 2026.09, and REUSED
        # (relaunched in place, left for the migration to decide). With a
        # token set, `legacy_slug` is None: undecided rows are never served.
        existing = self._db.get_instance_by_mcp_and_user(mcp_id, user_id,
                                                         legacy_slug=legacy_slug)
        cred_model = self._catalog.cred_model(mcp_id)
        relaunch_of = None
        if existing:
            # B9 fix round 1 (I2): an OAuth proxy holds the token it was
            # started with, so "Start" on one whose token has expired (or a
            # `restart`, e.g. after the user reconnected) starts it again with
            # a renewed token instead of answering "Already running".
            grant_stale = False
            if existing.get("state") == "running" and principal is not None:
                try:
                    grant_stale = self._grant_stale(existing, integ)
                except _GrantUncheckable:
                    logger.warning("technical instance %s: the company dataset could not be read; "
                                   "the running instance is left as it is", existing.get("id"))
                    return str(existing["id"]), (
                        "The company brain could not be checked right now; the running "
                        "instance was left as it is. Try again shortly.")
            if existing.get("state") == "running" and not restart and not (
                    cred_model == "oauth" and self._token_stale(existing)) and not grant_stale:
                return str(existing["id"]), "Already running."
            relaunch_of = existing["id"]
        else:
            # The container name is per slug, and a slug is not a person: a
            # live instance under this slug that is not this caller's (another
            # sub's, or an undecided pre-2026.11 row outside the legacy
            # window) blocks the name. Refuse instead of touching it.
            holder = self._db.get_live_instance_by_slug(mcp_id, user_slug)
            if holder and holder.get("owner_sub") != user_id:
                return None, ("Another account holds this integration's proxy name, or a "
                              "connection saved before the upgrade is not verified yet; "
                              "ask an administrator.")

        # Fetch ONLY this user's decrypted creds (sub-keyed first, then the
        # undecided legacy rows under the slug).
        decrypted = self._creds.get_decrypted(user_id, mcp_id, legacy_slug=legacy_slug)
        if cred_model == "pat" and not decrypted:
            return None, ("No credentials stored for this integration yet. "
                          "Add your credentials first, then provision.")
        if cred_model == "oauth" and "oauth_access" not in decrypted:
            return None, ("Not authorized yet. Complete the OAuth flow first, "
                          "then provision.")
        token_expires_at = None
        if cred_model == "oauth":
            decrypted, token_expires_at, err = self._launch_oauth_token(user_id, mcp_id, decrypted)
            if err:
                return None, err

        # #36 two-tier cognee: auto-provision the per-user cognee identity (no
        # user-supplied key). FAIL-CLOSED — if this can't mint a real per-user
        # key (backend access-control off / login fails), we refuse rather than
        # fall back to a shared key that would leak across users.
        extra: dict = {}
        switch_note = ""
        if integ.get("cognee_autoprovision"):
            ok, extra, err = self._provision_cognee_identity(
                integ, user_slug, username, user_email, groups,
                sub=user_id, legacy_slug=legacy_slug, technical=principal is not None)
            if not ok:
                return None, err
            switch_note = self._cognee_switch_note(existing, extra, user_id, user_slug,
                                                   legacy_slug, mcp_id)

        # Non-secret settings live in the instance config (if any prior instance).
        settings = {}
        if existing and existing.get("config"):
            cfg = existing["config"]
            if isinstance(cfg, dict):
                settings = cfg.get("settings", {})

        global_max = int(self._config.get("MCP_MAX_INSTANCES", 50))
        try:
            if len(self._db.get_all_instances()) >= global_max:
                return None, "System-wide MCP instance limit reached."
        except Exception:
            pass

        container_name = f"mcp-{mcp_id}-{user_slug}"
        # CRITICAL-1 (#61): the route secret is the REAL access control on the
        # per-user proxy (which holds THIS user's live creds). It is the bearer
        # the owning user's agents present; the opaque subdomain is only defense
        # in depth. >=128-bit entropy: token_urlsafe(32) = 256 bits.
        route_secret = secrets.token_urlsafe(32)
        instance_config = {"settings": settings, "_route_secret": route_secret,
                           # B9: what the gateway checks before relaying to
                           # this instance — the image it was launched from
                           # (the pinned razzfazz-mcp-proxy build, rulings
                           # I4/R2-2).
                           "_image": integ.get("image", "")}
        gateway_secret = None
        from app.services.gateway_credentials import PINNED_PROXY_IMAGES
        if integ.get("image") in PINNED_PROXY_IMAGES:
            # #3453: the gateway's own bearer for this instance, so the pinned
            # proxy tells a chat (relayed by the gateway) from its owner's
            # agents (the route secret) and applies the agents' send/delete
            # setting. Never handed to an agent: agent_wiring reads
            # _route_secret only.
            gateway_secret = secrets.token_urlsafe(32)
            instance_config["_gateway_secret"] = gateway_secret
        if extra.get("_kb_grant"):
            instance_config["_kb_grant"] = extra["_kb_grant"]
        renew_secret = None
        if extra.get("_cognee_login"):
            renew_secret = self._arm_cognee_renewal(instance_config, extra["_cognee_login"])
        if principal is None:
            # B9 fix round 3: the tier at provisioning, which the gateway and
            # the agents' path compare with min_tier in B6's no-token window
            # (2026.09-ga.4 parity); with the directory they use the CURRENT tier.
            from app.services.tier import resolve_tier
            instance_config["_tier"] = resolve_tier(groups)
        if cred_model == "oauth":
            # When the token the proxy starts with expires (None: unknown,
            # which counts as stale for the next "Start").
            instance_config["_token_expires_at"] = (
                token_expires_at.isoformat() if token_expires_at else None)
        if relaunch_of is not None:
            # Reuse the existing row (stopped/error/provisioning → provisioning)
            # instead of INSERTing a duplicate (which the partial-unique index
            # would reject — #66). Also tear down any stale container left by the
            # prior stop() (stop leaves the container present-but-stopped; a
            # create with the same name would otherwise conflict) and drop its
            # old Caddy route so we re-register cleanly with the new bearer.
            instance_id = relaunch_of
            # #619: no per-instance route to drop — the wildcard proxy reads
            # the CURRENT _route_secret from the DB row on every request, so
            # a relaunch's new bearer takes effect immediately.
            try:
                self._docker.remove_container(container_name)
            except Exception:
                logger.warning("relaunch: no stale container %s to remove (ok)",
                               container_name)
            self._db.reset_instance_for_relaunch(instance_id, container_name,
                                                 instance_config)
        else:
            instance_id = self._db.create_instance(mcp_id, user_id, user_slug,
                                                   container_name, instance_config)

        if legacy_slug and integ.get("cognee_autoprovision"):
            # #2574 item 1: the instance row exists now (created or reset for
            # relaunch), which is the evidence the migration decides on.
            self._db.register_cognee_account(user_slug)

        try:
            env = self._render_env(integ, user_slug, user_email, decrypted,
                                   settings, extra=extra, user_id=user_id)
            # Inject the bearer into the proxy container so the proxy itself can
            # REQUIRE `Authorization: Bearer <PROXY_AUTH_TOKEN>` (the test-echo
            # reference proxy enforces this). Caddy ALSO enforces it on the route
            # (below), so even an image that ignores the env still gets gated.
            env["PROXY_AUTH_TOKEN"] = route_secret
            if gateway_secret:
                env["PROXY_GATEWAY_TOKEN"] = gateway_secret
            if renew_secret:
                env.update(self.cognee_renewal_env(instance_id, renew_secret))
            labels = {
                "razzfazz.managed": "true",
                "razzfazz.mcp.managed": "true",
                "razzfazz.mcp.id": mcp_id,
                "razzfazz.mcp.user": user_slug,
                "razzfazz.mcp.instance": str(instance_id),
            }
            # #36 follow-up: a proxy that must reach a shared backend (cognee-mcp
            # in API mode -> cognee:8000) joins a SCOPED extra network the backend
            # is on. #36 BLOCKER-1 / #785: `reach_backend: cognee` maps to the
            # DEDICATED `cognee-backend` net, NOT `coding-agents` — this comment
            # described the pre-BLOCKER-1 behaviour and said the opposite of what
            # `_BACKEND_NETWORKS` above does. On the sandbox bridge a peer
            # sandbox could name another user's proxy container directly. NEVER
            # `default` either (docker_client refuses it) so the /internal
            # isolation invariant holds.
            extra_networks = _BACKEND_NETWORKS.get(integ.get("reach_backend"), [])
            container_id = self._docker.create_container(
                name=container_name,
                image=integ["image"],
                environment=env,
                mem_limit=integ.get("mem_limit", "256m"),
                cpu_limit=float(integ.get("cpu_limit", 0.5)),
                labels=labels,
                command=integ.get("command") or None,
                extra_networks=extra_networks,
            )
            self._docker.start_container(container_id)
            # #619: no Caddy route registration — the static *.MCP_DOMAIN
            # wildcard proxies every instance host to the manager's bearer
            # proxy, which re-creates the CRITICAL-1 gate + host-rewrite from
            # the DB row/catalog per request.

            self._db.update_instance_state(instance_id, "running",
                                           container_id=container_id)
            self._db.log_audit(user_id, "provision", mcp_id,
                               {"container": container_name})
            return str(instance_id), f"{integ['display_name']} proxy provisioned.{switch_note}"
        except Exception as e:
            logger.exception("Failed to launch MCP proxy %s", container_name)
            reason = launch_failure_reason(e, integ.get("image", ""))
            self._db.update_instance_state(instance_id, "error",
                                           error_message=reason[:500])
            self._db.log_audit(user_id, "provision_failed", mcp_id,
                               {"error": str(e)[:200]})
            return str(instance_id), f"Provisioning failed: {reason}"

    def stop(self, instance_id, username: str) -> tuple:
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)
        inst = self._db.get_instance(instance_id)
        if not inst:
            return None, "Instance not found."
        with _instance_lock(_row_lock_key(inst), inst["mcp_id"]):
            self._docker.stop_container(inst["container_name"])
            self._db.update_instance_state(instance_id, "stopped")
        self._db.log_audit(inst.get("owner_sub") or inst["user_slug"], "stop", inst["mcp_id"])
        return str(instance_id), "Stopped."

    def delete(self, instance_id, username: str) -> tuple:
        if isinstance(instance_id, str):
            instance_id = uuid.UUID(instance_id)
        inst = self._db.get_instance(instance_id)
        if not inst:
            return None, "Instance not found."
        with _instance_lock(_row_lock_key(inst), inst["mcp_id"]):
            self._docker.remove_container(inst["container_name"])
            self._db.delete_instance(instance_id)
        self._db.log_audit(inst.get("owner_sub") or inst["user_slug"], "delete", inst["mcp_id"])
        return str(instance_id), "Deleted."
