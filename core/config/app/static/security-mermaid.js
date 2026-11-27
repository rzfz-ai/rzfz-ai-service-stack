/* SPDX-License-Identifier: BUSL-1.1
 * Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
 *
 * #2564 — render the ```mermaid fences of the Security Documentation page.
 *
 * blueprints/security.py renders docs/security-architecture.md server-side and
 * emits each diagram as <pre class="mermaid">SOURCE</pre> — the same element the
 * Help service emits. This hook lets the vendored library (vendor/mermaid.min.js,
 * byte-identical to the Help service's copy) draw it. Loaded as a file, not
 * inline, so it works under `script-src 'self'`.
 *
 * Failure is never a blank area: when a diagram does not render, its source
 * comes back as a plain code block with a one-line note.
 */
(function () {
    'use strict';

    var FAILED_NOTE = 'Diagram could not be rendered.';

    function showSource(pre, source) {
        pre.classList.remove('mermaid');
        pre.classList.add('mermaid-source');
        pre.textContent = source;           // textContent: never parsed as HTML
        var note = document.createElement('p');
        note.className = 'mermaid-error';
        note.textContent = FAILED_NOTE;
        pre.parentNode.insertBefore(note, pre.nextSibling);
    }

    function render() {
        var pres = document.querySelectorAll('.markdown-body pre.mermaid');
        if (!pres.length) { return; }

        var items = [];
        Array.prototype.forEach.call(pres, function (pre) {
            items.push({ pre: pre, source: pre.textContent });
        });

        if (!window.mermaid) {
            items.forEach(function (item) { showSource(item.pre, item.source); });
            return;
        }

        // Same settings as core/help/templates/viewer.html. startOnLoad=false:
        // we run explicitly and only on our own blocks. securityLevel 'strict'
        // keeps any HTML in labels escaped and disables click handlers.
        window.mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: 'default',
            fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
        });

        // One diagram at a time, so one that fails to parse does not take the
        // others down with it.
        var chain = Promise.resolve();
        items.forEach(function (item) {
            chain = chain.then(function () {
                return window.mermaid.run({ nodes: [item.pre] });
            }).then(function () {
                if (!item.pre.querySelector('svg')) { showSource(item.pre, item.source); }
            }, function () {
                showSource(item.pre, item.source);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', render);
    } else {
        render();
    }
})();
