// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* 2026.09-ga.4 (#2517, #959 critique "Dialogs"): the portal's own dialog and
 * menu. No native prompt/confirm/alert. User-controlled strings (agent and
 * folder names) are only ever assigned to textContent / value. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AmUi = api;
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';

    async function api(method, url, body) {
        try {
            const init = { method, credentials: 'same-origin', headers: {} };
            if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
            const r = await fetch(url, init);
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok && !data.error, status: r.status, data };
        } catch (e) {
            return { ok: false, status: 0, data: { error: String(e) } };
        }
    }

    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = String(text);
        return e;
    }

    // One modal at a time. `build(body, setValid)` fills the body and returns
    // a getter for the resolved value; `setValid(bool, error?)` gates the button.
    function modal({ title, body, confirmLabel, danger, build, cancelValue, noCancel }) {
        return new Promise((resolve) => {
            const opener = document.activeElement;
            const d = el('dialog', 'am-dialog');
            d.setAttribute('aria-labelledby', 'am-dialog-title');
            const h = el('h3', 'am-dialog-title', title); h.id = 'am-dialog-title';
            d.appendChild(h);
            if (body) d.appendChild(el('p', 'am-dialog-body', body));
            const slot = el('div', 'am-dialog-fields'); d.appendChild(slot);
            const err = el('div', 'am-dialog-error'); err.setAttribute('role', 'alert'); err.hidden = true;
            d.appendChild(err);
            const foot = el('div', 'am-dialog-foot');
            const cancel = el('button', 'btn btn-secondary btn-sm', 'Cancel'); cancel.type = 'button';
            const ok = el('button', 'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-primary'), confirmLabel || 'OK');
            ok.type = 'button'; ok.className += ' am-dialog-ok';
            if (!noCancel) foot.appendChild(cancel);
            foot.appendChild(ok); d.appendChild(foot);
            let settled = false;
            const done = (v) => {
                if (settled) return; settled = true;
                d.close(); d.remove();
                if (opener && opener.focus && document.contains(opener)) opener.focus();
                resolve(v);
            };
            const setValid = (valid, message) => {
                ok.disabled = !valid;
                err.hidden = !message; err.textContent = message || '';
            };
            const getValue = build ? build(slot, setValid, () => { if (!ok.disabled) done(getValue()); }) : () => true;
            const cancelled = (cancelValue === undefined) ? false : cancelValue;   // prompt: null
            cancel.onclick = () => done(cancelled);
            d.addEventListener('cancel', (e) => { e.preventDefault(); done(cancelled); });
            ok.onclick = () => { if (!ok.disabled) done(getValue()); };
            document.body.appendChild(d);
            d.showModal();
            const first = d.querySelector('input') || ok;
            first.focus();
        });
    }

    const dialog = {
        // Property functions, not method shorthand: the shorthand form reads
        // as a native dialog call to the no-native-dialogs guard's regex.
        prompt: function ({ title, label, value, confirmLabel, validate }) {
            return modal({ title, confirmLabel: confirmLabel || 'Save', cancelValue: null, build(slot, setValid, submit) {
                const lab = el('label', 'am-dialog-label', label);
                const inp = el('input', 'am-dialog-input'); inp.type = 'text'; inp.value = value || '';
                inp.maxLength = 64; inp.autocomplete = 'off'; inp.spellcheck = false;
                lab.appendChild(inp); slot.appendChild(lab);
                const check = () => { const e = validate ? validate(inp.value) : (inp.value.trim() ? null : 'Required.'); setValid(!e, e); };
                inp.addEventListener('input', check);
                inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
                check();
                return () => inp.value.trim();
            } });
        },
        confirm: function ({ title, body, confirmLabel, danger }) {
            return modal({ title, body, confirmLabel, danger });
        },
        // An acknowledgement (error or outcome) with a single button; resolves
        // true on OK, false on Escape. Stands in for the browser's alert dialog.
        notice: function ({ title, body, confirmLabel }) {
            return modal({ title, body, confirmLabel: confirmLabel || 'OK', noCancel: true });
        },
        typeToConfirm: function ({ title, body, expected, confirmLabel }) {
            return modal({ title, body, confirmLabel: confirmLabel || 'Delete', danger: true,
                build(slot, setValid, submit) {
                    const lab = el('label', 'am-dialog-label');
                    // #2536 item 25: ONE line — the label is a flex column, so
                    // the sentence is one child, not three.
                    const prompt = el('span', 'am-dialog-prompt');
                    prompt.appendChild(document.createTextNode('Type '));
                    prompt.appendChild(el('strong', 'am-dialog-expected', expected));
                    prompt.appendChild(document.createTextNode(' to confirm:'));
                    lab.appendChild(prompt);
                    const inp = el('input', 'am-dialog-input'); inp.type = 'text';
                    inp.autocomplete = 'off'; inp.spellcheck = false;
                    lab.appendChild(inp); slot.appendChild(lab);
                    const check = () => setValid(inp.value.trim() === expected, null);
                    inp.addEventListener('input', check);
                    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
                    check();
                    return () => true;
                } });
        },
    };

    // ── menu ────────────────────────────────────────────────────────────────
    let openMenus = [];   // root first
    // Every way a menu goes away runs its cleanup (the trigger's aria-expanded).
    function drop(m) { if (m._cleanup) m._cleanup(); m.remove(); }
    function closeAll() { while (openMenus.length) drop(openMenus.pop()); }

    // `trigger`: the control that opened the menu; it carries aria-expanded
    // while the menu is open. Defaults to an `anchor` that declares
    // aria-haspopup. A submenu's trigger is the item that opened it
    // (`_opener`), and focus goes back to it on ArrowLeft / Escape.
    function menu_open({ label, items, anchor, x, y, onClose, trigger, _parent, _opener }) {
        if (!_parent) closeAll();
        const opener = _parent ? null : document.activeElement;
        const expander = _opener || trigger
            || (anchor && anchor.hasAttribute && anchor.hasAttribute('aria-haspopup') ? anchor : null);
        const ul = el('ul', 'am-menu'); ul.setAttribute('role', 'menu');
        ul.setAttribute('aria-label', label || 'Actions'); ul.tabIndex = -1;
        const lis = [];
        (typeof items === 'function' ? items() : items).forEach((it) => {
            if (it.separatorBefore) { const s = el('li', 'am-menu-sep'); s.setAttribute('role', 'separator'); ul.appendChild(s); }
            const li = el('li', 'am-menu-item' + (it.danger ? ' danger' : ''));
            li.setAttribute('role', it.checked === undefined ? 'menuitem' : 'menuitemradio');
            if (it.checked !== undefined) li.setAttribute('aria-checked', it.checked ? 'true' : 'false');
            li.tabIndex = -1; li.dataset.id = it.id;
            li.appendChild(el('span', 'am-menu-label', it.label));
            if (it.secondary) li.appendChild(el('span', 'am-menu-secondary', it.secondary));
            if (it.submenu) {
                li.setAttribute('aria-haspopup', 'menu'); li.setAttribute('aria-expanded', 'false');
                li.appendChild(el('span', 'am-menu-arrow', '▸'));
            }
            if (it.disabled) li.setAttribute('aria-disabled', 'true');
            li._item = it; ul.appendChild(li); lis.push(li);
        });
        const close = () => {
            const i = openMenus.indexOf(ul);
            if (i >= 0) openMenus.splice(i).forEach(drop);
            if (!_parent) {
                document.removeEventListener('mousedown', outside, true);
                if (opener && opener.focus && document.contains(opener)) opener.focus();
                if (onClose) onClose();
            }
        };
        const activate = (li) => {
            const it = li._item;
            if (it.disabled) return;
            if (it.submenu) { openSub(li); return; }
            closeAll(); document.removeEventListener('mousedown', outside, true);
            if (opener && opener.focus && document.contains(opener)) opener.focus();
            if (onClose) onClose();
            if (it.run) it.run();
        };
        const openSub = (li) => {
            const r = li.getBoundingClientRect();
            // one submenu per level: a sibling's open submenu closes first
            const mine = openMenus.indexOf(ul);
            if (mine >= 0) openMenus.splice(mine + 1).forEach(drop);
            const sub = menu_open({ label: li._item.label, items: li._item.submenu, x: r.right, y: r.top,
                                    _parent: ul, _opener: li });
            sub.focusFirst();
        };
        const focusAt = (i) => { if (lis.length) lis[(i + lis.length) % lis.length].focus(); };
        ul.addEventListener('click', (e) => { const li = e.target.closest('.am-menu-item'); if (li) activate(li); });
        ul.addEventListener('keydown', (e) => {
            const i = lis.indexOf(document.activeElement);
            if (e.key === 'ArrowDown') { e.preventDefault(); focusAt(i + 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); focusAt(i - 1); }
            else if (e.key === 'Home') { e.preventDefault(); focusAt(0); }
            else if (e.key === 'End') { e.preventDefault(); focusAt(lis.length - 1); }
            else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (i >= 0) activate(lis[i]); }
            else if (e.key === 'ArrowRight') { if (i >= 0 && lis[i]._item.submenu && !lis[i]._item.disabled) { e.preventDefault(); openSub(lis[i]); } }
            else if (e.key === 'ArrowLeft' && _parent) { e.preventDefault(); close(); (_opener || _parent).focus(); }
            else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (_parent) { close(); (_opener || _parent).focus(); } else close(); }
            else if (e.key === 'Tab') { closeAll(); }
        });
        const outside = (e) => { if (!openMenus.some((m) => m.contains(e.target))) { closeAll(); document.removeEventListener('mousedown', outside, true); if (onClose) onClose(); } };
        if (!_parent) document.addEventListener('mousedown', outside, true);
        if (expander) {
            expander.setAttribute('aria-expanded', 'true');
            ul._cleanup = () => expander.setAttribute('aria-expanded', 'false');
        }
        document.body.appendChild(ul); openMenus.push(ul);
        // position inside the viewport (flip left/up when it would overflow)
        let px = x, py = y;
        if (anchor) { const r = anchor.getBoundingClientRect(); px = r.left; py = r.bottom + 2; }
        const w = ul.offsetWidth, h = ul.offsetHeight;
        px = Math.max(4, Math.min(px || 0, window.innerWidth - w - 4));
        py = Math.max(4, Math.min(py || 0, window.innerHeight - h - 4));
        ul.style.left = px + 'px'; ul.style.top = py + 'px';
        const api_ = { close, focusFirst: () => focusAt(0), el: ul };
        if (!_parent) focusAt(0);
        return api_;
    }

    // ── inline edit (2026.09-ga.4, #2517 §3: rename in the tree) ──────────
    // Replaces `host`'s content with an <input>. Enter commits through
    // `onCommit(value)`, which resolves to null (accepted) or an error text;
    // an error keeps the input open and shows the text in a role=alert
    // element. Escape and blur cancel and restore the original content.
    // `onEnd(committed, value)` runs once the DOM is restored either way.
    function inlineEdit(host, { value, label, onCommit, onCancel, onEnd }) {
        const saved = Array.from(host.childNodes);
        host.textContent = '';
        host.classList.add('am-inline-edit');
        const inp = el('input', 'am-inline-input');
        inp.type = 'text'; inp.value = value || ''; inp.maxLength = 64;
        inp.autocomplete = 'off'; inp.spellcheck = false;
        inp.setAttribute('aria-label', label || 'Name');
        const err = el('span', 'am-inline-error'); err.setAttribute('role', 'alert'); err.hidden = true;
        host.appendChild(inp); host.appendChild(err);
        let busy = false, done = false;
        const finish = (committed, v) => {
            if (done) return;
            done = true;
            host.classList.remove('am-inline-edit');
            host.textContent = '';
            if (committed) host.textContent = v;
            else saved.forEach((n) => host.appendChild(n));
            if (!committed && onCancel) onCancel();
            if (onEnd) onEnd(committed, v);
        };
        const commit = async () => {
            if (busy || done) return;
            busy = true;
            const v = inp.value.trim();
            let msg;
            try { msg = onCommit ? await onCommit(v) : null; } catch (e) { msg = String(e); }
            busy = false;
            if (done) return;
            if (msg) {
                err.textContent = String(msg); err.hidden = false;
                inp.setAttribute('aria-invalid', 'true');
                inp.focus();
            } else {
                finish(true, v);
            }
        };
        inp.addEventListener('keydown', (e) => {
            e.stopPropagation();
            if (e.key === 'Escape') { e.preventDefault(); finish(false); }
            else if (e.key === 'Enter') { e.preventDefault(); commit(); }
        });
        inp.addEventListener('input', () => { err.hidden = true; err.textContent = ''; inp.removeAttribute('aria-invalid'); });
        inp.addEventListener('blur', () => { if (!busy) finish(false); });
        ['click', 'dblclick', 'mousedown'].forEach((t) => inp.addEventListener(t, (e) => e.stopPropagation()));
        inp.focus();
        inp.select();
    }

    // A self-dismissing status line for pages without the portal's toast.
    function toast(text) {
        const t = el('div', 'am-toast', text);
        t.setAttribute('role', 'status');
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 6000);
        return t;
    }

    return { api, dialog, menu: { open: menu_open, closeAll }, inlineEdit, toast };
});
