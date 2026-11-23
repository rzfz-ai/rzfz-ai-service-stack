# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2476 — per-agent, owner-scoped OpenBao tokens (decision record D8/D9).

Token role rzfz-agent: policy rzfz-user only, entity alias = the owner's OIDC
sub (so the templated path secret/users/<oidc-sub>/* applies, the same path
the owner sees after an OIDC login), orphan, 4 h period. The agent reads it
from /run/rzfz/bao-token, an owner-only tmpfs (C11).

The token reaches the container on the stdin of a `docker exec`, run as the
image's own uid (the file must be readable by the agent and nobody else, and
a sandboxed container's root has no capability to write into a directory it
does not own). It is never in the container environment (Docker persists
that in config.v2.json), never on argv, never in an image.

Nothing is minted until sub == X-Authentik-Uid is proven on the box
(OPENBAO_SUB_IS_UID, A19's gate), and an agent launched in technical mode
never gets its owner's personal token (D7/D8).
"""
from __future__ import annotations

import logging
import os
import re
import socket
import time

log = logging.getLogger(__name__)

TOKEN_PATH = "/run/rzfz/bao-token"
TOKEN_ROLE = "rzfz-agent"
#: Where the agent manager's svc-agent-manager AppRole credentials are mounted
#: (the openbao-agent-cred volume, read-only).
CRED_DIR = "/run/rzfz-bao"
#: An OIDC sub as Authentik issues it (sub_mode hashed_user_id: 64 lowercase
#: hex characters); anything else (a username, a slug) is refused.
_SUB = re.compile(r"^[0-9a-f]{64}$")
_TMPFS_CHECK = ["sh", "-c", "grep -q ' /run/rzfz tmpfs ' /proc/mounts"]
_WRITE = ["sh", "-c", f"umask 077; cat > {TOKEN_PATH}.new && mv -f {TOKEN_PATH}.new {TOKEN_PATH}"]


class OpenBaoError(RuntimeError):
    pass


def is_technical(instance: dict) -> bool:
    """B16/C12: an instance launched as a technical user. Read from both the
    launch config and the persisted instance config; either one marks it."""
    for key in ("user_config", "instance_config"):
        cfg = instance.get(key) or {}
        if isinstance(cfg, dict) and (cfg.get("identity_mode") == "technical" or cfg.get("technical_user")):
            return True
    return False


class OpenBaoTokenBroker:
    """Mint, deliver and revoke an agent's owner-scoped token."""

    def __init__(self, bao, docker, sub_is_uid=False, uid_for=None):
        if uid_for is None:
            from app.services.catalog import SECRETS_TMPFS_UID as uid_for
        self._bao, self._docker = bao, docker
        self._sub_is_uid = bool(sub_is_uid)
        self._uid_for = dict(uid_for)

    def enabled(self) -> bool:
        """A delivery can succeed at all here (the gate, the credentials)."""
        return self._sub_is_uid and getattr(self._bao, "ready", lambda: True)()

    def deliver(self, instance: dict, owner_sub, user_active: bool):
        """The token's accessor, or None when no token may (or can) be given."""
        name = instance["container_name"]
        if not self._sub_is_uid:
            log.warning("openbao: sub == X-Authentik-Uid not proven here (OPENBAO_SUB_IS_UID) "
                        "- no token for %s", name)
            return None
        if is_technical(instance):
            log.info("openbao: %s runs as a technical user - no personal token", name)
            return None
        if not isinstance(owner_sub, str) or not _SUB.match(owner_sub):
            log.warning("openbao: %s has no usable owner sub - no token", name)
            return None
        if not user_active:
            log.info("openbao: %s's owner is not known to be active - no token", name)
            return None
        if not getattr(self._bao, "ready", lambda: True)():
            log.info("openbao: no agent manager credentials yet (identity reconcile) - no token for %s", name)
            return None
        uid = self._uid_for.get(instance.get("agent_type"))
        if uid is None:
            log.warning("openbao: %s is an image the stack does not build (no /run/rzfz) - no token", name)
            return None
        if self._docker.exec_run(name, _TMPFS_CHECK, user=str(uid)) != 0:
            log.warning("openbao: %s has no /run/rzfz tmpfs - refusing to write a token to disk", name)
            return None
        tok = self._bao.create_token(TOKEN_ROLE, owner_sub, {"agent_instance": str(instance["id"])})
        rc = self._docker.exec_run(name, _WRITE, stdin=tok["client_token"].encode(), user=str(uid))
        if rc != 0:
            # a token nobody holds is revoked at once, not left to expire
            log.warning("openbao: writing the token into %s failed (exit %s) - revoked", name, rc)
            self.revoke(tok["accessor"])
            return None
        return tok["accessor"]

    def revoke(self, accessor) -> None:
        if accessor:
            self._bao.revoke_accessor(accessor)


class OpenBaoHTTP:
    """The agent manager's own OpenBao client: AppRole svc-agent-manager,
    which may only create rzfz-agent tokens and revoke by accessor."""

    def __init__(self, addr: str, cred_dir: str = CRED_DIR, http=None, timeout: float = 10.0):
        import httpx
        self._addr = addr.rstrip("/")
        self._cred_dir = cred_dir
        self._http = http or httpx
        self._timeout = timeout
        self._token, self._expires = None, 0.0

    def ready(self) -> bool:
        """The identity reconcile has minted this manager's AppRole."""
        return os.path.isfile(os.path.join(self._cred_dir, "secret_id"))

    def _read(self, name: str) -> str:
        with open(os.path.join(self._cred_dir, name), encoding="utf-8") as f:
            v = f.read().strip()
        if not v:
            raise OpenBaoError(f"{name} is empty")
        return v

    def _call(self, path: str, body: dict, token: str | None) -> dict:
        headers = {"X-Vault-Token": token} if token else {}
        r = self._http.post(f"{self._addr}/v1/{path}", json=body, headers=headers, timeout=self._timeout)
        if not 200 <= r.status_code < 300:
            # the body names the error; it never carries a token on a failure
            raise OpenBaoError(f"POST {path}: HTTP {r.status_code}")
        return r.json() if r.status_code != 204 else {}

    def _login(self) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        auth = self._call("auth/approle/login", {"role_id": self._read("role_id"),
                                                 "secret_id": self._read("secret_id")}, None).get("auth") or {}
        if not auth.get("client_token"):
            raise OpenBaoError("AppRole login returned no token")
        # renew a minute early; the role's token_ttl is 15 minutes
        self._token = auth["client_token"]
        self._expires = time.monotonic() + max(int(auth.get("lease_duration") or 0) - 60, 0)
        return self._token

    def create_token(self, role: str, entity_alias: str, meta: dict) -> dict:
        auth = self._call(f"auth/token/create/{role}", {"entity_alias": entity_alias, "meta": meta},
                          self._login()).get("auth") or {}
        if not auth.get("client_token") or not auth.get("accessor"):
            raise OpenBaoError("token create returned no token")
        return {"client_token": auth["client_token"], "accessor": auth["accessor"]}

    def revoke_accessor(self, accessor: str) -> None:
        self._call("auth/token/revoke-accessor", {"accessor": accessor}, self._login())


class DockerExec:
    """`docker exec -i` through the Docker API: stdin on the attached socket,
    the exit code from exec_inspect. Never puts the payload on argv."""

    def __init__(self, docker_api, timeout: float = 15.0):
        self._api = docker_api      # docker.APIClient (AgentDockerClient._client.api)
        self._timeout = timeout

    def exec_run(self, container: str, cmd: list, stdin: bytes | None = None, user: str | None = None) -> int:
        try:
            eid = self._api.exec_create(container, cmd, stdin=stdin is not None, stdout=True,
                                        stderr=True, user=user or "")["Id"]
            sock = self._api.exec_start(eid, socket=True)
            raw = getattr(sock, "_sock", sock)
            raw.settimeout(self._timeout)
            try:
                if stdin is not None:
                    raw.sendall(stdin)
                raw.shutdown(socket.SHUT_WR)
                while raw.recv(4096):
                    pass
            finally:
                sock.close()
            deadline = time.monotonic() + self._timeout
            while True:
                info = self._api.exec_inspect(eid)
                if not info.get("Running"):
                    code = info.get("ExitCode")
                    return code if isinstance(code, int) else -1
                if time.monotonic() > deadline:
                    return -1
                time.sleep(0.1)
        except Exception as e:  # noqa: BLE001 — a failed exec delivers nothing
            log.warning("openbao: exec in %s failed: %s", container, type(e).__name__)
            return -1


def build_broker(env=None, docker_client=None):
    """The broker create_app wires in, or None without an OpenBao address.
    The AppRole credentials are read at each login, so a box whose identity
    reconcile mints them after the manager started needs no restart; until
    then every delivery fails closed (no token)."""
    env = os.environ if env is None else env
    addr = (env.get("OPENBAO_ADDR") or "").strip()
    cred = env.get("OPENBAO_AGENT_CRED_DIR") or CRED_DIR
    if not addr:
        return None
    api = docker_client._client.api
    return OpenBaoTokenBroker(OpenBaoHTTP(addr, cred), DockerExec(api),
                              sub_is_uid=str(env.get("OPENBAO_SUB_IS_UID", "")).strip().lower() == "true")
