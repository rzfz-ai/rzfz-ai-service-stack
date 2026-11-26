/* #299 — native "My Profile" dialog (progressive enhancement).
 *
 * Portal-local: saves a display name + avatar into THIS portal's own store
 * (/api/portal/profile) so the greeting/avatar chip can be personalized.
 * Does NOT write back to Authentik/SSO identity.
 *
 * Drives, when present:
 *   1. Modal open/close — trigger [data-profile-open], close on × / backdrop
 *      / Esc (mirrors start-portal-pw.js's pw-modal, but its own
 *      [data-profile-*] hooks so the two modals never cross-fire).
 *   2. Avatar upload preview — reads the chosen file client-side via
 *      FileReader and stages it as a data URL; nothing is uploaded until
 *      Save is pressed.
 *   3. AJAX save — POSTs {display_name, avatar?} to /api/portal/profile and
 *      reloads on success so the header reflects the new name/avatar.
 *
 * Degrades gracefully: with this script absent the trigger button simply
 * does nothing (there is no separate full-page fallback for this feature —
 * unlike the #54 password broker, it isn't a security-sensitive action, so
 * a JS-required dialog is an acceptable trade-off here).
 */
(function () {
    'use strict';

    var modal = document.getElementById('profile-modal');
    if (!modal) return;   // not authenticated — nothing to wire up

    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    var nameInput = document.getElementById('profile-display-name');
    var avatarInput = document.getElementById('profile-avatar-input');
    var avatarPreview = document.getElementById('profile-avatar-preview');
    var saveBtn = document.getElementById('profile-save');
    var errorEl = document.getElementById('profile-error');
    var pendingAvatar = null;   // staged data URL; null = leave unchanged on save

    function showError(msg) {
        if (!errorEl) return;
        errorEl.textContent = msg;
        errorEl.hidden = !msg;
    }

    function openModal(ev) {
        if (ev) ev.preventDefault();
        modal.hidden = false;
        document.body.classList.add('pw-modal-open');
        showError('');
        pendingAvatar = null;
        var first = modal.querySelector('input, button');
        if (first) first.focus();
    }
    function closeModal() {
        modal.hidden = true;
        document.body.classList.remove('pw-modal-open');
    }

    document.querySelectorAll('[data-profile-open]').forEach(function (btn) {
        btn.addEventListener('click', openModal);
    });
    modal.querySelectorAll('[data-profile-close]').forEach(function (el) {
        el.addEventListener('click', closeModal);
    });
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !modal.hidden) closeModal();
    });

    // ── Avatar upload preview (client-side only until Save) ───────────────
    // Downscale to a small square avatar (cover-crop to AV_MAX px, re-encode
    // WebP→PNG) BEFORE staging/upload, so any normal photo works and the stored
    // data-URI stays tiny regardless of the source file's size. The server cap
    // is only a backstop for a JS-off / crafted POST.
    var AV_MAX = 256;
    if (avatarInput) {
        avatarInput.addEventListener('change', function () {
            var file = avatarInput.files && avatarInput.files[0];
            if (!file) return;
            if (!/^image\//.test(file.type || '')) { showError('Please choose an image file (PNG, JPEG, GIF or WEBP).'); return; }
            var reader = new FileReader();
            reader.onload = function () {
                var img = new Image();
                img.onload = function () {
                    // centre cover-crop to a square, then scale to AV_MAX
                    var side = Math.min(img.naturalWidth, img.naturalHeight) || AV_MAX;
                    var sx = (img.naturalWidth - side) / 2, sy = (img.naturalHeight - side) / 2;
                    var canvas = document.createElement('canvas');
                    canvas.width = AV_MAX; canvas.height = AV_MAX;
                    var ctx = canvas.getContext('2d');
                    ctx.drawImage(img, sx, sy, side, side, 0, 0, AV_MAX, AV_MAX);
                    var out = null;
                    try { out = canvas.toDataURL('image/webp', 0.85); } catch (e) { out = null; }
                    if (!out || out.indexOf('data:image/webp') !== 0) out = canvas.toDataURL('image/png');
                    pendingAvatar = out;
                    if (avatarPreview) { avatarPreview.src = out; avatarPreview.hidden = false; }
                    showError('');
                };
                img.onerror = function () { showError('That image could not be read — try a PNG or JPEG.'); };
                img.src = reader.result;
            };
            reader.readAsDataURL(file);
        });
    }

    // ── Save ────────────────────────────────────────────────────────────
    if (saveBtn) {
        saveBtn.addEventListener('click', function () {
            var body = { display_name: nameInput ? nameInput.value : '' };
            if (pendingAvatar) body.avatar = pendingAvatar;
            saveBtn.disabled = true;
            showError('');
            fetch('/api/portal/profile', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
                body: JSON.stringify(body)
            }).then(function (r) {
                return r.json().catch(function () { return {}; }).then(function (data) {
                    return { httpOk: r.ok, data: data };
                });
            }).then(function (result) {
                saveBtn.disabled = false;
                if (!result.httpOk || !result.data || result.data.ok !== true) {
                    showError((result.data && result.data.error) || 'Save failed — please try again.');
                    return;
                }
                window.location.reload();
            }).catch(function () {
                saveBtn.disabled = false;
                showError('Save failed — please try again.');
            });
        });
    }
})();
