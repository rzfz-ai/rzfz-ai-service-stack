// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* #2553 — "My links": personal link shortcuts (add, edit, delete, reorder).
 *
 * Talks to the portal API (same-origin, SSO):
 *   GET    /api/me/links            the caller's own private links, in order
 *   POST   /api/shortcuts           create {kind: 'link', visibility: 'private'}  (CSRF)
 *   POST   /api/shortcuts/<id>      update title / icon / url / sort_order        (CSRF)
 *   DELETE /api/shortcuts/<id>      delete                                         (CSRF)
 *
 * The server validates every URL (https only, or http on this box's own
 * domain) and refuses anything else with a message this dialog shows. This
 * file never builds HTML from user input: textContent and DOM nodes only.
 * Same modal chrome and open/close idiom as start-portal-profile.js.
 */
(function () {
    'use strict';

    var modal = document.getElementById('my-links-dialog');
    var list = document.getElementById('my-links-list');
    var form = document.getElementById('my-links-form');
    var err = document.getElementById('my-links-error');
    var submit = document.getElementById('my-links-submit');
    var cancelEdit = document.getElementById('my-links-cancel-edit');
    if (!modal || !list || !form) { return; }   // signed out: nothing to wire up

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    var editing = null;    // id of the link being edited, or null when adding
    var changed = false;   // reload the portal on close only if something changed

    function showError(msg) {
        if (!err) { return; }
        err.textContent = msg || '';
        err.hidden = !msg;
    }

    // One fetch per method, each with a literal `method:` and the CSRF
    // header in the call itself, so tests/api/razzfazz-start-portal/
    // test_csrf.py (CFG-35) sees and checks every write in this file.
    function settle(r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
            if (!r.ok) { throw new Error((j && j.error) || ('HTTP ' + r.status)); }
            return j;
        });
    }
    function getJSON(url) {
        return fetch(url, { method: 'GET', credentials: 'same-origin',
                            headers: { 'Accept': 'application/json' } }).then(settle);
    }
    function postJSON(url, body) {
        return fetch(url, { method: 'POST', credentials: 'same-origin',
                            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
                            body: JSON.stringify(body) }).then(settle);
    }
    function deleteJSON(url) {
        return fetch(url, { method: 'DELETE', credentials: 'same-origin',
                            headers: { 'X-CSRF-Token': CSRF } }).then(settle);
    }

    function fail(e) { showError(String((e && e.message) || e)); }

    function load() {
        return getJSON('/api/me/links').then(render).catch(fail);
    }

    function button(label, title, onClick) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'my-links-btn';
        b.textContent = label;
        b.title = title;
        b.setAttribute('aria-label', title);
        b.addEventListener('click', onClick);
        return b;
    }

    function render(rows) {
        while (list.firstChild) { list.removeChild(list.firstChild); }
        if (!rows.length) {
            var empty = document.createElement('li');
            empty.className = 'my-links-empty';
            empty.textContent = 'No links yet. Add one below.';
            list.appendChild(empty);
            return;
        }
        rows.forEach(function (r, i) {
            var li = document.createElement('li');
            li.className = 'my-links-row';
            var text = document.createElement('span');
            text.className = 'my-links-text';
            var name = document.createElement('span');
            name.className = 'my-links-name';
            name.textContent = (r.icon ? r.icon + ' ' : '') + r.title;
            var url = document.createElement('small');
            url.className = 'my-links-url';
            url.textContent = (r.config || {}).url || '';
            text.appendChild(name);
            text.appendChild(url);
            li.appendChild(text);
            var actions = document.createElement('span');
            actions.className = 'my-links-actions';
            var up = button('↑', 'Move ' + r.title + ' up', function () { move(rows, i, -1); });
            up.disabled = i === 0;
            var down = button('↓', 'Move ' + r.title + ' down', function () { move(rows, i, 1); });
            down.disabled = i === rows.length - 1;
            actions.appendChild(up);
            actions.appendChild(down);
            actions.appendChild(button('Edit', 'Edit ' + r.title, function () { startEdit(r); }));
            actions.appendChild(button('Delete', 'Delete ' + r.title, function () {
                if (!window.confirm('Delete the link "' + r.title + '"?')) { return; }
                deleteJSON('/api/shortcuts/' + encodeURIComponent(r.id))
                    .then(function () { changed = true; if (editing === r.id) { stopEdit(); } return load(); })
                    .catch(fail);
            }));
            li.appendChild(actions);
            list.appendChild(li);
        });
    }

    // Reorder by renumbering the whole list (10, 20, 30 …): the stored
    // sort_order values need not be consecutive, so swapping two of them
    // could leave the pair where it was.
    function move(rows, i, d) {
        var order = rows.slice();
        var moved = order.splice(i, 1)[0];
        order.splice(i + d, 0, moved);
        var writes = [];
        order.forEach(function (r, k) {
            var want = (k + 1) * 10;
            if (r.sort_order !== want) {
                writes.push(postJSON('/api/shortcuts/' + encodeURIComponent(r.id), { sort_order: want }));
            }
        });
        showError('');
        Promise.all(writes).then(function () { changed = true; return load(); }).catch(fail);
    }

    function startEdit(r) {
        editing = r.id;
        form.elements.title.value = r.title || '';
        form.elements.url.value = (r.config || {}).url || '';
        form.elements.icon.value = r.icon || '';
        if (submit) { submit.textContent = 'Save'; }
        if (cancelEdit) { cancelEdit.hidden = false; }
        showError('');
        form.elements.title.focus();
    }

    function stopEdit() {
        editing = null;
        form.reset();
        if (submit) { submit.textContent = 'Add'; }
        if (cancelEdit) { cancelEdit.hidden = true; }
    }

    form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        showError('');
        var body = { title: form.elements.title.value.trim(),
                     icon: form.elements.icon.value.trim() || '🔗',
                     config: { url: form.elements.url.value.trim() } };
        var req;
        if (editing) {
            req = postJSON('/api/shortcuts/' + encodeURIComponent(editing), body);
        } else {
            body.kind = 'link';
            body.visibility = 'private';
            req = getJSON('/api/me/links').then(function (rows) {
                var last = rows.length ? (rows[rows.length - 1].sort_order || 0) : 0;
                body.sort_order = last + 10;
                return postJSON('/api/shortcuts', body);
            });
        }
        req.then(function () { changed = true; stopEdit(); return load(); }).catch(fail);
    });
    if (cancelEdit) { cancelEdit.addEventListener('click', stopEdit); }

    function openModal(ev) {
        if (ev) { ev.preventDefault(); }
        modal.hidden = false;
        document.body.classList.add('pw-modal-open');
        showError('');
        stopEdit();
        load();
        form.elements.title.focus();
    }
    function closeModal() {
        modal.hidden = true;
        document.body.classList.remove('pw-modal-open');
        if (changed) { window.location.reload(); }
    }

    document.querySelectorAll('[data-my-links-open]').forEach(function (b) {
        b.addEventListener('click', openModal);
    });
    modal.querySelectorAll('[data-my-links-close]').forEach(function (b) {
        b.addEventListener('click', closeModal);
    });
    document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && !modal.hidden) { closeModal(); }
    });
})();
