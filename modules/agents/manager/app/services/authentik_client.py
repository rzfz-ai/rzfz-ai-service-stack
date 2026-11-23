# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Authentik REST-API client for per-instance forward-auth (PR #84 C1).

Each coding-agent instance is served at its own subdomain
  {type}-{token}.agents.<domain>
and the C1 `forward_auth` on the Caddy `*.agents` wildcard sends the embedded
Authentik outpost that per-instance host. The outpost matches an incoming host
against its providers — and every provider is `forward_single` (exact host).
The static "Caddy Forward Auth Provider for Agents" covers ONLY the dashboard
host (agents.<domain>), so per-instance hosts had no provider → 404.

A single domain-level `forward_domain` provider was tried first, but it LOOPS on
the post-login callback (~8 redirects → HTTP 400) — the domain-cookie handshake
doesn't complete cleanly on the instance subdomain. So instead we register a
per-instance `forward_single` provider + application at provision time (the
battle-tested mode every other app uses) and deregister it at stop/delete —
mirroring how caddy_client registers/removes the per-instance Caddy route.

This client only AUTHENTICATES + group-gates the instance host. Per-instance
OWNERSHIP is still independently enforced by the C1 anchors (Caddy↔manager proof
header, manager owner-check, container source-IP) that run AFTER auth. Auth
failures here fail SAFE: without a provider the outpost 404s the host (no shell
is exposed), and the anchors still block any direct hit.

Config (env):
  AUTHENTIK_URL            base URL of the authentik server (default
                           http://authentik-server:9000)
  AUTHENTIK_BOOTSTRAP_TOKEN  admin API token (same one the config-portal /
                           password-broker use). Absent → this client no-ops
                           with a loud warning (outpost will 404 the instance
                           host until an operator wires the token).
  AGENT_INSTANCE_GATE_GROUP  Authentik group that gates instance access
                           (default "razzfazz.ai AI Agents Users").
  RAZZFAZZ_ACCESS_GROUP    the box access group (A1 D10, staging canary).
                           Set: every instance app is born under the same
                           restriction core/Authentik/apply-policy-bindings.py
                           puts on every other app. Empty: unchanged.
"""

import logging
import os
from urllib.parse import quote

import httpx

from razzfazz_common.rbac import CatalogueError
from razzfazz_common.rbac_guard import role_groups

logger = logging.getLogger(__name__)

# Name/slug prefixes so the per-instance objects are recognisable + collision-
# free, and so deregistration + the init-time sweep can find them.
#: How much of an Authentik error body to carry into the log. Its validation
#: errors are small JSON objects naming the offending field; 800 chars is far
#: more than one needs and still bounded, because this goes into a log line that
#: repeats on every reconcile.
_ERROR_BODY_MAX = 800


class AuthentikAPIError(RuntimeError):
    """An Authentik API call failed, WITH the response body attached (#516).

    `httpx`'s own `HTTPStatusError` stringifies to
    `Client error '400 Bad Request' for url '...'` — the status and the URL, and
    nothing about WHY. Authentik answers a rejected application create with a
    JSON body naming the offending field (`slug` already taken, missing flow,
    …), and `raise_for_status()` discards it.

    Prod 8.246 hot-looped `register_instance` for nine instances, every call
    400ing, for as long as anyone had been watching — and the log said only
    "400 Bad Request" nine times a cycle. The one fact needed to fix it was in
    the body of every one of those responses.
    """

    def __init__(self, method: str, path: str, status: int, body: str):
        self.status = status
        self.body = body
        detail = (body or "").strip().replace("\n", " ")
        if len(detail) > _ERROR_BODY_MAX:
            detail = detail[:_ERROR_BODY_MAX] + f"… (+{len(body) - _ERROR_BODY_MAX} chars)"
        super().__init__(
            f"{method} {path} -> {status}" + (f": {detail}" if detail else " (empty body)"))


class AccessRestrictionError(RuntimeError):
    """The box access-group restriction could not be put on an instance app,
    or the read-back after putting it on did not show it holding. The app is
    then never served (register_instance removes it entirely)."""


_PROVIDER_PREFIX = "Agent Instance — "
_APP_SLUG_PREFIX = "ai-"

# The base dashboard provider we clone flows + property-mappings from, so the
# per-instance providers behave identically (same scopes, same auth flow).
_BASE_PROVIDER_NAME = "Caddy Forward Auth Provider for Agents"

_DEFAULT_GATE_GROUP = "razzfazz.ai AI Agents Users"

# A1 D10 — the box access group. These three names are the ones
# core/Authentik/apply-policy-bindings.py uses (GATE_POLICY,
# APP_ACCESS_POLICY_PREFIX, ACCESS_MARKER); a test pins them together. The
# apply creates the gate policy; an instance app binds it by name.
_ACCESS_GATE_POLICY = "razzfazz.ai - Box access group"
_APP_ACCESS_PREFIX = "razzfazz.ai - App access: "
_ACCESS_MARKER = "[rzfz: access group]"


def _access_group() -> str:
    return os.environ.get("RAZZFAZZ_ACCESS_GROUP", "").strip()


def _app_access_expression(access_group: str, group_names) -> str:
    """The instance app's own policy: access group AND any of its groups — the
    expression apply-policy-bindings.access_policy_expressions() writes, with
    the groups in the same (sorted) order, so a refresh and a reconcile never
    rewrite each other's policy."""
    member = "ak_is_group_member(request.user, name=%r)"
    gate = member % access_group
    if not group_names:
        return "return " + gate
    return "return %s and (%s)" % (gate, " or ".join(member % g for g in sorted(group_names)))


def _admin_groups() -> list:
    """Groups bound on every per-instance app besides the gate group: the catalogue's
    platform-admin role (Super Admins only). Deliberately NOT agent-manager/admin:
    that would bind `authentik Admins` on every instance (#2473, D3).
    Raises CatalogueError when the catalogue is unusable."""
    return sorted(role_groups("platform-admin"))


class AuthentikClient:
    """Registers/deregisters per-instance forward_single providers in Authentik."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 gate_group: str | None = None):
        self._base = (base_url or os.environ.get(
            "AUTHENTIK_URL", "http://authentik-server:9000")).rstrip("/")
        self._token = token if token is not None else os.environ.get(
            "AUTHENTIK_BOOTSTRAP_TOKEN", "")
        self._gate_group = gate_group or os.environ.get(
            "AGENT_INSTANCE_GATE_GROUP", _DEFAULT_GATE_GROUP)
        # cache of things that don't change per-instance
        self._base_provider = None
        self._outpost_pk = None
        # host -> why its access restriction could not be applied; cleared on
        # the next successful registration or a deregistration.
        self._restriction_errors = {}

    # ── low-level API ────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json"}

    def _check(self, r, method: str, path: str):
        """`raise_for_status()` with the response BODY kept (#516).

        Every call site gets this, not just the one that was noisy on prod: a
        discarded error body is equally useless on a 403 from the outpost patch
        or a 500 from the provider create.
        """
        # `200 <= status < 300` rather than httpx's `is_success`: it needs one
        # attribute instead of two, so a test double only has to model
        # `status_code` and `text` — and a double narrower than the real thing is
        # how #352 turned a correct change into a red main. Same semantics; a 3xx
        # is deliberately NOT success, since httpx follows redirects and a 302
        # reaching here would make `.json()` fail confusingly.
        if 200 <= r.status_code < 300:
            return r
        raise AuthentikAPIError(method, path, r.status_code, getattr(r, "text", ""))

    def _get(self, path, **params):
        r = httpx.get(f"{self._base}{path}", headers=self._headers(),
                      params=params or None, timeout=15)
        self._check(r, "GET", path)
        return r.json()

    def _get_optional(self, path, **params):
        """GET where **404 is a normal answer, not an error** → returns None.

        Used for the by-key object lookups (#237). Any other non-2xx still
        raises, so a 500/401 is never silently read as "absent" — that
        distinction is the whole point: mistaking an error for absence is what
        makes reconcile try to CREATE something that already exists.
        """
        r = httpx.get(f"{self._base}{path}", headers=self._headers(),
                      params=params or None, timeout=15)
        if r.status_code == 404:
            return None
        self._check(r, "GET", path)
        return r.json()

    def _post(self, path, body):
        r = httpx.post(f"{self._base}{path}", headers=self._headers(),
                       json=body, timeout=15)
        self._check(r, "POST", path)
        return r.json()

    def _patch(self, path, body):
        r = httpx.patch(f"{self._base}{path}", headers=self._headers(),
                        json=body, timeout=15)
        self._check(r, "PATCH", path)
        return r.json()

    def _put(self, path, body):
        r = httpx.put(f"{self._base}{path}", headers=self._headers(),
                      json=body, timeout=15)
        self._check(r, "PUT", path)
        return r.json()

    def _delete(self, path):
        r = httpx.delete(f"{self._base}{path}", headers=self._headers(), timeout=15)
        if r.status_code not in (200, 204, 404):
            self._check(r, "DELETE", path)
        return r.status_code

    # ── helpers ──────────────────────────────────────────────────────────────
    def _provider_name(self, host: str) -> str:
        return f"{_PROVIDER_PREFIX}{host}"

    def _app_slug(self, host: str) -> str:
        # slug charset is [-a-z0-9]; host is already DNS-safe. Prefix keeps it
        # unique + greppable for deregistration and the init-time sweep.
        return f"{_APP_SLUG_PREFIX}{host.replace('.', '-')}"

    def _load_base_provider(self):
        if self._base_provider is None:
            res = self._get("/api/v3/providers/proxy/", search=_BASE_PROVIDER_NAME)
            match = [p for p in res.get("results", [])
                     if p["name"] == _BASE_PROVIDER_NAME]
            if not match:
                raise RuntimeError(
                    f"base provider {_BASE_PROVIDER_NAME!r} not found — "
                    "26-agents.yaml blueprint not applied?")
            self._base_provider = match[0]
        return self._base_provider

    def _outpost(self):
        if self._outpost_pk is None:
            res = self._get("/api/v3/outposts/instances/", search="Embedded")
            match = [o for o in res.get("results", []) if "Embedded" in o["name"]]
            if not match:
                raise RuntimeError("embedded outpost not found")
            self._outpost_pk = match[0]["pk"]
        # always re-fetch the current provider list (it changes as we add/remove)
        return self._get(f"/api/v3/outposts/instances/{self._outpost_pk}/")

    def _find_provider_pk(self, host: str):
        """Provider pk by EXACT name, across ALL pages (#516 — the #237 class,
        one endpoint over: `?search=` is fuzzy AND paginated, so an existing
        provider could read as absent, the create then 400-collided, and
        reconcile hot-looped on it forever). Providers have no by-key URL like
        applications do, so exactness comes from the `name=` filter plus an
        explicit equality check, and completeness from following pagination."""
        name = self._provider_name(host)
        path, params = "/api/v3/providers/proxy/", {"name": name}
        while path:
            res = self._get(path, **params)
            for p in res.get("results", []):
                if p["name"] == name:
                    return p["pk"]
            pag = res.get("pagination") or {}
            cur = int(pag.get("current") or 1)
            total = int(pag.get("total_pages") or 1)
            if cur < total:
                params["page"] = cur + 1
            else:
                path = None
        return None

    def _find_app(self, slug: str):
        """Return the application with EXACTLY this slug, or None (#237 / H0).

        Looked up by SLUG in the URL PATH — the canonical key for this endpoint,
        and the same form `_patch`/`_delete` already use a few lines below. It is
        exact by construction: 200 → the app, 404 → genuinely absent.

        The previous implementation issued a `?search=<slug>` LIST query and
        filtered the results for an exact match. That is exact only for the rows
        it actually sees, and `search` is both fuzzy AND **paginated** — an
        application that exists but does not surface on the first page read as
        "absent". reconcile then tried to CREATE a slug that already existed, the
        create failed, and the instance stayed 404 behind its outpost until
        something else healed it. That is the #237 "agent 404s after container
        restart" report.

        Filtering the search results (the prior fix) removed the
        wrong-app-returned half of the bug but not the not-on-page-1 half; a
        direct GET removes both.
        """
        return self._get_optional(
            f"/api/v3/core/applications/{quote(slug, safe='')}/")

    def _group_pks(self):
        pks = []
        self._resolved_group_names = []
        try:
            names = [self._gate_group, *_admin_groups()]
        except CatalogueError as exc:
            logger.error("cannot resolve the per-instance admin groups: %s", exc)
            return []  # fail closed: no pks -> register_instance refuses to create an unbound app
        for gname in names:
            res = self._get("/api/v3/core/groups/", search=gname)
            for g in res.get("results", []):
                if g["name"] == gname:
                    pks.append(g["pk"])
                    self._resolved_group_names.append(gname)
                    break
        return pks

    # ── public API ───────────────────────────────────────────────────────────
    def register_instance(self, host: str) -> bool:
        """Create (idempotently) the per-instance forward_single provider +
        application, attach it to the embedded outpost, and bind the gate group.

        Returns True on success. Fails SAFE: on any error we log and return
        False — the outpost then 404s the host (no shell exposed) and the C1
        anchors still block direct hits.
        """
        if not self.enabled:
            logger.warning(
                "AUTHENTIK_BOOTSTRAP_TOKEN unset — cannot register forward-auth "
                "for %s; the outpost will 404 it until the token is wired.", host)
            return False
        try:
            base = self._load_base_provider()
            pname = self._provider_name(host)
            slug = self._app_slug(host)

            # 0. FAIL-CLOSED group gate (LOW-1): resolve the gate group(s) FIRST.
            # An Authentik application with ZERO policy bindings is accessible to
            # ANY authenticated user — so if the group-name search misses
            # (renamed / absent / transient API-empty) we must NOT create or
            # leave an unbound app. Abort here → the host has no provider → the
            # outpost 404s it → fail-closed. The 120s reconcile retries once the
            # group resolves. (The C1 ownership anchors still fence it regardless.)
            group_pks = self._group_pks()
            if not group_pks:
                logger.error(
                    "Refusing to register forward-auth for %s: gate group(s) "
                    "%r/%r not found — an unbound app would be open to ALL "
                    "authenticated users. Failing closed (outpost 404s the host) "
                    "until the group resolves (or the role catalogue is readable).", host, self._gate_group,
                    "platform-admin")
                return False

            # 1. provider (idempotent by name)
            ppk = self._find_provider_pk(host)
            provider_created = False
            if ppk is None:
                prov = self._post("/api/v3/providers/proxy/", {
                    "name": pname,
                    "mode": "forward_single",
                    "external_host": f"https://{host}",
                    "intercept_header_auth": True,
                    "authorization_flow": base["authorization_flow"],
                    "invalidation_flow": base.get("invalidation_flow"),
                    "property_mappings": base.get("property_mappings", []),
                    "access_token_validity": "hours=24",
                })
                ppk = prov["pk"]
                provider_created = True

            # 2. application (idempotent by EXACT slug). NB: Authentik's
            # applications REST endpoint is keyed by SLUG in the URL path
            # (/api/v3/core/applications/<slug>/), NOT the pk — using the pk 404s.
            app_created = False
            existing = self._find_app(slug)
            if existing:
                # ensure it points at the provider (self-heal)
                if existing.get("provider") != ppk:
                    self._patch(f"/api/v3/core/applications/{slug}/",
                                {"provider": ppk})
                app_pk = existing["pk"]
            else:
                body = {
                    "name": pname,
                    "slug": slug,
                    "provider": ppk,
                    "meta_launch_url": "blank://blank",
                    "meta_description":
                        "Per-instance forward-auth for a coding-agent terminal.",
                }
                if _access_group():
                    # A1 D10: born restricted — mode `all` from the first
                    # moment, so no binding added below can widen it.
                    body["policy_engine_mode"] = "all"
                    body["meta_description"] += " " + _ACCESS_MARKER
                created = self._post("/api/v3/core/applications/", body)
                app_pk = created["pk"]  # UUID — the policy-binding target
                app_created = True

            # 3. bind the gate group(s) BEFORE attaching to the outpost, so the
            #    host is never briefly reachable as an unbound (all-users) app.
            #    On failure, roll back a freshly-created app+provider (fail-closed).
            restricted = bool(_access_group())
            try:
                self._ensure_group_bindings(app_pk, group_pks)
                if restricted:
                    self._ensure_access_restriction(app_pk, slug)
                    # Read back BEFORE the outpost attach below: the host is
                    # served only once Authentik itself shows the restriction.
                    self._verify_access_restriction(app_pk, slug)
            except Exception as be:  # noqa: BLE001
                if restricted:
                    # Fail closed, whether the app is new or not: a restricted
                    # box never serves an instance host whose restriction is
                    # not confirmed. Detach + delete app, own policy, provider.
                    self._restriction_errors[host] = str(be)
                    logger.error(
                        "Access-group restriction for %s could not be applied or confirmed (%s) — "
                        "removing its forward-auth entirely; the host is not served until "
                        "`rzfz post-install --refresh` has run and the agent is started again.",
                        host, be)
                    try:
                        if not self._delete_instance_objects(host, ppk):
                            logger.error("removing the forward-auth of %s was incomplete", host)
                    except Exception as de:  # noqa: BLE001
                        logger.error("removing the forward-auth of %s failed: %s", host, de)
                    return False
                logger.error(
                    "Group binding failed for %s (%s) — rolling back to keep the "
                    "app from being open to all authenticated users.", host, be)
                if app_created:
                    self._delete(f"/api/v3/core/applications/{slug}/")
                if provider_created:
                    self._delete(f"/api/v3/providers/proxy/{ppk}/")
                return False

            # 4. attach provider to the embedded outpost (idempotent) — last, so
            #    the outpost only serves the host once it's group-gated.
            op = self._outpost()
            provs = set(op.get("providers", []))
            if ppk not in provs:
                provs.add(ppk)
                self._patch(f"/api/v3/outposts/instances/{self._outpost_pk}/",
                            {"providers": list(provs)})

            self._restriction_errors.pop(host, None)
            logger.info("Registered Authentik forward-auth for %s (provider=%s app=%s)",
                        host, ppk, slug)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Authentik register_instance(%s) failed: %s", host, e)
            return False

    def _ensure_group_bindings(self, app_pk, group_pks):
        """Bind the given group pks to the app (idempotent). group_pks MUST be
        non-empty — the caller guarantees the fail-closed gate."""
        # existing bindings for this app
        existing = self._get("/api/v3/policies/bindings/", target=app_pk)
        bound_groups = {b.get("group") for b in existing.get("results", [])
                        if b.get("group")}
        for order, gpk in enumerate(group_pks):
            if gpk not in bound_groups:
                self._post("/api/v3/policies/bindings/",
                           {"target": app_pk, "group": gpk, "order": order})

    def _find_expression_policy(self, name: str):
        res = self._get("/api/v3/policies/expression/", name=name)
        return next((p for p in res.get("results", []) if p.get("name") == name), None)

    def restriction_error(self, host: str):
        """Why the access restriction of `host` could not be applied at its
        last registration, or None. The provisioner's readiness gate reads it
        to fail a launch with a clear message instead of waiting on the
        outpost for a host that is deliberately not served."""
        return self._restriction_errors.get(host)

    def _set_binding_enabled(self, b, enabled: bool):
        """Switch one policy binding on or off with a FULL PUT.

        Authentik 2026.8.3's PolicyBindingSerializer.validate()
        (authentik/policies/api/bindings.py:90-103) reads
        `attrs.get("target").supported_policy_binding_targets()` and then
        requires exactly one of policy/group/user in `attrs`. A partial PATCH
        carries only the fields sent, so `{"enabled": false}` has no target:
        AttributeError, HTTP 500. A PUT with the binding's own target, its one
        policy/group/user and every writable field passes that validation and
        changes nothing but `enabled`."""
        body = {"target": b["target"], "order": b["order"],
                "policy": b.get("policy"), "group": b.get("group"), "user": b.get("user"),
                "negate": bool(b.get("negate", False)), "timeout": b.get("timeout", 30),
                "failure_result": bool(b.get("failure_result", False)), "enabled": enabled}
        return self._put(f"/api/v3/policies/bindings/{b['pk']}/", body)

    def _ensure_access_restriction(self, app_pk, slug):
        """A1 D10 — put the box access-group restriction on an instance app,
        the shape apply-policy-bindings.py gives every other app: mode `all`,
        the marker, the gate policy (found by name, order -100), the app's own
        policy (access group AND any of its groups, order 0), group bindings
        disabled. Every step only narrows. When the gate policy does not exist
        (the apply has not run), AccessRestrictionError: the caller removes the
        app, and the instance is not served until `rzfz post-install --refresh`."""
        group = _access_group()
        gate = self._find_expression_policy(_ACCESS_GATE_POLICY)
        if gate is None:
            raise AccessRestrictionError(
                f"the access group {group!r} is set but the policy {_ACCESS_GATE_POLICY!r} does not "
                f"exist yet — run `rzfz post-install --refresh`")
        app = self._find_app(slug) or {}
        desc = app.get("meta_description") or ""
        if app.get("policy_engine_mode") != "all" or _ACCESS_MARKER not in desc:
            self._patch(f"/api/v3/core/applications/{quote(slug, safe='')}/", {
                "policy_engine_mode": "all",
                "meta_description": desc if _ACCESS_MARKER in desc else (desc + " " + _ACCESS_MARKER).strip()})
        expression = _app_access_expression(group, getattr(self, "_resolved_group_names", []))
        own_name = _APP_ACCESS_PREFIX + slug
        own = self._find_expression_policy(own_name)
        if own is None:
            own = self._post("/api/v3/policies/expression/", {"name": own_name, "expression": expression})
        elif own.get("expression") != expression:
            own = self._patch(f"/api/v3/policies/expression/{own['pk']}/", {"expression": expression})
        existing = self._get("/api/v3/policies/bindings/", target=app_pk).get("results", [])
        for ppk, order in ((own["pk"], 0), (gate["pk"], -100)):
            b = next((b for b in existing if b.get("policy") == ppk), None)
            if b is None:
                self._post("/api/v3/policies/bindings/",
                           {"target": app_pk, "policy": ppk, "order": order, "enabled": True})
            elif not b.get("enabled", True):
                self._set_binding_enabled(b, True)
        for b in existing:
            if b.get("policy") not in (own["pk"], gate["pk"]) and b.get("enabled", True):
                self._set_binding_enabled(b, False)
        self._access_pks = (gate["pk"], own["pk"])

    def _verify_access_restriction(self, app_pk, slug):
        """Read back what Authentik now holds for the app; raise
        AccessRestrictionError unless it is restricted: mode `all`, the gate
        and the app's own policy bound and enabled, nothing else enabled."""
        gate_pk, own_pk = getattr(self, "_access_pks", (None, None))
        app = self._find_app(slug)
        if app is None:
            raise AccessRestrictionError(f"read-back: application {slug} is gone")
        problems = []
        if app.get("policy_engine_mode") != "all":
            problems.append(f"policy_engine_mode is {app.get('policy_engine_mode')!r}, not 'all'")
        on = [b for b in self._get("/api/v3/policies/bindings/", target=app_pk).get("results", [])
              if b.get("enabled", True)]
        gate_on = [b for b in on if gate_pk is not None and b.get("policy") == gate_pk]
        if not gate_on or any(b.get("negate") for b in gate_on):
            problems.append(f"no enabled binding of {_ACCESS_GATE_POLICY!r}")
        if not any(own_pk is not None and b.get("policy") == own_pk for b in on):
            problems.append(f"no enabled binding of {_APP_ACCESS_PREFIX + slug!r}")
        extra = [b.get("pk") for b in on if b.get("policy") not in (gate_pk, own_pk)]
        if extra:
            problems.append(f"bindings still enabled besides the restriction: {extra}")
        if problems:
            raise AccessRestrictionError("read-back: " + "; ".join(problems))

    def _delete_instance_objects(self, host: str, ppk=None) -> bool:
        """Detach from outpost + delete the app + provider for `host`.
        Idempotent, 404-tolerant. Returns True on full success."""
        ok = True
        slug = self._app_slug(host)
        if ppk is None:
            ppk = self._find_provider_pk(host)

        # detach from outpost first (so a lingering ref doesn't 500 anything)
        if ppk is not None:
            try:
                op = self._outpost()
                provs = set(op.get("providers", []))
                if ppk in provs:
                    provs.discard(ppk)
                    self._patch(
                        f"/api/v3/outposts/instances/{self._outpost_pk}/",
                        {"providers": list(provs)})
            except Exception as e:  # noqa: BLE001
                logger.warning("detach %s from outpost failed: %s", host, e)
                ok = False

        # delete the application (also drops its policy bindings). The
        # applications endpoint is keyed by SLUG in the URL, not the pk.
        existing = self._find_app(slug)
        if existing:
            self._delete(f"/api/v3/core/applications/{existing['slug']}/")

        # A1 D10: the app's own access policy goes with it (an orphan would
        # pile up with every instance start/stop on a restricted box).
        try:
            own = self._find_expression_policy(_APP_ACCESS_PREFIX + slug)
            if own is not None:
                self._delete(f"/api/v3/policies/expression/{own['pk']}/")
        except Exception as e:  # noqa: BLE001
            logger.warning("deleting the access policy of %s failed: %s", host, e)
            ok = False

        # delete the provider
        if ppk is not None:
            self._delete(f"/api/v3/providers/proxy/{ppk}/")
        return ok

    def deregister_instance(self, host: str) -> bool:
        """Delete the per-instance application + provider and detach from the
        outpost. Idempotent; missing objects are treated as already-gone."""
        if not self.enabled:
            return False
        self._restriction_errors.pop(host, None)
        try:
            ok = self._delete_instance_objects(host)
            logger.info("Deregistered Authentik forward-auth for %s", host)
            return ok
        except Exception as e:  # noqa: BLE001
            logger.error("Authentik deregister_instance(%s) failed: %s", host, e)
            return False

    def _host_from_provider_name(self, name: str) -> str | None:
        """Recover the instance host from a per-instance provider name."""
        if name.startswith(_PROVIDER_PREFIX):
            return name[len(_PROVIDER_PREFIX):].strip()
        return None

    def sweep_orphans(self, live_hosts) -> dict:
        """Delete per-instance forward-auth providers/apps that have no matching
        live instance (LOW-2). A failed-delete deregister or an upgrade leftover
        would otherwise accumulate orphan Authentik providers. Idempotent,
        404-tolerant; matches on the exact per-instance provider-name prefix
        (`Agent Instance — <host>`), independent of the unreliable ?slug filter.

        `live_hosts` — iterable of the currently-provisioned instance hosts
        (e.g. from provisioner._instance_host for every DB instance).
        """
        summary = {"checked": 0, "orphans_removed": 0, "failed": 0}
        if not self.enabled:
            return summary
        live = set(live_hosts)
        try:
            # page through ALL proxy providers, filter by our name prefix
            res = self._get("/api/v3/providers/proxy/",
                            search=_PROVIDER_PREFIX.strip(" —"), page_size=200)
            for p in res.get("results", []):
                name = p.get("name", "")
                host = self._host_from_provider_name(name)
                if host is None:
                    continue  # not one of ours
                summary["checked"] += 1
                if host in live:
                    continue  # has a live instance — keep
                try:
                    self._delete_instance_objects(host, ppk=p.get("pk"))
                    summary["orphans_removed"] += 1
                    logger.info("sweep_orphans: removed orphan forward-auth for %s", host)
                except Exception as e:  # noqa: BLE001
                    summary["failed"] += 1
                    logger.warning("sweep_orphans: failed to remove %s: %s", host, e)
        except Exception as e:  # noqa: BLE001
            logger.error("sweep_orphans failed: %s", e)
        return summary

    def active_by_uid(self, page_size: int = 200, max_pages: int = 500):
        """#2476 (A21): {uid: is_active} for every Authentik user, keyed by the
        `uid` the manager stores as an instance's owner (X-Authentik-Uid).

        COMPLETE or None: any error, a page without its pagination, a count
        that moved between pages, a row without a uid, or a listing short of
        its count decides nothing (the token reconcile then revokes nothing
        on "absent")."""
        if not self.enabled:
            return None
        out, total = {}, None
        try:
            for page in range(1, max_pages + 1):
                d = self._get("/api/v3/core/users/", page=page, page_size=page_size,
                              include_groups="false")
                res, pag = d.get("results"), d.get("pagination")
                if not isinstance(res, list) or not isinstance(pag, dict) \
                        or not isinstance(pag.get("count"), int):
                    return None
                if total is None:
                    total = pag["count"]
                elif pag["count"] != total:
                    return None
                for u in res:
                    uid = u.get("uid") if isinstance(u, dict) else None
                    if not isinstance(uid, str) or not uid:
                        return None
                    out[uid] = bool(u.get("is_active"))
                if not pag.get("next"):
                    return out if len(out) == total else None
        except Exception as e:  # noqa: BLE001 — undecided, never "inactive"
            logger.warning("active_by_uid: the Authentik user listing failed: %s", type(e).__name__)
            return None
        return None

    def ping(self) -> bool:
        try:
            self._get("/api/v3/admin/version/")
            return True
        except Exception:  # noqa: BLE001
            return False
