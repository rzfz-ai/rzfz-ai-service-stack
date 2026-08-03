/* #54 — password broker modal UX (progressive enhancement).
 *
 * Drives, when present:
 *   1. Modal open/close — trigger [data-pw-open], close on × / backdrop / Esc.
 *   2. Live "match" indicator — new vs confirm, compared on every keystroke.
 *   3. Live rules checklist — mirrors password_broker._validate():
 *        - at least <min-len> chars (data-min-len on the form, = 12)
 *        - mixes letters with a digit or symbol
 *      Submit stays disabled until ALL rules pass AND the fields match.
 *   4. AJAX submit — shows the per-target result inline, with a spinner and an
 *      idempotent in-flight guard so a triple-click can't re-fire the fan-out.
 *
 * Degrades gracefully: with this script absent the link navigates to the
 * /password page and the form POSTs normally (the server still validates).
 */
(function () {
    'use strict';

    // ── Modal open/close ──────────────────────────────────────────────
    var modal = document.getElementById('pw-modal');
    var trigger = document.querySelector('[data-pw-open]');

    function openModal(ev) {
        if (!modal) return;            // no modal on this page → let the link navigate
        if (ev) ev.preventDefault();
        modal.hidden = false;
        document.body.classList.add('pw-modal-open');
        var first = modal.querySelector('input, button');
        if (first) first.focus();
    }
    function closeModal() {
        if (!modal) return;
        modal.hidden = true;
        document.body.classList.remove('pw-modal-open');
    }
    if (trigger) trigger.addEventListener('click', openModal);
    if (modal) {
        modal.querySelectorAll('[data-pw-close]').forEach(function (el) {
            el.addEventListener('click', closeModal);
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && !modal.hidden) closeModal();
        });
    }

    // ── Per-form enhancement (works for the modal form AND the /password
    //    fallback page form). ──────────────────────────────────────────
    document.querySelectorAll('form.pw-form').forEach(enhanceForm);

    // ── Eye (show/hide) toggle on every password field (Fix 1, #54) ──────
    // For each [data-pw-reveal] wrapper holding a password <input>, mount a real
    // <button> that flips type password<->text and reflects state via an icon +
    // aria-pressed + aria-label. Real button ⇒ keyboard-operable & focusable;
    // type=button ⇒ never submits the form. Idempotent (skips if already done).
    function mountRevealToggles(root) {
        (root || document).querySelectorAll('[data-pw-reveal]').forEach(function (wrap) {
            if (wrap.querySelector('.pw-reveal')) return;          // already mounted
            var input = wrap.querySelector('input[type="password"], input[type="text"]');
            if (!input) return;
            var btn = document.createElement('button');
            btn.type = 'button';                                   // never submit
            btn.className = 'pw-reveal';
            btn.setAttribute('aria-pressed', 'false');
            btn.setAttribute('aria-label', 'Show password');
            btn.setAttribute('tabindex', '0');
            btn.textContent = '👁';                                 // eye (hidden state)
            btn.addEventListener('click', function () {
                var revealed = input.getAttribute('type') === 'text';
                if (revealed) {
                    input.setAttribute('type', 'password');
                    btn.setAttribute('aria-pressed', 'false');
                    btn.setAttribute('aria-label', 'Show password');
                    btn.textContent = '👁';
                } else {
                    input.setAttribute('type', 'text');
                    btn.setAttribute('aria-pressed', 'true');
                    btn.setAttribute('aria-label', 'Hide password');
                    btn.textContent = '🙈';
                }
            });
            wrap.appendChild(btn);
            wrap.classList.add('pw-input-wrap--has-toggle');
        });
    }
    mountRevealToggles(document);

    function enhanceForm(form) {
        mountRevealToggles(form);
        var newPw = form.querySelector('#pw-new, [name="new_password"]');
        var confirmPw = form.querySelector('#pw-confirm, [name="confirm_password"]');
        var matchEl = form.querySelector('#pw-match, .pw-match');
        var rulesRoot = form.querySelector('#pw-rules, .pw-rules');
        var submitBtn = form.querySelector('#pw-submit, .pw-submit');
        var resultEl = form.querySelector('#pw-result, .pw-result');
        var minLen = parseInt(form.getAttribute('data-min-len'), 10) || 12;
        if (!newPw || !confirmPw || !submitBtn) return;

        var ruleLength = rulesRoot && rulesRoot.querySelector('[data-rule="length"]');
        var ruleComplex = rulesRoot && rulesRoot.querySelector('[data-rule="complexity"]');

        function setRule(li, ok) {
            if (!li) return;
            li.classList.toggle('pw-rule-ok', ok);
            var mark = li.querySelector('.pw-rule-mark');
            if (mark) mark.textContent = ok ? '✓' : '○';
        }

        function evaluate() {
            var pw = newPw.value;
            var confirm = confirmPw.value;
            // Mirror password_broker._validate(): not all-alpha and not
            // all-digit ⇒ at least one letter AND at least one non-letter.
            var hasLetter = /[A-Za-z]/.test(pw);
            var hasNonLetter = /[^A-Za-z]/.test(pw);
            var okLength = pw.length >= minLen;
            var okComplex = pw.length > 0 && hasLetter && hasNonLetter;
            setRule(ruleLength, okLength);
            setRule(ruleComplex, okComplex);

            var matched = pw.length > 0 && pw === confirm;
            if (matchEl) {
                if (!confirm.length && !pw.length) {
                    matchEl.hidden = true;
                } else {
                    matchEl.hidden = false;
                    matchEl.textContent = matched
                        ? '✓ passwords match'
                        : "✗ passwords don't match";
                    matchEl.setAttribute('data-state', matched ? 'match' : 'mismatch');
                }
            }
            // Submit enabled only when every rule passes AND fields match.
            submitBtn.disabled = !(okLength && okComplex && matched);
        }

        newPw.addEventListener('input', evaluate);
        confirmPw.addEventListener('input', evaluate);
        evaluate();  // initial state → button disabled

        // ── AJAX submit + anti-double-submit ───────────────────────────
        var inFlight = false;
        form.addEventListener('submit', function (e) {
            if (inFlight) { e.preventDefault(); return; }
            // Let the server still validate; but enhance with fetch.
            e.preventDefault();
            inFlight = true;
            submitBtn.disabled = true;
            var label = submitBtn.querySelector('.pw-submit-label');
            var spinner = submitBtn.querySelector('.pw-spinner');
            if (label) label.textContent = 'Updating…';
            if (spinner) spinner.hidden = false;

            fetch('/password', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' },
                body: new FormData(form)
            }).then(function (r) {
                return r.json().then(function (data) { return { status: r.status, data: data }; });
            }).then(function (res) {
                renderResult(res.data);
            }).catch(function () {
                renderResult({ ok: false, error: 'Network error — please try again.', results: [] });
            }).finally(function () {
                inFlight = false;
                if (spinner) spinner.hidden = true;
                if (label) label.textContent = 'Update password';
                // Re-evaluate so the button only re-enables if still valid.
                evaluate();
            });
        });

        function renderResult(data) {
            if (!resultEl) return;
            resultEl.hidden = false;
            var html = '';
            if (data.notice) {
                html += '<p class="pw-result-notice ' +
                    (data.ok ? 'pw-ok' : 'pw-warn') + '">' +
                    escapeHtml(data.notice) + '</p>';
            }
            if (data.error) {
                html += '<p class="pw-result-notice pw-warn">' +
                    escapeHtml(data.error) + '</p>';
            }
            if (data.results && data.results.length) {
                html += '<ul class="pw-result-list">';
                data.results.forEach(function (r) {
                    html += '<li class="' + (r.ok ? 'pw-ok' : 'pw-warn') + '">' +
                        (r.ok ? '✓' : '✗') + ' <strong>' + escapeHtml(r.app) +
                        '</strong> — ' + escapeHtml(r.message || '') + '</li>';
                });
                html += '</ul>';
            }
            resultEl.innerHTML = html;
        }

        function escapeHtml(s) {
            return String(s).replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
                         '"': '&quot;', "'": '&#39;' }[c];
            });
        }
    }
})();
