/* razzfazz-shortcuts admin CRUD (#185, 2026.08 P1).
 *
 * Talks to the portal admin API (all same-origin, SSO + source-IP-anchored):
 *   GET    /api/shortcuts               list
 *   POST   /api/shortcuts               create   (CSRF)
 *   POST   /api/shortcuts/<id>          update   (CSRF)
 *   DELETE /api/shortcuts/<id>          delete   (CSRF)
 *   GET    /api/shortcuts/groups        Authentik group names
 *   GET    /api/shortcuts/members       resolved members for the ticked groups
 *   POST   /api/shortcuts/<id>/run      test-as-admin (CSRF) -> {url}
 *
 * P1 = redirect kinds only. Mirrors the fetch idiom in start-portal.js.
 */
(function () {
    'use strict';

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    var REDIRECT_KINDS = ['owui_persona', 'dify_chat', 'cloud_link'];

    var el = function (id) { return document.getElementById(id); };
    var jsonHeaders = { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF };

    // ── List ──────────────────────────────────────────────────────────
    function loadList() {
        fetch('/api/shortcuts', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(renderList)
            .catch(function () { el('sc-list').innerHTML = '<li>Could not load shortcuts.</li>'; });
    }

    function renderList(rows) {
        var list = el('sc-list');
        list.innerHTML = '';
        if (!rows || !rows.length) {
            list.innerHTML = '<li>No shortcuts yet — create one on the right.</li>';
            return;
        }
        rows.forEach(function (r) {
            var li = document.createElement('li');
            if (!r.enabled) { li.className = 'sc-off'; }
            var groups = (r.allowed_groups || []).join(', ') || '—';
            li.innerHTML =
                '<span class="sc-emoji">' + escapeHtml(r.icon || '✨') + '</span>' +
                '<span class="sc-meta"><strong>' + escapeHtml(r.title) + '</strong>' +
                '<small>' + escapeHtml(r.kind) + ' · ' + escapeHtml(groups) + '</small></span>';
            var edit = document.createElement('button');
            edit.className = 'sc-btn-ghost'; edit.textContent = 'Edit';
            edit.addEventListener('click', function () { fillForm(r); });
            var del = document.createElement('button');
            del.className = 'sc-btn-ghost'; del.textContent = 'Delete';
            del.addEventListener('click', function () { removeShortcut(r); });
            li.appendChild(edit); li.appendChild(del);
            list.appendChild(li);
        });
    }

    // ── Groups multiselect + resolved members ─────────────────────────
    function loadGroups(selected) {
        selected = selected || [];
        fetch('/api/shortcuts/groups', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) { renderGroups(data.groups || [], selected); })
            .catch(function () {
                el('sc-groups').innerHTML = '<em>Groups unavailable (Authentik).</em>';
            });
    }

    function renderGroups(groups, selected) {
        var box = el('sc-groups');
        box.innerHTML = '';
        var all = ['any'].concat(groups);
        all.forEach(function (name) {
            var id = 'sc-grp-' + name.replace(/[^a-zA-Z0-9]/g, '_');
            var label = document.createElement('label');
            var cb = document.createElement('input');
            cb.type = 'checkbox'; cb.value = name; cb.id = id;
            cb.className = 'sc-group-cb';
            if (selected.indexOf(name) !== -1) { cb.checked = true; }
            cb.addEventListener('change', refreshMembers);
            label.appendChild(cb);
            label.appendChild(document.createTextNode(name === 'any' ? 'any (all users)' : name));
            box.appendChild(label);
        });
        refreshMembers();
    }

    function selectedGroups() {
        return Array.prototype.slice.call(document.querySelectorAll('.sc-group-cb'))
            .filter(function (cb) { return cb.checked; })
            .map(function (cb) { return cb.value; });
    }

    function refreshMembers() {
        var groups = selectedGroups().filter(function (g) { return g !== 'any'; });
        var panel = el('sc-members');
        if (selectedGroups().indexOf('any') !== -1) {
            panel.hidden = false;
            panel.innerHTML = 'Visible to <strong>every signed-in user</strong>.';
            return;
        }
        if (!groups.length) { panel.hidden = true; return; }
        fetch('/api/shortcuts/members?groups=' + encodeURIComponent(groups.join('|')),
              { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var members = data.members || [];
                panel.hidden = false;
                panel.innerHTML = 'Users who can use this (' + members.length + '):' +
                    (members.length
                        ? '<ul>' + members.map(function (m) {
                              return '<li>' + escapeHtml(m) + '</li>'; }).join('') + '</ul>'
                        : ' <em>none yet</em>');
            })
            .catch(function () { panel.hidden = true; });
    }

    // ── Kind-specific field toggling ──────────────────────────────────
    function syncKindFields() {
        var kind = el('sc-kind').value;
        document.querySelectorAll('.sc-kind-fields').forEach(function (div) {
            div.hidden = (div.getAttribute('data-kind') !== kind);
        });
    }

    // ── Form <-> shortcut mapping ─────────────────────────────────────
    function fillForm(r) {
        el('sc-form-title').textContent = 'Edit: ' + r.title;
        el('sc-id').value = r.id;
        el('sc-title').value = r.title || '';
        el('sc-description').value = r.description || '';
        el('sc-icon').value = r.icon || '✨';
        el('sc-category').value = r.category || 'Shortcuts';
        el('sc-sort').value = (r.sort_order != null) ? r.sort_order : 100;
        el('sc-kind').value = r.kind;
        el('sc-enabled').checked = (r.enabled !== false);
        var cfg = r.config || {};
        el('sc-model-id').value = cfg.model_id || '';
        el('sc-app-path').value = cfg.app_path || '';
        el('sc-provider').value = cfg.provider || 'claude';
        el('sc-prompt-tmpl').value = cfg.prompt_tmpl || '';
        syncKindFields();
        loadGroups(r.allowed_groups || []);
        el('sc-test-result').hidden = true;
        window.scrollTo(0, 0);
    }

    function resetForm() {
        el('sc-form').reset();
        el('sc-id').value = '';
        el('sc-icon').value = '✨';
        el('sc-category').value = 'Shortcuts';
        el('sc-sort').value = 100;
        el('sc-form-title').textContent = 'New shortcut';
        syncKindFields();
        loadGroups([]);
        el('sc-test-result').hidden = true;
    }

    function gatherConfig(kind) {
        if (kind === 'owui_persona') { return { model_id: el('sc-model-id').value.trim() }; }
        if (kind === 'dify_chat') { return { app_path: el('sc-app-path').value.trim() }; }
        if (kind === 'cloud_link') {
            return { provider: el('sc-provider').value,
                     prompt_tmpl: el('sc-prompt-tmpl').value };
        }
        return {};
    }

    function gatherBody() {
        var kind = el('sc-kind').value;
        return {
            title: el('sc-title').value.trim(),
            description: el('sc-description').value.trim(),
            icon: el('sc-icon').value.trim() || '✨',
            category: el('sc-category').value.trim() || 'Shortcuts',
            sort_order: parseInt(el('sc-sort').value, 10) || 100,
            kind: kind,
            allowed_groups: selectedGroups(),
            config: gatherConfig(kind),
            enabled: el('sc-enabled').checked
        };
    }

    // ── Save / delete / test ──────────────────────────────────────────
    function save(ev) {
        ev.preventDefault();
        var body = gatherBody();
        if (!body.title) { alert('Title is required.'); return; }
        if (REDIRECT_KINDS.indexOf(body.kind) === -1) { alert('Unsupported type.'); return; }
        var id = el('sc-id').value;
        var url = id ? '/api/shortcuts/' + encodeURIComponent(id) : '/api/shortcuts';
        fetch(url, { method: 'POST', credentials: 'same-origin',
                     headers: jsonHeaders, body: JSON.stringify(body) })
            .then(function (r) {
                if (!r.ok) { throw new Error('save ' + r.status); }
                return r.json();
            })
            .then(function () { resetForm(); loadList(); })
            .catch(function () { alert('Save failed — check the fields and try again.'); });
    }

    function removeShortcut(r) {
        if (!confirm('Delete shortcut "' + r.title + '"?')) { return; }
        fetch('/api/shortcuts/' + encodeURIComponent(r.id),
              { method: 'DELETE', credentials: 'same-origin', headers: jsonHeaders })
            .then(function (resp) {
                if (!resp.ok) { throw new Error('delete ' + resp.status); }
                if (el('sc-id').value === r.id) { resetForm(); }
                loadList();
            })
            .catch(function () { alert('Delete failed.'); });
    }

    function testAsMe() {
        var id = el('sc-id').value;
        var out = el('sc-test-result');
        if (!id) {
            out.hidden = false;
            out.innerHTML = 'Save the shortcut first, then Test.';
            return;
        }
        fetch('/api/shortcuts/' + encodeURIComponent(id) + '/run',
              { method: 'POST', credentials: 'same-origin', headers: jsonHeaders,
                body: JSON.stringify({ input: '' }) })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
                out.hidden = false;
                if (res.ok && res.d.url) {
                    out.innerHTML = 'Opens: <a href="' + escapeAttr(res.d.url) +
                        '" target="_blank" rel="noopener">' + escapeHtml(res.d.url) + '</a>';
                } else {
                    out.innerHTML = 'Test failed: ' + escapeHtml((res.d && res.d.error) || 'error');
                }
            })
            .catch(function () { out.hidden = false; out.textContent = 'Test failed.'; });
    }

    // ── Escaping helpers ──────────────────────────────────────────────
    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

    // ── Wire up ───────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        el('sc-kind').addEventListener('change', syncKindFields);
        el('sc-form').addEventListener('submit', save);
        el('sc-new').addEventListener('click', resetForm);
        el('sc-cancel').addEventListener('click', resetForm);
        el('sc-test').addEventListener('click', testAsMe);
        syncKindFields();
        loadGroups([]);
        loadList();
    });
})();
