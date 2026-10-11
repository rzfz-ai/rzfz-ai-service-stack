// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* 2026.09-ga.4 (#2516, E5): the portal's mobile remote-control view.
 *
 * On a touch device, or a window 1024 px wide or less, the portal becomes a
 * list of your agents; tapping one opens it full screen with a top bar (back,
 * state, name, font size, the agent's "…" menu) and, for a terminal, the key
 * bar. A per-device override in localStorage forces either view.
 *
 * Rules this controller keeps:
 * - Going back to the list only switches body classes. It never closes the
 *   pane, so the xterm and its WebSocket stay alive (client scrollback kept).
 *   Re-opening the same agent switches classes again; a different agent
 *   replaces the one live session (tmux keeps the server side, #611).
 * - The mobile view never writes the desktop layout: portal.html's savePrefs
 *   keeps the desktop layout/slots while isOn().
 * - Every localStorage access is wrapped in try/catch (private windows,
 *   blocked site data).
 * UMD: the browser global `AmMobile`, CommonJS under node (want() is pure). */
(function (root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AmMobile = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
    'use strict';

    const KEY_MODE = 'rzfz.portal.viewMode';
    const KEY_FONT = 'rzfz.portal.termFontSize';
    const FONT_MIN = 10, FONT_MAX = 24, FONT_DEFAULT = 14;

    // The one decision: a device override wins; otherwise touch capability
    // or a narrow window.
    function want({ coarse, width, override }) {
        return override === 'mobile' || (override !== 'desktop' && (!!coarse || width <= 1024));
    }

    function lsGet(k) {
        try { return root.localStorage.getItem(k); } catch (e) { return null; }
    }
    function lsSet(k, v) {
        try {
            if (v === null || v === undefined) root.localStorage.removeItem(k);
            else root.localStorage.setItem(k, String(v));
        } catch (e) { /* private window / blocked storage: per-session only */ }
    }
    function override() {
        const v = lsGet(KEY_MODE);
        return v === 'mobile' || v === 'desktop' ? v : null;
    }
    function clampFont(n) {
        n = Math.round(Number(n));
        if (!Number.isFinite(n)) return FONT_DEFAULT;
        return Math.min(FONT_MAX, Math.max(FONT_MIN, n));
    }
    function fontSize() {
        const v = lsGet(KEY_FONT);
        return v === null || v === '' ? FONT_DEFAULT : clampFont(v);
    }

    let ctx = null, on = false, mode = 'list', lastSessionId = null;
    let mq = null, timer = null;
    function coarseQuery() {
        if (!mq && root.matchMedia) mq = root.matchMedia('(pointer: coarse)');
        return mq;
    }
    function env() {
        const q = coarseQuery();
        return { coarse: !!(q && q.matches), width: root.innerWidth, override: override() };
    }
    function wantsNow() { return want(env()); }
    const body = () => root.document.body;
    const $ = (sel) => root.document.querySelector(sel);

    function setMode(m) {
        mode = m;
        body().classList.toggle('m-list', m === 'list');
        body().classList.toggle('m-session', m === 'session');
    }
    // Paints the view's classes as early as possible (before the tree and the
    // prefs load), so a phone never flashes the desktop drawer layout.
    // init() still performs the actual switch.
    function early() {
        if (!wantsNow()) return;
        body().classList.add('mobile-view');
        setMode('list');
    }

    function paintTop(node) {
        const name = $('.m-topbar .m-name');
        if (name) name.textContent = node ? node.label : '';
        const ic = $('.m-topbar .state-icon');
        if (ic && node && ctx) {
            const cls = ctx.circleClass(node);
            ic.className = 'circle state-icon ' + cls;
            const t = (ctx.circleTitle && ctx.circleTitle[cls]) || cls;
            ic.setAttribute('aria-label', t);
            ic.title = t;
            ic.textContent = cls === 'error' ? '⚠' : '';
        }
    }
    function refitSoon() {
        const raf = root.requestAnimationFrame || ((f) => setTimeout(f, 16));
        raf(() => { if (ctx) ctx.refit(); });
    }

    function showList() {
        if (!on) return;
        setMode('list');
    }
    // `wantKind`: open the pane on that tab (the "…" menu's Settings).
    function showSession(node, wantKind) {
        if (!on || !ctx || !node) return;
        // the classes first: the pane is visible (and sized) BEFORE a terminal
        // fits or a web frame loads into it
        setMode('session');
        lastSessionId = node.instance_id;
        const cur = ctx.currentNode();
        if (cur && cur.instance_id === node.instance_id) {
            // the same agent: never re-open (the xterm and its WS stay alive)
            if (wantKind) ctx.showKind(wantKind);
            else ctx.reveal();
        } else {
            ctx.openAgent(node, wantKind);
        }
        paintTop(ctx.currentNode() || node);
        refitSoon();
    }
    // The tree poll, a swipe, a deletion: keep the top bar true, and leave a
    // session whose agent is gone.
    function update() {
        if (!on || !ctx || mode !== 'session') return;
        const n = ctx.currentNode();
        if (!n) { showList(); return; }
        lastSessionId = n.instance_id;
        paintTop(n);
    }

    function apply() {
        if (!ctx) return;
        const w = wantsNow();
        if (w === on) return;
        on = w;
        if (w) {
            const prevSession = mode === 'session' && lastSessionId ? ctx.findNode(lastSessionId) : null;
            body().classList.add('mobile-view');
            setMode('list');
            ctx.enter();                                  // one slot, nothing live
            if (prevSession) showSession(prevSession);
        } else {
            // the grid is visible again BEFORE the desktop panes re-open, so
            // their terminals fit and their web frames load sized
            body().classList.remove('mobile-view', 'm-list', 'm-session', 'm-term');
            ctx.leave();
        }
    }
    function applySoon() {
        clearTimeout(timer);
        timer = setTimeout(apply, 150);
    }

    function setOverride(v) {
        lsSet(KEY_MODE, v === 'mobile' || v === 'desktop' ? v : null);
        apply();
    }

    function setFontSize(n) {
        n = clampFont(n);
        lsSet(KEY_FONT, n);
        const sl = ctx && ctx.activeSlot();
        if (sl && sl.term) {
            sl.term.options.fontSize = n;
            sl.refit();
            if (sl.ws && sl.ws.readyState === 1)
                sl.ws.send(JSON.stringify({ type: 'resize', cols: sl.term.cols, rows: sl.term.rows }));
        }
        return n;
    }

    // Soft keyboard: keep the key bar AND the terminal's bottom rows above
    // it. Where the layout viewport does not shrink (iOS Safari, Android
    // Chrome >= 108 without interactive-widget), the pane's padding grows by
    // --kb-inset (portal.css) and the terminal refits to the smaller pane,
    // so the line being typed stays on screen (fix round 1).
    let kbTimer = null;
    function trackKeyboard() {
        const vv = root.visualViewport;
        if (!vv) return;
        const set = () => {
            body().style.setProperty('--kb-inset',
                Math.max(0, root.innerHeight - vv.height - vv.offsetTop) + 'px');
            clearTimeout(kbTimer);
            kbTimer = setTimeout(() => { if (on && ctx) ctx.refit(); }, 100);
        };
        vv.addEventListener('resize', set);
        vv.addEventListener('scroll', set);
        set();
    }

    function wireChrome() {
        const back = $('.m-topbar .m-back');
        if (back) back.addEventListener('click', () => showList());
        const down = $('.m-topbar .m-font-down');
        if (down) down.addEventListener('click', () => setFontSize(fontSize() - 1));
        const up = $('.m-topbar .m-font-up');
        if (up) up.addEventListener('click', () => setFontSize(fontSize() + 1));
        const more = $('.m-topbar .m-more');
        if (more) more.addEventListener('click', () => {
            const n = ctx.currentNode();
            if (n) ctx.openMenu(n, more);
        });
        const lmore = $('.m-listbar .m-list-more');
        if (lmore) lmore.addEventListener('click', () => {
            root.AmUi.menu.open({ label: 'Agents menu', anchor: lmore, items: ctx.listMenuItems() });
        });
    }

    function init(c) {
        ctx = c;
        on = false;
        wireChrome();
        trackKeyboard();
        const q = coarseQuery();
        if (q) {
            if (q.addEventListener) q.addEventListener('change', applySoon);
            else if (q.addListener) q.addListener(applySoon);
        }
        root.addEventListener('resize', applySoon);
        apply();
    }

    return {
        want, wantsNow, early, init, isOn: () => on, showList, showSession, update,
        setOverride, override, fontSize, setFontSize,
        mode: () => (on ? mode : null),
    };
});
