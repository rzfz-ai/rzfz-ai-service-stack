// help-center.js — rzfz.ai Help Center shell interactions (#982 Task 5).
//
// Ports the operator-signed-off prototype's JS: rail collapse (« <-> ☰),
// topic click swaps the sub-nav's page list from a server-emitted JSON
// blob (no round trip), page click navigates to its /docs/<id>/ URL, and
// the ONE global topbar search box debounces into /api/search and renders
// a results overlay. Vanilla JS only — no build step, no CDN (air-gap safe).
(function () {
    'use strict';

    function pageUrl(id) {
        return '/docs/' + encodeURIComponent(id) + '/';
    }

    // ------------------------------------------------------------------
    // Rail collapse: icon-only <-> full, like the Agent Manager.
    // ------------------------------------------------------------------
    var rail = document.getElementById('rail');
    var railtog = document.getElementById('railtog');
    if (rail && railtog) {
        railtog.addEventListener('click', function () {
            var collapsed = rail.classList.toggle('collapsed');
            railtog.innerHTML = collapsed ? '&#9776;' : '&laquo;'; // ☰ : «
        });
    }

    // ------------------------------------------------------------------
    // Topic -> sub-nav page swap, driven by the JSON nav blob emitted by
    // base.html. Falls back to a normal navigation-free no-op if the blob
    // is missing/malformed — the server-rendered sub-nav still works.
    // ------------------------------------------------------------------
    var navDataEl = document.getElementById('help-nav-data');
    var NAV = { topics: [], active_topic: null };
    if (navDataEl) {
        try {
            var parsed = JSON.parse(navDataEl.textContent || '{}');
            if (parsed && Array.isArray(parsed.topics)) { NAV = parsed; }
        } catch (e) { /* malformed blob — keep the server-rendered sub-nav as-is */ }
    }

    var topicsEl = document.getElementById('topics');
    var snlist = document.getElementById('snlist');
    var snIc = document.getElementById('sn-ic');
    var snTitle = document.getElementById('sn-title');
    var snCount = document.getElementById('sn-count');
    var crumb = document.getElementById('crumb');

    // HLP-15: the server marks the doc currently open in the sub-nav with
    // `.sn.on` (base.html). renderPages() rebuilds that list client-side on a
    // topic switch and used to emit every entry as a bare `.sn`, so navigating
    // to another topic and back dropped the "you are here" marker while the
    // doc pane on the right still showed that very page. The id is read once
    // from the server-rendered list and re-applied on every re-render.
    var ACTIVE_PAGE = (snlist && snlist.dataset.activePage) || '';

    function renderPages(topic) {
        if (!snlist) { return; }
        snlist.innerHTML = '';
        (topic.pages || []).forEach(function (p) {
            var a = document.createElement('a');
            a.className = (p.id === ACTIVE_PAGE) ? 'sn on' : 'sn';
            a.dataset.pageId = p.id;
            a.href = pageUrl(p.id);
            var label = document.createElement('span');
            label.textContent = p.title;
            a.appendChild(label);
            if (p.is_own === false) {
                var dot = document.createElement('span');
                dot.className = 'dot ' + (p.cached ? 'ok' : 'no');
                a.appendChild(dot);
            }
            snlist.appendChild(a);
        });
    }

    function selectTopic(topic) {
        if (topicsEl) {
            topicsEl.querySelectorAll('.topic').forEach(function (el) {
                var on = el.dataset.topicId === topic.id;
                el.classList.toggle('on', on);
                // HLP-14: the active topic was conveyed by COLOUR ALONE.
                if (on) { el.setAttribute('aria-current', 'page'); }
                else { el.removeAttribute('aria-current'); }
            });
        }
        if (snIc) { snIc.innerHTML = topic.icon || ''; }  // icon is inline SVG, not text
        if (snTitle) { snTitle.textContent = topic.label || ''; }
        if (snCount) { snCount.textContent = (topic.pages || []).length + ' pages'; }
        if (crumb) { crumb.textContent = topic.label || 'Help Center'; }
        renderPages(topic);
    }

    function topicById(id) {
        return NAV.topics.filter(function (t) { return t.id === id; })[0];
    }

    if (topicsEl) {
        topicsEl.querySelectorAll('.topic').forEach(function (a) {
            a.addEventListener('click', function (e) {
                e.preventDefault();
                var topic = topicById(a.dataset.topicId);
                if (!topic) { return; }
                selectTopic(topic);
                // HLP-14: the click never touched the URL, so the two-level
                // nav state was lost on reload and on back/forward. The hash
                // costs nothing and makes the selection linkable.
                if (window.history && window.history.replaceState) {
                    window.history.replaceState(null, '', '#topic=' + encodeURIComponent(topic.id));
                } else {
                    window.location.hash = 'topic=' + encodeURIComponent(topic.id);
                }
            });
        });
    }

    function applyTopicHash() {
        var m = /^#topic=(.+)$/.exec(window.location.hash || '');
        if (!m) { return; }
        var topic = topicById(decodeURIComponent(m[1]));
        if (topic) { selectTopic(topic); }
    }
    applyTopicHash();
    window.addEventListener('hashchange', applyTopicHash);

    // ------------------------------------------------------------------
    // ONE global full-text search (topbar) — debounced fetch to
    // /api/search, rendered into the results overlay. Esc / outside click
    // closes it.
    // ------------------------------------------------------------------
    var q = document.getElementById('q');
    var results = document.getElementById('results');
    var resultsList = document.getElementById('results-list');
    var searchTimer = null;
    var active = -1;   // index of the arrow-key-highlighted result, -1 = none

    function openResults() {
        if (results) { results.classList.add('on'); }
        if (q) { q.setAttribute('aria-expanded', 'true'); }
    }

    function closeResults() {
        if (results) { results.classList.remove('on'); }
        if (q) { q.setAttribute('aria-expanded', 'false'); }
        active = -1;
    }

    // HLP-14: the failure state did not exist — a 500 from /api/search left the
    // PREVIOUS query's results on screen with nothing marking them as stale.
    function renderMessage(text, cls) {
        if (!resultsList) { return; }
        resultsList.innerHTML = '';
        var el = document.createElement('div');
        el.className = cls;
        el.textContent = text;
        resultsList.appendChild(el);
    }

    function renderResults(items) {
        if (!resultsList) { return; }
        active = -1;
        if (!items.length) {
            renderMessage('No matches.', 'rempty');
            return;
        }
        resultsList.innerHTML = '';
        items.forEach(function (r) {
            var a = document.createElement('a');
            a.className = 'res';
            a.setAttribute('role', 'option');
            a.setAttribute('aria-selected', 'false');
            a.href = r.url;

            var title = document.createElement('span');
            title.className = 'rt';
            title.textContent = r.title;
            a.appendChild(title);

            if (r.section) {
                var sec = document.createElement('span');
                sec.className = 'rc';
                sec.textContent = r.section;
                a.appendChild(sec);
            }

            var snippet = document.createElement('div');
            snippet.className = 'rs';
            // r.snippet is server-escaped HTML with <mark> around the
            // matched terms (search_index.py); safe to inject as-is.
            snippet.innerHTML = r.snippet || '';
            a.appendChild(snippet);

            resultsList.appendChild(a);
        });
    }

    function runSearch(value) {
        var term = value.trim();
        if (!term) { closeResults(); return; }
        fetch('/api/search?q=' + encodeURIComponent(term))
            .then(function (r) {
                if (!r.ok) { throw new Error('HTTP ' + r.status); }
                return r.json();
            })
            .then(function (data) {
                renderResults((data && data.results) || []);
                openResults();
            })
            .catch(function () {
                // Never leave a stale result list passing for a fresh one.
                renderMessage('Search is unavailable right now. Try again.', 'rerror');
                openResults();
            });
    }

    // HLP-14: Up/Down/Enter through the results — the overlay was mouse-and-Tab
    // only, so a keyboard user had to tab past every result to leave the box.
    function items() {
        return resultsList ? resultsList.querySelectorAll('.res') : [];
    }

    function highlight(next) {
        var list = items();
        if (!list.length) { return; }
        if (active >= 0 && list[active]) {
            list[active].classList.remove('sel');
            list[active].setAttribute('aria-selected', 'false');
        }
        active = (next + list.length) % list.length;
        list[active].classList.add('sel');
        list[active].setAttribute('aria-selected', 'true');
        if (list[active].scrollIntoView) {
            list[active].scrollIntoView({ block: 'nearest' });
        }
    }

    if (q) {
        q.addEventListener('input', function () {
            var value = q.value;
            clearTimeout(searchTimer);
            if (!value.trim()) { closeResults(); return; }
            searchTimer = setTimeout(function () { runSearch(value); }, 200);
        });
        q.addEventListener('focus', function () {
            if (q.value.trim() && resultsList && resultsList.children.length) {
                openResults();
            }
        });
        q.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown') { e.preventDefault(); highlight(active + 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); highlight(active - 1); }
            else if (e.key === 'Enter') {
                var list = items();
                if (active >= 0 && list[active]) {
                    e.preventDefault();
                    window.location.href = list[active].href;
                }
            }
        });
    }

    document.addEventListener('click', function (e) {
        if (!e.target.closest || !e.target.closest('.tb-search')) { closeResults(); }
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { closeResults(); }
    });
})();
