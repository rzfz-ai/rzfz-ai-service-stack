#!/bin/sh
# Audited against: docling-serve v1.38.2 (tests/unit/docling/test_2837_no_external_viewer.py
# ties this line to the image tag in compose.yml, so an image bump forces a re-audit).
#
# docling-serve's web UI (static bundle in docling_serve/ui_static, since 1.36)
# has an "Open in viewer" button for DocLang (.dclx) results. A click opens
# https://doclang.ai/viewer/ in the browser and posts the converted document to
# it. The address is a BUILD-time constant of the upstream bundle
# (VITE_DCLX_VIEWER_URL); the server has no setting to turn it off. This runs
# before the server starts, rewrites every doclang.ai reference in the served
# bundle to a harmless one (the button then opens an empty page and reports that
# the viewer did not answer) and refuses to start if one survives. Idempotent.
set -eu

dir=$(python3 -c 'import os, docling_serve; print(os.path.join(os.path.dirname(docling_serve.__file__), "ui_static"))')
if [ -d "$dir" ]; then
    for f in $(grep -rlF 'doclang.ai' "$dir" 2>/dev/null || true); do
        sed -i -e 's#https://doclang\.ai/viewer/#about:blank#g' -e 's#doclang\.ai#invalid.invalid#g' "$f"
    done
    if grep -rqF 'doclang.ai' "$dir" 2>/dev/null; then
        echo "strip-external-viewer: a doclang.ai reference survived in $dir - refusing to start" >&2
        exit 1
    fi
    echo "strip-external-viewer: no external viewer address in the served UI"
fi
exec "$@"
