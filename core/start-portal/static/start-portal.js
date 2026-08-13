/* M028 — start portal client-side behaviour:
 *   1. Pin/unpin via the heart button (S08).
 *   2. Drag-to-rearrange within each section, ACROSS sections (S11.6),
 *      and entire-category reorder (S11.6).
 *   3. Live-agent readiness polling (S06 reuses rc6.7 #91 pattern).
 *   4. Category management — add new, rename, delete-if-empty (S11.6).
 */

(function () {
    'use strict';

    // ── Reset layout (S11.6) ──────────────────────────────────────────
    var resetBtn = document.querySelector('.portal-reset');
    if (resetBtn) {
        resetBtn.addEventListener('click', function () {
            if (!confirm('Reset all pins, categories, and tile order to defaults?')) return;
            fetch('/api/prefs/reset', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' }
            }).then(function (r) {
                if (!r.ok) throw new Error('reset api ' + r.status);
                window.location.reload();
            }).catch(function () {
                alert('Reset failed — please try again.');
            });
        });
    }

    // ── Pin / unpin ────────────────────────────────────────────────────
    document.querySelectorAll('button[data-pin-tile]').forEach(function (btn) {
        btn.addEventListener('click', function (event) {
            event.preventDefault();
            event.stopPropagation();
            var tileId = btn.dataset.pinTile;
            var isPinned = btn.classList.contains('portal-tile-pinned');
            var newState = !isPinned;
            btn.classList.toggle('portal-tile-pinned', newState);
            fetch('/api/prefs/pin/' + encodeURIComponent(tileId), {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pinned: newState })
            }).then(function (r) {
                if (!r.ok) throw new Error('pin api ' + r.status);
                return r.json();
            }).then(function (data) {
                if (!data.ok) throw new Error('pin api ok=false');
                setTimeout(function () { window.location.reload(); }, 300);
            }).catch(function () {
                btn.classList.toggle('portal-tile-pinned', isPinned);
            });
        });
    });

    // ── Drag-to-rearrange (S11.6 — cross-category) ────────────────────
    if (typeof Sortable === 'function' || typeof Sortable === 'object') {

        // Per-tile-grid sortable. group:'tiles' means tiles can move
        // between any of these grids, not just within their own.
        // Favorites stays its own group ('favorites') — drag-into-favorites
        // is the pin button (intentional friction).
        document.querySelectorAll('.portal-grid').forEach(function (grid) {
            var groupName = grid.dataset.sortGroup === 'favorites' ? 'favorites' : 'tiles';
            new Sortable(grid, {
                animation: 150,
                handle: '.portal-tile',
                filter: '.portal-tile-pin',
                preventOnFilter: false,
                group: { name: groupName, pull: true, put: groupName === 'tiles' },
                onEnd: function (event) {
                    var section = event.to.closest('.portal-section');
                    var newCategory = section ? section.dataset.sectionId : null;
                    var sourceSection = event.from.closest('.portal-section');
                    var oldCategory = sourceSection ? sourceSection.dataset.sectionId : null;
                    var movedTile = event.item.dataset.tileId;

                    // Cross-category drop — persist the move.
                    if (newCategory && oldCategory && newCategory !== oldCategory
                        && newCategory !== '__favorites__'
                        && oldCategory !== '__favorites__') {
                        fetch('/api/prefs/move', {
                            method: 'POST',
                            credentials: 'same-origin',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                tile_id: movedTile,
                                category: newCategory
                            })
                        }).catch(function () { /* silent — re-renders on next load */ });
                    }

                    // Persist in-section order (same call regardless of cross/within).
                    var orders = {};
                    event.to.querySelectorAll('[data-tile-id]').forEach(function (el, idx) {
                        orders[el.dataset.tileId] = (idx + 1) * 10;
                    });
                    fetch('/api/prefs/order', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ orders: orders })
                    }).catch(function () { });
                }
            });
        });

        // S11.6 #14 — drag whole category headers to reorder. Favorites
        // is outside the .portal-categories container so it stays pinned.
        // group:'sections' isolates section drags from tile drags so a
        // section can never get accepted as a child of a .portal-grid
        // (which would nest the category and trap it).
        var catRoot = document.querySelector('.portal-categories');
        if (catRoot) {
            new Sortable(catRoot, {
                animation: 150,
                handle: '.portal-section-title',
                draggable: '.portal-section',
                group: { name: 'sections', pull: false, put: false },
                onEnd: function () {
                    var orders = {};
                    catRoot.querySelectorAll('.portal-section').forEach(function (s, idx) {
                        var name = s.dataset.sectionId;
                        if (name && name !== '__favorites__') {
                            orders[name] = (idx + 1) * 10;
                        }
                    });
                    fetch('/api/categories/order', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ orders: orders })
                    }).catch(function () { });
                }
            });
        }
    }

    // ── Live-agent readiness polling (rc6.7 #91 reuse) ────────────────
    document.querySelectorAll('.portal-tile-pending[data-instance-id]').forEach(function (tile) {
        var instanceId = tile.dataset.instanceId;
        var attempts = 0;
        function tick() {
            fetch('/api/ready/' + encodeURIComponent(instanceId), {
                credentials: 'same-origin'
            }).then(function (r) { return r.ok ? r.json() : { ready: false }; })
              .then(function (data) {
                  if (data.ready) {
                      tile.classList.remove('portal-tile-pending');
                  } else if (++attempts < 90) {
                      setTimeout(tick, 2000);
                  } else {
                      tile.classList.remove('portal-tile-pending');
                  }
              })
              .catch(function () {
                  if (++attempts < 90) setTimeout(tick, 2000);
              });
        }
        tick();
    });

    // ── S11.6 category management — rename / delete / add ─────────────
    // Rename: dblclick the section title, becomes contenteditable.
    document.querySelectorAll('.portal-section[data-section-id] .portal-section-title').forEach(function (title) {
        title.addEventListener('dblclick', function (event) {
            event.preventDefault();
            var section = title.closest('.portal-section');
            var oldName = section.dataset.sectionId;
            if (oldName === '__favorites__') return;  // Favorites is immutable
            var oldDisplay = title.textContent.trim();
            var input = document.createElement('input');
            input.type = 'text';
            input.value = oldDisplay;
            input.className = 'portal-cat-rename-input';
            title.replaceWith(input);
            input.focus();
            input.select();
            function finish(commit) {
                var v = input.value.trim();
                var newTitle = document.createElement('h2');
                newTitle.className = 'portal-section-title';
                newTitle.textContent = commit && v ? v : oldDisplay;
                input.replaceWith(newTitle);
                // Re-bind dblclick on the new element.
                newTitle.addEventListener('dblclick', arguments.callee.caller);
                if (commit && v && v !== oldDisplay) {
                    fetch('/api/categories/' + encodeURIComponent(oldName), {
                        method: 'PATCH',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name: v })
                    }).then(function (r) { return r.json(); })
                      .then(function (data) {
                          if (data.ok) {
                              section.dataset.sectionId = v;
                          }
                      })
                      .catch(function () { });
                }
            }
            input.addEventListener('blur', function () { finish(true); });
            input.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
                if (e.key === 'Escape') { e.preventDefault(); finish(false); }
            });
        });
    });

    // Delete-if-empty button — appears on hover for empty categories.
    document.querySelectorAll('.portal-section[data-section-id]').forEach(function (section) {
        var grid = section.querySelector('.portal-grid');
        var name = section.dataset.sectionId;
        if (name === '__favorites__') return;
        if (!grid || grid.children.length > 0) return;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'portal-cat-delete';
        btn.title = 'Delete empty category';
        btn.textContent = '×';
        btn.addEventListener('click', function () {
            if (!confirm('Delete category "' + name + '"?')) return;
            fetch('/api/categories/' + encodeURIComponent(name), {
                method: 'DELETE',
                credentials: 'same-origin'
            }).then(function (r) { return r.json(); })
              .then(function (data) {
                  if (data.ok) section.remove();
              });
        });
        section.querySelector('.portal-section-title').appendChild(btn);
    });

    // Add-category button at the bottom of .portal-categories.
    var catRoot = document.querySelector('.portal-categories');
    if (catRoot) {
        var addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'portal-cat-add';
        addBtn.textContent = '+ New category';
        addBtn.addEventListener('click', function () {
            var name = prompt('Category name:');
            if (!name) return;
            fetch('/api/categories', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name.trim() })
            }).then(function (r) { return r.json(); })
              .then(function (data) {
                  if (data.ok) window.location.reload();
              });
        });
        catRoot.parentNode.appendChild(addBtn);
    }
})();
