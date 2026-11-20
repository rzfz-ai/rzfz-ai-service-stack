// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* 2026.09-ga.4 (#2517 §1; operator decision 2026-09-26: "By type" is gone).
 * The portal tree is ONE tree of the user's real instances, grouped into the
 * user's folders plus an "Unfiled" group. This module is the pure folder
 * model: it normalises the stored prefs (including every ga.3 shape), builds
 * the folder/instance tree, performs folder operations and yields the visible
 * row order for keyboard navigation. No DOM; every function returns a NEW
 * object and never mutates its input. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AmTree = api;
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';
    const MAX = 64;
    const UNFILED = 'Unfiled';
    const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
    // Folder names are user text, so they are never looked up on a plain {}:
    // "constructor", "toString" or "__proto__" would resolve to (or be
    // swallowed by) Object.prototype (2026.09-ga.4 U3 fix round 1). Folder
    // maps are prototype-less; JSON.stringify still writes them as plain
    // JSON objects.
    const dict = () => Object.create(null);
    const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
    const clone = (p) => {
        const folders = dict();
        for (const k of Object.keys(p.folders)) folders[k] = p.folders[k].slice();
        return { folders, collapsed: p.collapsed.slice(), unfiledCollapsed: p.unfiledCollapsed };
    };
    const fold = (s) => String(s).trim().toLocaleLowerCase();
    const isReserved = (s) => fold(s) === 'unfiled';

    function normalizePrefs(raw) {
        const r = (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : {};
        const src = (r.folders && typeof r.folders === 'object' && !Array.isArray(r.folders)) ? r.folders : {};
        const folders = dict();
        const byFold = dict();
        const seen = new Set();
        for (const key of Object.keys(src)) {
            const ids = has(src, key) ? src[key] : undefined;
            if (!Array.isArray(ids)) continue;
            const name = String(key).trim().slice(0, MAX);
            if (!name || isReserved(name)) continue;
            // Two keys that only differ by case/whitespace are one folder.
            const target = has(byFold, fold(name)) ? byFold[fold(name)] : name;
            byFold[fold(name)] = target;
            if (!has(folders, target)) folders[target] = [];
            for (const id of ids) {
                if (typeof id !== 'string' || !id || seen.has(id)) continue;
                seen.add(id);
                folders[target].push(id);
            }
        }
        const collapsed = Array.isArray(r.collapsed)
            ? r.collapsed.filter((c, i, a) => typeof c === 'string' && has(folders, c) && a.indexOf(c) === i)
            : [];
        return { folders, collapsed, unfiledCollapsed: r.unfiledCollapsed === true };
    }

    function build(nodes, prefs) {
        const p = normalizePrefs(prefs);
        const list = Array.isArray(nodes) ? nodes.filter((n) => n && n.instance_id) : [];
        const byId = new Map(list.map((n) => [n.instance_id, n]));
        const byLabel = (a, b) => coll.compare(String(a.label || ''), String(b.label || ''));
        const filed = new Set();
        const out = Object.keys(p.folders).sort(coll.compare).map((name) => {
            const children = [];
            for (const id of p.folders[name]) {
                const n = byId.get(id);
                if (n && !filed.has(id)) { filed.add(id); children.push(n); }
            }
            children.sort(byLabel);
            return { kind: 'folder', key: 'f:' + name, name, count: children.length,
                     collapsed: p.collapsed.includes(name), children };
        });
        if (list.length > 0) {
            const children = list.filter((n) => !filed.has(n.instance_id)).sort(byLabel);
            out.push({ kind: 'unfiled', key: 'unfiled', name: UNFILED, count: children.length,
                       collapsed: p.unfiledCollapsed, children });
        }
        return out;
    }

    function visibleRows(model) {
        const rows = [];
        for (const g of model || []) {
            rows.push({ key: g.key, level: 1, kind: g.kind });
            if (g.collapsed) continue;
            for (const c of g.children) {
                rows.push({ key: 'i:' + c.instance_id, level: 2, kind: 'instance',
                            instance_id: c.instance_id, parent: g.key });
            }
        }
        return rows;
    }

    function validateFolderName(name, prefs, except) {
        const v = String(name == null ? '' : name).trim();
        if (!v) return 'Give the folder a name.';
        if (isReserved(v)) return '"Unfiled" is reserved.';
        if (v.length > MAX) return 'At most 64 characters.';
        const ex = except == null ? null : fold(except);
        const clash = Object.keys((prefs && prefs.folders) || {})
            .some((k) => fold(k) === fold(v) && fold(k) !== ex);
        return clash ? 'A folder with that name exists.' : null;
    }

    function createFolder(prefs, name) {
        const p = clone(normalizePrefs(prefs));
        const v = String(name).trim();
        if (validateFolderName(v, p)) return p;
        p.folders[v] = [];
        return p;
    }

    function renameFolder(prefs, oldName, newName) {
        const p = clone(normalizePrefs(prefs));
        const v = String(newName).trim();
        if (!has(p.folders, oldName)) return p;
        if (validateFolderName(v, p, oldName)) return p;
        const folders = dict();
        for (const k of Object.keys(p.folders)) folders[k === oldName ? v : k] = p.folders[k];
        p.folders = folders;
        p.collapsed = p.collapsed.map((c) => (c === oldName ? v : c));
        return p;
    }

    function deleteFolder(prefs, name) {
        const p = clone(normalizePrefs(prefs));
        delete p.folders[name];
        p.collapsed = p.collapsed.filter((c) => c !== name);
        return p;
    }

    function moveTo(prefs, iid, folder) {
        const p = clone(normalizePrefs(prefs));
        for (const k of Object.keys(p.folders)) p.folders[k] = p.folders[k].filter((x) => x !== iid);
        if (folder != null && has(p.folders, folder)) p.folders[folder].push(iid);
        return p;
    }

    function prune(prefs, liveIds) {
        const p = clone(normalizePrefs(prefs));
        const live = new Set(liveIds || []);
        for (const k of Object.keys(p.folders)) p.folders[k] = p.folders[k].filter((x) => live.has(x));
        return p;
    }

    function folderOf(prefs, iid) {
        const f = (prefs && prefs.folders) || {};
        for (const k of Object.keys(f)) if (has(f, k) && Array.isArray(f[k]) && f[k].includes(iid)) return k;
        return null;
    }

    function toggle(prefs, key) {
        const p = clone(normalizePrefs(prefs));
        if (key === 'unfiled') { p.unfiledCollapsed = !p.unfiledCollapsed; return p; }
        if (typeof key !== 'string' || key.indexOf('f:') !== 0) return p;
        const name = key.slice(2);
        if (!has(p.folders, name)) return p;
        p.collapsed = p.collapsed.includes(name) ? p.collapsed.filter((c) => c !== name) : p.collapsed.concat([name]);
        return p;
    }

    return { normalizePrefs, build, visibleRows, validateFolderName, createFolder, renameFolder,
             deleteFolder, moveTo, prune, folderOf, toggle, UNFILED, MAX };
});
