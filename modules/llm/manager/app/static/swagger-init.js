// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
// #1195 — boots the self-hosted Swagger UI on /docs.
//
// Lives in its OWN same-origin file on purpose: the LLM Manager domain's CSP is
// `script-src 'self'` with no 'unsafe-inline' (#347), so FastAPI's stock inline
// <script> initializer is blocked exactly like its CDN assets. The spec URL is
// handed over as a data attribute on the mount node (app/docs.py) rather than
// templated into JS, so this file stays static and cacheable.
(function () {
  "use strict";
  var mount = document.getElementById("swagger-ui");
  if (!mount || typeof window.SwaggerUIBundle !== "function") { return; }
  var openapiUrl = mount.dataset.openapiUrl || "/openapi.json";
  window.ui = window.SwaggerUIBundle({
    url: openapiUrl,
    dom_id: "#swagger-ui",
    deepLinking: true,
    layout: "BaseLayout",
    presets: [window.SwaggerUIBundle.presets.apis],
    // Swagger UI's DEFAULT validatorUrl is swagger.io's public validator: the
    // page would build an <img> pointing there with the spec URL — i.e. this
    // customer's manager hostname — in the query string. Today only the CSP
    // (img-src 'self' data:) stops that request; null turns the feature off
    // at the source so the page makes NO egress even on a box with a looser
    // policy (#1195 rev-B finding 2; pinned by test_1195_docs_selfhosted.py).
    validatorUrl: null,
    // The console's XHRs reach /api/* with the Authentik session cookie; keep
    // "Try it out" on the same footing so it can exercise SSO-gated routes.
    withCredentials: true,
    showExtensions: true,
    showCommonExtensions: true
  });
})();
