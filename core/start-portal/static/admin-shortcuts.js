/* razzfazz "App Builder" (#248 rebuild of the #185 razzfazz-shortcuts admin
 * CRUD).
 *
 * Talks to the portal admin API (all same-origin, SSO + source-IP-anchored):
 *   GET    /api/shortcuts               list
 *   POST   /api/shortcuts               create   (CSRF)
 *   POST   /api/shortcuts/<id>          update   (CSRF)
 *   DELETE /api/shortcuts/<id>          delete   (CSRF)
 *   GET    /api/shortcuts/groups        Authentik group names
 *   GET    /api/shortcuts/members       resolved members for the ticked groups
 *   GET    /api/shortcuts/who-can-use   #248 App Builder — resolved count + avatar initials
 *   POST   /api/shortcuts/<id>/run      test-as-admin (CSRF) -> {url}
 *
 * P1 = redirect kinds only. Mirrors the fetch idiom in start-portal.js.
 *
 * #248 App Builder rebuild: the markup changed a lot (template gallery,
 * icon picker with upload, clickable type-cards, visibility buttons, a live
 * WYSIWYG preview pane), but the SAVE/CRUD contract did not — every
 * function below that the existing test suite binds to by name
 * (gatherConfig/gatherBody/resetForm/fillForm/syncKindFields/
 * syncVisibilityFields/applyTemplate/loadModels/...) keeps reading and
 * writing the exact same technical fields it always did
 * (#sc-title/#sc-icon/#sc-kind/#sc-visibility/...). The new clickable
 * widgets (type-cards, visibility buttons, emoji/curated/upload icon tabs)
 * are a friendlier layer that DRIVES those technical fields — the same
 * idiom #227 established for #sc-model-select -> #sc-model-id.
 */
(function () {
    'use strict';

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    var REDIRECT_KINDS = ['owui_persona', 'owui_app', 'dify_chat', 'cloud_link'];

    var el = function (id) { return document.getElementById(id); };
    var jsonHeaders = { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF };

    // ── List ("Your apps" sidebar) ──────────────────────────────────────
    function loadList() {
        fetch('/api/shortcuts', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(renderList)
            .catch(function () { el('sc-list').innerHTML = '<li class="ab-empty">Could not load apps.</li>'; });
    }

    function renderList(rows) {
        var list = el('sc-list');
        list.innerHTML = '';
        if (!rows || !rows.length) {
            list.innerHTML = '<li class="ab-empty">No apps yet — build one on the right.</li>';
            return;
        }
        rows.forEach(function (r) {
            var li = document.createElement('li');
            li.className = 'ab-item' + (r.enabled ? '' : ' ab-off');
            var icon = document.createElement('span');
            icon.className = 'ab-ic';
            renderIconGlyph(icon, r.icon);
            var name = document.createElement('span');
            name.className = 'ab-nm';
            name.textContent = r.title;
            var del = document.createElement('button');
            del.type = 'button'; del.className = 'ab-del'; del.title = 'Delete';
            del.textContent = '×';
            del.addEventListener('click', function (ev) { ev.stopPropagation(); removeShortcut(r); });
            li.appendChild(icon); li.appendChild(name); li.appendChild(del);
            li.addEventListener('click', function () { fillForm(r); });
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
            cb.addEventListener('change', function () { refreshMembers(); refreshWhoCanUse(); });
            label.appendChild(cb);
            label.appendChild(document.createTextNode(name === 'any' ? 'any (all users)' : name));
            box.appendChild(label);
        });
        applyGroupFilter();
        refreshMembers();
        refreshWhoCanUse();
    }

    // #185 fix: filter the (potentially long) group checkbox list by substring.
    function applyGroupFilter() {
        var f = el('sc-groups-filter');
        var q = (f && f.value ? f.value : '').trim().toLowerCase();
        var labels = document.querySelectorAll('#sc-groups label');
        Array.prototype.forEach.call(labels, function (lbl) {
            var hit = !q || lbl.textContent.toLowerCase().indexOf(q) !== -1;
            lbl.style.display = hit ? '' : 'none';
        });
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

    // ── #248 App Builder — Step 4 "who can use it" preview ─────────────
    // Best-effort/degrade-gracefully: hides the widget entirely (never shows
    // a misleading zero) when Authentik/the resolve is unavailable.
    function refreshWhoCanUse() {
        var panel = el('ab-who');
        if (!panel) { return; }
        var groups = selectedGroups().filter(function (g) { return g !== 'any'; });
        if (selectedGroups().indexOf('any') !== -1) {
            panel.hidden = false;
            el('ab-who-count').textContent = 'everyone';
            el('ab-who-avatars').innerHTML = '';
            return;
        }
        if (!groups.length) { panel.hidden = true; return; }
        fetch('/api/shortcuts/who-can-use?groups=' + encodeURIComponent(groups.join('|')),
              { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || data.available !== true) { panel.hidden = true; return; }
                panel.hidden = false;
                el('ab-who-count').textContent = data.count;
                el('ab-who-avatars').innerHTML = (data.initials || []).map(function (i) {
                    return '<span class="ab-av">' + escapeHtml(i) + '</span>';
                }).join('');
            })
            .catch(function () { panel.hidden = true; });
    }

    // ── OWUI model picker (#227 point 6; #248 P3 adds the owui_app twin) ──
    // Each <select> is a convenience that DRIVES its own free-text id — it
    // never replaces it. gatherConfig() keeps reading only the free-text
    // input, so the submitted {model_id} contract is unchanged whether the
    // admin picked from the list or typed a value by hand. owui_persona and
    // owui_app get their OWN select+input pair (sc-model-* / sc-app-model-*)
    // since both groups exist in the DOM at once — sharing one id would mean
    // one kind's edits silently overwrite the other's.
    var MODEL_PICKERS = [
        { select: 'sc-model-select', hintClass: 'sc-model-hint' },
        { select: 'sc-app-model-select', hintClass: 'sc-app-model-hint' }
    ];

    function loadModels() {
        fetch('/api/owui/models', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var models = data.models || [];
                if (data.available === true && models.length > 0) {
                    renderModelOptions(models);
                } else {
                    degradeModelPicker();
                }
            })
            .catch(function () { degradeModelPicker(); });
    }

    function renderModelOptions(models) {
        MODEL_PICKERS.forEach(function (picker) {
            var sel = el(picker.select);
            if (!sel) { return; }
            sel.innerHTML = '';
            var blank = document.createElement('option');
            blank.value = ''; blank.textContent = '— pick —';
            sel.appendChild(blank);
            models.forEach(function (m) {
                var opt = document.createElement('option');
                opt.value = m.id; opt.textContent = m.name || m.id;
                sel.appendChild(opt);
            });
            sel.hidden = false;
            var hint = document.querySelector('.' + picker.hintClass);
            if (hint) { hint.hidden = true; }
        });
    }

    // OWUI unreachable / no models / fetch failed: degrade, don't fail.
    // The free-text inputs are untouched here — they stay the fully usable
    // fallback.
    function degradeModelPicker() {
        MODEL_PICKERS.forEach(function (picker) {
            var sel = el(picker.select);
            if (sel) { sel.innerHTML = ''; sel.hidden = true; }
            var hint = document.querySelector('.' + picker.hintClass);
            if (hint) { hint.hidden = false; }
        });
    }

    // ── Kind-specific field toggling ──────────────────────────────────
    function syncKindFields() {
        var kind = el('sc-kind').value;
        document.querySelectorAll('.sc-kind-fields').forEach(function (div) {
            div.hidden = (div.getAttribute('data-kind') !== kind);
        });
    }

    // ── #248 P3 — visibility field toggling ────────────────────────────
    // private has no group audience, so the "Access — allowed groups" block
    // is noise (and a false affordance — the backend ignores allowed_groups
    // for a private row's user_can_use() check) until the admin widens it.
    function syncVisibilityFields() {
        var vis = el('sc-visibility').value;
        var wrap = el('sc-groups-wrap');
        if (wrap) { wrap.hidden = (vis === 'private'); }
    }

    // ── #248 App Builder — friendly-widget <-> technical-field sync ────
    // The clickable type-cards / visibility buttons / icon tabs are driven
    // FROM the technical fields here (never the other way around) so a
    // programmatic value change (fillForm, applyTemplate, resetForm) always
    // reflects into the right widget.
    function syncTypeUI() {
        var val = el('sc-kind').value;
        var cards = document.querySelectorAll('#ab-types .ab-type');
        Array.prototype.forEach.call(cards, function (c) {
            c.classList.toggle('on', c.getAttribute('data-type') === val);
        });
    }

    function syncVisUI() {
        var val = el('sc-visibility').value;
        var buttons = document.querySelectorAll('#ab-vis button');
        Array.prototype.forEach.call(buttons, function (b) {
            b.classList.toggle('on', b.getAttribute('data-vis') === val);
        });
    }

    // Renders an icon value (emoji | curated:<name> | data:image/... URI)
    // into a target element — shared by the icon-picker preview swatch, the
    // live-preview tile, and each "Your apps" sidebar row.
    function renderIconGlyph(target, value) {
        if (!target) { return; }
        value = value || '✨';
        if (value.indexOf('data:image/') === 0) {
            target.innerHTML = '<img src="' + escapeAttr(value) + '" alt="">';
        } else if (value.indexOf('curated:') === 0) {
            var name = value.slice('curated:'.length);
            target.innerHTML = '<img src="/static/icons/razzfazz-ai_' + escapeAttr(name) + '_icon.png" alt="">';
        } else {
            target.textContent = value;
        }
    }

    function setIconTab(name) {
        document.querySelectorAll('.ab-icontabs button').forEach(function (b) {
            b.classList.toggle('on', b.getAttribute('data-itab') === name);
        });
        ['emoji', 'curated', 'upload'].forEach(function (t) {
            var panel = el('ab-itab-' + t);
            if (panel) { panel.hidden = (t !== name); }
        });
    }

    function syncIconUI() {
        var val = el('sc-icon').value;
        renderIconGlyph(el('ab-icon-preview'), val);
        var emojiButtons = document.querySelectorAll('#ab-itab-emoji button');
        Array.prototype.forEach.call(emojiButtons, function (b) {
            b.classList.toggle('on', b.textContent === val);
        });
        var curatedButtons = document.querySelectorAll('#ab-itab-curated button');
        Array.prototype.forEach.call(curatedButtons, function (b) {
            b.classList.toggle('on', b.getAttribute('data-curated') === val);
        });
        if (val.indexOf('data:image/') === 0) { setIconTab('upload'); }
        else if (val.indexOf('curated:') === 0) { setIconTab('curated'); }
        else { setIconTab('emoji'); }
    }

    // ── Live preview (right pane) — WYSIWYG tile mirror ────────────────
    var CHIP = { owui_app: 'app', owui_persona: 'persona', dify_chat: 'dify',
                 cloud_link: 'link', agent: 'agent', cloud_api: 'api' };

    function syncPreview() {
        var name = el('ab-pv-name'), desc = el('ab-pv-desc'), chip = el('ab-pv-chip');
        if (!name) { return; }   // preview pane absent (defensive; always present today)
        name.textContent = el('sc-title').value || 'Untitled app';
        desc.textContent = el('sc-description').value || 'Describe what this app does…';
        chip.textContent = CHIP[el('sc-kind').value] || 'app';
        renderIconGlyph(el('ab-pv-icon'), el('sc-icon').value);
    }

    // ── Icon upload (Step 2, Upload tab) ────────────────────────────────
    // Client-side downscale to a ~128px square + re-encode (WebP->PNG
    // fallback) BEFORE staging — same shape as the #299 avatar upload fix
    // (start-portal-profile.js), just a smaller target size since a tile
    // icon renders far smaller than a profile avatar. The server-side cap
    // (shortcuts.MAX_ICON_BYTES) is only a backstop for a JS-off/crafted
    // POST.
    var ICON_MAX = 128;

    function onIconUpload(ev) {
        var file = ev.target.files && ev.target.files[0];
        if (!file) { return; }
        var errEl = el('ab-icon-error');
        if (errEl) { errEl.hidden = true; errEl.textContent = ''; }
        if (!/^image\//.test(file.type || '')) {
            if (errEl) { errEl.hidden = false; errEl.textContent = 'Please choose an image file (PNG, JPEG, GIF or WEBP).'; }
            return;
        }
        var reader = new FileReader();
        reader.onload = function () {
            var img = new Image();
            img.onload = function () {
                var side = Math.min(img.naturalWidth, img.naturalHeight) || ICON_MAX;
                var sx = (img.naturalWidth - side) / 2, sy = (img.naturalHeight - side) / 2;
                var canvas = document.createElement('canvas');
                canvas.width = ICON_MAX; canvas.height = ICON_MAX;
                var ctx = canvas.getContext('2d');
                ctx.drawImage(img, sx, sy, side, side, 0, 0, ICON_MAX, ICON_MAX);
                var out = null;
                try { out = canvas.toDataURL('image/webp', 0.85); } catch (e) { out = null; }
                if (!out || out.indexOf('data:image/webp') !== 0) { out = canvas.toDataURL('image/png'); }
                el('sc-icon').value = out;
                syncIconUI();
                syncPreview();
            };
            img.onerror = function () {
                if (errEl) { errEl.hidden = false; errEl.textContent = 'That image could not be read — try a PNG or JPEG.'; }
            };
            img.src = reader.result;
        };
        reader.readAsDataURL(file);
    }

    // ── Form <-> shortcut mapping ───────────────────────────────────────
    function fillForm(r) {
        el('sc-form-title').textContent = 'Edit: ' + r.title;
        el('sc-id').value = r.id;
        el('sc-title').value = r.title || '';
        el('sc-description').value = r.description || '';
        el('sc-icon').value = r.icon || '✨';
        el('sc-category').value = r.category || '1 Workspace';
        el('sc-sort').value = (r.sort_order != null) ? r.sort_order : 100;
        el('sc-kind').value = r.kind;
        el('sc-enabled').checked = (r.enabled !== false);
        el('sc-visibility').value = r.visibility || 'company';
        // Backward-compat: a pre-#248 row carries no visibility column value
        // in the payload only if the backend ever omitted it — it never
        // does (defaults to 'company' server-side), but fall back the same
        // way here in case of a stale/partial row.
        var cfg = r.config || {};
        el('sc-model-id').value = cfg.model_id || '';
        // Reflect into the picker too, if it already has a matching option
        // loaded (harmless no-op — leaves the select unselected — otherwise).
        if (el('sc-model-select')) { el('sc-model-select').value = cfg.model_id || ''; }
        // #248 P3: owui_app gets its OWN model reference fields (see the
        // markup comment on the .sc-kind-fields[data-kind=owui_app] group).
        el('sc-app-model-id').value = cfg.model_id || '';
        if (el('sc-app-model-select')) { el('sc-app-model-select').value = cfg.model_id || ''; }
        el('sc-app-prompt-tmpl').value = cfg.prompt_tmpl || '';
        el('sc-app-path').value = cfg.app_path || '';
        el('sc-provider').value = cfg.provider || 'claude';
        el('sc-prompt-tmpl').value = cfg.prompt_tmpl || '';
        syncKindFields();
        syncVisibilityFields();
        loadGroups(r.allowed_groups || []);
        el('sc-test-result').hidden = true;
        // #248 App Builder — reflect into the new clickable widgets + preview.
        syncTypeUI();
        syncVisUI();
        syncIconUI();
        syncPreview();
        document.querySelectorAll('#sc-list .ab-item').forEach(function (li) {
            li.classList.remove('sel');
        });
        window.scrollTo(0, 0);
    }

    function resetForm() {
        el('sc-form').reset();
        el('sc-id').value = '';
        el('sc-icon').value = '✨';
        el('sc-category').value = '1 Workspace';
        el('sc-sort').value = 100;
        // #248 P3: a NEW shortcut must never silently default to
        // company-wide — that broadcasts to every signed-in user before the
        // admin has explicitly opted into sharing it that widely. 'private'
        // is the safe starting point: visible only to the creating admin
        // (the server auto-fills owner_username for a private create), and
        // upgrading to group/company is one deliberate select change away.
        el('sc-visibility').value = 'private';
        el('sc-form-title').textContent = 'New app';
        syncKindFields();
        syncVisibilityFields();
        loadGroups([]);
        el('sc-test-result').hidden = true;
        // #248 App Builder — reset the new clickable widgets + preview too.
        syncTypeUI();
        syncVisUI();
        syncIconUI();
        syncPreview();
    }

    // ── #248 P3 — scenario-template gallery (scenario_templates.py) ────
    // Client-side prefill ONLY: fills the (already-empty, freshly-reset)
    // form from a curated template so the admin starts from a known-good
    // shape instead of blank fields. Never submits — the admin still
    // reviews every field (esp. the blank model_id placeholders) and clicks
    // Save explicitly, exactly like a hand-filled new shortcut.
    function loadTemplates() {
        var node = el('sc-templates-data');
        if (!node) { return []; }
        try { return JSON.parse(node.textContent || '[]'); }
        catch (e) { return []; }
    }

    function applyTemplate(tmpl) {
        resetForm();
        el('sc-title').value = tmpl.title || '';
        el('sc-description').value = tmpl.description || '';
        el('sc-icon').value = tmpl.icon || '✨';
        el('sc-category').value = tmpl.category || '1 Workspace';
        el('sc-kind').value = tmpl.kind;
        syncKindFields();
        // Same field mapping as fillForm()'s config reflection — see the
        // comment there on why owui_app needs its own model-reference ids.
        var cfg = tmpl.config || {};
        el('sc-model-id').value = cfg.model_id || '';
        if (el('sc-model-select')) { el('sc-model-select').value = cfg.model_id || ''; }
        el('sc-app-model-id').value = cfg.model_id || '';
        if (el('sc-app-model-select')) { el('sc-app-model-select').value = cfg.model_id || ''; }
        el('sc-app-prompt-tmpl').value = cfg.prompt_tmpl || '';
        el('sc-app-path').value = cfg.app_path || '';
        el('sc-provider').value = cfg.provider || 'claude';
        el('sc-prompt-tmpl').value = cfg.prompt_tmpl || '';
        el('sc-form-title').textContent = 'New app — from template: ' + (tmpl.title || '');
        // #248 App Builder — reflect into the new widgets + preview, and
        // mark the picked gallery card selected.
        syncTypeUI();
        syncIconUI();
        syncPreview();
        document.querySelectorAll('#sc-template-gallery .ab-tpl').forEach(function (t) {
            t.classList.toggle('sel', t.getAttribute('data-template') === tmpl.id);
        });
    }

    function gatherConfig(kind) {
        if (kind === 'owui_persona') { return { model_id: el('sc-model-id').value.trim() }; }
        if (kind === 'owui_app') {
            var cfg = { model_id: el('sc-app-model-id').value.trim() };
            var tmpl = el('sc-app-prompt-tmpl').value.trim();
            if (tmpl) { cfg.prompt_tmpl = tmpl; }
            return cfg;
        }
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
            category: el('sc-category').value.trim() || '1 Workspace',
            sort_order: parseInt(el('sc-sort').value, 10) || 100,
            visibility: el('sc-visibility').value,
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
                if (!r.ok) {
                    return r.json().catch(function () { return {}; }).then(function (d) {
                        throw new Error((d && d.error) || ('save ' + r.status));
                    });
                }
                return r.json();
            })
            .then(function () { resetForm(); loadList(); })
            .catch(function (e) {
                alert((e && e.message) || 'Save failed — check the fields and try again.');
            });
    }

    function removeShortcut(r) {
        if (!confirm('Delete app "' + r.title + '"?')) { return; }
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
            out.innerHTML = 'Save the app first, then Test.';
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

    // ── Curated icon set + emoji palette (#248 App Builder icon picker) ──
    // Mirrors shortcuts.py::CURATED_ICONS — presentation-only, small enough
    // to keep as a plain constant here (same call shortcuts.py's own
    // docstring makes about SCENARIO_TEMPLATES).
    var EMOJIS = ['🧾', '📮', '💬', '🔀', '☁️',
                  '📄', '🧠', '⚡', '🤖', '🔎',
                  '📊', '🗂️', '✅', '🛠️', '🔒',
                  '📝', '💡', '🎯', '📥', '🧩',
                  '🖼️', '🌐', '⚙️', '📌'];
    var CURATED_ICONS = [
        { name: 'pdf', label: 'PDF' }, { name: 'chat', label: 'Chat' },
        { name: 'documents', label: 'Documents' }, { name: 'workflow', label: 'Workflow' },
        { name: 'search', label: 'Search' }, { name: 'rag', label: 'RAG' },
        { name: 'mcp', label: 'MCP' }, { name: 'coding', label: 'Coding' }
    ];

    // ── Wire up ───────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        el('sc-kind').addEventListener('change', function () {
            syncKindFields(); syncTypeUI(); syncPreview();
        });
        el('sc-visibility').addEventListener('change', function () {
            syncVisibilityFields(); syncVisUI(); refreshMembers(); refreshWhoCanUse();
        });
        el('sc-form').addEventListener('submit', save);
        el('sc-new').addEventListener('click', resetForm);
        el('sc-cancel').addEventListener('click', resetForm);
        el('sc-test').addEventListener('click', testAsMe);
        el('sc-title').addEventListener('input', syncPreview);
        el('sc-description').addEventListener('input', syncPreview);
        var gf = el('sc-groups-filter');
        if (gf) { gf.addEventListener('input', applyGroupFilter); }
        var modelSel = el('sc-model-select');
        if (modelSel) {
            modelSel.addEventListener('change', function () {
                if (modelSel.value) { el('sc-model-id').value = modelSel.value; }
            });
        }
        var appModelSel = el('sc-app-model-select');
        if (appModelSel) {
            appModelSel.addEventListener('change', function () {
                if (appModelSel.value) { el('sc-app-model-id').value = appModelSel.value; }
            });
        }

        // ── icon picker: tabs, emoji grid, curated grid, upload ────────
        document.querySelectorAll('.ab-icontabs button').forEach(function (b) {
            b.addEventListener('click', function () { setIconTab(b.getAttribute('data-itab')); });
        });
        var eg = el('ab-itab-emoji');
        if (eg) {
            EMOJIS.forEach(function (e) {
                var b = document.createElement('button');
                b.type = 'button'; b.textContent = e;
                b.addEventListener('click', function () {
                    el('sc-icon').value = e; syncIconUI(); syncPreview();
                });
                eg.appendChild(b);
            });
        }
        var cg = el('ab-itab-curated');
        if (cg) {
            CURATED_ICONS.forEach(function (c) {
                var b = document.createElement('button');
                b.type = 'button'; b.setAttribute('data-curated', 'curated:' + c.name); b.title = c.label;
                b.innerHTML = '<img src="/static/icons/razzfazz-ai_' + c.name + '_icon.png" alt="' + c.label + '">';
                b.addEventListener('click', function () {
                    el('sc-icon').value = 'curated:' + c.name; syncIconUI(); syncPreview();
                });
                cg.appendChild(b);
            });
        }
        var iconUpload = el('ab-icon-upload-input');
        if (iconUpload) { iconUpload.addEventListener('change', onIconUpload); }

        // ── type cards (drive #sc-kind) ─────────────────────────────────
        document.querySelectorAll('#ab-types .ab-type').forEach(function (c) {
            if (c.classList.contains('ab-disabled')) { return; }   // P2/P3, shown badged-off only
            c.addEventListener('click', function () {
                el('sc-kind').value = c.getAttribute('data-type');
                syncKindFields(); syncTypeUI(); syncPreview();
            });
        });

        // ── visibility buttons (drive #sc-visibility) ──────────────────
        document.querySelectorAll('#ab-vis button').forEach(function (b) {
            b.addEventListener('click', function () {
                el('sc-visibility').value = b.getAttribute('data-vis');
                syncVisibilityFields(); syncVisUI(); refreshMembers(); refreshWhoCanUse();
            });
        });

        // ── template gallery ─────────────────────────────────────────
        var templates = loadTemplates();
        document.querySelectorAll('.sc-tmpl-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var tid = btn.getAttribute('data-template');
                if (tid === '__blank__') { resetForm(); return; }
                var tmpl = templates.filter(function (t) { return t.id === tid; })[0];
                if (tmpl) { applyTemplate(tmpl); }
            });
        });

        syncKindFields();
        syncVisibilityFields();
        syncTypeUI();
        syncVisUI();
        syncIconUI();
        syncPreview();
        loadGroups([]);
        loadModels();
        loadList();
    });
})();
