# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#254 P2 / #262 — worker enrollment (the "add a worker" flow).

An admin mints a short-lived, HMAC-signed enrollment token in the console
(``POST /api/workers/enroll-token``, SSO / require_admin). The operator runs
the stack on the TARGET box in worker-agent mode carrying that token; the node
exchanges it for its long-lived node key (``POST /api/workers/enroll``, gated
by the token itself — NOT admin, since a joining node has no Authentik
session). The manager never SSH-spawns anything: workers self-join.

The token is an HMAC over ``{name, exp, jti}`` with the manager's node key as
the signing secret. Only a party that already holds the node key (the manager)
can mint a valid token; an expired one is refused; and the jti is claimed
ATOMICALLY on exchange (enroll_jti_spent, #340) so a token is SINGLE-USE —
whoever exchanges it second, original holder or thief, is refused. This keeps
the initial join credential short-lived, scoped and unreplayable instead of
pasting the raw long-lived node key into a chat window.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import time
from hashlib import sha256
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.authz import Role, require_role
from app.ca_fingerprint import DEFAULT_CA_PEM
from app.config import get_settings


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hub_credentials_for_node() -> dict:
    """The hub (registry) HTTP-Basic pair a joining node must carry (#1408).

    Read from the manager's own environment — the same values
    `app.api.hub_auth` verifies at the edge, so what the node is handed is by
    construction what the edge accepts. Withheld entirely while no password is
    minted (`rzfz hub-credentials` has not run): the edge is fail-closed then,
    and an empty pair would only overwrite a value an operator set by hand."""
    password = os.environ.get("LLM_HUB_REGISTRY_PASSWORD") or ""
    if not password:
        return {}
    return {"registry_user": os.environ.get("LLM_HUB_REGISTRY_USER") or "fleet",
            "registry_password": password}


def mint_enroll_token(
    name: str, node_key: str, ttl_seconds: int = 3600, *, now: Optional[int] = None
) -> tuple[str, int]:
    """HMAC-signed ``{name, exp}`` → ``'body.sig'``. Signing secret = the node
    key, so only a holder of it (the manager) can mint a valid token. Returns
    (token, exp_epoch). TTL floored at 60s."""
    exp = int(now if now is not None else time.time()) + max(60, int(ttl_seconds))
    # #340: random jti makes the token SINGLE-USE — the exchange claims it in
    # enroll_jti_spent, and a second exchange is refused. 16 random bytes.
    jti = _b64u(os.urandom(16))
    body = _b64u(json.dumps({"n": name, "e": exp, "j": jti},
                            separators=(",", ":")).encode())
    sig = _b64u(hmac.new(node_key.encode(), body.encode(), sha256).digest())
    return f"{body}.{sig}", exp


def decode_verified_token(
    token: str, node_key: str, *, now: Optional[int] = None
) -> Optional[dict]:
    """The verified claims ({n, e, j?}) iff the token is well-formed, correctly
    signed with the node key, and unexpired; else None. Constant-time signature
    check. `j` (jti) is absent on tokens minted before #340's replay guard —
    the enroll route refuses those with a specific message."""
    if not token or not node_key:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        expect = _b64u(hmac.new(node_key.encode(), body.encode(), sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        data = json.loads(_b64u_dec(body))
        if int(data.get("e", 0)) < int(now if now is not None else time.time()):
            return None
        if not str(data.get("n") or "").strip():
            return None
        return data
    except Exception:
        return None


def verify_enroll_token(
    token: str, node_key: str, *, now: Optional[int] = None
) -> Optional[str]:
    """Name-only form of decode_verified_token (kept for existing callers)."""
    data = decode_verified_token(token, node_key, now=now)
    return str(data["n"]).strip() if data else None


#: Hard ceiling on an enrollment token's lifetime (#340).
#:
#: `ttl_seconds` was a bare `int` with no upper bound, so nothing rejected
#: `ttl_seconds=315360000`. That matters more since #454 put
#: `POST /api/workers/enroll` on the Caddy ingress (correctly — a joining node
#: has no session and cannot get one until it is enrolled) and #460 makes
#: `--ca-pin` optional: a node that joins without the pin sends its token to
#: whatever answers the URL. The token is replayable and cannot be revoked, so
#: its TTL is the ONLY thing bounding a leak.
#:
#: A day is generous for an operator pasting a join command and still bounds the
#: blast radius. Replay is separately closed by the jti single-use claim in
#: enroll() (enroll_jti_spent), and per-worker revocation by workers.key_epoch —
#: together these are the substance of #340.
ENROLL_TTL_MAX_SECONDS = 86400
ENROLL_TTL_MIN_SECONDS = 60


class EnrollTokenRequest(BaseModel):
    name: str
    ttl_seconds: int = Field(default=3600, ge=ENROLL_TTL_MIN_SECONDS,
                             le=ENROLL_TTL_MAX_SECONDS)


class EnrollRequest(BaseModel):
    token: str
    name: Optional[str] = None  # node's preferred name; falls back to the token's


_PIN_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def build_join_command(*, manager_url: str, token: str, name: str,
                       ca_fingerprint: Optional[str] = None) -> str:
    """The copy-paste an operator runs on the TARGET box to join it as a worker.

    The CA pin travels IN THIS STRING deliberately. A fingerprint returned only
    as a JSON field is not a security control: nobody passes it by hand, so the
    node's verification step silently never happens. If it is in the command the
    operator pastes, verification is the default and skipping it is the
    deliberate act.

    A malformed pin RAISES rather than being emitted or silently dropped —
    shipping one would pin the node to a value it can never match, producing an
    un-joinable box with a confusing error. No pin at all is a supported state
    (best-effort, see ca_fingerprint) and simply omits the flag.
    """
    if ca_fingerprint is not None and not _PIN_RE.match(ca_fingerprint):
        raise ValueError(
            f"refusing to emit a malformed CA pin: {ca_fingerprint!r} "
            f"(expected 'sha256:<64 lowercase hex>')")
    cmd = (f"rzfz worker-join --manager {manager_url or '<manager-url>'} "
           f"--token {token} --name {name}")
    if ca_fingerprint:
        cmd += f" --ca-pin {ca_fingerprint}"
    return cmd


def register_enroll_api(app) -> None:
    # Gated mint (transits THIS box's Caddy + Authentik). #314: minting a worker
    # enrollment token is worker federation → the SUPER-ADMIN tier.
    admin = APIRouter(dependencies=[Depends(require_role(Role.SUPERADMIN))])

    @admin.post("/api/workers/enroll-token")
    def enroll_token(req: EnrollTokenRequest):
        settings = get_settings()
        if not settings.node_key:
            raise HTTPException(
                status_code=503,
                detail="node registration disabled (no node key configured)",
            )
        name = (req.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="worker name required")
        if ":" in name:
            # #340: ':' separates fields inside the epoch-aware command-key
            # HMAC — a colon name could collide with another worker's rotated
            # key. Refused here and at registration.
            raise HTTPException(status_code=422, detail=(
                "worker names must not contain ':' (reserved by the "
                "command-key derivation, #340)"))
        token, exp = mint_enroll_token(name, settings.node_key, req.ttl_seconds)
        manager_url = settings.advertise_url or ""
        # #419 P0: pin the master's CA so the node VERIFIES us on first contact
        # rather than trusting whoever answers. Best-effort — a box whose Caddy
        # has not issued a root yet still mints a (pin-less) join command.
        from app.ca_fingerprint import read_ca_fingerprint
        ca_fp = read_ca_fingerprint(
            os.environ.get("LLM_MANAGER_CA_PEM", DEFAULT_CA_PEM))
        join = build_join_command(manager_url=manager_url, token=token,
                                  name=name, ca_fingerprint=ca_fp)
        # #1059 P2.2: the checkout-free one-liner for a BLANK box (no repo, no
        # rzfz). Minted HERE, behind SUPERADMIN, and never by the open
        # /install-worker route — so there is still exactly one minting path and
        # the only secret travels in the command the admin copies, not in a
        # script anyone can fetch. `join_command` stays for a box that already
        # has the repo checked out.
        from app.api.install_worker import build_install_command
        install = build_install_command(master_url=manager_url, token=token,
                                        name=name, ca_fingerprint=ca_fp)
        return {
            "worker_name": name,
            "enroll_token": token,
            "expires_at": exp,
            "manager_url": manager_url,
            "ca_fingerprint": ca_fp,
            "join_command": join,
            "install_command": install,
        }

    # Token-gated exchange: the joining node has NO Authentik session — it
    # authenticates with the enrollment token the admin handed it, in-body.
    node = APIRouter()

    @node.get("/api/workers/ca.pem")
    def ca_pem():
        """The master's CA, served UNAUTHENTICATED and in the clear (#419).

        This is what makes `--ca-pin` usable at all. A joining node holds only a
        FINGERPRINT, and you cannot verify a chain against a hash — you need the
        CA's bytes. Caddy presents leaf + intermediate and NOT its root, so the
        pinned root never appears in the TLS handshake; a node that looked for it
        there would reject every legitimate master. (Measured on 0.91: 2 certs
        presented, neither the root.)

        Serving it openly is safe, and is the same shape as kubeadm's
        `--discovery-token-ca-cert-hash`: a CA certificate is public by
        construction, and the node authenticates these bytes by hashing them and
        comparing to the pin it already holds. An attacker who serves their own
        CA fails that comparison; an attacker who replays the REAL CA gains
        nothing, because the node then requires the presented leaf to verify
        against it, which needs the CA's private key.
        """
        path = os.environ.get("LLM_MANAGER_CA_PEM", DEFAULT_CA_PEM)
        try:
            with open(path, "rb") as fh:
                pem = fh.read()
        except OSError:
            # No local CA (e.g. a Let's Encrypt box): there is nothing to pin,
            # and the node verifies against the public PKI instead.
            raise HTTPException(status_code=404, detail="no local CA to pin")
        return Response(content=pem, media_type="application/x-pem-file")

    @node.post("/api/workers/enroll")
    def enroll(req: EnrollRequest):
        settings = get_settings()
        if not settings.node_key:
            raise HTTPException(
                status_code=503,
                detail="node registration disabled (no node key configured)",
            )
        data = decode_verified_token(req.token, settings.node_key)
        if data is None:
            raise HTTPException(
                status_code=401, detail="invalid or expired enrollment token"
            )
        name = str(data["n"]).strip()
        # #340: a caller-supplied name that DISAGREES with the token is either a
        # bug or an attempt — refuse loudly instead of silently ignoring it
        # (the token's name stays authoritative either way, #285).
        if req.name is not None and req.name.strip() and req.name.strip() != name:
            raise HTTPException(status_code=422, detail=(
                f"name {req.name!r} disagrees with the token's enrolled name — "
                f"the token's name is authoritative; omit the field"))
        jti = str(data.get("j") or "").strip()
        if not jti:
            raise HTTPException(status_code=401, detail=(
                "token predates replay protection (#340) — mint a new "
                "enrollment token"))
        # Atomic single-use claim: first INSERT wins, a replay finds the row.
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from app.db import session_scope
        from app.models import EnrollJtiSpent
        import datetime as _dt

        with session_scope() as s:
            got = s.execute(
                pg_insert(EnrollJtiSpent)
                .values(jti=jti, worker_name=name)
                .on_conflict_do_nothing(index_elements=["jti"])
            )
            if not got.rowcount:
                raise HTTPException(status_code=401, detail=(
                    "enrollment token already used — tokens are single-use; "
                    "mint a new one"))
            # Bounded table: anything older than the TTL ceiling belongs to a
            # token that can no longer verify — sweep opportunistically.
            cutoff = (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(seconds=ENROLL_TTL_MAX_SECONDS))
            s.query(EnrollJtiSpent).filter(EnrollJtiSpent.used_at < cutoff).delete()

        from app.api.workers import derive_command_key, _worker_key_epoch

        # #285: the TOKEN's name is authoritative — do NOT let a caller-chosen
        # req.name pick a different worker's credential.
        node_name = name
        resp = {
            "status": "enrolled",
            "node_name": node_name,
            # #207/#285: per-worker key scoped to THIS worker's name. The node
            # uses it (LLM_WORKER_COMMAND_KEY) for BOTH registration and the
            # command channel — its ONLY credential in enforce mode.
            # #340: derived at the worker's CURRENT epoch, so a re-enrollment
            # after rotate-key hands out the key that actually validates.
            "command_key": derive_command_key(settings.node_key, node_name,
                                              _worker_key_epoch(node_name)),
            "manager_url": settings.advertise_url or "",
            "profile": "llm-worker-agent",
        }
        # #1408 rev-B: the hub's docker/blob API is HTTP-Basic at the edge
        # (#571) and the node needs the pair for every REMOTE image/weight
        # pull — and nobody handed it over: LLM_WORKER_REGISTRY_USER/PASSWORD
        # stayed empty on every thin node (0.175 round 4, gb10-191). Same
        # channel as command_key: token-gated, single-use, over the pinned TLS.
        resp.update(hub_credentials_for_node())
        # #285 true isolation: in enforce mode the node must NEVER hold the shared
        # node_key (it could derive every worker's key). Withhold it — the
        # per-worker command_key authorizes registration + commands. In allow
        # mode we still return node_key for back-compat with nodes that use it.
        if settings.command_key_mode != "enforce":
            resp["node_key"] = settings.node_key
        return resp

    app.include_router(admin)
    app.include_router(node)
