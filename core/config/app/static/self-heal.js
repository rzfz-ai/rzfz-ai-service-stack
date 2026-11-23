// The Settings portal repairs its own .env bind by recreating itself: while it
// does, requests fail. Keep asking until the new portal has a verdict, then
// resume, offer the change again, or show the host command. Exposed as functions
// so a test can drive them with a fake document and fetch.
(function (root) {
  function watchSelfHeal(fetchFn, info, handlers, opts) {
    opts = opts || {};
    var interval = opts.interval == null ? 2000 : opts.interval;
    var maxTries = opts.maxTries || 150;
    var wait = opts.wait || function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    return (async function () {
      for (var i = 0; i < maxTries; i++) {
        await wait(interval);
        var st = null;
        try {
          var r = await fetchFn('/api/self-heal', { credentials: 'same-origin' });
          if (r.redirected) { handlers.onOffer('Sign in again, then apply the change again.', null); return 'offer'; }
          if (r.ok) st = await r.json();
        } catch (e) { st = null; }  // the portal is being replaced: ask again
        if (!st || st.heal_id !== info.heal_id || st.status === 'requested') continue;
        // blocked/stale: the bind broke again after this repair, and no new one may start.
        if (st.status === 'failed' || st.status === 'blocked' || st.status === 'stale') {
          handlers.onFailed(st.message); return 'failed';
        }
        if (st.status === 'healed') {
          if (info.resume_expected && st.resumed_action_id) { handlers.onResumed(st.resumed_action_id); return 'resumed'; }
          handlers.onOffer(st.message, st.csrf_token); return 'offer';
        }
      }
      handlers.onFailed(info.fallback || 'The Settings portal did not come back.');
      return 'timeout';
    })();
  }

  // Keeps every later fetch() and form of this page valid in the new portal.
  function adoptCsrf(doc, token) {
    if (!token) return;
    var meta = doc.querySelector('meta[name="csrf-token"]');
    if (meta) meta.setAttribute('content', token);
    var fields = doc.querySelectorAll('input[name="_csrf_token"]');
    for (var i = 0; i < fields.length; i++) fields[i].value = token;
  }

  // The answer to a settings save: the page rendered by the error handler.
  function runPage(doc, fetchFn, opts) {
    var el = doc.getElementById('self-heal-status');
    if (!el) return null;
    var state = el.getAttribute('data-state');
    var again = doc.getElementById('self-heal-again');
    function offer() {
      if (!again) return;
      again.classList.remove('hidden');
      again.addEventListener('submit', function () {
        fetchFn('/api/self-heal/ack', { method: 'POST', credentials: 'same-origin', keepalive: true,
          headers: root.rzfzCsrfHeaders ? root.rzfzCsrfHeaders() : {} });
      }, { once: true });
    }
    if (state !== 'reconnecting') { offer(); return null; }
    var info = { heal_id: el.getAttribute('data-heal-id'), resume_expected: false,
                 fallback: el.getAttribute('data-fallback') };
    return watchSelfHeal(fetchFn, info, {
      onResumed: function () {},
      onOffer: function (msg, token) { el.className = 'flash flash-info'; el.textContent = msg; adoptCsrf(doc, token); offer(); },
      onFailed: function (msg) { el.className = 'flash flash-error'; el.textContent = msg; adoptCsrf(doc, null); offer(); }
    }, opts);
  }

  // Any other caller: one line at the top of the page, with the retry it hands in.
  function showLine(doc, cls, text, label, onClick) {
    var el = doc.getElementById('self-heal-line');
    if (!el) return;
    el.className = 'flash flash-' + cls;
    el.textContent = text + ' ';
    if (label) {
      var b = doc.createElement('button');
      b.type = 'button'; b.className = 'btn sm'; b.textContent = label;
      b.addEventListener('click', onClick);
      el.appendChild(b);
    }
  }
  function followAnywhere(doc, fetchFn, info, retry, opts) {
    if (info.state !== 'reconnecting') { showLine(doc, 'error', info.message); return null; }
    showLine(doc, 'info', info.message);
    return watchSelfHeal(fetchFn, info, {
      onResumed: function () { root.location && root.location.reload(); },
      onOffer: function (msg, token) {
        adoptCsrf(doc, token);
        showLine(doc, 'info', msg, retry ? 'Apply again' : 'Reload', function () {
          fetchFn('/api/self-heal/ack', { method: 'POST', credentials: 'same-origin',
            headers: root.rzfzCsrfHeaders ? root.rzfzCsrfHeaders() : {} });
          if (retry) { doc.getElementById('self-heal-line').className = 'hidden'; retry(); } else { root.location.reload(); }
        });
      },
      onFailed: function (msg) { showLine(doc, 'error', msg); }
    }, opts);
  }

  function bindBanner(doc, fetchFn) {
    var b = doc.getElementById('self-heal-now');
    if (!b) return;
    b.addEventListener('click', function () {
      b.disabled = true;
      fetchFn('/api/self-heal', { method: 'POST', credentials: 'same-origin',
        headers: root.rzfzCsrfHeaders ? root.rzfzCsrfHeaders() : {} })
        .then(function (r) { return r.json(); })
        .then(function (resp) {
          if (resp.self_heal.state === 'healthy') { root.location.reload(); return; }
          followAnywhere(doc, fetchFn, resp.self_heal, null);
        });
    });
  }

  root.rzfzSelfHeal = { watch: watchSelfHeal, adoptCsrf: adoptCsrf, runPage: runPage, follow: followAnywhere };
  if (typeof document !== 'undefined' && typeof fetch !== 'undefined') {
    var f = fetch.bind(root);
    runPage(document, f);
    bindBanner(document, f);
  }
})(typeof window !== 'undefined' ? window : globalThis);
