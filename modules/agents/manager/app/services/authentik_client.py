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
"""

import logging
import os
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Name/slug prefixes so the per-instance objects are recognisable + collision-
# free, and so deregistration + the init-time sweep can find them.
_PROVIDER_PREFIX = "Agent Instance — "
_APP_SLUG_PREFIX = "ai-"

# The base dashboard provider we clone flows + property-mappings from, so the
# per-instance providers behave identically (same scopes, same auth flow).
_BASE_PROVIDER_NAME = "Caddy Forward Auth Provider for Agents"

_DEFAULT_GATE_GROUP = "razzfazz.ai AI Agents Users"
_ADMIN_GROUP = "razzfazz.ai Super Admins"


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

    # ── low-level API ────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json"}

    def _get(self, path, **params):
        r = httpx.get(f"{self._base}{path}", headers=self._headers(),
                      params=params or None, timeout=15)
        r.raise_for_status()
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
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = httpx.post(f"{self._base}{path}", headers=self._headers(),
                       json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    def _patch(self, path, body):
        r = httpx.patch(f"{self._base}{path}", headers=self._headers(),
                        json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    def _delete(self, path):
        r = httpx.delete(f"{self._base}{path}", headers=self._headers(), timeout=15)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()
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
        name = self._provider_name(host)
        res = self._get("/api/v3/providers/proxy/", search=name)
        for p in res.get("results", []):
            if p["name"] == name:
                return p["pk"]
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
        for gname in (self._gate_group, _ADMIN_GROUP):
            res = self._get("/api/v3/core/groups/", search=gname)
            for g in res.get("results", []):
                if g["name"] == gname:
                    pks.append(g["pk"])
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
                    "until the group resolves.", host, self._gate_group, _ADMIN_GROUP)
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
                created = self._post("/api/v3/core/applications/", {
                    "name": pname,
                    "slug": slug,
                    "provider": ppk,
                    "meta_launch_url": "blank://blank",
                    "meta_description":
                        "Per-instance forward-auth for a coding-agent terminal.",
                })
                app_pk = created["pk"]  # UUID — the policy-binding target
                app_created = True

            # 3. bind the gate group(s) BEFORE attaching to the outpost, so the
            #    host is never briefly reachable as an unbound (all-users) app.
            #    On failure, roll back a freshly-created app+provider (fail-closed).
            try:
                self._ensure_group_bindings(app_pk, group_pks)
            except Exception as be:  # noqa: BLE001
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

        # delete the provider
        if ppk is not None:
            self._delete(f"/api/v3/providers/proxy/{ppk}/")
        return ok

    def deregister_instance(self, host: str) -> bool:
        """Delete the per-instance application + provider and detach from the
        outpost. Idempotent; missing objects are treated as already-gone."""
        if not self.enabled:
            return False
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

    def ping(self) -> bool:
        try:
            self._get("/api/v3/admin/version/")
            return True
        except Exception:  # noqa: BLE001
            return False
