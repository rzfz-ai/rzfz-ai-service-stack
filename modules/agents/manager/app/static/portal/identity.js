// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
/* #2554: the launch body's identity keys, decided in one pure function so the
 * spawn modal and its test agree by construction. The server re-checks all of
 * it (api._authorize_identity, then the provisioner); this only decides what
 * the form sends. A technical choice without a user throws: the form refuses
 * to post rather than launching as the person instead. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.AmIdentity = api;
})(typeof self !== 'undefined' ? self : this, function () {
    'use strict';
    function launchBody(base, opts) {
        const o = opts || {};
        const body = Object.assign({}, base);
        if (!o.canLaunchTechnical || !o.sandboxed || o.mode !== 'technical') return body;
        if (!o.technicalUser) throw new Error('choose a technical user');
        body.identity_mode = 'technical';
        body.technical_user = String(o.technicalUser);
        return body;
    }
    return { launchBody };
});
