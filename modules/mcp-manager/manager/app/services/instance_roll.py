# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Running per-user MCP instances follow an upgraded image.

Measured on a test box: after `rzfz upgrade` both running cognee-personal
instances still ran the previous cognee-authshim image, so a fix in that
image reached a user only when they relaunched by hand. Nothing in the
upgrade recreated per-user containers.

The manager now compares, at start and every ROLL_INTERVAL, each RUNNING
instance's container image with the current local image of its catalogue
tag and recreates the container from the current one when they differ.
Its state is kept: the instance row (id, route bearer, settings) is not
touched and the container gets the same environment it had, limited to the
keys the launch sets (the catalogue's env_map, the route bearer and the
cognee renewal keys), so nothing the old image baked in overrides the new
one. A proxy holds no data of its own (no volume; /run/mcp is tmpfs).

A cognee instance started before token renewal existed also gets it here,
but only for the account it already holds: the account a launch would pick
now must log in to the SAME cognee user its current token names. Otherwise
(the account would change, or cannot be confirmed) the container is still
moved to the new image with its old token, and the run reports that the
user has to start it again. A company instance gets its dataset scope from
the dataset it was started with.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

ROLL_INTERVAL = 15 * 60
ROLL_FIRST_DELAY = 20
AUDIT_ROLLED = "instance_rolled_to_current_image"
SCOPE_KEY = "RZFZ_COGNEE_DATASET_SCOPE"


def _cfg(row) -> dict:
    import json
    cfg = row.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg or "{}")
        except ValueError:
            cfg = {}
    return dict(cfg) if isinstance(cfg, dict) else {}


def _held_account(prov, row, old_token: str):
    """(login, fresh_token) for the cognee account the instance already holds,
    or (None, reason). Login only: never registers or resets an account."""
    from app.services import cognee_identity
    held = str(cognee_identity.token_claims(old_token).get("sub") or "")
    if not held:
        return None, "its current cognee token names no account"
    owner = str(row.get("owner_sub") or "")
    slug = str(row.get("user_slug") or "")
    candidates = []
    if owner:
        candidates.append(prov.cognee_login(owner, slug, legacy_slug=None))
    if not owner.startswith("~"):
        # the slug account, under the same decided-account rules as the window
        candidates.append(prov.cognee_login(owner, slug, legacy_slug=slug))
    base = os.environ.get("COGNEE_BASE_URL", "http://cognee:8000")
    for email, key, err in candidates:
        if err or not email:
            continue
        tok = cognee_identity._login(base, email, prov._hmac_password(key))
        if tok and str(cognee_identity.token_claims(tok).get("sub") or "") == held:
            return {"email": email, "key": key}, tok
    return None, "the account it holds is not the one a start would use now"


def roll_instances(app) -> dict:
    """One pass. Returns {"rolled": [...], "current": n, "relaunch": [...],
    "skipped": [...]} (container names, never a secret)."""
    from app.services.provisioner import (_BACKEND_NETWORKS, RENEW_ENV_INSTANCE,
                                          RENEW_ENV_SECRET, RENEW_ENV_URL, _instance_lock,
                                          _row_lock_key)
    db, docker, catalog, prov = app.db, app.docker_client, app.catalog, app.provisioner
    out = {"rolled": [], "current": 0, "relaunch": [], "skipped": []}
    images: dict = {}
    for row in db.get_all_instances():
        integ = catalog.get(row.get("mcp_id")) or {}
        name = row.get("container_name") or ""
        if not integ.get("image") or integ.get("runtime") == "gateway" or not name:
            continue
        ref = integ["image"]
        if ref not in images:
            images[ref] = docker.image_id(ref)
        want = images[ref]
        if not want:
            out["skipped"].append((name, "the current image is not on this box"))
            continue
        with _instance_lock(_row_lock_key(row), row["mcp_id"]):
            fresh = db.get_instance(row["id"])
            if not fresh or fresh.get("state") != "running":
                continue
            info = docker.inspect_instance(name)
            if info is None:
                out["skipped"].append((name, "its container is gone"))
                continue
            # #3453: box settings and the owner ({{policy:…}}, {{user_id}}) are
            # rendered NOW, never carried over: an old container has none (its
            # proxy would read the open defaults whatever the admin set), and a
            # setting changed since its start must reach it — a restriction
            # (read-only, send off) is not left to the person's next restart
            # (review 282). A container whose rendered settings differ from
            # its environment is rolled even on the current image.
            current = {k: v for k, v in (integ.get("env_map") or {}).items()
                       if "{{policy:" in str(v) or "{{user_id}}" in str(v)}
            rendered = (prov._render_env({"env_map": current}, fresh.get("user_slug") or "",
                                         "", {}, {}, user_id=str(fresh.get("owner_sub") or ""))
                        if current else {})
            settings_changed = any(info["env"].get(k) != v for k, v in rendered.items())
            if info["image_id"] == want and not settings_changed:
                out["current"] += 1
                continue
            keep = set((integ.get("env_map") or {}).keys()) | {
                "PROXY_AUTH_TOKEN", "PROXY_GATEWAY_TOKEN", RENEW_ENV_URL, RENEW_ENV_SECRET,
                RENEW_ENV_INSTANCE}
            env = {k: v for k, v in info["env"].items() if k in keep}
            if not env.get("PROXY_AUTH_TOKEN"):
                out["skipped"].append((name, "its container has no route bearer"))
                continue
            cfg = _cfg(fresh)
            note = ""
            env.update(rendered)
            from app.services.gateway_credentials import PINNED_PROXY_IMAGES
            if ref in PINNED_PROXY_IMAGES:
                # The pinned proxy: the gateway's own bearer (minted for an
                # instance started before it existed) and the image it runs
                # now, which the gateway checks before relaying.
                import secrets as _secrets
                cfg["_gateway_secret"] = cfg.get("_gateway_secret") or _secrets.token_urlsafe(32)
                env["PROXY_GATEWAY_TOKEN"] = cfg["_gateway_secret"]
                cfg["_image"] = ref
                db.set_instance_config(fresh["id"], cfg)
            if integ.get("cognee_autoprovision"):
                if integ.get("cognee_autoprovision") == "company" and SCOPE_KEY in keep \
                        and SCOPE_KEY not in env:
                    env[SCOPE_KEY] = env.get("COGNEE_DEFAULT_DATASET", "")
                if not env.get(RENEW_ENV_SECRET) or not cfg.get("_cognee_login"):
                    login, tok_or_reason = _held_account(prov, fresh, env.get("API_TOKEN", ""))
                    if login:
                        secret = prov._arm_cognee_renewal(cfg, login)
                        env.update(prov.cognee_renewal_env(fresh["id"], secret))
                        for k in ("API_TOKEN", "LLM_API_KEY"):
                            if k in env:
                                env[k] = tok_or_reason
                        db.set_instance_config(fresh["id"], cfg)
                    else:
                        note = tok_or_reason
            docker.remove_container(name)
            labels = {"razzfazz.managed": "true", "razzfazz.mcp.managed": "true",
                      "razzfazz.mcp.id": fresh["mcp_id"],
                      "razzfazz.mcp.user": fresh.get("user_slug") or "",
                      "razzfazz.mcp.instance": str(fresh["id"])}
            try:
                cid = docker.create_container(
                    name=name, image=ref, environment=env,
                    mem_limit=integ.get("mem_limit", "256m"),
                    cpu_limit=float(integ.get("cpu_limit", 0.5)), labels=labels,
                    command=integ.get("command") or None,
                    extra_networks=_BACKEND_NETWORKS.get(integ.get("reach_backend"), []))
                docker.start_container(cid)
            except Exception as e:  # noqa: BLE001
                logger.exception("instance roll: recreating %s failed", name)
                db.update_instance_state(fresh["id"], "error",
                                         error_message=f"upgrade roll failed: {e}"[:500])
                out["skipped"].append((name, f"recreate failed: {type(e).__name__}"))
                continue
            db.update_instance_state(fresh["id"], "running", container_id=cid)
            db.log_audit(fresh.get("owner_sub") or fresh.get("user_slug") or "", AUDIT_ROLLED,
                         fresh["mcp_id"], {"container": name, "relaunch_needed": bool(note)})
            out["rolled"].append(name)
            if note:
                out["relaunch"].append((name, note))
    if out["rolled"] or out["relaunch"] or out["skipped"]:
        logger.info("instance roll: %d moved to the current image, %d current; skipped %s",
                    len(out["rolled"]), out["current"], out["skipped"])
    for name, why in out["relaunch"]:
        logger.warning("instance roll: %s runs the current image, but its memory sign-in "
                       "could not be renewed (%s); its user must start it again in the MCP "
                       "portal", name, why)
    return out


def start_instance_roll(app):
    """The background pass: once shortly after start, then every ROLL_INTERVAL."""
    def run():
        time.sleep(ROLL_FIRST_DELAY)
        while True:
            try:
                roll_instances(app)
            except Exception:  # noqa: BLE001 — next pass retries
                logger.exception("instance roll failed; retried in %ss", ROLL_INTERVAL)
            time.sleep(ROLL_INTERVAL)
    t = threading.Thread(target=run, name="mcp-instance-roll", daemon=True)
    t.start()
    return t
