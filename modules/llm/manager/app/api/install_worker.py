# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#1059 P2.1 — the master serves its own worker-install script.

``GET /install-worker`` renders ``scripts/install-worker.sh.tmpl`` with THIS
master's coordinates: its URL, its CA fingerprint (#585), the agent image and
the registry a worker should pull from. The point of rendering per request
rather than shipping a static file is that the script can never be stale or
belong to a different master — the operator copies one line and the box it runs
on learns everything else from the answer.

WHY THE ROUTE IS UNAUTHENTICATED — AND WHY IT MINTS NO TOKEN
-----------------------------------------------------------
A blank box has no Authentik session and cannot acquire one until it is
enrolled, so the FETCH has to be open; the same reasoning already puts
``/api/workers/enroll`` and ``/api/workers/ca.pem`` on the open side of the
Caddy ingress. What makes that safe is that the rendered script carries **no
secret**: a master URL is not one, and a CA fingerprint is public by
construction (the joining node authenticates the CA it fetches by hashing it).

The plan's P2.1 text reads "…and a freshly-minted enrol token". Implemented
literally that would make this unauthenticated route an open credential
dispenser: anyone who can reach the master could `curl /install-worker`, read a
valid enrolment token out of the body and join the fleet — which is precisely
the property #340 (TTL, single-use, revocable) exists to bound. So the token is
minted where it already is, behind SUPERADMIN in
``POST /api/workers/enroll-token``, and travels in the one-liner the console
shows (``…| bash -s -- --token <minted>``) rather than in the script body.
``build_install_command`` below is what that endpoint uses, so there is still
exactly ONE minting path.

``test_1059_install_worker_endpoint.py`` asserts the "no secret in the body"
half of this mechanically.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response

from app.ca_fingerprint import DEFAULT_CA_PEM, read_ca_fingerprint
from app.config import get_settings

#: Where the rendered template lives inside the manager container. Bind-mounted
#: read-only from the repo's `scripts/` (modules/llm/manager/compose.yml). The
#: in-repo path is the fallback so the renderer is exercisable off-box.
TEMPLATE_ENV = "LLM_MANAGER_INSTALL_TEMPLATE"
TEMPLATE_DEFAULT = "/srv/install-worker.sh.tmpl"
def _repo_template() -> Optional[Path]:
    """Off-box fallback ONLY (a repo checkout, where this file sits at
    modules/llm/manager/app/api/ — five levels below the root, so the root is
    parents[5]; parents[4] is `modules/` and never held the template). Inside
    the IMAGE this file is /app/app/api/… — parents[5] does not exist there
    and the bind-mounted TEMPLATE_DEFAULT is the real source. Resolving
    eagerly at import crashed the whole manager on every freshly built image
    (IndexError before uvicorn could serve) — resolve lazily and defensively
    instead.
    """
    try:
        return (Path(__file__).resolve().parents[5]
                / "scripts" / "install-worker.sh.tmpl")
    except IndexError:
        return None

#: The agent image a worker runs, and the registry it pulls from (#559/#571).
#: Overridable so an operator can repin without a code change, exactly like the
#: RAZZFAZZ_ENGINE_IMAGE_* knobs.
AGENT_IMAGE_ENV = "LLM_MANAGER_WORKER_AGENT_IMAGE"
AGENT_IMAGE_DEFAULT = "razzfazz-llm-worker-agent:latest"
REGISTRY_ENV = "LLM_MANAGER_WORKER_REGISTRY"

_PIN_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Every placeholder the template declares. Rendering is required to leave none
#: behind: an unsubstituted `@@…@@` is treated as UNSET by the script, so a
#: forgotten one degrades silently into "master not configured".
PLACEHOLDERS = (
    "RZFZ_MASTER_URL",
    "RZFZ_CA_PIN",
    "RZFZ_AGENT_IMAGE",
    "RZFZ_REGISTRY",
    "RZFZ_STACK_VERSION",
)


def template_path() -> Path:
    p = Path(os.environ.get(TEMPLATE_ENV) or TEMPLATE_DEFAULT)
    if p.is_file():
        return p
    repo = _repo_template()
    # In-image with no bind mount AND no repo checkout: report the path the
    # deployment SHOULD have mounted, so read_template's 503 names the fix.
    return repo if repo is not None else Path(TEMPLATE_DEFAULT)


def read_template() -> str:
    path = template_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - surfaced as a 503 below
        raise FileNotFoundError(str(path)) from exc


def render_install_script(
    template: str,
    *,
    master_url: str,
    ca_fingerprint: Optional[str] = None,
    agent_image: str = AGENT_IMAGE_DEFAULT,
    registry: str = "",
    stack_version: str = "",
) -> str:
    """Substitute this master's coordinates into the template.

    A malformed pin RAISES rather than being emitted: shipping one would pin
    every worker to a value it can never match, producing un-joinable boxes with
    a confusing error. Same rule as ``build_join_command`` (#419).
    """
    if ca_fingerprint and not _PIN_RE.match(ca_fingerprint):
        raise ValueError(
            f"refusing to render a malformed CA pin: {ca_fingerprint!r} "
            f"(expected 'sha256:<64 lowercase hex>')")
    values = {
        "RZFZ_MASTER_URL": (master_url or "").rstrip("/"),
        "RZFZ_CA_PIN": ca_fingerprint or "",
        "RZFZ_AGENT_IMAGE": agent_image or "",
        "RZFZ_REGISTRY": registry or "",
        "RZFZ_STACK_VERSION": stack_version or "",
    }
    out = template
    for name, value in values.items():
        out = out.replace(f"@@{name}@@", value)
    left = re.findall(r"@@[A-Z0-9_]+@@", out)
    if left:
        raise ValueError(
            "the template declares placeholders this renderer does not know: "
            + ", ".join(sorted(set(left))))
    return out


#: The console line's CA scratch file on the blank box; the fingerprint formula
#: is the template's own (`install-worker.sh.tmpl` verify step: DER → sha256 →
#: hex), so the console and the script judge the SAME bytes the SAME way.
CONSOLE_CA_FILE = "/tmp/rzfz-ca.pem"
CONSOLE_CA_ROUTE = "/api/workers/ca.pem"
CONSOLE_FINGERPRINT = (
    "sha256:$(openssl x509 -in " + CONSOLE_CA_FILE + " -outform DER 2>/dev/null"
    " | openssl dgst -sha256 -hex 2>/dev/null | awk '{print $NF}')")


def build_install_command(
    *,
    master_url: str,
    token: str,
    name: str = "",
    hardware: str = "",
    ca_fingerprint: str = "",
) -> str:
    """The one copyable line an operator runs on the blank box.

    The token travels HERE — in the command the admin copies out of the SSO'd
    console — and never in the script body served to anyone who asks.

    `ca_fingerprint` (#1421): a master that issues its own CA (TLS_MODE=internal
    — every test box and every appliance before the certificate step) renders
    a pin, and a BLANK box does not trust that CA: the plain line died at the
    first byte with `curl: (60) SSL certificate problem` (0.175 → 0.91,
    2026-09-05). The fix is NOT `-k` on the script fetch (rev-A, refused in
    review): the script carries the pin, so an attacker on the path would hand
    over script AND pin together and the "verification afterwards" would check
    his CA against his pin. The pin has to travel out of band — and this line
    IS the out-of-band channel (SSO-protected console). So the line fetches
    the CA unverified, checks it against the pin from the console, and only
    then fetches the script over that CA (--cacert) and passes the pin on as
    --ca-pin for the script's own re-check. A master behind a public CA renders
    no pin and keeps the strict one-liner.
    """
    base = master_url or "<master-url>"
    tail = f"bash -s -- --token {token}"
    if ca_fingerprint:
        tail += f" --ca-pin {ca_fingerprint}"
    if name:
        tail += f" --name {name}"
    if hardware:
        tail += f" --hardware {hardware}"
    if not ca_fingerprint:
        return f"curl -fsSL {base}/install-worker | {tail}"
    return (
        f"curl -fsSk {base}{CONSOLE_CA_ROUTE} -o {CONSOLE_CA_FILE} \\\n"
        f"  && [ \"{CONSOLE_FINGERPRINT}\" = \"{ca_fingerprint}\" ] \\\n"
        f"  && curl -fsSL --cacert {CONSOLE_CA_FILE} {base}/install-worker | {tail}"
    )


def register_install_worker_api(app) -> None:
    # No dependencies: see the module docstring. Open, and carrying no secret.
    public = APIRouter()

    @public.get("/install-worker")
    def install_worker() -> Response:
        settings = get_settings()
        ca_fp = read_ca_fingerprint(
            os.environ.get("LLM_MANAGER_CA_PEM", DEFAULT_CA_PEM))
        try:
            template = read_template()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503,
                detail=(f"install template not available on this master "
                        f"({exc}); fetch /install-worker.sh instead and pass "
                        f"--master explicitly"))
        try:
            body = render_install_script(
                template,
                master_url=settings.advertise_url or "",
                ca_fingerprint=ca_fp,
                agent_image=(os.environ.get(AGENT_IMAGE_ENV)
                             or AGENT_IMAGE_DEFAULT),
                registry=os.environ.get(REGISTRY_ENV, ""),
                stack_version=os.environ.get("RAZZFAZZ_VERSION", ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return Response(content=body, media_type="text/x-shellscript")

    app.include_router(public)
