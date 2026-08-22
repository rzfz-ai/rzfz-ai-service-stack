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
        if inst.get('id') and agents_domain:
            inst['url'] = (
                f"https://{inst['agent_type']}-{instance_token(inst['id'])}"
                f".{agents_domain}"
            )
        instances.append(inst)
    return jsonify({'instances': instances})


@api_bp.route('/launch/<agent_type>', methods=['POST'])
def launch(agent_type):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    user_config = request.get_json(silent=True) or {}
    instance_id, message = current_app.provisioner.launch(
        agent_type, user_id, username, groups, user_config
    )

    if instance_id:
        return jsonify({'instance_id': instance_id, 'message': message})
    return jsonify({'error': message}), 400


@api_bp.route('/start/<instance_id>', methods=['POST'])
def start(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    iid, message = current_app.provisioner.start(instance_id, username)
    return jsonify({'instance_id': iid, 'message': message})


@api_bp.route('/stop/<instance_id>', methods=['POST'])
def stop(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    iid, message = current_app.provisioner.stop(instance_id, username)
    return jsonify({'instance_id': iid, 'message': message})


@api_bp.route('/restart/<instance_id>', methods=['POST'])
def restart(instance_id):
    """ga.2 (#219): restart a running instance in place (docker restart).

    Non-destructive — the container, its named volumes, the Caddy route and
    the per-instance Authentik provider are untouched; only the process
    inside is bounced. Recovers a wedged agent (hung UI / stuck runtime)
    without losing chats, skills, files or config.
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
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    body = request.get_json(silent=True) or {}
    mem_gb = body.get('mem_gb')
    iid, message = current_app.provisioner.update_memory(
        instance_id, username, groups, mem_gb=mem_gb,
    )
    if iid is None:
        # None == a hard server-side refusal (tier gate / budget / cap).
        return jsonify({'error': 'refused', 'message': message}), 403
    return jsonify({'instance_id': iid, 'message': message}), 200


@api_bp.route('/logs/<instance_id>', methods=['GET'])
def logs(instance_id):
    username, user_id, user_slug, groups = _get_user()
    if not username:
        return jsonify({'error': 'Unauthorized'}), 403

    instance = current_app.db.get_instance(uuid.UUID(instance_id))
    if not _owns_instance(instance, username):
        return jsonify({'error': 'Instance not found'}), 404

    tail = request.args.get('tail', 200, type=int)
    log_text = current_app.docker_client.get_container_logs(
        instance['container_name'], tail=tail
    )
    return jsonify({'logs': log_text})


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
