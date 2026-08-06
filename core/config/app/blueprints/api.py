# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""API blueprint — health check, resource data, and apply action endpoints."""

import json
import os
import re
import time
from flask import Blueprint, Response, current_app, jsonify, render_template, request, send_file, session, stream_with_context

api_bp = Blueprint('api', __name__)


def _cycle_from_version(version):
    """Extract the cycle prefix from a CalVer version. The cycle is the
    consolidated release-doc directory name (e.g. `releases/2026.05/` covers
    the entire `2026.05-rc1..rc6.N` + `2026.05-ga[.N]` cycle). Returns None
    for non-CalVer versions (semver), in which case the per-version lookup
    is the only option.

    Examples:
      2026.05-rc6.6  → 2026.05
      2026.05-ga     → 2026.05
      2026.05-ga.1   → 2026.05
      2026.04-ga     → 2026.04
      1.0.0          → None
    """
    if not version:
        return None
    m = re.match(r'^(\d{4}\.\d{2})-', version)
    return m.group(1) if m else None


def _release_doc_candidates(stack_root, version, basename):
    """Build the path-precedence list for a release-doc lookup. rc6.7:
    the consolidated cycle-level doc (`releases/<cycle>/<file>`) wins over
    per-rc historical docs (`releases/<full-version>/<file>`), so a box on
    any 2026.05-rc6.* shows the consolidated 2026.05 notes by default and
    operators verifying the GA-shape content don't have to wait for the
    GA tag to be cut.

    Order:
      1. releases/<MAJOR.MM>/<basename>      (consolidated, M023-S04.5+)
      2. releases/<full-version>/<basename>  (per-rc, M023+ layout)
      3. <basename without .md>_<version>.md (pre-M023 root layout)
      4. <basename>                          (generic root fallback)
    """
    candidates = []
    cycle = _cycle_from_version(version)
    if cycle:
        candidates.append(os.path.join(stack_root, 'releases', cycle, basename))
    if version:
        candidates.append(os.path.join(stack_root, 'releases', version, basename))
        # Pre-M023 layout: <STEM>_<version>.md at stack root
        stem = basename[:-3] if basename.endswith('.md') else basename
        candidates.append(os.path.join(stack_root, f'{stem}_{version}.md'))
    candidates.append(os.path.join(stack_root, basename))
    return candidates


# M026 S05 #5: /healthz removed — razzfazz_common.health.get_health_blueprint
# is registered via create_base_app() in app/__init__.py and serves the same
# {status: ok, service: razzfazz-config} response.


@api_bp.route('/api/resources/live')
def resources_live():
    data = current_app.resource_monitor.get_all()
    return jsonify(data)


@api_bp.route('/api/updates/check', methods=['POST'])
def updates_check():
    """Fetch latest manifest from remote, then compare locally."""
    checker = current_app.image_checker
    ok, message = checker.fetch_remote_manifest()
    if not ok:
        return f'<p style="font-size:0.8125rem;color:#991b1b">Failed to fetch manifest: {message}</p>'
    # Re-render with fresh data
    manifest_updates = checker.get_manifest_updates()
    manifest_info = checker.get_manifest_info()
    cve_alerts = checker.get_manifest_cve_alerts()
    return render_template('fragments/image_updates.html',
                           manifest_updates=manifest_updates,
                           manifest_info=manifest_info,
                           cve_alerts=cve_alerts,
                           fetch_message=message)


@api_bp.route('/api/updates')
def updates_list():
    """Get manifest-based update results as HTML fragment for HTMX."""
    checker = current_app.image_checker
    manifest_updates = checker.get_manifest_updates()
    manifest_info = checker.get_manifest_info()
    cve_alerts = checker.get_manifest_cve_alerts()
    return render_template('fragments/image_updates.html',
                           manifest_updates=manifest_updates,
                           manifest_info=manifest_info,
                           cve_alerts=cve_alerts)


@api_bp.route('/api/gpustack/models')
def gpustack_models():
    """Get loaded GPUStack models as HTML fragment for HTMX."""
    from app.services.gpustack_client import get_models
    api_key = current_app.config_manager.read_env().get('GPUSTACK_API_KEY', '')
    models = get_models(api_key)
    return render_template('fragments/gpustack_models.html', models=models, api_key_set=bool(api_key))


@api_bp.route('/api/docs/getting-started')
def docs_getting_started():
    """Open the Getting Started guide. Prefers a vendored PDF at
    `${STACK_ROOT}/getting-started.pdf` if shipped (for offline-first
    installs), otherwise redirects to the help service's documentation hub
    at `https://help.<MAIN_DOMAIN>/` (the canonical source is the customer
    doc tree under docs/enterprise/, baked into the razzfazz-help container's
    own_docs at image build; the "Get Started" section is the on-ramp)."""
    from flask import redirect
    stack_root = current_app.config['STACK_ROOT']
    for name in ('getting-started.pdf', 'quickstart.pdf'):
        path = os.path.join(stack_root, name)
        if os.path.exists(path):
            return send_file(path, mimetype='application/pdf')
    help_domain = current_app.config_manager.read_env().get('HELP_DOMAIN', '')
    if help_domain:
        return redirect(f'https://{help_domain}/')
    return 'Getting Started guide not found.', 404


@api_bp.route('/api/release-notes')
def release_notes():
    """Return release notes HTML for the current version.

    rc6.7+ lookup precedence (see _release_doc_candidates):
      1. releases/<cycle>/RELEASE_NOTES.md   (consolidated 2026.05 etc.)
      2. releases/<version>/RELEASE_NOTES.md (per-rc, retained for history)
      3. RELEASE_NOTES_<version>.md          (pre-M023 root layout)
      4. RELEASE_NOTES.md                    (generic root fallback)
    """
    stack_root = current_app.config['STACK_ROOT']
    version = _live_stack_version()

    candidate_paths = _release_doc_candidates(stack_root, version, 'RELEASE_NOTES.md')
    for path in candidate_paths:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            try:
                import markdown
                html = markdown.markdown(content, extensions=['tables', 'fenced_code'])
                return f'<div class="doc-content">{html}</div>'
            except ImportError:
                return f'<pre style="white-space:pre-wrap;font-size:0.8125rem">{content}</pre>'

    return f'<p style="color:var(--color-text-muted)">No release notes found for v{version}.</p>'


def _live_stack_version():
    """Read RAZZFAZZ_VERSION on every call (see same helper in app/__init__.py
    — we don't cache because verify_upgrade writes the new version AFTER
    restart_stack recreates this container)."""
    stack_root = current_app.config['STACK_ROOT']
    env_path = os.path.join(stack_root, '.env')
    if os.path.exists(env_path):
        try:
            # rc6.7: shared env_utils (strips inline ` # comment`).
            from app.services.env_utils import read_env_key
            v = read_env_key(env_path, 'RAZZFAZZ_VERSION')
            if v:
                return v
        except Exception:
            pass
    version_file = os.path.join(stack_root, 'VERSION')
    if os.path.exists(version_file):
        try:
            with open(version_file) as f:
                return f.read().strip()
        except Exception:
            pass
    return ''


@api_bp.route('/api/whats-new')
def whats_new():
    """Return What's New HTML highlights for the current version.

    rc6.7+ lookup precedence: cycle-consolidated first, then per-rc, then
    legacy, then generic — see _release_doc_candidates.
    """
    stack_root = current_app.config['STACK_ROOT']
    version = _live_stack_version()

    candidate_paths = _release_doc_candidates(stack_root, version, 'WHATS_NEW.md')
    for path in candidate_paths:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            try:
                import markdown
                html = markdown.markdown(content, extensions=['tables', 'fenced_code'])
                return f'<div class="doc-content">{html}</div>'
            except ImportError:
                return f'<pre style="white-space:pre-wrap;font-size:0.8125rem">{content}</pre>'

    # No WHATS_NEW.md candidate present (e.g. the public/Codeberg export culls
    # releases/) — return a graceful fallback so the view never returns None
    # (which would raise Flask "view did not return a valid response" → HTTP 500).
    return ('<p style="color:var(--color-text-muted)">'
            'No What\'s New notes are available for this release.</p>')


@api_bp.route('/api/whats-new-history')
def whats_new_history():
    """Return concatenated What's New for ALL releases, current first.

    M023+ layout: releases/<version>/WHATS_NEW.md per release.
    Pre-M023 layout: WHATS_NEW_<version>.md at stack root.
    Both are scanned and merged; if a version exists in both layouts
    (mid-refactor edge case) the new layout wins.

    Each release wrapped in a <details> accordion. Current version is
    open by default; older versions are collapsed so the user can scroll
    down and click to expand.
    """
    stack_root = current_app.config['STACK_ROOT']
    current_version = _live_stack_version()

    import glob
    # Map version → path. New layout first so it wins on duplicates.
    version_to_path = {}
    for p in glob.glob(os.path.join(stack_root, 'releases', '*', 'WHATS_NEW.md')):
        version = os.path.basename(os.path.dirname(p))
        version_to_path[version] = p
    for p in glob.glob(os.path.join(stack_root, 'WHATS_NEW_*.md')):
        basename = os.path.basename(p)
        version = basename[len('WHATS_NEW_'):-len('.md')]
        version_to_path.setdefault(version, p)
    if not version_to_path:
        return '<p style="color:var(--color-text-muted)">No What\'s New highlights found.</p>'

    # Sort versions newest-first. String sort works for our scheme
    # (rc1 < rc2 < ... < rc9 < rc10 may need a fix at rc10+; for now
    # the same caveat as the previous _version_sort_key applies).
    files = [version_to_path[v] for v in sorted(version_to_path, reverse=True)]

    try:
        import markdown
        have_markdown = True
    except ImportError:
        have_markdown = False

    sections = []
    for path in files:
        # Recover version from either path shape:
        if os.sep + 'releases' + os.sep in path:
            version = os.path.basename(os.path.dirname(path))
        else:
            basename = os.path.basename(path)
            version = basename[len('WHATS_NEW_'):-len('.md')]
        try:
            with open(path) as f:
                content = f.read()
        except Exception:
            continue
        if have_markdown:
            body_html = markdown.markdown(content, extensions=['tables', 'fenced_code'])
        else:
            body_html = f'<pre style="white-space:pre-wrap;font-size:0.8125rem">{content}</pre>'
        is_current = (version == current_version)
        open_attr = ' open' if is_current else ''
        marker = ' (current)' if is_current else ''
        sections.append(
            f'<details class="whats-new-release"{open_attr}>'
            f'<summary><strong>v{version}</strong>{marker}</summary>'
            f'<div class="whats-new-release-body">{body_html}</div>'
            f'</details>'
        )
    return '<div class="doc-content">' + '\n'.join(sections) + '</div>'

    return '', 404


@api_bp.route('/api/updates/apply', methods=['POST'])
def updates_apply():
    """Apply a single image update: bump .env version, pull, recreate."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    image_key = data.get('key', '')
    checker = current_app.image_checker
    manifest = checker._load_manifest()
    if not manifest:
        return jsonify({'error': 'No manifest found'}), 404

    # Find the image entry in the manifest
    entry = manifest.get('images', {}).get(image_key)
    if not entry:
        return jsonify({'error': f'Unknown image key: {image_key}'}), 404

    env_var = entry.get('env_var', '')
    new_version = entry.get('current', '')
    if not env_var or not new_version:
        return jsonify({'error': 'Image is hardcoded (not env-controlled)'}), 400

    # Target only the container(s) that actually use THIS image's version var —
    # not the whole profile. Bug (ga.6): updating one core image (e.g. valkey)
    # used to pull + recreate ALL of core (caddy/postgres/authentik/…), a whole-
    # stack disruption for a single patch. Match the profile's containers by image
    # repo against the manifest entry's `image` (handles multi-container vars too,
    # e.g. DIFY_VERSION → dify-api/worker/worker-beat, all langgenius/dify-api).
    profile_id = entry.get('profile', '')
    profile = current_app.profile_manager.get_profile(profile_id)
    all_containers = profile.get('containers', []) if profile else []
    image_repo = (entry.get('image') or '').strip()
    containers = [
        c['name'] for c in all_containers
        if image_repo and c.get('image', '').split(':', 1)[0] == image_repo
    ]
    # Fallback: couldn't match by image (custom-built tag / manifest gap) → keep
    # prior whole-profile behavior rather than no-op.
    if not containers:
        containers = [c['name'] for c in all_containers]
    if not containers:
        return jsonify({'error': f'No containers for profile {entry.get("profile")}'}), 400

    user = session.get('admin_username', request.headers.get('X-Authentik-Username', 'admin'))
    source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    description = f'Update {image_key}: {env_var}={new_version}'

    action_id, error = current_app.apply_manager.apply_image_update(
        env_var, new_version, containers, description, user, source_ip,
        profile_id=profile_id,
    )
    if error:
        return jsonify({'error': error}), 409

    # Invalidate manifest cache so badges refresh after update
    checker.invalidate_manifest_cache()

    return jsonify({'action_id': action_id})


@api_bp.route('/api/apply/preview', methods=['POST'])
def apply_preview():
    """Return impact preview for a proposed change."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    action = data.get('action')
    if action == 'profile_toggle':
        profile_id = data.get('profile_id')
        enable = data.get('enable', True)
        if not profile_id:
            return jsonify({'error': 'profile_id required'}), 400
        preview = current_app.apply_manager.preview_profile_toggle(profile_id, enable)
        return jsonify(preview)

    return jsonify({'error': f'Unknown action: {action}'}), 400


@api_bp.route('/api/apply', methods=['POST'])
def apply_action():
    """Execute an apply action. Returns action_id for SSE streaming."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    action_type = data.get('action')
    password = data.get('password', '')

    # Verify admin password for caution/danger actions
    if action_type == 'profile_toggle':
        profile_id = data.get('profile_id')
        enable = data.get('enable', True)

        if not profile_id:
            return jsonify({'error': 'profile_id required'}), 400

        profile = current_app.profile_manager.get_profile(profile_id)
        if not profile:
            return jsonify({'error': f'Unknown profile: {profile_id}'}), 400

        risk = profile.get('risk_on_toggle', 'safe')
        if risk in ('caution', 'danger'):
            admin_password = current_app.config.get('ADMIN_PASSWORD', '')
            if not password or password != admin_password:
                return jsonify({'error': 'Admin password required for this action'}), 403

        user = session.get('admin_username', request.headers.get('X-Authentik-Username', 'admin'))
        source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

        action_id, error = current_app.apply_manager.apply_profile_toggle(
            profile_id, enable, user, source_ip
        )

        if error:
            return jsonify({'error': error}), 409

        return jsonify({'action_id': action_id})

    if action_type == 'llm_runtime_toggle':
        # M029-S04: flip between stable (v0.7.1) and experimental (v2.1.x).
        target = data.get('target_runtime')
        if target not in ('stable', 'experimental'):
            return jsonify({'error': 'target_runtime must be stable or experimental'}), 400
        # Treated as a "caution" risk action — requires admin password.
        admin_password = current_app.config.get('ADMIN_PASSWORD', '')
        if not password or password != admin_password:
            return jsonify({'error': 'Admin password required for this action'}), 403
        user = session.get('admin_username', request.headers.get('X-Authentik-Username', 'admin'))
        source_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        action_id, error = current_app.apply_manager.apply_llm_runtime_toggle(
            target, user, source_ip
        )
        if error:
            return jsonify({'error': error}), 409
        return jsonify({'action_id': action_id})

    return jsonify({'error': f'Unknown action: {action_type}'}), 400


@api_bp.route('/api/apply/<action_id>/stream')
def apply_stream(action_id):
    """SSE endpoint streaming apply action output."""
    action = current_app.apply_manager.get_action(action_id)
    if not action:
        return jsonify({'error': 'Action not found'}), 404

    def generate():
        index = 0
        while True:
            lines, done, success, error, duration_ms = action.get_lines_from(index)
            for line in lines:
                yield f"data: {json.dumps({'type': 'line', 'text': line})}\n\n"
                index += 1
            if done:
                yield f"data: {json.dumps({'type': 'done', 'success': success, 'error': error, 'duration_ms': duration_ms})}\n\n"
                break
            time.sleep(0.3)

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )
