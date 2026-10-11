// #959 Task C1 — spawn modal open/close/type-select/submit.
//
// Replaces the OLD `openLaunchDialog`/`closeLaunchDialog`/`confirmLaunch`
// inline functions that used to live in dashboard/index.html. This is a
// PRESENTATION swap only: the server contract is unchanged — a launch still
// POSTs to `/api/launch/<agent_type>` (via the existing `agentAction`
// helper defined in index.html) with an optional `{mem_gb}` body, and the
// SERVER still enforces the tier/memory gate regardless of what the client
// sends. `window._canSetMemory` is set by components/spawn_modal.html from
// the same `can_set_memory` context dashboard.py computes.

let _spawnType = null;
// AGU-9: the element that opened the modal, so focus can be handed back on
// close instead of being dropped to <body>.
let _spawnOpener = null;

function _spawnModalEl() {
    return document.getElementById('spawn-modal');
}

function _spawnIsOpen() {
    const m = _spawnModalEl();
    return !!m && m.style.display !== 'none';
}

// AGU-10: "Launch agent" is the modal's primary button. Opened unscoped (the
// launch band's "+ New agent…" passes no type) `_spawnType` is null and
// confirmSpawn() returns immediately — so the button looked primary, was fully
// enabled, and did nothing at all until the user happened to click a type tile.
// The portal's own inline launcher already does this (`btn.disabled =
// !this.pendingType`); mirror it here.
// #2517 §3 (2026.09-ga.4): the button also needs a name — every agent has one.
function _spawnNameValue() {
    const input = document.getElementById('spawn-name');
    return input ? input.value.trim() : '';
}

function _syncSpawnConfirm() {
    const btn = document.getElementById('launch-confirm-btn');
    if (btn) btn.disabled = !_spawnType || !_spawnNameValue();
    const hint = document.getElementById('spawn-type-hint');
    if (hint) {
        hint.hidden = !!_spawnType && !!_spawnNameValue();
        hint.textContent = _spawnType ? 'Give the agent a name.' : 'Pick an agent type first.';
    }
}

function _spawnNameError(text) {
    const err = document.getElementById('spawn-name-error');
    if (!err) return;
    err.textContent = text || '';
    err.hidden = !text;
}

// The suggestion the server computed for a type (a free name for this user):
// from the type tile, else from the page's per-type #spawn-suggestions map,
// else the display name (the server answers 409 with the next free one).
function _spawnSuggestion(agentType, displayName) {
    if (!agentType) return '';
    const sel = '.type-opt[data-launch-type="' + (window.CSS && CSS.escape ? CSS.escape(agentType) : agentType) + '"]';
    const opt = document.querySelector(sel);
    if (opt && opt.dataset.suggestedName) return opt.dataset.suggestedName;
    // Fix round 1: types the user already runs have no tile; the page carries
    // a suggestion for every type in #spawn-suggestions.
    try {
        const el = document.getElementById('spawn-suggestions');
        const all = el ? JSON.parse(el.textContent || '{}') : {};
        if (all && typeof all[agentType] === 'string' && all[agentType]) return all[agentType];
    } catch (e) { /* malformed data: fall back to the display name */ }
    return displayName || '';
}
let _spawnPrefill = '';

// Opens the modal already scoped to one agent type — this is what every
// existing per-card "Launch" button (data-launch-type) now calls instead of
// the old openLaunchDialog(agentType, displayName).
function openSpawnModal(agentType, displayName) {
    _spawnOpener = document.activeElement;
    selectSpawnType(agentType, displayName);
    const modal = _spawnModalEl();
    if (!modal) return;

    // #1865: a SCOPED launch shows no chooser.
    //
    // Operator report: "Launch Coding agent" and "Launch Bring your own agent"
    // presented a grid of Hermes / Moltis / OpenHands / Paperclip — none of
    // them the agent in the title. The grid renders every card with
    // `can_launch`, and a scoped type whose own tile is not in that set (an
    // instance already runs, or the type is user-defined) leaves the grid
    // showing OTHER agents with nothing selected, under a title naming this
    // one. The launch itself was fine; the content contradicted the heading.
    //
    // The type is already chosen when the modal is opened from an agent's own
    // button, so the block goes away entirely — label included. The unscoped
    // "+ New agent…" flow is the one case that asks the question, and it keeps
    // the grid.
    const typeBlock = document.getElementById('spawn-type-block');
    if (typeBlock) typeBlock.hidden = !!agentType;

    modal.style.display = 'flex';
    // AGU-9: move focus INTO the dialog. It is marked aria-modal="true", which
    // tells assistive tech everything outside it does not exist — leaving focus
    // on the button behind the scrim strands AT users in content their reader
    // has been told to ignore.
    //
    // #1865: and never onto something that is now hidden — a scoped launch
    // starts at the first control it actually has.
    const first = (agentType ? null
                   : (modal.querySelector('#spawn-type-grid .type-opt.on')
                      || modal.querySelector('#spawn-type-grid .type-opt')))
        // #2517: the name is the first control a scoped launch has.
        || modal.querySelector('#spawn-name')
        || modal.querySelector('#launch-mem-gb')
        || modal.querySelector('.mh .x');
    if (first) first.focus();
}

// Also used to switch the highlighted tile inside the type grid without
// closing the modal, so a user who opened it from one card can still pick
// a different launchable type before confirming.
function selectSpawnType(agentType, displayName) {
    _spawnType = agentType || null;
    // #2517: prefill the name with this type's suggestion unless the user
    // has typed one of their own (empty, or still the previous suggestion).
    const nameInput = document.getElementById('spawn-name');
    if (nameInput && (!nameInput.value.trim() || nameInput.value === _spawnPrefill)) {
        _spawnPrefill = _spawnSuggestion(agentType, displayName);
        nameInput.value = _spawnPrefill;
    }
    _spawnNameError('');
    const title = document.getElementById('spawn-modal-title');
    if (title) title.textContent = displayName ? ('Launch ' + displayName) : 'Launch an agent';
    document.querySelectorAll('#spawn-type-grid .type-opt').forEach(function (el) {
        const on = !!agentType && el.dataset.launchType === agentType;
        el.classList.toggle('on', on);
        // AGU-20(a): these are native <button>s, not listbox options — say
        // "pressed", which is the role-correct selected state for a button.
        el.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    _syncSpawnConfirm();
}

function closeSpawnModal() {
    const modal = _spawnModalEl();
    if (modal) modal.style.display = 'none';
    _spawnType = null;
    const nameInput = document.getElementById('spawn-name');
    if (nameInput) nameInput.value = '';
    _spawnPrefill = '';
    _spawnNameError('');
    _syncSpawnConfirm();
    // AGU-9: hand focus back to whatever opened the modal.
    const opener = _spawnOpener;
    _spawnOpener = null;
    if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
}

// #2517 §3 (2026.09-ga.4): the modal is the "New agent" surface, so it always
// CREATES (`new_instance: true`) and sends the required name. It posts itself
// instead of going through agentAction(): a refused name must keep the modal
// open with the server's message and its suggestion in the field, not end in
// a native alert() after the modal is gone.
function confirmSpawn() {
    if (!_spawnType) return;
    const name = _spawnNameValue();
    if (!name) { _spawnNameError('Give the agent a name.'); _syncSpawnConfirm(); return; }
    const body = { custom_name: name, new_instance: true };
    // Only power/admin ever see the picker; a regular user's launch carries
    // no mem_gb and the server provisions the default (the tier gate is
    // server-side, so a hand-crafted body from a regular user is ignored too).
    if (window._canSetMemory) {
        const sel = document.getElementById('launch-mem-gb');
        if (sel) body.mem_gb = parseInt(sel.value, 10);
    }
    const type = _spawnType;
    const btn = document.getElementById('launch-confirm-btn');
    if (btn) btn.disabled = true;
    _spawnNameError('');
    fetch('/api/launch/' + encodeURIComponent(type), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin', body: JSON.stringify(body),
    })
        .then(r => r.json().catch(() => ({})).then(data => ({ ok: r.ok, status: r.status, data: data })))
        .then(({ ok, status, data }) => {
            if (!ok || data.error) {
                _spawnNameError(data.message || data.error || ('HTTP ' + status));
                const input = document.getElementById('spawn-name');
                if ((status === 400 || status === 409) && data.suggested_name && input) {
                    input.value = data.suggested_name;
                    _spawnPrefill = data.suggested_name;
                }
                _syncSpawnConfirm();
                return;
            }
            location.reload();
        })
        .catch(err => { _spawnNameError('Launch failed: ' + err); _syncSpawnConfirm(); });
}

// #2517: typing a name enables/disables the button and clears a stale error.
document.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'spawn-name') { _spawnNameError(''); _syncSpawnConfirm(); }
});

// Escape closes the modal, same affordance the prototype's scrim/Escape
// handler gives. AGU-9: guarded on the modal actually being OPEN — this
// listener is global, and an unguarded closeSpawnModal() on every Escape
// anywhere on the page also cleared _spawnType and stole focus.
// Tab is trapped inside the dialog for the same reason focus is moved in.
document.addEventListener('keydown', function (e) {
    if (!_spawnIsOpen()) return;
    if (e.key === 'Escape') { closeSpawnModal(); return; }
    if (e.key !== 'Tab') return;
    const modal = _spawnModalEl();
    const focusables = Array.prototype.filter.call(
        modal.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'),
        function (el) { return el.offsetParent !== null; });
    if (focusables.length === 0) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// AGU-9: click the scrim to dismiss, matching the portal's own overlays. The
// target check keeps a click INSIDE the modal from closing it.
document.addEventListener('DOMContentLoaded', function () {
    const modal = _spawnModalEl();
    if (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target === modal) closeSpawnModal();
        });
    }
    _syncSpawnConfirm();
});
