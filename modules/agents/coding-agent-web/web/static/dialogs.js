// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
//
// In-page dialogs and notices for the coding-agent terminal.
//
// The page used the browser's own prompt(), confirm() and alert(). Chromium
// browsers offer "prevent this page from creating additional dialogs" after a
// few of them; once ticked, prompt() returns null, confirm() returns false and
// alert() shows nothing, so renaming a tab, closing a session and every error
// message stopped working until the page was reloaded. These dialogs are part
// of the page, so the browser cannot suppress them.
//
//   RzfzUi.prompt(title, {value, okLabel})        -> Promise<string|null>
//   RzfzUi.confirm(message, {okLabel, danger})    -> Promise<boolean>
//   RzfzUi.notify(message, kind)                  -> the notice element
//
// kind is 'error' (stays until closed), 'ok' or 'info' (fade after a few
// seconds). Text is always set with textContent, never as HTML.
(function (global) {
    'use strict';
    var doc = global.document;

    function el(tag, cls, text) {
        var e = doc.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    // One dialog at a time; a second request waits for the first to close.
    var queue = Promise.resolve();

    function dialog(build) {
        var run = queue.then(function () {
            return new Promise(function (resolve) {
                var bg = el('div', 'modal-bg open rzfz-dialog');
                bg.setAttribute('role', 'dialog');
                bg.setAttribute('aria-modal', 'true');
                var box = el('div', 'modal');
                bg.appendChild(box);
                var finished = false;
                var prevFocus = doc.activeElement;
                function finish(value) {
                    if (finished) return;
                    finished = true;
                    bg.remove();
                    if (prevFocus && prevFocus.focus) {
                        try { prevFocus.focus(); } catch (_) { /* gone */ }
                    }
                    resolve(value);
                }
                var parts = build(box, finish);
                bg.addEventListener('keydown', function (e) {
                    if (e.key === 'Escape') { e.preventDefault(); finish(parts.cancelValue); }
                    else if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); parts.accept(); }
                });
                // a click on the dimmed backdrop (not inside the box) cancels
                bg.addEventListener('mousedown', function (e) {
                    if (e.target === bg) finish(parts.cancelValue);
                });
                doc.body.appendChild(bg);
                if (parts.focus && parts.focus.focus) parts.focus.focus();
            });
        });
        queue = run.then(function () {}, function () {});
        return run;
    }

    function buttons(box, okLabel, danger, onCancel, onOk) {
        var row = el('div', 'row');
        var cancel = el('button', 'btn', 'Cancel');
        cancel.type = 'button';
        cancel.addEventListener('click', onCancel);
        var ok = el('button', 'btn primary' + (danger ? ' danger' : ''), okLabel || 'OK');
        ok.type = 'button';
        ok.addEventListener('click', onOk);
        row.appendChild(cancel);
        row.appendChild(ok);
        box.appendChild(row);
        return { cancel: cancel, ok: ok };
    }

    function askText(title, opts) {
        opts = opts || {};
        return dialog(function (box, finish) {
            box.appendChild(el('h2', '', title));
            var input = el('input');
            input.type = 'text';
            input.value = opts.value || '';
            input.setAttribute('aria-label', title);
            box.appendChild(input);
            if (opts.hint) box.appendChild(el('div', 'hint', opts.hint));
            var accept = function () { finish(input.value); };
            buttons(box, opts.okLabel, false, function () { finish(null); }, accept);
            if (input.select) setTimeout(function () { try { input.select(); } catch (_) {} }, 0);
            return { accept: accept, cancelValue: null, focus: input };
        });
    }

    function askConfirm(message, opts) {
        opts = opts || {};
        return dialog(function (box, finish) {
            box.appendChild(el('h2', '', opts.title || 'Please confirm'));
            box.appendChild(el('p', 'rzfz-dialog-text', message));
            var accept = function () { finish(true); };
            var b = buttons(box, opts.okLabel, opts.danger, function () { finish(false); }, accept);
            // focus Cancel: Enter on a destructive question must be a choice
            return { accept: accept, cancelValue: false, focus: opts.danger ? b.cancel : b.ok };
        });
    }

    var tray = null;
    function notify(message, kind) {
        kind = kind || 'error';
        if (!tray || !tray.parentNode) {
            tray = el('div', 'rzfz-toasts');
            tray.setAttribute('aria-live', 'polite');
            doc.body.appendChild(tray);
        }
        var t = el('div', 'rzfz-toast ' + kind);
        t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
        t.appendChild(el('span', 'rzfz-toast-text', message));
        var close = el('button', 'rzfz-toast-close', '✕');
        close.type = 'button';
        close.setAttribute('aria-label', 'Close');
        close.addEventListener('click', function () { t.remove(); });
        t.appendChild(close);
        tray.appendChild(t);
        if (kind !== 'error') setTimeout(function () { t.remove(); }, 5000);
        return t;
    }

    global.RzfzUi = { prompt: askText, confirm: askConfirm, notify: notify };
})(window);
