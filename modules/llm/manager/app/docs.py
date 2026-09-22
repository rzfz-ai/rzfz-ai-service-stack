# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1195 — self-hosted API reference (``/docs``).

FastAPI's stock Swagger page pulls ``swagger-ui.css`` / ``swagger-ui-bundle.js``
from ``cdn.jsdelivr.net`` and boots the UI from an INLINE ``<script>``. The
LLM Manager domain runs behind a CSP of ``script-src 'self'`` with no
``'unsafe-inline'`` (#347), so the browser blocks BOTH and paints a blank page
— which is exactly what the operator saw on 0.91. On an air-gap / corporate-
proxy box the CDN fetch would be dead regardless of CSP.

So the page is ours: every asset is same-origin under ``/docs/static/`` (the
one prefix Caddy already forwards to the manager next to ``/docs`` and
``/openapi.json``), and the boot logic is an external file that reads the spec
URL off a data attribute — no inline JS, no CSP loosening, no egress.

Where the Swagger UI files come from: the manager image's ``swagger-ui`` build
stage (``modules/llm/manager/Dockerfile``) fetches ``swagger-ui-dist`` at an
exact pin from the npm registry at IMAGE BUILD time and copies the served
files into ``app/static/vendor/swagger-ui/``. Nothing binary is committed
(``vendor/`` is gitignored); offline packages ship the built image. A bare
git checkout therefore has ``swagger-init.js`` but NO vendor dir — the app
must still boot (import-side-effect guard, unit tests). What ``/docs`` does
then is the #824 lesson ("no silent degradation"): when the Swagger bundle is
not under the static root, ``/docs`` answers **503 with a plain HTML notice**
that names the missing tree and the rebuild command — NOT the Swagger shell,
which would come back 200 and paint a white page while every asset 404s
(rev-B finding 3). A missing root altogether is skipped with a warning
rather than mounted. ``STATIC_DIR`` is a module attribute read at
``register_docs`` time so tests can point it at a fixture directory.

ReDoc is deliberately NOT re-added: it was another CDN-only page, linked
nowhere in the console, and Caddy never routed ``/redoc`` to the manager in
the first place.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse

#: Static root served at STATIC_URL. Read when register_docs() runs, not at
#: import — monkeypatch it before create_app() to serve a fixture tree.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_URL = "/docs/static"
#: Where the Dockerfile's swagger-ui stage lands the assets, relative to
#: STATIC_DIR / STATIC_URL. The page below and the Dockerfile COPY must agree
#: (pinned by tests/unit/llm-manager/test_1195_docs_selfhosted.py).
VENDOR_SUBDIR = "vendor/swagger-ui"

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — API reference</title>
  <link rel="icon" type="image/png" href="{vendor}/favicon-32x32.png">
  <link rel="stylesheet" href="{vendor}/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui" data-openapi-url="{openapi_url}" data-title="{title}"></div>
  <script src="{vendor}/swagger-ui-bundle.js"></script>
  <script src="{static}/swagger-init.js"></script>
</body>
</html>
"""

#: The file whose absence means "this image was built without the swagger-ui
#: stage" (or the code runs from a bare checkout). The page below is only
#: served when it exists; otherwise _MISSING_PAGE is.
VENDOR_BUNDLE = "swagger-ui-bundle.js"

#: Served INSTEAD of the Swagger shell when the vendor tree is missing. No
#: <script>, no <link>, no inline style: it must render under the manager's
#: CSP and must not itself reference any asset that could 404. Names the tree
#: and the rebuild command — the operator reading it has no other clue.
_MISSING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — API reference assets missing</title>
</head>
<body>
  <h1>API reference assets missing</h1>
  <p>This LLM Manager image has no Swagger UI files under
     <code>app/static/{vendor_subdir}/</code>, so the API reference cannot be
     rendered. The files are fetched at image build (#1195) — this image was
     built without that stage, or the code is running from a bare checkout.</p>
  <p>Rebuild the Manager image: on an installed box run
     <code>rzfz post-install --refresh</code>; on a dev checkout run
     <code>docker compose build llm-manager</code>. See
     <code>modules/llm/manager/Dockerfile</code>.</p>
  <p>The OpenAPI document itself is still served at
     <a href="{openapi_url}">{openapi_url}</a>.</p>
</body>
</html>
"""


def render_docs_page(*, title: str, openapi_url: str) -> str:
    return _PAGE.format(title=title, openapi_url=openapi_url, static=STATIC_URL,
                        vendor=f"{STATIC_URL}/{VENDOR_SUBDIR}")


def render_missing_assets_page(*, title: str, openapi_url: str) -> str:
    return _MISSING_PAGE.format(title=title, openapi_url=openapi_url,
                                vendor_subdir=VENDOR_SUBDIR)


def vendor_bundle_path(static_dir: Path) -> Path:
    return static_dir / VENDOR_SUBDIR / VENDOR_BUNDLE


def register_docs(app) -> None:
    """Mount the static assets and serve the self-hosted Swagger page.

    Import-safe off-box: no network, no DB. The static root is mounted only if
    it exists: Starlette's ``check_dir=False`` merely defers the check to the
    first request and then raises (a 500 on every asset), whereas a missing
    root — a checkout without the built vendor tree, see the module docstring
    — should simply 404 the assets while the app keeps working. ``/docs``
    itself serves the Swagger shell only when the vendored bundle is present;
    otherwise it answers 503 with a plain notice (never a blank page).
    """
    import logging

    from starlette.staticfiles import StaticFiles

    static_dir = STATIC_DIR          # read ONCE, here — see the module docstring
    log = logging.getLogger("orchestrator")

    if static_dir.is_dir():
        app.mount(STATIC_URL, StaticFiles(directory=str(static_dir)), name="docs-static")
    else:
        log.warning(
            "docs: static root %s is missing — /docs answers 503 and its assets 404 "
            "(the Swagger UI files are fetched at image build, #1195)", static_dir)
    if static_dir.is_dir() and not vendor_bundle_path(static_dir).is_file():
        log.warning(
            "docs: %s is not under %s/%s — this image was built without the "
            "swagger-ui stage (or runs from a bare checkout); /docs answers 503 "
            "with a rebuild notice instead of a blank Swagger shell (#1195)",
            VENDOR_BUNDLE, static_dir, VENDOR_SUBDIR)

    @app.get("/docs", include_in_schema=False)
    def swagger_ui() -> HTMLResponse:
        # Checked per request (one stat; /docs is not a hot path) so a vendor
        # tree that appears or vanishes under a running process is reflected
        # without a restart, and so tests can exercise both arms.
        if not vendor_bundle_path(static_dir).is_file():
            return HTMLResponse(
                render_missing_assets_page(title=app.title, openapi_url=app.openapi_url),
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return HTMLResponse(render_docs_page(title=app.title, openapi_url=app.openapi_url))
