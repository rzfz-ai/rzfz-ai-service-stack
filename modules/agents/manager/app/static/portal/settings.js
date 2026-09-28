// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* 2026.09-ga.4 (#2517 §5): wires the settings partial
 * (templates/dashboard/_instance_settings.html) on BOTH surfaces that show it:
 * the portal's Settings tab (ctx = PORTAL_CTX + reloadSettings) and the full
 * settings page /instance/<id> (ctx = PAGE_CTX in detail.html).
 *
 * The partial carries no script and no inline handlers; everything is found
 * through data-am-* hooks:
 *   [data-am-action=start|stop|restart|refresh|delete] → AmActions.run(id, node, ctx),
 *       the same registry the tree menu, the pane head and the palette use;
 *   form[data-am-form=rename|memory|pids]               → the matching API call;
 *   [data-am-result=rename|memory|pids]                 → the outcome, as textContent.
 * Server messages and suggested names are user-influenced text: they are only
 * ever assigned to textContent. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AmSettings = api;
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';
    const enc = (s) => encodeURIComponent(String(s));
    const ERR = 'var(--error, #C0271F)';
    const ACTION_LABEL = { start: 'Start', stop: 'Stop', restart: 'Restart', refresh: 'Reinstall', delete: 'Delete' };

    function errText(r) {
        const d = (r && r.data) || {};
        return d.message || d.error || ('HTTP ' + (r ? r.status : 0));
    }

    // The type-the-name confirmation both surfaces use for Delete.
    function confirmDeleteDialog(ui, node) {
        return ui.dialog.typeToConfirm({
            title: 'Delete ' + node.label + '?',
            body: 'This permanently deletes the agent and ALL its data: chats, files, memory and config. There is no automatic backup.',
            expected: node.label, confirmLabel: 'Delete agent' });
    }

    function wire(root, node, ctx, deps) {
        if (!root || !node || root.dataset.amWired === '1') return;
        root.dataset.amWired = '1';
        const A = (deps && deps.actions) || (typeof AmActions !== 'undefined' ? AmActions : null);
        const id = node.instance_id;

        const setResult = (key, text, isError) => {
            const el = root.querySelector('[data-am-result="' + key + '"]');
            if (!el) return;
            el.textContent = String(text);
            el.style.color = isError ? ERR : '';
            el.dataset.state = isError ? 'error' : 'ok';
        };

        root.addEventListener('click', async (e) => {
            const b = e.target.closest('[data-am-action]');
            if (!b || !root.contains(b) || b.disabled) return;
            e.preventDefault();
            const act = b.dataset.amAction;
            b.disabled = true;
            // The Update button names its target version; the portal's tree
            // node does not carry it, so the button hands it to the registry.
            const n = b.dataset.updateTo ? Object.assign({}, node, { update_target_version: b.dataset.updateTo }) : node;
            // A surface that re-fetches the settings by itself during the
            // action (the portal's status-crossing re-open) moves its mark,
            // and the reload below is then not needed (fix round 1).
            const mark = ctx.settingsMark ? ctx.settingsMark() : undefined;
            let res = null;
            try {
                res = await A.run(act, n, ctx);
            } catch (err) {
                ctx.toast((ACTION_LABEL[act] || act) + ' failed: ' + err);
            } finally {
                b.disabled = false;
            }
            if (res && res.refused) ctx.toast(res.refused);
            if (res && res.ok && ctx.reloadSettings
                && (mark === undefined || mark === ctx.settingsMark())) ctx.reloadSettings();
        });

        const forms = {
            async rename(form) {
                const v = form.elements.name.value.trim();
                if (!v) { setResult('rename', 'Give the agent a name.', true); return; }
                setResult('rename', 'Saving…');
                const r = await ctx.api('PATCH', '/api/instances/' + enc(id), { name: v });
                if (!r.ok) {
                    const d = r.data || {};
                    setResult('rename', errText(r)
                        + ((r.status === 400 || r.status === 409) && d.suggested_name
                           ? ' Try: ' + d.suggested_name : ''), true);
                    return;
                }
                setResult('rename', (r.data && r.data.message) || 'Saved.');
                // every surface re-reads the persisted name (tree, pane title)
                if (ctx.refresh) await ctx.refresh();
            },
            async memory(form) {
                setResult('memory', 'Applying…');
                const r = await ctx.api('POST', '/api/memory/' + enc(id),
                                        { mem_gb: parseInt(form.elements.mem_gb.value, 10) });
                if (!r.ok) { setResult('memory', errText(r), true); return; }
                setResult('memory', (r.data && r.data.message) || 'Updated.');
                // the "Current limit" line is server-rendered: re-read it once
                // the user had a moment to see the outcome
                if (ctx.reloadSettings) setTimeout(() => ctx.reloadSettings(), 1200);
            },
            async pids(form) {
                setResult('pids', 'Applying…');
                const r = await ctx.api('POST', '/api/pids/' + enc(id),
                                        { pids_limit: parseInt(form.elements.pids_limit.value, 10) });
                if (!r.ok) { setResult('pids', errText(r), true); return; }
                // Deliberately no reload: the message distinguishes "applied
                // live" from "applies on next start", and that is what to read.
                setResult('pids', (r.data && r.data.message) || 'Updated.');
            },
        };
        root.querySelectorAll('form[data-am-form]').forEach((f) => {
            f.addEventListener('submit', (e) => {
                e.preventDefault();
                const h = forms[f.dataset.amForm];
                if (!h) return;
                h(f).catch((err) => setResult(f.dataset.amForm, 'Failed: ' + err, true));
            });
        });
    }

    return { wire, confirmDeleteDialog };
});
