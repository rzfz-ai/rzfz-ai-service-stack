// SPDX-License-Identifier: Apache-2.0
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
//
// #983 licenses redesign — Task 4: rail collapse, jump-to-section, live
// filter, and the in-site license side pane.
//
// Air-gap safe: the only network call this file makes is `fetch()` against
// the same-origin `/static/licenses/*.txt` files already shipped by
// download_licenses.py — no CDN, no external host. Rows that only have an
// upstream URL (no locally-cached text) carry `data-upstream` and keep their
// native `target="_blank"` link; this script does nothing for them, so the
// browser just opens the new tab.
(function () {
  'use strict';

  // ---- rail collapse -------------------------------------------------
  var app = document.getElementById('app');
  var rail = document.getElementById('rail');
  var railtog = document.getElementById('railtog');
  var railopen = document.getElementById('railopen');

  if (railtog) {
    railtog.addEventListener('click', function () {
      rail.classList.add('collapsed');
      app.classList.add('railhidden');
    });
  }
  if (railopen) {
    railopen.addEventListener('click', function () {
      rail.classList.remove('collapsed');
      app.classList.remove('railhidden');
    });
  }

  // ---- rail → jump to section -----------------------------------------
  var rails = document.querySelectorAll('.rl[data-j]');
  rails.forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      // A card the live filter has hidden cannot be scrolled to — don't move
      // the selection to a link that visibly does nothing.
      if (a.classList.contains('hidden-by-filter')) return;
      rails.forEach(function (x) { x.classList.remove('on'); });
      a.classList.add('on');
      var target = document.getElementById('sec-' + a.dataset.j);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  // ---- live filter ------------------------------------------------------
  var q = document.getElementById('q');
  var empty = document.getElementById('empty');
  var tiers = document.getElementById('tiers');

  if (q) {
    q.addEventListener('input', function () {
      var v = this.value.trim().toLowerCase();
      var any = false;
      var visibleBySection = {};
      document.querySelectorAll('.card').forEach(function (card) {
        var shown = 0;
        card.querySelectorAll('.row').forEach(function (r) {
          // The haystack is the two DATA attributes only — component name and
          // license id. It used to include `r.textContent`, which carries the
          // chip AND the anchor's literal "view →"/"view ↗", so typing "view"
          // matched every row on the page and typing a license name matched
          // rows the operator was trying to filter OUT by component name.
          var hay = ((r.dataset.nm || '') + ' ' + (r.dataset.lic || '')).toLowerCase();
          var hit = !v || hay.indexOf(v) >= 0;
          r.style.display = hit ? '' : 'none';
          if (hit) shown++;
        });
        card.style.display = shown ? '' : 'none';
        // Counts must describe what is ON SCREEN. While a filter was active
        // the card's `.cnt` and the rail's per-category `.n` kept showing the
        // unfiltered totals, so a card reading "12" could show three rows.
        var cnt = card.querySelector('h2 .cnt');
        if (cnt) {
          if (cnt.dataset.total === undefined) cnt.dataset.total = cnt.textContent.trim();
          cnt.textContent = v ? String(shown) : cnt.dataset.total;
        }
        visibleBySection[(card.id || '').replace(/^sec-/, '')] = shown;
        if (shown) any = true;
      });
      rails.forEach(function (a) {
        var shown = visibleBySection[a.dataset.j];
        var n = a.querySelector('.n');
        if (n) {
          if (n.dataset.total === undefined) n.dataset.total = n.textContent.trim();
          n.textContent = v ? String(shown || 0) : n.dataset.total;
        }
        // A rail link whose card is hidden scrolls nowhere — say so.
        var dead = !!v && !shown;
        a.classList.toggle('hidden-by-filter', dead);
        if (dead) a.setAttribute('aria-disabled', 'true');
        else a.removeAttribute('aria-disabled');
      });
      if (empty) empty.style.display = any ? 'none' : 'block';
      if (tiers) tiers.style.display = v ? 'none' : 'grid';
    });
  }

  // ---- license side pane --------------------------------------------
  var scrim = document.getElementById('scrim');
  var pane = document.getElementById('pane');
  var paneTitle = document.getElementById('pane-title');
  var paneChip = document.getElementById('pane-chip');
  var paneText = document.getElementById('pane-text');
  var paneSrc = document.getElementById('pane-src');
  var paneUp = document.getElementById('pane-up');
  var paneX = document.getElementById('pane-x');

  // HLP-6: focus management for the side pane. It is a modal dialog in every
  // respect except that nothing said so: focus was never moved into it, never
  // trapped, and never restored to the `.view` link that opened it, while its
  // own controls stayed tabbable off-screen (fixed with `inert` + the CSS
  // visibility rules). `lastTrigger` is the element focus returns to.
  var lastTrigger = null;

  function paneFocusables() {
    if (!pane) return [];
    return Array.prototype.slice.call(pane.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )).filter(function (el) { return !el.disabled; });
  }

  function trapTab(e) {
    if (e.key !== 'Tab' || !pane || !pane.classList.contains('on')) return;
    var f = paneFocusables();
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function openPane(row, link) {
    var nameEl = row.querySelector('.nm');
    var chipEl = row.querySelector('.chip');
    var name = nameEl ? nameEl.textContent.trim() : '';
    var chipText = chipEl ? chipEl.textContent.trim() : '';
    var chipClass = chipEl ? chipEl.className : 'chip';

    if (paneTitle) paneTitle.textContent = name;
    if (paneChip) {
      paneChip.className = chipClass;
      paneChip.textContent = chipText;
    }
    if (paneSrc) paneSrc.textContent = link;
    if (paneUp) paneUp.href = link;
    if (paneText) paneText.textContent = 'Loading…';

    if (scrim) scrim.classList.add('on');
    if (pane) {
      pane.classList.add('on');
      pane.setAttribute('aria-hidden', 'false');
      pane.removeAttribute('inert');
      if (paneX) paneX.focus();
    }

    fetch(link)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.text();
      })
      .then(function (text) {
        // Rendered as textContent, never innerHTML — the fetched file is
        // plain-text license text, not markup, and must never be parsed
        // as HTML.
        if (paneText) paneText.textContent = text;
      })
      .catch(function () {
        if (paneText) paneText.textContent = 'Could not load the license text (' + link + ').';
      });
  }

  function closePane() {
    var wasOpen = pane && pane.classList.contains('on');
    if (scrim) scrim.classList.remove('on');
    if (pane) {
      pane.classList.remove('on');
      pane.setAttribute('aria-hidden', 'true');
      pane.setAttribute('inert', '');
    }
    // Return focus where the user left it, not to the top of the document.
    if (wasOpen && lastTrigger && lastTrigger.focus) lastTrigger.focus();
    lastTrigger = null;
  }

  document.querySelectorAll('.row').forEach(function (row) {
    var view = row.querySelector('.view');
    if (!view) return;
    view.addEventListener('click', function (e) {
      // Upstream-only rows have no local text file — let the native
      // target="_blank" link open the upstream URL in a new tab.
      if (row.dataset.upstream) return;
      e.preventDefault();
      lastTrigger = view;
      openPane(row, view.getAttribute('href'));
    });
  });

  if (paneX) paneX.addEventListener('click', closePane);
  if (scrim) scrim.addEventListener('click', closePane);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closePane();
    else trapTab(e);
  });
})();
