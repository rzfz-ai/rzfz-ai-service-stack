// start-portal-rail.js — #1054 Start Portal rail chrome.
//
// Two independent affordances, mirroring the Help Center / Agent Manager
// rail (core/help/static/help-center.js, modules/agents/manager/.../portal.html):
//
//   WIDE <-> NARROW (desktop): the « button at the rail head toggles
//   `.rail.collapsed` (icons only). The glyph flips « <-> ☰ and the choice is
//   remembered in localStorage so the rail comes back the way you left it.
//
//   DRAWER (mobile): the ☰ button in the content topbar slides the rail in
//   over the content and shows a scrim. Escape or a scrim click closes it.
//
// Vanilla JS, no build step, no CDN (the stack ships air-gapped). Every
// storage access is wrapped in try/catch — a browser with site data blocked
// throws on the bare property access, and a rail that cannot remember its
// state must still open and close.
(function () {
    'use strict';

    var STORAGE_KEY = 'rzfz.startPortal.railCollapsed';
    // Keep in sync with the `@media (max-width: 900px)` drawer breakpoint in
    // start-portal-overrides.css.
    var DRAWER_MAX_WIDTH = 900;

    var app = document.getElementById('portal-app');
    var rail = document.getElementById('rail');
    if (!app || !rail) { return; }

    var toggle = document.getElementById('railtog');
    var burger = document.getElementById('rail-burger');
    var scrim = document.getElementById('rail-scrim');

    function readStored() {
        try {
            return window.localStorage.getItem(STORAGE_KEY) === '1';
        } catch (e) {
            return false;
        }
    }

    function store(collapsed) {
        try {
            window.localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
        } catch (e) { /* private mode / site data blocked — state is per-page */ }
    }

    // ── wide <-> narrow ───────────────────────────────────────────────────
    function paintToggle(collapsed) {
        if (!toggle) { return; }
        toggle.innerHTML = collapsed ? '&#9776;' : '&laquo;';   // ☰ : «
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        var label = collapsed ? 'Expand the rail' : 'Collapse the rail';
        toggle.setAttribute('title', label);
        toggle.setAttribute('aria-label', label);
    }

    function setCollapsed(collapsed) {
        rail.classList.toggle('collapsed', collapsed);
        paintToggle(collapsed);
    }

    setCollapsed(readStored());

    if (toggle) {
        toggle.addEventListener('click', function () {
            var collapsed = !rail.classList.contains('collapsed');
            setCollapsed(collapsed);
            store(collapsed);
        });
    }

    // ── mobile drawer ─────────────────────────────────────────────────────
    function setDrawer(open) {
        app.classList.toggle('rail-open', open);
        if (scrim) { scrim.hidden = !open; }
        if (burger) { burger.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    }

    if (burger) {
        burger.addEventListener('click', function () {
            setDrawer(!app.classList.contains('rail-open'));
        });
    }
    if (scrim) {
        scrim.addEventListener('click', function () { setDrawer(false); });
    }
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && app.classList.contains('rail-open')) {
            setDrawer(false);
            if (burger) { burger.focus(); }
        }
    });
    // Tapping a rail destination navigates; closing first avoids the drawer
    // flashing over the new page on a same-page (#anchor) jump.
    rail.addEventListener('click', function (event) {
        if (event.target.closest('a')) { setDrawer(false); }
    });
    // Rotating a phone / resizing past the breakpoint must not leave an
    // orphaned scrim over a desktop-width layout.
    window.addEventListener('resize', function () {
        if (window.innerWidth > DRAWER_MAX_WIDTH) { setDrawer(false); }
    });
}());
