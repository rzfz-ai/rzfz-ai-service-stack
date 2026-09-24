# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""REST API for agent instance lifecycle management."""

import json
import logging
import os
import uuid

from flask import Blueprint, current_app, jsonify, request

from app.services.provisioner import make_user_slug, slug_candidates
from razzfazz_common.auth import parse_authentik_headers
from razzfazz_common.proxy_anchor import DEFAULT_INGRESS_HOST, from_trusted_proxy

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')

# --- source-IP anchor (#390) -------------------------------------------------
# Every route below authorises off `X-Authentik-*` headers via `_get_user()`.
# Those headers are only trustworthy because Caddy — the sole ingress, running
# Authentik `forward_auth` — sets them. Nothing in `api_bp` checked that the
# request ACTUALLY came from Caddy, so any peer on the shared `_default` network
# could dial `agent-manager:5000` directly, forge an identity, and drive the
# full lifecycle: launch, delete, upgrade, read logs, mint a `bootstrap-url`.
# The provisioned agent sandboxes sit on that same network (#256), which makes
# the blast radius "any agent can delete/read any user's agents".
#
# `proxy.py::_proxy_proof_ok` (C1 / PR #84) closed exactly this for the agent
# proxy routes; `api_bp` was simply never given the same guard. We now share one
# implementation (`razzfazz_common.proxy_anchor`) rather than write a fourth.
_TRUST_ENV = "RZFZ_AGENT_MANAGER_TRUST_ALL_PROXIES"

# Caddy is trusted for the WHOLE blueprint.
_INGRESS_HOSTS = (DEFAULT_INGRESS_HOST,)

# start-portal gets a narrow, EXPLICIT allowance. It server-side proxies two
# read-only GETs to render the start page's live agent tiles, forwarding the
# caller's identity headers (`core/start-portal/app.py`):
#
#   GET /api/instances     — `_fetch_running_agents`, lists the user's tiles
#   GET /api/ready/<id>    — `api_ready`, the "Starting…" spinner's poll
#
# Its peer address is start-portal's, not Caddy's, so a Caddy-only anchor 403s
# both. Worse, BOTH callers swallow the failure — `_fetch_running_agents` returns
# `[]` and `api_ready` returns `{'ready': False}` with HTTP 200 — so the breakage
# would be silent: agent tiles simply vanish from the start page, and any that
# remained would spin on "Starting…" forever, with nothing in the UI to diagnose.
#
# The allowance is deliberately an allow-list of those two GETs, not a blanket
# trust of the host: start-portal must NOT gain launch/delete/upgrade/logs/
# bootstrap-url authority it never had. agent-manager still scopes both
# responses to the forwarded identity, exactly as when Caddy calls them.
_START_PORTAL_HOSTS = tuple(
    h.strip() for h in (os.environ.get("START_PORTAL_HOST")
                        or "razzfazz-start-portal").split(",") if h.strip()
)
_START_PORTAL_GET_PATHS = ('/api/instances',)     # exact match
_START_PORTAL_GET_PREFIXES = ('/api/ready/',)     # parameterised route


def _start_portal_path_allowed() -> bool:
    """True for the two read-only GETs start-portal is permitted to proxy."""
    if request.method != 'GET':
        return False
    path = request.path or ''
    return path in _START_PORTAL_GET_PATHS or path.startswith(_START_PORTAL_GET_PREFIXES)


# The per-user chat pipes get the same kind of narrow allowance (#2093). The
# hermes / moltis / opencode pipes are Open WebUI Functions — they run INSIDE the
# openwebui container and resolve the calling user's instance by dialling
# `agent-manager:5000/api/find/<type>` directly
# (`modules/chat/functions/_lib/per_user_routing.py`), forwarding the identity
# Open WebUI itself established through the same Authentik SSO (`__user__`).
# Their peer address is openwebui's, not Caddy's, so the #390 anchor refused
# every one of those calls — measured on 0.91: 403 from the openwebui container,
# 404 for the identical call from caddy, log line "Refusing GET /api/find/hermes
# … not the ingress (#390)". The pipe helper treats any non-404 as "degraded"
# and answers NOT PROVISIONED, so EVERY per-user pipe told EVERY user — running
# instance or not — that they had no agent (rc1 Phase 3, scenario 18; #2093).
#
# Allow-list, not host trust: openwebui may call exactly `GET /api/find/<type>`,
# which agent-manager still scopes to the forwarded identity (the DB query
# filters by user_slug; the response carries only that user's own instance and
# its per-instance secret — what the user's own pipe needs to reach it). It gains
# no launch/delete/upgrade/logs/bootstrap-url authority. A peer that is neither
# Caddy, start-portal nor openwebui gets nothing, this path included.
#
# STATED TRADE (review, DevBox 2026-09-14): the scoping is BY FORWARDED IDENTITY,
# so from inside the openwebui container the identity is forgeable by
# construction — anything executing there can name any user and read that
# user's /api/find response, per-instance secret included. Exactly as the
# start-portal exception already is for /api/instances. It is deliberate:
# openwebui already holds every user's session and chat history, the pipes
# cannot work without it, and #390's target was arbitrary network peers (the
# agent sandboxes), not the two containers admitted here on purpose. Widening
# this to loopback or to any further host is a security decision, not a fix.
_PIPES_HOSTS = tuple(
    h.strip() for h in (os.environ.get("OPENWEBUI_HOST")
                        or "openwebui").split(",") if h.strip()
)
_PIPES_GET_PREFIXES = ('/api/find/',)              # parameterised route


def _pipes_path_allowed() -> bool:
    """True for the one read-only GET the per-user chat pipes are permitted."""
    if request.method != 'GET':
        return False
    return (request.path or '').startswith(_PIPES_GET_PREFIXES)


@api_bp.before_request
def _require_trusted_upstream():
    """Refuse header-authenticated API calls that did not transit Caddy (#390).

    Fails CLOSED: an unresolvable `caddy` denies rather than admits. Does not
    consult X-Forwarded-For — a forged header must never move the anchor.
    """
    if from_trusted_proxy(_INGRESS_HOSTS, trust_env=_TRUST_ENV):
        return None
    if _start_portal_path_allowed() and from_trusted_proxy(
            _START_PORTAL_HOSTS, trust_env=_TRUST_ENV):
        return None
    if _pipes_path_allowed() and from_trusted_proxy(
            _PIPES_HOSTS, trust_env=_TRUST_ENV):
        return None
    logger.warning(
        "Refusing %s %s from %s — not the ingress (#390). Forged X-Authentik-* "
        "headers cannot authorise agent lifecycle calls.",
        request.method, request.path, request.remote_addr,
    )
    return jsonify({'error': 'Forbidden'}), 403


def _get_user():
    info = parse_authentik_headers()
    if not info["username"]:
        return None, None, None, None
    user_slug = make_user_slug(info["username"])
    return info["username"], info["uid"], user_slug, info["groups"]


def _slug_candidates(username: str) -> tuple[str, ...]:
    """Ownership-match candidates for `username` (#192): the CURRENT
    make_user_slug plus the LEGACY pre-hash-suffix slug some instances are
    still stored under (provisioned before #36/PR#61 introduced the
    <=14>-<6hex> scheme). Used everywhere an instance's stored `user_slug`
    is compared against the calling user's identity, so a user regains
    "Open" (not "Launch") on their own pre-existing instances instead of
    being 404'd/403'd by an exact-match comparison.

    SECURITY: candidates are derived ONLY from `username` (the REQUESTING
    user), never from the instance under test — a different user's legacy
    slug can never satisfy this match, so cross-user isolation is preserved.
    """
    return slug_candidates(username)


#: #1957 — the ONE admin bypass in this file, and the routes it covers.
#: Operator decision 2026-09-12: an admin may start, stop and resize ANY
#: user's agent — those three, and no others. The other eleven ownership
#: checks in this file stay absolute; test_1957 pins both halves so the next
#: widening is a deliberate act rather than a drift. The tuple mirrors
#: app/__init__.py's `admin_groups` (test_1957 holds them equal).
ADMIN_GROUPS = ('authentik Admins', 'razzfazz.ai Super Admins')
ADMIN_OPERABLE_ROUTES = ('start', 'stop', 'memory')


def _is_admin(groups) -> bool:
    return any(g in ADMIN_GROUPS for g in (groups or []))


def _may_operate(instance, username: str, groups, route: str) -> tuple[bool, bool]:
    """``(allowed, as_admin)``. The owner may always operate; an admin may
    operate someone else's instance on the three ADMIN_OPERABLE_ROUTES only.
    `as_admin` is True exactly when the bypass carried the request, so the
    caller can write the audit line that tells the two apart afterwards."""
    if _owns_instance(instance, username):
        return True, False
    if instance and route in ADMIN_OPERABLE_ROUTES and _is_admin(groups):
        return True, True
    return False, False


def _audit_admin_action(route: str, instance, username: str, user_id, extra: dict | None = None) -> None:
    """An admin acting on someone else's agent must be distinguishable
    afterwards from the owner doing it (#1957): one audit row, action
    `admin.<route>`, carrying WHO (the admin) and WHOSE (the owner's slug)."""
    details = {'as_admin': True, 'admin': username, 'owner_slug': instance.get('user_slug'), 'route': route}
    if extra:
        details.update(extra)
    try:
        current_app.db.log_audit(user_id or username, f'admin.{route}',
                                 agent_type=instance.get('agent_type'),
                                 instance_id=instance.get('id'), details=details)
    except Exception:
        logger.warning("audit row for admin.%s on %s by %r could not be written",
                       route, instance.get('id'), username, exc_info=True)


def _owns_instance(instance, username: str) -> bool:
    """True iff `username` owns `instance` under either slug scheme (#192)."""
    return bool(instance) and instance['user_slug'] in _slug_candidates(username)


def _reconcile_legacy_slug(instance, username: str):
    """Best-effort forward-migration (#192): if `instance` was matched via
    the LEGACY slug candidate (not the current one), update its stored
    `user_slug` to the current slug so future lookups are an exact match.
    Failures are swallowed — ownership continues to work via
    _owns_instance()/slug_candidates() regardless of whether the migration
    lands, so this is pure cleanup, never load-bearing for correctness.
    """
    if not instance:
        return
    current_slug = make_user_slug(username)
    if instance.get('user_slug') == current_slug:
        return
    try:
        current_app.db.migrate_legacy_user_slug(instance['id'], current_slug)
    except Exception:
        logger.warning(
            "Legacy user_slug migration failed for instance %s (user=%r)",
            instance.get('id'), username, exc_info=True,
        )


@api_bp.route('/instances', methods=['GET'])
def list_instances():
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    from app.services.caddy_client import instance_token
    from app.services.provisioner import instance_display_name
    agents_domain = current_app.config.get('AGENTS_DOMAIN', '')

    # #192: query by BOTH the current and legacy slug so instances provisioned
    # before the hash-suffix change still show up (and render "Open", not
    # "Launch", on the start-portal tiles that read this endpoint).
    instances = []
    for row in current_app.db.get_user_instances(_slug_candidates(username)):
        _reconcile_legacy_slug(row, username)
        inst = dict(row)
        # Canonical per-instance URL, derived from the SAME opaque token that
        # caddy_client uses to register the route: {type}-{token}.agents.<domain>.
        # Consumers (start-portal tiles, etc.) MUST use this `url` rather than
        # building one from agent_type + user_slug — the username is
        # intentionally NOT part of the subdomain (it would leak fleet
        # membership via DNS / TLS SNI; see caddy_client.py). Deriving it here
        # keeps token logic in the one service that owns AGENT_DOMAIN_TOKEN_SECRET.
        # #233 — one derivation of the shown name, shared with the tree and
        # start-portal's tiles (which build from this endpoint). A second
        # derivation elsewhere is how the surfaces drift apart.
        #
        # No catalog lookup: the row already carries `type_display_name` from
        # the agent_types JOIN, so asking the catalog per instance would be an
        # extra round trip for a value we are holding.
        inst['display_name'] = instance_display_name(row)
        # The raw override too, so a consumer can tell "the user named this"
        # from "this is just the type name" — start-portal keeps its friendlier
        # "My <type>" wording for agents nobody has renamed.
        _cfg = row.get('config') or {}
        if isinstance(_cfg, str):
            try:
                _cfg = json.loads(_cfg)
            except (ValueError, TypeError):
                _cfg = {}
        inst['custom_name'] = _cfg.get('custom_name') or None
        # #615: the raw config JSONB carries provisioner-internal secrets
        # (_generated_secret, _gitea_token, _db_password, _llm_manager_key).
        # They are the owner's own secrets — but they do not belong in
        # DevTools/HAR dumps. Consumers use ONLY the derived fields above.
        #
        # AGM-14 (#1039) — SCOPE OF THIS REDACTION, so nobody reads it as a
        # boundary it is not. `/api/find/<agent_type>` DELIBERATELY still
        # returns `cfg['_generated_secret']` as `auth.token` to the same
        # browser session over the same origin: the Open WebUI / chat pipes
        # need that token to talk to the user's own agent, and there is no
        # second channel to hand it over on. So this is hygiene — it keeps a
        # per-user secret out of an incidental HAR dump and off the wire on
        # every list call — NOT an isolation boundary. Nothing here (or in
        # `/api/find`) ever crosses users: both are filtered by the caller's
        # own slug candidates. Moving `/api/find` behind a service-to-service
        # anchor (its only real callers are in-cluster pipes) is the change
        # that would make it one; that needs a live stack to validate and is
        # tracked separately, NOT silently assumed here.
        inst.pop('config', None)
        if inst.get('id') and agents_domain:
            inst['url'] = (
                f"https://{inst['agent_type']}-{instance_token(inst['id'])}"
                f".{agents_domain}"
            )
        instances.append(inst)
    return jsonify({'instances': instances})


@api_bp.route('/rename/<instance_id>', methods=['POST'])
def rename(instance_id):
    """#233 — set or clear this instance's user-chosen name.

    Body: {"name": "..."}; an empty/whitespace name clears the override and the
    agent shows its type name again.

    Deliberately NOT tier-gated. A name is cosmetic and scoped to your own
    agent — gating it would be governance theatre. What IS enforced is the
    content: `validate_custom_name` refuses control and formatting characters,
    because this string is injected into the container as `AGENT_LABEL` and
    rendered into a terminal title bar, where escape sequences are interpreted
    rather than displayed.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        instance = current_app.db.get_instance(uuid.UUID(instance_id))
    except (ValueError, TypeError):
        return jsonify({'error': 'Instance not found'}), 404
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    from app.services.provisioner import validate_custom_name
    name, error = validate_custom_name((request.get_json(silent=True) or {}).get('name'))
    if error:
        return jsonify({'error': 'invalid_name', 'message': error}), 400

    iid, message = current_app.provisioner.rename_instance(
        instance_id, username, name)
    if iid is None:
        return jsonify({'error': message}), 404
    return jsonify({'instance_id': iid, 'message': message, 'name': name})


@api_bp.route('/pids/<instance_id>', methods=['POST'])
def pids(instance_id):
    """#232 — change this instance's PID cap. Body: {"pids_limit": N}.

    Tier-gated SERVER-SIDE (the UI only reflects it), like memory: PIDs are a
    shared kernel resource, so a raised cap is a whole-box risk rather than a
    personal preference. Bounds are enforced here too — a cap high enough to
    survive a fork bomb has stopped being a cap.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        instance = current_app.db.get_instance(uuid.UUID(instance_id))
    except (ValueError, TypeError):
        return jsonify({'error': 'Instance not found'}), 404
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    from app.services.provisioner import validate_pids_limit
    value, error = validate_pids_limit(
        (request.get_json(silent=True) or {}).get('pids_limit'))
    if error:
        return jsonify({'error': 'invalid_pids_limit', 'message': error}), 400

    iid, message = current_app.provisioner.update_pids(
        instance_id, username, groups, value)
    if iid is None:
        # Tier refusal vs not-found — a refusal must not read as a 404.
        status = 404 if 'not found' in message.lower() else 403
        return jsonify({'error': 'refused', 'message': message}), status
    return jsonify({'instance_id': iid, 'message': message,
                    'pids_limit': value})


# --- #244-Q2: the portal tree ------------------------------------------------
# DB state → the circle the user sees. Deliberately NOT a 1:1 echo of `state`:
# `provisioning` and `running` are both "not usable yet, but on their way" from
# the operator's seat, and `error` has to be visually distinct from `stopped` —
# a crashed agent that renders as merely stopped hides a failure behind a state
# the user thinks they caused.
_TREE_STATUS = {
    'running': 'running',        # green once /api/ready confirms it is serving
    'provisioning': 'starting',  # amber
    'stopped': 'stopped',        # grey
    'error': 'error',            # red
}


def _capacity(user_slug):
    try:
        budget = current_app.provisioner.effective_mem_budget_mb()
        in_use = int(current_app.db.sum_running_mem_mb())
        cap = current_app.provisioner._per_user_cap_mb()
        user_in_use = int(current_app.db.sum_user_running_mem_mb(user_slug))
        return {'budget_mb': budget or None, 'in_use_mb': in_use,
                'user_cap_mb': cap or None, 'user_in_use_mb': user_in_use}
    except Exception:  # noqa: BLE001 - meter is advisory, tree must render
        return None


@api_bp.route('/tree', methods=['GET'])
def tree():
    """The portal's left rail (#244-Q2): every agent type, with live state.

    Rendered client-side rather than server-side so a circle can turn green
    without a page reload — the browser overlays readiness per running node.

    Three deliberate properties:

    * **Owner-scoped.** Same `_slug_candidates` match every other route uses. A
      tree that listed another user's instances would be #397 with a nicer UI.
    * **Every catalog type appears**, including ones the user has never
      launched (`status: absent`). The `+` has to be discoverable for an agent
      you do not have yet — that is the whole point of a tree over a card grid.
    * **No probing.** `/api/ready` is an HTTP call INTO the container with a 2s
      timeout, and `docker stats` is a per-container round trip. Doing either
      per node inside this handler would make the sidebar the slowest thing on
      the page and would scale with the number of agent types. The browser
      fires `/api/ready/<id>` per RUNNING node instead — in parallel, and only
      where it can change anything.

    Pane URLs are the same-origin `/i/<token>/` form from Q1, never the
    per-instance subdomain: a subdomain in a pane is cross-origin again, which
    is exactly the frame-ancestors/CSP/SSO problem Q1 exists to remove.

    The `groups` shape is present from day one even though P1 emits one group
    per agent type with a single node each. By-folder grouping (P2) and
    multiple instances per type change what fills a group, not the shape — so
    the client does not get rewritten when they land.
    """
    username, user_id, user_slug, groups_hdr = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    from app.services.caddy_client import instance_token
    from app.services.provisioner import instance_display_name

    tier = current_app.db.resolve_user_tier(groups_hdr)
    instances = list(current_app.db.get_user_instances(_slug_candidates(username)))
    by_type = {}
    for row in instances:
        by_type.setdefault(row['agent_type'], []).append(row)

    allowed_types = tier.get('allowed_types') if tier else []

    groups = []
    for at in current_app.catalog.get_types():
        type_allowed = bool(tier and (allowed_types is None or at['id'] in allowed_types))
        heavy_ok = at['tier'] != 'heavy' or bool(tier and tier['max_heavy'] > 0)
        restricted = not type_allowed or not heavy_ok
        # #1904: WHICH of the two, so the console does not have to guess. It
        # guessed "not available on your tier" for both, and on a heavy-quota
        # box that sends the operator to check a type list the type is in —
        # they look, find it, and are back where they started.
        restricted_reason = (None if not restricted
                             else 'tier' if not type_allowed else 'heavy-quota')

        try:
            ports = (json.loads(at['ports']) if isinstance(at['ports'], str)
                     else at['ports']) or {}
        except (TypeError, ValueError):
            ports = {}
        has_web_ui = bool(ports.get('internal'))
        from app.services.catalog import ui_kind_for as _ukf
        _kind = _ukf(at)

        rows = by_type.get(at['id'], [])
        nodes = []
        for row in rows:
            running = row['state'] == 'running'
            token = instance_token(row['id']) if running else None
            nodes.append({
                'instance_id': str(row['id']),
                'agent_type': at['id'],
                # #233 — the user's own name when they set one. The tree is the
                # surface that needs it most: "Coding Tools / Hermes / Moltis"
                # is a type list, not YOUR agents.
                'label': instance_display_name(row, at),
                'icon_url': at.get('icon_url'),
                'description': at.get('description'),
                'state': row['state'],
                'status': _TREE_STATUS.get(row['state'], 'stopped'),
                'can_launch': False,
                'restricted': restricted,
                'restricted_reason': restricted_reason,
                # Same-origin only — see the docstring.
                'pane_url': f"/i/{token}/" if (running and has_web_ui) else None,
                'terminal_url': f"/ws/terminal/{row['id']}" if running else None,
                # #1986: the tree/portal says WHERE it is sending the user, so the
                # detail page can send them back there. Without it the back link
                # falls back to the agents overview and a multi-pane Workspace
                # layout is lost on every settings visit.
                'settings_url': f"/instance/{row['id']}?from=portal",
                'ui': {
                    # #244-Q4 / W1: catalog-driven. 'web'-kind types still get a
                    # terminal (docker exec works on every image) but open on
                    # their UI; pure-terminal types never show a dead web tab.
                    'terminal': running,
                    'web': running and has_web_ui and _kind in ('web', 'both'),
                },
                # #1871: default to the agent's MAIN UI whenever it has one
                # ('both' = a web UI AND a terminal, e.g. coding agents) — the
                # raw terminal is last-resort access via Settings, not the
                # entrypoint. Pure-terminal types (no web UI) keep 'terminal'.
                'default_pane': ('web' if (has_web_ui and _kind in ('web', 'both'))
                                 else 'terminal'),
                # W1 files panel: offered iff the type declares volume mounts
                'files': bool(_type_mounts(at)),
            })

        # #1988: instances are real. A group that already has instances offers
        # ANOTHER one while the tier's max_per_type allows it - the "+" a card
        # grid could never show, and the reason the folders have something to
        # order. The launcher sends `new_instance: true` for this node.
        max_per_type = (tier or {}).get('max_per_type')
        # An explicit integer allowance only: a tier row without the column (test
        # doubles, legacy shapes) offers nothing more rather than everything.
        if nodes and not restricted and isinstance(max_per_type, int) and len(rows) < max_per_type:
            nodes.append({
                'instance_id': None,
                'agent_type': at['id'],
                'label': f"{at['display_name']} (another)",
                'icon_url': at.get('icon_url'),
                'description': at.get('description'),
                'state': None,
                'status': 'absent',
                'can_launch': True,
                'launch_more': True,
                'restricted': False,
                'restricted_reason': None,
                'pane_url': None,
                'terminal_url': None,
                'settings_url': None,
                'ui': {'terminal': False, 'web': False},
            })
        if not nodes:
            nodes.append({
                'instance_id': None,
                'agent_type': at['id'],
                'label': at['display_name'],
                'icon_url': at.get('icon_url'),
                'description': at.get('description'),
                'state': None,
                'status': 'absent',
                'can_launch': not restricted,
                'restricted': restricted,
                'restricted_reason': restricted_reason,
                'pane_url': None,
                'terminal_url': None,
                'settings_url': None,
                'ui': {'terminal': False, 'web': False},
            })

        groups.append({'key': at['id'], 'label': at['display_name'], 'nodes': nodes})

    return jsonify({
        'user': username,
        'grouping': 'type',
        'groups': groups,
        # Rework-W3 (#244 item 2): the capacity METER. Enforcement already
        # lives in provisioner.check_memory_budget (launch refuses past the
        # budget) — this is the visibility half, so the portal can grey the
        # controls BEFORE a refused launch instead of after. Best-effort: a
        # docker /info hiccup must not take the tree down.
        'capacity': _capacity(user_slug),
        # #959/W3: max_instances was reading a tier key ('max_instances')
        # that no real quota_tiers row (or the synthetic admin tier) ever
        # set, so this label silently rendered blank forever — the cap was
        # never enforced OR shown. Now sourced from the real max_running
        # column, which check_quota/start actually enforce (see
        # provisioner.py). Wire key name kept as max_instances — the
        # portal's updateQuota() already reads q.max_instances.
        'quota': {
            'running': sum(1 for i in instances if i['state'] == 'running'),
            'max_instances': (tier or {}).get('max_running'),
        },
    })


@api_bp.route('/launch/<agent_type>', methods=['POST'])
def launch(agent_type):
    """Launch a new instance of `agent_type` for the calling user.

    AGM-2: the request body is NOT the instance config. It is filtered here to
    an explicit allow-list (`sanitize_launch_config`) — the launch body used to
    land verbatim in `instance_config`, which let a basic-tier caller set an
    unbounded `pids_limit` (docker reads -1 as UNLIMITED → fork-bomb DoS in a
    sandboxed agent), an unvalidated `custom_name` (OSC escape into the
    terminal title bar) and provisioner-internal `_`-prefixed keys such as
    `_llm_manager_key_id` (revoking another user's LLM key on delete).
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    from app.services.provisioner import sanitize_launch_config
    try:
        tier = current_app.db.resolve_user_tier(groups)
    except Exception:  # noqa: BLE001 — an unreadable tier is a basic tier here
        tier = None
    user_config, error = sanitize_launch_config(
        request.get_json(silent=True), tier)
    if error:
        return jsonify({'error': 'invalid_config', 'message': error}), 400

    instance_id, message = current_app.provisioner.launch(
        agent_type, user_id, username, groups, user_config
    )

    if instance_id:
        return jsonify({'instance_id': instance_id, 'message': message})
    return jsonify({'error': message}), 400


@api_bp.route('/presets', methods=['GET'])
def list_presets():
    """W5 (#620): quick-start presets, filtered to currently-enabled types.

    A preset whose agent_type is disabled on this box simply disappears
    from the strip — no dead buttons."""
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403
    # #1186: shared with the server-rendered launch band on /agents — one
    # data source for both surfaces (catalog.launchable_presets).
    from app.services.catalog import launchable_presets
    return jsonify({'presets': launchable_presets(current_app.catalog)})


@api_bp.route('/launch-preset/<preset_id>', methods=['POST'])
def launch_preset(preset_id):
    """W5 (#620): one-click preset launch — config resolved SERVER-SIDE.

    The client sends only the preset id; the launch config comes from the
    catalog's PRESETS table, so a tampered request cannot smuggle arbitrary
    config through this path (the raw /api/launch keeps its own rules for
    the advanced flow). Quota/tier enforcement is provisioner.launch's,
    unchanged."""
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403
    from app.services.catalog import preset_by_id
    preset = preset_by_id(preset_id)
    if not preset:
        return jsonify({'error': 'Unknown preset'}), 404
    type_row = current_app.catalog.get_type(preset['agent_type'])
    if not type_row or not type_row.get('enabled', True):
        return jsonify({'error': 'Agent type not enabled on this box'}), 409
    instance_id, message = current_app.provisioner.launch(
        preset['agent_type'], user_id, username, groups,
        dict(preset.get('config') or {})
    )
    if instance_id:
        return jsonify({'instance_id': instance_id, 'message': message,
                        'agent_type': preset['agent_type']})
    return jsonify({'error': message}), 400


@api_bp.route('/audit', methods=['GET'])
def audit_log():
    """W5 (#620, EPIC #244 item 10): the owner's own activity trail.

    Strictly self-scoped — user_id comes from the authenticated identity,
    never from a parameter, so this cannot become an enumeration surface.
    Rows come from the existing audit_log table (log_audit writes)."""
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403
    try:
        limit = min(max(int(request.args.get('limit', 50)), 1), 200)
    except (TypeError, ValueError):
        limit = 50
    rows = current_app.db.get_audit_log(limit=limit, user_id=user_id)
    out = []
    for r in rows or []:
        out.append({
            'timestamp': (r['timestamp'].isoformat()
                          if hasattr(r.get('timestamp'), 'isoformat')
                          else r.get('timestamp')),
            'action': r.get('action'),
            'agent_type': r.get('agent_type'),
            'instance_id': str(r.get('instance_id') or '') or None,
        })
    return jsonify({'audit': out})


@api_bp.route('/start/<instance_id>', methods=['POST'])
def start(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    allowed, as_admin = _may_operate(instance, username, groups, 'start')
    if not allowed:
        return jsonify({'error': 'Instance not found'}), 404
    if as_admin:
        _audit_admin_action('start', instance, username, user_id)

    # #959/W3: pass groups so start() can enforce the per-user max_running
    # cap on a resumed (previously-stopped) instance — same as a fresh launch.
    iid, message = current_app.provisioner.start(instance_id, username, groups)
    if not iid:
        return jsonify({'error': message}), 400
    return jsonify({'instance_id': iid, 'message': message})


@api_bp.route('/stop/<instance_id>', methods=['POST'])
def stop(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    allowed, as_admin = _may_operate(instance, username, groups, 'stop')
    if not allowed:
        return jsonify({'error': 'Instance not found'}), 404
    if as_admin:
        _audit_admin_action('stop', instance, username, user_id)

    iid, message = current_app.provisioner.stop(instance_id, username)
    return jsonify({'instance_id': iid, 'message': message})


@api_bp.route('/repair/<instance_id>', methods=['POST'])
def repair(instance_id):
    """#244-H1: one-click Reconnect / Repair for a single instance.

    Re-registers the per-instance Authentik provider immediately, instead of
    waiting for the periodic sweep (up to ~2 min) to notice — the fix for
    "my agent suddenly 404s" (#237). #606: there is no Caddy-route half
    anymore; the static *.agents wildcard carries every instance host, so
    a route can neither be lost nor need repair.

    Unlike the sweep, it re-registers unconditionally — the user clicks
    because something is broken. `_register_authentik` is idempotent.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    result = current_app.provisioner.repair_instance(instance_id, username)
    if result is None:
        return jsonify({'error': 'Instance not found'}), 404
    # #1143: the container was GONE and repair recreated it from the DB row.
    # That is the repair succeeding, so it must not come back as the 409 dead
    # end ("use Start") — start() could not help either, because the stale
    # 'running' row made it refuse too.
    if 'recreated' in result:
        recreated = bool(result['recreated'])
        return jsonify({
            'repaired': recreated,
            'result': result,
            'message': ('The container was missing and has been recreated — '
                        'your workspace volumes were kept.'
                        if recreated else
                        f"Could not recreate the missing container: "
                        f"{result.get('message') or 'unknown error'}"),
        }), (200 if recreated else 500)
    if result.get('skipped'):
        return jsonify({
            'repaired': False,
            'result': result,
            'message': 'Container is not running — use Start instead.',
        }), 409

    ok = bool(result.get('authentik'))
    return jsonify({
        'repaired': ok,
        'result': result,
        'message': ('Reconnected — the agent should be reachable again.'
                    if ok else
                    'Partial repair: '
                    f"route={'ok' if result.get('caddy') else 'FAILED'}, "
                    f"sso={'ok' if result.get('authentik') else 'FAILED'}."),
    }), (200 if ok else 500)


@api_bp.route('/restart/<instance_id>', methods=['POST'])
def restart(instance_id):
    """ga.2 (#219): restart a running instance in place (docker restart).

    Non-destructive — the container and its named volumes are untouched; only
    the process inside is bounced. Recovers a wedged agent (hung UI / stuck
    runtime) without losing chats, skills, files or config.

    #244-H1: the Caddy route and per-instance Authentik provider are now
    re-registered after the bounce rather than assumed to have survived — that
    assumption is exactly how a restarted agent came back 404ing (#237).
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    iid, message = current_app.provisioner.restart(instance_id, username)
    # Mirror /upgrade: an explicit failure ('Restart failed: …') surfaces as
    # 5xx so the dashboard JS renders the reason instead of a success toast;
    # a None id (not-found) as 404.
    status_code = 200
    if iid is None or 'failed' in message.lower():
        status_code = 500 if iid else 404
    return jsonify({'instance_id': iid, 'message': message}), status_code


@api_bp.route('/delete/<instance_id>', methods=['POST'])
def delete(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    # M030-S2: server-side confirm gate. The dashboard's per-instance
    # Settings page Danger Zone presents a type-to-confirm modal where
    # the user types "Delete" exactly; the JS adds ?confirm=Delete to the
    # POST. Server REJECTS without it — defense-in-depth against API-level
    # accidents (curl scripts, CSRF). Case-sensitive: typing the word
    # forces the user to read what they're confirming.
    if request.args.get('confirm') != 'Delete':
        return jsonify({
            'error': 'confirmation_required',
            'message': ('Delete requires explicit confirmation. The dashboard '
                        'presents a type-to-confirm modal — use that, not direct '
                        'API calls. If invoking from automation, append '
                        '?confirm=Delete (case-sensitive) to the POST URL.'),
        }), 400

    iid, message = current_app.provisioner.delete(instance_id, username)
    return jsonify({'instance_id': iid, 'message': message})


@api_bp.route('/upgrade/<instance_id>', methods=['POST'])
def upgrade(instance_id):
    """M030-S2: in-place upgrade an instance to a new image version.

    Non-destructive: stops the container, removes it WITHOUT removing
    volumes, recreates with the new image attached to the same named
    volumes. Body: optional {target_version: "..."} to pin a specific
    version (default: catalog's current version for this agent type).
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    body = request.get_json(silent=True) or {}
    target_version = body.get('target_version')
    iid, message = current_app.provisioner.upgrade(
        instance_id, username, target_version=target_version,
    )
    # Surface explicit failure as 4xx so dashboard JS can render an
    # alert instead of silently turning success-toast on top of an error.
    status_code = 200
    if iid is None or 'failed' in message.lower():
        status_code = 500 if iid else 404
    return jsonify({'instance_id': iid, 'message': message}), status_code


@api_bp.route('/memory/<instance_id>', methods=['POST'])
def memory(instance_id):
    """#36 / PR #84 — change an instance's per-instance memory limit.

    Body: {mem_gb: <int>}. ALL enforcement is server-side in
    provisioner.update_memory (tier gate + global budget + per-user cap); this
    route only proves ownership and passes the caller's groups through so the
    server-side tier check runs on the REAL identity. A hard refusal
    (tier-denied / budget-denied / not-found) is surfaced as 4xx so the settings
    page renders the reason instead of a silent success.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    allowed, as_admin = _may_operate(instance, username, groups, 'memory')
    if not allowed:
        return jsonify({'error': 'Instance not found'}), 404

    body = request.get_json(silent=True) or {}
    mem_gb = body.get('mem_gb')
    if as_admin:
        _audit_admin_action('memory', instance, username, user_id, {'mem_gb': mem_gb})
    iid, message = current_app.provisioner.update_memory(
        instance_id, username, groups, mem_gb=mem_gb,
    )
    if iid is None:
        # None == a hard server-side refusal (tier gate / budget / cap).
        return jsonify({'error': 'refused', 'message': message}), 403
    return jsonify({'instance_id': iid, 'message': message}), 200


# AGM-13 (#1039) — bounds for /api/logs?tail=N.
LOGS_TAIL_DEFAULT = 200
LOGS_TAIL_MAX = 5000


@api_bp.route('/logs/<instance_id>', methods=['GET'])
def logs(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    # AGM-13 (#1039): clamp, like /api/audit's `limit` already does. `type=int`
    # accepted anything — a huge value made the manager buffer the whole log in
    # memory on the caller's behalf, and docker reads some negatives as "all".
    # A non-integer yields None from `type=int`, which is the default case too.
    try:
        tail = min(max(int(request.args.get('tail', LOGS_TAIL_DEFAULT)), 1),
                   LOGS_TAIL_MAX)
    except (TypeError, ValueError):
        tail = LOGS_TAIL_DEFAULT
    log_text = current_app.docker_client.get_container_logs(
        instance['container_name'], tail=tail
    )
    return jsonify({'logs': log_text})


# ── Rework-W2 (#244 ext. 1): per-user portal prefs (layout/panes/folders) ───
PORTAL_PREFS_CAP = 64 * 1024  # a layout, not a document store


@api_bp.route('/portal/prefs', methods=['GET'])
def portal_prefs_get():
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403
    raw = current_app.db.get_portal_prefs(username)
    try:
        return jsonify({'prefs': json.loads(raw) if raw else {}})
    except ValueError:
        return jsonify({'prefs': {}})


@api_bp.route('/portal/prefs', methods=['PUT'])
def portal_prefs_put():
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403
    body = request.get_data(as_text=True) or ''
    if len(body) > PORTAL_PREFS_CAP:
        return jsonify({'error': 'Prefs too large'}), 413
    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError
    except ValueError:
        return jsonify({'error': 'Body must be a JSON object'}), 400
    current_app.db.set_portal_prefs(username, json.dumps(parsed))
    return jsonify({'ok': True})


# ── Rework-W1 (#244 item 12): per-instance files panel ───────────────────────
# Rules, all load-bearing: writes go ONLY into the owner's instance volume
# (the catalog's declared mount roots — never host paths, never arbitrary
# container paths), owner-gated like every route here, size-capped, and it is
# an HTTP sidecar through the manager — NOT zmodem over the PTY.

FILES_UPLOAD_CAP = 200 * 1024 * 1024  # 200 MiB — a dataset, not a disk image
# #595 review nit 2: downloads buffer through the manager's RAM (get_archive
# yields a tar we unpack) — a multi-GB model weight would OOM the manager.
# Same ceiling as uploads: the files panel moves documents, not disk images.
FILES_DOWNLOAD_CAP = 200 * 1024 * 1024


def _type_mounts(type_row: dict) -> list:
    """The type's NAMED-VOLUME mount points — the roots the files panel owns.

    AGM-10 (#1039): bind-mount specs (`host_path`, rc6.7 #91 — openhands binds
    `{{STACK_HOST_PATH}}/…/openhands-monkeypatch.sh`) are skipped. The panel is
    documented as "writes go ONLY into the owner's instance volume", and a
    host-side path is not that: today's single spec is a repo file mounted at
    `/opt/openhands-monkeypatch.sh` (downloadable, not writable, low impact),
    but a directory-shaped `host_path` added later would become a browsable
    read/write root over the host filesystem. The provisioner already
    special-cases these specs in launch/upgrade/delete; this is the same rule
    on the read side.
    """
    vols = type_row.get('volumes')
    if isinstance(vols, str):
        try:
            vols = json.loads(vols)
        except ValueError:
            vols = []
    return [v['mount'] for v in (vols or [])
            if v.get('mount') and 'host_path' not in v]


def _files_roots(agent_type: str) -> list:
    """The container paths the files panel may touch: the type's declared
    volume mounts. Empty list = the type keeps no per-instance data (panel
    disabled)."""
    t = current_app.catalog.get_type(agent_type)
    return _type_mounts(t) if t else []


def _files_resolve(agent_type: str, raw_path: str):
    """Normalize ``raw_path`` and require it under a declared mount root.
    Returns the clean absolute path or None (refused). posixpath.normpath +
    prefix check with the '/' boundary — '..'-escapes and lookalike-prefix
    roots ('/opt/data-evil' vs '/opt/data') both die here."""
    import posixpath
    clean = posixpath.normpath('/' + (raw_path or '').lstrip('/'))
    for root in _files_roots(agent_type):
        root = root.rstrip('/')
        if clean == root or clean.startswith(root + '/'):
            return clean
    return None


def _files_instance(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return None, (jsonify({'error': 'Unauthorized'}), 403)
    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return None, (jsonify({'error': 'Instance not found'}), 404)
    if instance['state'] != 'running':
        return None, (jsonify({'error': 'Agent is not running'}), 409)
    return instance, None


@api_bp.route('/files/<instance_id>/list', methods=['GET'])
def files_list(instance_id):
    instance, err = _files_instance(instance_id)
    if err:
        return err
    path = _files_resolve(instance['agent_type'],
                          request.args.get('path', '') or
                          (_files_roots(instance['agent_type']) or ['/'])[0])
    if path is None:
        return jsonify({'error': 'Path outside the instance volume'}), 400
    try:
        entries = current_app.docker_client.list_path(instance['container_name'], path)
    except FileNotFoundError:
        return jsonify({'error': 'No such directory'}), 404
    return jsonify({'path': path,
                    'roots': _files_roots(instance['agent_type']),
                    'entries': entries})


@api_bp.route('/files/<instance_id>/download', methods=['GET'])
def files_download(instance_id):
    instance, err = _files_instance(instance_id)
    if err:
        return err
    path = _files_resolve(instance['agent_type'], request.args.get('path', ''))
    if path is None:
        return jsonify({'error': 'Path outside the instance volume'}), 400
    import io
    import posixpath
    import tarfile
    try:
        stream, stat = current_app.docker_client.read_file_tar(
            instance['container_name'], path)
    except Exception:
        return jsonify({'error': 'No such file'}), 404
    # #595 review nit 3: a DIRECTORY path also resolves (the panel needs that
    # for listing) but "download a directory" silently picking the tar's first
    # file is surprising — refuse it. Decided off the TAR itself (a directory
    # archive leads with its dir entry) rather than guessing at Go FileMode
    # bits in get_archive's stat, whose encoding is docker-internal.
    # get_archive hands back a TAR containing the file — unpack the single
    # member and stream its bytes (the panel downloads files, not tarballs).
    # Capped while joining: stop reading past the ceiling instead of buffering
    # a multi-GB weight into the manager (nit 2).
    chunks, total = [], 0
    for chunk in stream:
        total += len(chunk)
        if total > FILES_DOWNLOAD_CAP + 4096:   # + tar header slack
            return jsonify({'error':
                f'File exceeds the {FILES_DOWNLOAD_CAP // (1024*1024)} MiB '
                f'download cap'}), 413
        chunks.append(chunk)
    buf = io.BytesIO(b''.join(chunks))
    tf = tarfile.open(fileobj=buf)
    members = tf.getmembers()
    if members and members[0].isdir():
        return jsonify({'error': 'Not a file (directories cannot be downloaded)'}), 400
    member = next((m for m in members if m.isfile()), None)
    if member is None:
        return jsonify({'error': 'Not a file'}), 400
    data = tf.extractfile(member).read()
    from flask import Response as _Resp
    return _Resp(data, mimetype='application/octet-stream', headers={
        'Content-Disposition':
            f'attachment; filename="{posixpath.basename(path)}"'})


@api_bp.route('/files/<instance_id>/upload', methods=['POST'])
def files_upload(instance_id):
    instance, err = _files_instance(instance_id)
    if err:
        return err
    dest = _files_resolve(instance['agent_type'], request.args.get('path', ''))
    if dest is None:
        return jsonify({'error': 'Path outside the instance volume'}), 400
    f = request.files.get('file')
    if f is None or not f.filename:
        return jsonify({'error': 'No file'}), 400
    # the filename lands INSIDE the volume dir — flatten to basename so a
    # crafted filename ("../../etc/cron.d/x") cannot climb out of dest
    import posixpath
    name = posixpath.basename(f.filename.replace('\\', '/'))
    if not name:
        return jsonify({'error': 'Bad filename'}), 400
    data = f.read(FILES_UPLOAD_CAP + 1)
    if len(data) > FILES_UPLOAD_CAP:
        return jsonify({'error':
                        f'File exceeds the {FILES_UPLOAD_CAP // (1024*1024)} MiB cap'}), 413
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tf:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))
    ok = current_app.docker_client.write_file_tar(
        instance['container_name'], dest, buf.getvalue())
    if not ok:
        return jsonify({'error': 'Write failed'}), 502
    # audit trail (#244 item 10): the manager has no audit service object yet
    # (W5 surfaces it); until then the structured log line IS the record.
    logger.info("files_upload user-owned instance=%s name=%s size=%dB dest=%s",
                instance_id, name, len(data), dest)
    return jsonify({'ok': True, 'name': name, 'size': len(data), 'dest': dest})


@api_bp.route('/stats', methods=['GET'])
def stats_all():
    """#1186 — ONE memory sample for every RUNNING instance the caller owns.

    The /agents page used to take these samples synchronously while
    rendering — one docker inspect + one stats round trip per running agent,
    all before the first byte, so N agents meant N round trips of frozen
    page (and a wedged dockerd meant a hung page). It now paints from the DB
    alone and fills the RAM meters from this call. Memory only: the OOM
    figure costs a container exec and stays on the per-instance
    `/stats/<id>` the portal's pane header asks for. Owner-scoped exactly
    like `/tree` (both slug candidates, #192). A container that vanished
    between the DB read and the sample is simply absent from the map.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403
    out = {}
    for row in current_app.db.get_user_instances(_slug_candidates(username)):
        if row['state'] != 'running':
            continue
        s = current_app.docker_client.get_container_stats(row['container_name'])
        if s:
            out[str(row['id'])] = s
    return jsonify({'stats': out})


@api_bp.route('/stats/<instance_id>', methods=['GET'])
def stats(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    # include_oom: this endpoint is per-instance (the detail view), not the
    # dashboard's per-render loop, so it can afford the cgroup probe. #238 —
    # a cgroup OOM kills processes INSIDE a container whose PID 1 survives, so
    # `State.OOMKilled` stays false and the agent looks healthy while the user's
    # sessions are gone. `oom_kills > 0` is the only signal that happened.
    s = current_app.docker_client.get_container_stats(
        instance['container_name'], include_oom=True)
    return jsonify({'stats': s})


@api_bp.route('/ready/<instance_id>', methods=['GET'])
def ready(instance_id):
    """Probe whether an instance's HTTP port is actually serving.

    rc6.7 #91 — DB state flips to `running` the moment the container
    starts, but the inner app (openhands, hermes-workspace, dify-web,
    paperclip-onboard, etc.) may need 10–60s before answering. The
    dashboard polls this endpoint while the Open button is rendered as
    "Starting…" and swaps to the live link as soon as it returns ready.

    Returns 200 {ready: true|false} — never errors so the JS poll loop
    can keep ticking on transient network blips.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'ready': False, 'error': 'Unauthorized'}), 403

    try:
        instance = current_app.db.get_instance(uuid.UUID(instance_id))
    except (ValueError, TypeError):
        return jsonify({'ready': False}), 200
    if not _owns_instance(instance, username):
        return jsonify({'ready': False}), 200
    if instance['state'] != 'running':
        return jsonify({'ready': False, 'state': instance['state']}), 200

    # Probe the container on its primary internal port via the agent
    # network. Container name is resolvable via docker DNS.
    type_info = current_app.catalog.get_type(instance['agent_type'])
    if not type_info:
        return jsonify({'ready': False}), 200
    try:
        ports = (json.loads(type_info['ports']) if isinstance(type_info['ports'], str)
                 else type_info['ports'])
    except (TypeError, ValueError):
        return jsonify({'ready': False}), 200
    port = ports.get('internal')
    if not port:
        return jsonify({'ready': True}), 200  # nothing to probe; assume ready

    import httpx
    container_target = instance['container_name']
    # A companion-UI agent's user-facing UI lives on its `-<suffix>` companion
    # container. #36 Option B: hermes dropped its companion (the built-in v0.18
    # dashboard on the primary container is the UI now), so this is a no-op for
    # hermes — but keep it companion-aware (via companion_image) so any future
    # companion agent probes the right container instead of hardcoding hermes.
    if type_info.get('companion_image'):
        suffix = type_info.get('companion_suffix') or 'workspace'
        container_target = f"{container_target}-{suffix}"
    # HTTP-level readiness — NOT a bare TCP connect. The kernel completes a TCP
    # handshake the instant the app calls listen(), but gunicorn/uvicorn/vite/
    # moltis/openhands only answer HTTP 10–60s later. The old
    # socket.create_connection probe therefore flipped the dashboard "Open"
    # button to ready on *port-bound*, while the proxy (blueprints/proxy.py) was
    # still getting httpx.ConnectError → the user clicked a green button and got
    # "container is not ready yet" every session. Probe with a real request so
    # `ready` agrees with what the proxy actually does: ANY HTTP response
    # (200/302/401/404/500…) proves the app is serving; only a transport-level
    # failure (connect refused / timeout / reset) counts as not-ready.
    try:
        httpx.get(f"http://{container_target}:{int(port)}/",
                  timeout=2, follow_redirects=False)
        return jsonify({'ready': True}), 200
    except (httpx.TransportError, OSError):
        return jsonify({'ready': False}), 200


@api_bp.route('/bootstrap-url/<instance_id>', methods=['POST'])
def bootstrap_url(instance_id):
    """Mint a fresh first-login URL for an agent that gates UI behind a
    one-time invite (currently: paperclip's `bootstrap-ceo`).

    The pre-server entrypoint can't run this command (the CLI talks to
    the running app's DB to mint the invite), so the wrapper's
    "print on startup" path silently failed in paperclip v2026.513.0
    — operator-reported 2026-05-19. This endpoint shells into the
    already-running container via `docker exec` and runs the CLI on
    demand. Output is parsed for `Invite URL:` and `Expires:` lines.

    Idempotent: re-invoking revokes any previously-unaccepted invite
    and issues a fresh one. If the CEO has already claimed an invite,
    `bootstrap-ceo` returns a "CEO account already exists" message;
    we surface that as `{already_claimed: true}` so the dashboard can
    render a friendly note instead of a URL.
    """
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    try:
        instance = current_app.db.get_instance(uuid.UUID(instance_id))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid instance id'}), 400
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Not found'}), 404
    if instance['agent_type'] != 'paperclip':
        return jsonify({'error': 'Unsupported',
                        'message': 'Only paperclip supports bootstrap-url'}), 400
    if instance['state'] != 'running':
        return jsonify({'error': 'Not running',
                        'message': 'Container must be running to mint an invite'}), 409

    # docker-socket-proxy doesn't expose the CLI inside agent-manager, so
    # call exec_run via the existing docker_client wrapper rather than
    # shelling `docker exec` (which would talk to the unmapped socket).
    # The bootstrap-ceo CLI must run as the `node` user; pass via `sh -c`
    # so we can `cd /app` before invoking pnpm.
    cmd = [
        'su', '-s', '/bin/sh', 'node', '-c',
        'cd /app && pnpm paperclipai auth bootstrap-ceo --data-dir /paperclip/.paperclip 2>&1',
    ]
    exit_code, output = current_app.docker_client.exec_in_container(
        instance['container_name'], cmd, timeout=60,
    )
    out = (output or b'').decode('utf-8', errors='replace')
    if 'already exists' in out.lower() or 'already claimed' in out.lower():
        return jsonify({'already_claimed': True}), 200
    invite_url = ''
    expires = ''
    for line in out.splitlines():
        # The CLI uses a Unicode pipe prefix ("│  Invite URL: https://...")
        # so strip non-URL leading chars and split on the colon.
        line_stripped = line.strip().lstrip('│').strip()
        if line_stripped.startswith('Invite URL:'):
            invite_url = line_stripped.split('Invite URL:', 1)[1].strip()
        elif line_stripped.startswith('Expires:'):
            expires = line_stripped.split('Expires:', 1)[1].strip()
    if not invite_url:
        return jsonify({'error': 'No URL minted',
                        'message': out[:500] or 'bootstrap-ceo returned no Invite URL'}), 500
    return jsonify({'invite_url': invite_url, 'expires': expires}), 200


@api_bp.route('/tls/ask', methods=['GET'])
def tls_ask():
    """On-demand TLS guard for the *.AGENTS_DOMAIN wildcard block.

    Caddy queries this before issuing a Let's Encrypt cert for any
    hostname under *.agents.<domain>. Returns 200 if and only if the
    hostname maps to a real registered agent instance (so an attacker
    can't trigger cert-floods against arbitrary names).

    Public endpoint — no Authentik forward-auth in front of it (the
    Caddy admin can't carry a session). Caddy reaches it from the
    agents-domain Caddy block via `ask http://agent-manager:5000/api/tls/ask`.
    """
    from app.services.caddy_client import instance_token

    domain = (request.args.get('domain') or '').lower()
    agents_domain = current_app.config.get('AGENTS_DOMAIN', '').lower()

    # Apex (agents.<domain>) is served by the dedicated AGENTS_DOMAIN
    # Caddy block, not the wildcard — so the wildcard's ask should
    # reject it and let the higher-priority block win.
    if not agents_domain or domain == agents_domain:
        return ('', 404)
    if not domain.endswith(f'.{agents_domain}'):
        # #619: Caddy's global on_demand_tls supports exactly ONE ask
        # endpoint, and it points here. The *.MCP_DOMAIN wildcard (mcp
        # per-proxy hosts) shares it, so queries for that zone are delegated
        # to mcp-manager's own ask — it alone can validate its instance
        # tokens. Best-effort: any failure is a 404 (fail-closed, no cert).
        import os as _os
        mcp_domain = (_os.environ.get('MCP_DOMAIN')
                      or f"mcp.{_os.environ.get('MAIN_DOMAIN', '')}").lower()
        if mcp_domain and domain.endswith(f'.{mcp_domain}'):
            try:
                import httpx as _httpx
                r = _httpx.get('http://mcp-manager:5000/api/tls/ask',
                               params={'domain': domain}, timeout=5)
                return ('', 200 if r.status_code == 200 else 404)
            except Exception:
                return ('', 404)
        return ('', 404)

    instance_prefix = domain[: -(len(agents_domain) + 1)]
    parts = instance_prefix.rsplit('-', 1)
    if len(parts) != 2:
        for i in range(len(instance_prefix) - 1, 0, -1):
            if instance_prefix[i] == '-':
                agent_type = instance_prefix[:i]
                suffix = instance_prefix[i+1:]
                break
        else:
            return ('', 404)
    else:
        agent_type, suffix = parts

    # Check token-based scheme first
    for cand in current_app.db.list_active_instances_by_type(agent_type):
        if instance_token(cand['id']) == suffix:
            return ('', 200)

    # Legacy user_slug fallback — still accept until the instance is recreated
    if current_app.db.get_instance_by_type_and_user(agent_type, suffix):
        return ('', 200)

    return ('', 404)


@api_bp.route('/access/<agent_type>', methods=['POST'])
def record_access(agent_type):
    """Record that the user accessed their agent instance (for idle tracking)."""
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    # #192: match on either slug scheme so idle-tracking doesn't silently
    # no-op for a legacy-slug instance.
    instance = current_app.db.get_instance_by_type_and_user(agent_type, _slug_candidates(username))
    if instance:
        current_app.db.update_last_accessed(instance['id'])
    return jsonify({'ok': True})


# ──────────────────────────────────────────────────────────────────────────────
# M020 S01 — per-user routing primitive consumed by chat/functions/_lib/
# per_user_routing.py. Returns the calling user's running instance of
# `agent_type` or 404 with a launch_url for the agents dashboard.
#
# Security boundary: identity is the calling user's Authentik headers
# (X-Authentik-Username / Uid / Groups). The DB query filters by user_slug,
# so this can never leak another user's instance metadata. Auth tokens are
# returned in the response body — the calling pipe is also user-scoped, so
# the user only ever sees their own per-instance secret. Tokens are never
# logged here.
# ──────────────────────────────────────────────────────────────────────────────

# Per-agent-type auth method. The token value itself comes from
# instance.config['_generated_secret'] — that's what the provisioner sets
# at launch time and what {{generated_secret}} substitutes into the env
# template (e.g. API_SERVER_KEY for hermes; MOLTIS_PASSWORD for moltis when
# M020 S04 ships its catalog change; OPENCODE_SERVER_PASSWORD for opencode
# when M020 S05 ships).
#
# This keeps /api/find decoupled from per-type env-var naming. If a future
# agent type wants a different secret per slot, swap the lookup to read
# from a per-type aliases map written into instance.config by the provisioner.
_AUTH_METHOD = {
    'hermes':       'bearer',
    'moltis':       'password',
    'coding-tools': 'basic',
}

# rc6.7 #79: per-agent username for HTTP Basic auth. coding-tools' opencode
# serve expects username "opencode" (per
# https://opencode.ai/docs/server — `OPENCODE_SERVER_USERNAME` defaults to
# "opencode" when password is set). Defaulting to "admin" in the pipe
# helper library produced 401 on every request. Map per agent type so
# adding a new basic-auth backend is a one-line addition here.
_AUTH_USER = {
    'coding-tools': 'opencode',
}


def _agents_domain():
    """Best-effort lookup of agents.<MAIN_DOMAIN> for dashboard launch URLs."""
    domain = os.environ.get('MAIN_DOMAIN') or current_app.config.get('MAIN_DOMAIN', '<MAIN_DOMAIN>')
    return f"agents.{domain}"


@api_bp.route('/find/<agent_type>', methods=['GET'])
def find(agent_type):
    """Look up the calling user's instance of `agent_type` for per-user pipe routing."""
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'unauthorized'}), 403

    # #192: match on either slug scheme so a legacy-provisioned instance is
    # still found for per-user pipe routing (chat "Open in Chat" links etc.),
    # then forward-migrate the stored slug now that it's resolved.
    instance = current_app.db.get_instance_by_type_and_user(agent_type, _slug_candidates(username))
    _reconcile_legacy_slug(instance, username)
    domain = _agents_domain()

    if instance is None:
        return jsonify({
            'error': 'not_provisioned',
            'agent_type': agent_type,
            'launch_url': f"https://{domain}/dashboard?launch={agent_type}",
        }), 404

    state = instance.get('state', 'unknown')
    if state != 'running':
        return jsonify({
            'error': 'not_running',
            'agent_type': agent_type,
            'instance_id': str(instance['id']),
            'state': state,
            'start_url': f"https://{domain}/dashboard?start={instance['id']}",
        }), 404

    # Ports come from the catalog (joined as t.ports → JSONB). Primary key is
    # 'internal'; everything else is exposed under extra_ports for the pipe to
    # opt in to (e.g. hermes pipe wants 'agent_internal' = 8642 instead of the
    # built-in dashboard UI on 'internal' = 9119; #36 Option B).
    ports = instance.get('ports') or {}
    if isinstance(ports, str):
        try:
            ports = json.loads(ports)
        except Exception:
            ports = {}
    primary_port = ports.get('internal')
    extra_ports = {k: v for k, v in ports.items() if k not in ('internal', 'protocol')}
    proto = ports.get('protocol', 'http')
    internal_url = f"{proto}://{instance['container_name']}:{primary_port}" if primary_port else None

    # Auth descriptor — method per agent type, token from
    # instance.config['_generated_secret'] (provisioner-generated at launch).
    cfg = instance.get('config') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    method = _AUTH_METHOD.get(agent_type, 'none')
    auth = {
        'method': method,
        'token': str(cfg.get('_generated_secret', '')) if method != 'none' else '',
        # rc6.7 #79: include username for basic auth (e.g. opencode-serve
        # wants "opencode", default "admin" was 401-ing the opencode pipe).
        'user': _AUTH_USER.get(agent_type, 'admin') if method == 'basic' else '',
    }

    return jsonify({
        'instance_id': str(instance['id']),
        'container_name': instance['container_name'],
        'internal_url': internal_url,
        'state': state,
        'auth': auth,
        'extra_ports': extra_ports,
    })
