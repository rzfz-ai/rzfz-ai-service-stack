// Settings > Authentication: the save only starts the apply (a background
// restart plus the SSO step). Poll its status and say "applied" only when it
// succeeded; show the error otherwise. Exposed as a function so a test can
// drive it with a fake document and fetch.
(function (root) {
  function watchApply(doc, fetchFn, opts) {
    opts = opts || {};
    var el = doc.getElementById('apply-status');
    if (!el) return null;
    var id = el.getAttribute('data-apply-id');
    var interval = opts.interval == null ? 1500 : opts.interval;
    var maxTries = opts.maxTries || 400;
    var wait = opts.wait || function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    function show(cls, text) {
      el.className = 'flash flash-' + cls;
      el.textContent = text;
    }
    return (async function () {
      for (var i = 0; i < maxTries; i++) {
        await wait(interval);
        var st = null;
        try {
          var r = await fetchFn('/api/apply/' + encodeURIComponent(id), { credentials: 'same-origin' });
          if (r.status === 404) { show('error', 'The apply status is no longer available. Check the Logs page.'); return 'unknown'; }
          if (r.ok) st = await r.json();
        } catch (e) { st = null; }  // a restart can drop the portal briefly: ask again
        if (!st || !st.done) continue;
        if (st.success) { show('success', 'Authentication settings applied.'); return 'success'; }
        show('error', 'Not applied: ' + (st.error || 'the apply failed. Check the Logs page.'));
        return 'error';
      }
      show('error', 'Still applying after a long time. Check the Logs page.');
      return 'timeout';
    })();
  }
  root.watchApply = watchApply;
  if (typeof document !== 'undefined' && typeof fetch !== 'undefined') {
    watchApply(document, fetch.bind(root));
  }
})(typeof window !== 'undefined' ? window : globalThis);
