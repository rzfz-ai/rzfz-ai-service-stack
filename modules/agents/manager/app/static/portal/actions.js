// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* 2026.09-ga.4 (#2517 §2, #959 critique "one action registry per agent").
 * Pure and DOM-free: every side effect goes through the injected ctx
 * (see PORTAL_CTX in portal.html). The tree's "…" menu, the pane head, the
 * palette (Task 5) and the Settings tab (Task 6) render THESE descriptors, so
 * an action cannot exist on one surface and be missing, or differ, on another. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AmActions = api;
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';
    const NEEDS_START = 'Start the agent first.';
    const STARTING = 'The agent is starting…';
    // why an Open item is disabled: a starting agent needs no Start
    const openReason = (n) => (n.status === 'starting' ? STARTING : NEEDS_START);
    const running = (n) => n.status === 'running';
    const ui = (n) => n.ui || {};
    const enc = (s) => encodeURIComponent(String(s));
    const VERB_LABEL = { start: 'Start', stop: 'Stop', restart: 'Restart', upgrade: 'Reinstall' };

    async function lifecycle(n, ctx, verb) {
        const r = await ctx.api('POST', '/api/' + verb + '/' + enc(n.instance_id));
        const msg = (r.data && (r.data.message || r.data.error)) || ('HTTP ' + r.status);
        ctx.toast(r.ok ? msg : (VERB_LABEL[verb] + ' failed: ' + msg));
        await ctx.refresh();
        return { ok: !!r.ok, message: msg };
    }

    const ACTIONS = [
        { id: 'open.web', group: 'open', reason: openReason, label: 'Web UI', verbs: ['open', 'web', 'ui'],
          visible: (n) => !!(n.has_web_ui || ui(n).web),
          enabled: (n) => running(n) && !!ui(n).web,
          run: (n, ctx) => ctx.openPane(n, 'web') },
        { id: 'open.terminal', group: 'open', reason: openReason, label: 'Terminal', verbs: ['open', 'terminal', 'shell'],
          visible: () => true,
          enabled: (n) => running(n) && !!ui(n).terminal,
          run: (n, ctx) => ctx.openPane(n, 'terminal') },
        { id: 'open.logs', group: 'open', reason: openReason, label: 'Logs', verbs: ['logs'],
          visible: () => true, enabled: running,
          run: (n, ctx) => ctx.openPane(n, 'logs') },
        { id: 'open.files', group: 'open', reason: openReason, label: 'Files', verbs: ['files'],
          visible: (n) => !!n.files, enabled: running,
          run: (n, ctx) => ctx.openPane(n, 'files') },
        { id: 'start', group: 'lifecycle', label: 'Start', verbs: ['start'],
          visible: (n) => n.status === 'stopped' || n.status === 'error', enabled: () => true,
          run: (n, ctx) => lifecycle(n, ctx, 'start') },
        { id: 'stop', group: 'lifecycle', label: 'Stop', verbs: ['stop'],
          visible: (n) => n.status === 'running' || n.status === 'starting', enabled: () => true,
          run: (n, ctx) => lifecycle(n, ctx, 'stop') },
        { id: 'restart', group: 'lifecycle', label: 'Restart', verbs: ['restart'],
          visible: running, enabled: () => true,
          run: (n, ctx) => lifecycle(n, ctx, 'restart') },
        { id: 'rename', group: 'manage', label: 'Rename…', verbs: ['rename'],
          visible: () => true, enabled: () => true,
          run: (n, ctx) => ctx.rename(n) },
        { id: 'move', group: 'manage', label: 'Move to', verbs: [], submenu: true, palette: false,
          visible: () => true, enabled: () => true,
          run: (n, ctx, folder) => ctx.moveTo(n, folder === undefined ? null : folder) },
        { id: 'settings', group: 'manage', label: 'Settings', verbs: ['settings'],
          visible: () => true, enabled: () => true,
          run: (n, ctx) => ctx.openPane(n, 'settings') },
        // #3187: one-click Update for an instance behind on its image (badge
        // in the tree row). Same volumes-preserving /api/upgrade the settings
        // page uses; the confirm names the target when it is known.
        { id: 'update', group: 'manage', label: 'Update', verbs: ['update now', 'upgrade'],
          visible: (n) => !!n.update_available, enabled: () => true,
          run: async (n, ctx) => {
              const to = n.update_target_version;
              const ok = await ctx.confirm({
                  title: 'Update ' + n.label + (to ? ' to ' + to : '') + '?',
                  body: 'The agent is recreated on the new image (~30 s downtime). ' +
                        'Chats, skills, files and config are kept.',
                  confirmLabel: 'Update' });
              if (!ok) return { ok: false, cancelled: true };
              return lifecycle(n, ctx, 'upgrade');
          } },
        { id: 'refresh', group: 'manage', label: 'Reinstall / Refresh…', verbs: ['refresh', 'reinstall', 'update'],
          visible: (n) => n.status === 'running' || n.status === 'stopped', enabled: () => true,
          run: async (n, ctx) => {
              // U6 fix round 1: when a newer version is known (the settings
              // surfaces pass it), the question names it: "Update X to vY?".
              const to = n.update_target_version;
              const ok = await ctx.confirm(to ? {
                  title: 'Update ' + n.label + ' to ' + to + '?',
                  body: 'The agent is recreated on ' + to + ' (~30 s downtime). ' +
                        'Chats, skills, files and config are kept.',
                  confirmLabel: 'Update' } : {
                  title: 'Reinstall ' + n.label + '?',
                  body: 'The agent is recreated on the current image (~30 s downtime). ' +
                        'Chats, skills, files and config are kept.',
                  confirmLabel: 'Reinstall' });
              if (!ok) return { ok: false, cancelled: true };
              return lifecycle(n, ctx, 'upgrade');
          } },
        { id: 'delete', group: 'danger', label: 'Delete…', verbs: ['delete', 'remove'], danger: true,
          visible: () => true, enabled: () => true,
          run: async (n, ctx) => {
              if (!(await ctx.confirmDelete(n))) return { ok: false, cancelled: true };
              const r = await ctx.api('POST', '/api/delete/' + enc(n.instance_id) + '?confirm=Delete');
              const msg = (r.data && (r.data.message || r.data.error)) || ('HTTP ' + r.status);
              if (r.ok) ctx.closePanesOf(n);
              ctx.toast(r.ok ? ('Deleted “' + n.label + '”.') : ('Delete failed: ' + msg));
              await ctx.refresh();
              return { ok: !!r.ok, message: msg };
          } },
    ];
    const GROUP_ORDER = ['open', 'lifecycle', 'manage', 'danger'];
    const BY_ID = new Map(ACTIONS.map((a) => [a.id, a]));

    // #2517 §4 (Task 5): the palette's ranking. Every query char must appear
    // in order; earlier + denser matches rank higher. Empty query matches
    // everything (browse mode). Identical to the removed portal.html _palScore.
    function subseqScore(q, text) {
        q = (q || '').trim().toLowerCase();
        if (!q) return 0;
        const t = (text || '').toLowerCase();
        let ti = 0, score = 100;
        for (const ch of q) {
            const idx = t.indexOf(ch, ti);
            if (idx === -1) return -Infinity;
            score -= (idx - ti); ti = idx + 1;
        }
        return score;
    }
    const COLL = (typeof Intl !== 'undefined') ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' }) : null;
    const cmp = (a, b) => (COLL ? COLL.compare(a, b) : (a < b ? -1 : a > b ? 1 : 0));
    function entryLabel(a, name) {
        const base = a.label.replace(/…$/, '');
        return name + ' → ' + (a.group === 'open' ? 'Open ' + base : base);
    }
    // Builds the palette's per-agent rows from THIS registry, so "stop user"
    // ranks the same Stop the "…" menu shows, with the same label and the
    // same disabled reason (#959 critique: one source of truth per surface).
    function paletteEntries(nodes, query) {
        const out = [];
        const q = (query || '').trim();
        (nodes || []).filter((n) => n && n.instance_id).forEach((n) => {
            const name = String(n.label || n.agent_type || '');
            if (!q) {
                const def = BY_ID.get('open.' + (n.default_pane || 'terminal'));
                let a = def && def.visible(n) && def.enabled(n) ? def : null;
                if (!a) a = BY_ID.get('start').visible(n) ? BY_ID.get('start') : BY_ID.get('settings');
                out.push({ instance_id: n.instance_id, action: a.id, label: entryLabel(a, name),
                           hint: a.group === 'open' ? 'open' : a.id, score: 0, enabled: true, reason: null });
                return;
            }
            ACTIONS.forEach((a) => {
                if (a.palette === false || !a.visible(n)) return;
                const verbs = a.verbs.join(' ');
                const s = Math.max(subseqScore(q, verbs + ' ' + name), subseqScore(q, name + ' ' + verbs),
                                   subseqScore(q, entryLabel(a, name)));
                if (s === -Infinity) return;
                const en = !!a.enabled(n);
                out.push({ instance_id: n.instance_id, action: a.id, label: entryLabel(a, name),
                           hint: a.group, score: s - (en ? 0 : 5), enabled: en,
                           reason: en ? null : (a.reason ? a.reason(n) : NEEDS_START) });
            });
        });
        return out.sort((x, y) => (y.score - x.score) || cmp(x.label, y.label));
    }

    function actionsFor(node) {
        if (!node || !node.instance_id) return [];
        return ACTIONS.filter((a) => a.visible(node)).map((a) => {
            const enabled = !!a.enabled(node);
            return { id: a.id, label: a.label, group: a.group, danger: !!a.danger,
                     submenu: !!a.submenu, enabled,
                     reason: enabled ? null : (a.reason ? a.reason(node) : NEEDS_START) };
        });
    }

    async function run(id, node, ctx, arg) {
        const a = BY_ID.get(id);
        if (!a) throw new Error('unknown action ' + id);
        if (!node || !node.instance_id || !a.visible(node)) return { ok: false, refused: 'Not available for this agent.' };
        if (!a.enabled(node)) return { ok: false, refused: a.reason ? a.reason(node) : NEEDS_START };
        const res = await a.run(node, ctx, arg);
        return res && typeof res === 'object' ? res : { ok: true };
    }

    return { ACTIONS, GROUP_ORDER, actionsFor, run, NEEDS_START, STARTING, subseqScore, paletteEntries };
});
