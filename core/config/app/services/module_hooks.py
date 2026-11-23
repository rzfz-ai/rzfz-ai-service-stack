# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2681 — what an ENABLE runs after the module's containers are up, for both
enable paths: the Configuration Portal toggle (apply_manager._execute_toggle)
and `rzfz module enable` (cli/module.sh, via the command line at the bottom).

Before this, the two paths diverged: `rzfz module enable matrix` ran
scripts/lib-matrix.sh::matrix_post_start (synapse-data chown to 991), the
Portal ran nothing, and a box whose synapse-data volume the backup plumbing had
created root-owned crash-looped Synapse on its signing key (measured 2026-10-07).

Two steps, each FAILS the enable rather than warning past it:

  * run_post_start(profile) — the per-module post-start hook (POST_START_HOOKS).
  * close_unbound_apps(profile) — after authentik-init, every Application of
    the profile has at least one enabled PolicyBinding. An Application with
    none is open to every signed-in user (#2090); one found that way is bound
    to Super Admins on the spot (deny-by-default until the binding pass binds
    its group) and the enable fails, naming it.

Pure standard library: the host runs this file with its own python3, the
Portal imports it. Every docker call goes through the `run` callable
(subprocess.run by default), so the Portal's docker-socket-proxy is the only
privilege the Portal path needs: container create/run, exec, volumes — no
build, no host file except the ones its mounts make writable.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time

BINDINGS_SCRIPT = os.path.join("core", "Authentik", "apply-policy-bindings.py")
CADDY_ROOT = "/data/caddy/pki/authorities/local/root.crt"
SYNAPSE_UID = "991:991"
INIT_WAIT_SECONDS = 900


class PostEnableFailed(RuntimeError):
    """An enable step that must not be skipped did not take effect."""


def _out(r):
    return (getattr(r, "stdout", "") or "").strip()


def _call(run, argv, timeout=120, cwd=None, stdin=None):
    """One docker call. A missing docker CLI or a timeout is a failure of the
    step, never a silent skip. `stdin` carries a secret: never argv, never env."""
    kw = {} if stdin is None else {"input": stdin}
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PostEnableFailed(f"`{' '.join(argv[:3])} …` could not run: {e}") from e


# ----------------------------------------------------------------- post-start

def matrix_post_start(ctx):
    """synapse-data owned by 991:991 (Synapse writes its signing key there),
    the Caddy internal root in Synapse's trust store on TLS_MODE=internal,
    Synapse restarted so it starts over on both. Was lib-matrix.sh's
    matrix_post_start; that function now calls this one."""
    run, env, log = ctx["run"], ctx["env"], ctx["log"]
    failures = []
    vol = f"{env.get('COMPOSE_PROJECT_NAME') or 'razzfazz-stack'}_synapse-data"
    if _call(run, ["docker", "volume", "inspect", vol]).returncode == 0:
        # #2544 lesson: an image the box already runs, never alpine(:latest).
        img = _out(_call(run, ["docker", "inspect", "--type", "container",
                               "--format", "{{.Config.Image}}", "postgres"]))
        if not img:
            failures.append(f"cannot set {vol} to {SYNAPSE_UID}: the postgres container "
                            "(whose image does the chown) is not there")
        else:
            _call(run, ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "chown",
                        "--tmpfs", "/var/lib/postgresql/data",  # the image's VOLUME: no anonymous volume
                        "-v", f"{vol}:/data", img, SYNAPSE_UID, "/data"])
            # The effect, not the exit code: read the owner back.
            owner = _out(_call(run, ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "stat",
                                     "--tmpfs", "/var/lib/postgresql/data",  # the image's VOLUME: no anonymous volume
                                     "-v", f"{vol}:/data", img, "-c", "%u:%g", "/data"]))
            if owner == SYNAPSE_UID:
                log(f"synapse-data volume ownership set to {SYNAPSE_UID}")
            else:
                failures.append(f"{vol} is owned by {owner or 'an unreadable owner'}, not "
                                f"{SYNAPSE_UID} — Synapse cannot write its signing key")
    if (env.get("TLS_MODE") or "").strip() == "internal":
        ca = os.path.join(ctx["stack_root"], "modules", "apps", "matrix", "synapse", "caddy-local-ca.crt")
        r = _call(run, ["docker", "exec", "caddy", "cat", CADDY_ROOT])
        root = _out(r)
        if r.returncode != 0 or not root:
            failures.append("could not read Caddy's internal root CA — Synapse's OIDC login would "
                            "fail certificate verification")
        else:
            try:
                with open(ca, encoding="utf-8") as fh:
                    have = fh.read()
            except OSError:
                have = ""
            if root in have:
                log("Caddy internal root CA already in Synapse's trust store")
            else:
                try:
                    with open(ca, "a", encoding="utf-8") as fh:
                        fh.write(("" if not have or have.endswith("\n") else "\n") + root + "\n")
                    log("Appended Caddy internal root CA to Synapse trust store")
                except OSError as e:
                    failures.append(
                        f"cannot append Caddy's internal root CA to {ca} ({e.strerror or e}) — the "
                        "Configuration Portal mounts the repository read-only. Run on the host: "
                        "rzfz module enable matrix")
    names = _out(_call(run, ["docker", "ps", "--format", "{{.Names}}"])).splitlines()
    if "synapse" in names:
        r = _call(run, ctx["compose"]("restart", "synapse"), timeout=300, cwd=ctx["stack_root"])
        if r.returncode != 0:
            failures.append(f"`docker compose restart synapse` exited {r.returncode}")
        else:
            log("Restarted Synapse to apply volume ownership")
    if failures:
        raise PostEnableFailed("matrix post-start: " + "; ".join(failures))


# ------------------------------------------------------- clickhouse backup disk

#: ClickHouse runs as 101:101 (modules/observability/compose.yml `user:`). Its
#: image has no /var/lib/clickhouse/backups, so docker creates the
#: clickhouse-backup volume root:root 0755 and every `BACKUP … TO
#: Disk('backups', …)` fails with Permission denied (measured on a box: the
#: volume stayed empty from its creation on). The volume root must be 101's.
CLICKHOUSE_UID = "101:101"


def clickhouse_backup_owner(ctx):
    """The clickhouse-backup volume owned by 101:101. A no-op when the volume
    does not exist or already has that owner; otherwise chown it and read the
    owner back. Runs on every observability post-start: the enable, `rzfz
    post-install` and its --refresh (which `rzfz upgrade` ends in), so a box
    whose volume was created root-owned is repaired by the upgrade."""
    run, env, log = ctx["run"], ctx["env"], ctx["log"]
    vol = f"{env.get('COMPOSE_PROJECT_NAME') or 'razzfazz-stack'}_clickhouse-backup"
    if _call(run, ["docker", "volume", "inspect", vol]).returncode != 0:
        return
    # #2544 lesson: an image the box already runs, never alpine(:latest).
    img = _out(_call(run, ["docker", "inspect", "--type", "container",
                           "--format", "{{.Config.Image}}", "postgres"]))
    if not img:
        raise PostEnableFailed(f"cannot check the owner of {vol}: the postgres container (whose "
                               "image does the check) is not there")

    def owner():
        return _out(_call(run, ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "stat",
                                "--tmpfs", "/var/lib/postgresql/data",  # the image's VOLUME: no anonymous volume
                                "-v", f"{vol}:/data", img, "-c", "%u:%g", "/data"]))
    have = owner()
    if have == CLICKHOUSE_UID:
        log(f"ClickHouse backup volume already owned by {CLICKHOUSE_UID}")
        return
    _call(run, ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "chown",
                "--tmpfs", "/var/lib/postgresql/data",  # the image's VOLUME: no anonymous volume
                "-v", f"{vol}:/data", img, "-R", CLICKHOUSE_UID, "/data"])
    # The effect, not the exit code: read the owner back.
    now = owner()
    if now != CLICKHOUSE_UID:
        raise PostEnableFailed(
            f"{vol} is owned by {now or 'an unreadable owner'}, not {CLICKHOUSE_UID} — ClickHouse "
            "cannot write its backups there (every backup reports the observability backup as failed)")
    log(f"ClickHouse backup volume ownership set to {CLICKHOUSE_UID} (was {have or 'unknown'})")


# ------------------------------------------------------------------- openlit

#: OpenLIT ships ONE login: the upstream seed account, with a password that is
#: public in upstream's docs. It has no INIT_USER_* hook, so the stack adds the
#: stack admin (razzfazz-ai-admin@<MAIN_DOMAIN>, password
#: AUTHENTIK_BOOTSTRAP_PASSWORD). #2900: this used to run only in
#: `rzfz post-install`; a Portal enable left the public pair working.
#:
#: #2900 (rc7): the seed is LOCKED, never renamed or deleted. OpenLIT runs
#: `prisma db seed` on every container start (scripts/entrypoint.sh), and its
#: seed.js UPSERTS user@openlit.io: a missing row is created again with the
#: public password hash and made the owner of the default organisation and a
#: member of the default database config. So a renamed or deleted seed came
#: back at the next restart (the "seed AND stack admin" state measured on a
#: box), and the delete itself fails: databaseconfiguser and databaseconfig
#: reference the user with ON DELETE RESTRICT. A row that exists is only
#: touched on hasCompletedOnboarding by the upsert, so a locked seed (a bcrypt
#: hash of a random secret nobody keeps) stays locked across restarts.
#: The stack admin gets the seed's organisation, project and database-config
#: memberships where it lacks them (the seed's owner role included), and the
#: result is proven by real sign-ins against OpenLIT's own login endpoint.
#:
#: OpenLIT 2.x keeps every table and column this touches (users,
#: organisation_users, project_users, databaseconfiguser), its seed upserts the
#: seed row the same way and its sign-in is the same NextAuth "login" provider.
#: One seed behaviour changed: 1.x wrote INIT_DB_* (the stack's ClickHouse
#: login) over the stored "Default DB" connection on every start, 2.x creates
#: it once and never updates it. A ClickHouse password set after the first
#: start (a restored OpenLIT volume, an upgrade that mints a missing one) would
#: leave every dashboard empty, so the hook writes the stack's ClickHouse
#: login onto that row, as 1.x did.
OPENLIT_SEED_EMAIL = "user@openlit.io"
OPENLIT_SEED_PASSWORD = "openlituser"   # public upstream default (seed.js); never printed
OPENLIT_LOCKED_NAME = "Sign-in disabled (public default login)"
OPENLIT_ADMIN_NAME = "razzfazz-ai-admin"
OPENLIT_WAIT_SECONDS = 240

#: Runs INSIDE the openlit container (cd /app/client). Prints the user table.
OPENLIT_READ_SCRIPT = r"""
const { PrismaClient } = require("@prisma/client");
const p = new PrismaClient();
p.user.findMany({ select: { id: true, email: true, name: true } })
  .then(r => { console.log("RZFZ-OPENLIT " + JSON.stringify(r)); process.exit(0); })
  .catch(e => { console.error(e.message); process.exit(1); });
"""

#: stdin: the plain password. stdout: RZFZ-HASH <bcrypt>. Tries the bcrypt
#: libraries the image may carry; exit 3 when it has none.
OPENLIT_HASH_SCRIPT = r"""
let b = null;
for (const m of ["bcryptjs", "bcrypt", "bcrypt-ts"]) { try { b = require(m); break; } catch (e) {} }
if (!b) { process.exit(3); }
let pw = "";
process.stdin.on("data", d => pw += d);
process.stdin.on("end", () => {
  pw = pw.replace(/\r?\n$/, "");
  console.log("RZFZ-HASH " + (b.hashSync ? b.hashSync(pw, 10) : ""));
});
"""

#: stdin: JSON {statements: [{sql, args}]}. Runs them in ONE transaction
#: (Prisma's $transaction over $executeRawUnsafe; SQLite `?` placeholders), so a
#: half-moved membership set never stays behind. Prints the affected row counts.
OPENLIT_REPAIR_SCRIPT = r"""
const { PrismaClient } = require("@prisma/client");
const p = new PrismaClient();
let raw = "";
process.stdin.on("data", d => raw += d);
process.stdin.on("end", async () => {
  try {
    const a = JSON.parse(raw);
    const counts = await p.$transaction(a.statements.map(s => p.$executeRawUnsafe(s.sql, ...s.args)));
    console.log("RZFZ-OPENLIT-REPAIRED " + JSON.stringify(counts));
    process.exit(0);
  } catch (e) { console.error(e.message); process.exit(1); }
});
"""

#: stdin: JSON [{email, password}, ...]. A REAL sign-in per pair against
#: OpenLIT's own NextAuth credentials endpoint (provider id "login"): csrf token
#: first, then the form post; "accepted" only when OpenLIT issues a session
#: cookie. The server binds the container's hostname, not 127.0.0.1 (see the
#: healthcheck in modules/observability/compose.yml). Prints one line per pair,
#: never a password or a cookie value.
OPENLIT_SIGNIN_SCRIPT = r"""
const base = process.env.RZFZ_OPENLIT_URL || ("http://" + require("os").hostname() + ":3000");
const cookiesOf = (res) => (typeof res.headers.getSetCookie === "function"
  ? res.headers.getSetCookie() : [res.headers.get("set-cookie") || ""]).filter(Boolean);
async function signin(email, password) {
  const r1 = await fetch(base + "/api/auth/csrf");
  const jar = cookiesOf(r1).map(c => c.split(";")[0]).join("; ");
  const csrf = (await r1.json()).csrfToken;
  if (!csrf) return "error";
  const body = new URLSearchParams({ email, password, csrfToken: csrf, callbackUrl: base + "/", json: "true" });
  const r2 = await fetch(base + "/api/auth/callback/login", { method: "POST", redirect: "manual",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Cookie: jar }, body });
  if (r2.status >= 500) return "error";
  const set = cookiesOf(r2).join("\n");
  return /(^|\n)(__Secure-)?next-auth\.session-token(\.\d+)?=[^;\s]+/.test(set) ? "accepted" : "refused";
}
let raw = "";
process.stdin.on("data", d => raw += d);
process.stdin.on("end", async () => {
  const pairs = JSON.parse(raw);
  for (let i = 0; i < pairs.length; i++) {
    let v;
    try { v = await signin(pairs[i].email, pairs[i].password); } catch (e) { v = "error"; }
    console.log("RZFZ-SIGNIN " + i + " " + v);
  }
  process.exit(0);
});
"""

_BCRYPT = re.compile(r"^\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}$")


def openlit_repair_statements(seed, admin, *, admin_id=None, admin_hash=None, lock_hash=None,
                              db_login=None):
    """The SQL (OpenLIT 1.24.x and 2.x schema, SQLite) that gives the stack
    admin the seed's memberships and locks the seed, as [{sql, args}] for
    OPENLIT_REPAIR_SCRIPT. Every statement is idempotent: a second run on a
    repaired box affects zero rows.

    * admin_hash set → create the stack admin (copying the seed's timestamps,
      whatever format Prisma stores them in), unless it exists;
    * the seed's organisation memberships the admin lacks are copied (role,
      current project; `isCurrent` only when the admin has no current one), and
      an organisation the seed OWNS makes the admin its owner too;
    * the seed's project memberships follow onto the admin's membership row;
    * the seed's database-config memberships the admin lacks are copied with
      their permissions (`isCurrent` only when the admin has none current);
    * lock_hash set → the seed's password becomes that hash and its name says
      why; the row stays (see the module note above);
    * db_login (user, password) set → the default project's "Default DB"
      connection (the stack's own ClickHouse) carries that login.
    """
    S = "(SELECT id FROM users WHERE email = ?)"
    A = "(SELECT id FROM users WHERE email = ?)"
    out = []
    if admin_hash:
        out.append({"sql": "INSERT INTO users (id, name, email, password, has_completed_onboarding, "
                           "createdAt, updatedAt) SELECT ?, ?, ?, ?, 1, createdAt, updatedAt FROM users "
                           "WHERE email = ? AND NOT EXISTS (SELECT 1 FROM users WHERE email = ?)",
                    "args": [admin_id, OPENLIT_ADMIN_NAME, admin, admin_hash, seed, admin]})
    out.append({"sql": "INSERT INTO organisation_users (id, organisation_id, user_id, role, isCurrent, "
                       "current_project_id, createdAt, updatedAt) "
                       f"SELECT 'rzfz-' || ou.id, ou.organisation_id, {A}, ou.role, "
                       "CASE WHEN EXISTS (SELECT 1 FROM organisation_users c WHERE c.isCurrent = 1 "
                       f"AND c.user_id = {A}) THEN 0 ELSE ou.isCurrent END, "
                       "ou.current_project_id, ou.createdAt, ou.updatedAt FROM organisation_users ou "
                       f"WHERE ou.user_id = {S} AND NOT EXISTS (SELECT 1 FROM organisation_users x "
                       f"WHERE x.organisation_id = ou.organisation_id AND x.user_id = {A})",
                "args": [admin, admin, seed, admin]})
    out.append({"sql": "UPDATE organisation_users SET role = 'owner' "
                       f"WHERE user_id = {A} AND role <> 'owner' AND organisation_id IN "
                       f"(SELECT organisation_id FROM organisation_users WHERE user_id = {S} AND role = 'owner')",
                "args": [admin, seed]})
    out.append({"sql": "INSERT INTO project_users (id, project_id, organisation_user_id, user_id, createdAt) "
                       f"SELECT 'rzfz-' || pu.id, pu.project_id, aou.id, {A}, pu.createdAt "
                       "FROM project_users pu JOIN organisation_users sou ON sou.id = pu.organisation_user_id "
                       "JOIN organisation_users aou ON aou.organisation_id = sou.organisation_id "
                       f"AND aou.user_id = {A} WHERE pu.user_id = {S} AND NOT EXISTS "
                       "(SELECT 1 FROM project_users x WHERE x.project_id = pu.project_id "
                       "AND x.organisation_user_id = aou.id)",
                "args": [admin, admin, seed]})
    out.append({"sql": "INSERT INTO databaseconfiguser (databaseConfigId, user_id, isCurrent, canEdit, "
                       f"canShare, canDelete, createdAt, updatedAt) SELECT d.databaseConfigId, {A}, "
                       "CASE WHEN EXISTS (SELECT 1 FROM databaseconfiguser c WHERE c.isCurrent = 1 "
                       f"AND c.user_id = {A}) THEN 0 ELSE d.isCurrent END, d.canEdit, d.canShare, "
                       f"d.canDelete, d.createdAt, d.updatedAt FROM databaseconfiguser d WHERE d.user_id = {S} "
                       "AND NOT EXISTS (SELECT 1 FROM databaseconfiguser x WHERE "
                       f"x.databaseConfigId = d.databaseConfigId AND x.user_id = {A})",
                "args": [admin, admin, seed, admin]})
    if lock_hash:
        out.append({"sql": "UPDATE users SET password = ?, name = ? WHERE email = ?",
                    "args": [lock_hash, OPENLIT_LOCKED_NAME, seed]})
    if db_login is not None:
        user, pw = db_login
        out.append({"sql": "UPDATE databaseconfig SET username = ?, password = ? WHERE name = 'Default DB' "
                           "AND environment = 'production' AND project_id IN (SELECT p.id FROM projects p "
                           "JOIN organisations o ON o.id = p.organisation_id WHERE o.slug = 'default' "
                           "AND p.slug = 'default') AND (username IS NOT ? OR password IS NOT ?)",
                    "args": [user, pw, user, pw]})
    return out


def _openlit_node(run, script, stdin=None, timeout=60):
    argv = ["docker", "exec"] + (["-i"] if stdin is not None else []) + [
        "openlit", "sh", "-c", "cd /app/client && node -e \"$0\"", script]
    return _call(run, argv, timeout=timeout, stdin=stdin)


def _openlit_rows(run):
    """The user table as [{id, email, name}], or None while it is not readable yet."""
    r = _openlit_node(run, OPENLIT_READ_SCRIPT)
    line = next((ln for ln in (getattr(r, "stdout", "") or "").splitlines()
                 if ln.startswith("RZFZ-OPENLIT ")), None)
    if r.returncode != 0 or line is None:
        return None
    return json.loads(line[len("RZFZ-OPENLIT "):])


def _openlit_hash(run, password, log):
    """bcrypt of `password`, the password on stdin only. First inside the
    OpenLIT image (no network), else the stack's Caddy (`caddy hash-password`)."""
    r = _openlit_node(run, OPENLIT_HASH_SCRIPT, stdin=password)
    line = next((ln for ln in (getattr(r, "stdout", "") or "").splitlines()
                 if ln.startswith("RZFZ-HASH ")), None)
    h = line[len("RZFZ-HASH "):].strip() if (r.returncode == 0 and line) else None
    if not h:
        # The stack's own Caddy: no network, no extra image. With stdin not a
        # terminal `caddy hash-password` reads the password from it and prints
        # a bcrypt hash ($2a$...), which bcrypt/bcryptjs verify as-is.
        log("OpenLIT image has no bcrypt library - hashing with the stack's Caddy")
        # `caddy hash-password` reads ONE LINE from stdin: without the trailing
        # newline it stops with "EOF" (rc 1) and no hash (measured on Caddy
        # 2.11.4/2.11.7, #2900 box check). A password containing a newline
        # cannot pass as one line - refuse rather than hash a prefix.
        if "\n" in password or "\r" in password:
            raise PostEnableFailed("the password contains a line break and cannot be hashed with `caddy hash-password`")
        r = _call(run, ["docker", "exec", "-i", "caddy", "caddy", "hash-password", "--algorithm", "bcrypt"],
                  timeout=120, stdin=password + "\n")
        lines = [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]
        h = lines[-1] if (r.returncode == 0 and lines) else None
    if not h or not _BCRYPT.match(h):
        raise PostEnableFailed("could not compute the bcrypt hash of the stack admin password "
                               "(no bcrypt library in the OpenLIT image and `caddy hash-password` failed)")
    return h


def _openlit_signin(run, pairs, sleep, wait):
    """[verdict per pair] from OPENLIT_SIGNIN_SCRIPT ("accepted" / "refused" /
    "error"). Retries while OpenLIT's web server does not answer yet."""
    waited = 0
    while True:
        r = _openlit_node(run, OPENLIT_SIGNIN_SCRIPT, stdin=json.dumps(pairs), timeout=120)
        got = dict(re.findall(r"^RZFZ-SIGNIN (\d+) (accepted|refused|error)$",
                              getattr(r, "stdout", "") or "", re.M))
        verdicts = [got.get(str(i), "error") for i in range(len(pairs))]
        if "error" not in verdicts or waited >= wait:
            return verdicts
        sleep(5)
        waited += 5


def openlit_post_start(ctx):
    """Give OpenLIT the stack admin and lock its public seed login (idempotent),
    then PROVE it with real sign-ins: the public pair is refused and the stack
    admin is accepted. FAILS the enable otherwise: the alternative is a console
    whose seed login has a published password (#2900). Secrets travel on stdin."""
    import secrets
    import uuid
    run, env, log = ctx["run"], ctx["env"], ctx["log"]
    sleep = ctx.get("sleep", time.sleep)
    wait = ctx.get("wait", OPENLIT_WAIT_SECONDS)
    domain = (env.get("MAIN_DOMAIN") or "").strip()
    password = env.get("AUTHENTIK_BOOTSTRAP_PASSWORD") or ""
    if not domain or not password:
        raise PostEnableFailed(
            "openlit post-start: MAIN_DOMAIN or AUTHENTIK_BOOTSTRAP_PASSWORD is empty, so OpenLIT's "
            f"public default login ({OPENLIT_SEED_EMAIL}) cannot be replaced. Run: rzfz init, then "
            "rzfz post-install --refresh")
    admin = f"razzfazz-ai-admin@{domain}"
    waited, rows = 0, None
    while True:
        rows = _openlit_rows(run)
        if rows:
            break
        if waited >= wait:
            raise PostEnableFailed(
                "openlit post-start: the OpenLIT user table is not readable (container not up, or no "
                f"user row yet) — its default login {OPENLIT_SEED_EMAIL} could not be checked. "
                "Run: rzfz post-install --refresh")
        sleep(5)
        waited += 5
    by_email = {r.get("email"): r for r in rows}
    if OPENLIT_SEED_EMAIL not in by_email:
        log(f"OpenLIT: no upstream seed login ({'aligned: ' + admin if admin in by_email else 'operator-managed users'})")
        return
    locked = by_email[OPENLIT_SEED_EMAIL].get("name") == OPENLIT_LOCKED_NAME
    has_admin = admin in by_email
    if locked and has_admin:
        log("OpenLIT: the seed login is locked and the stack admin exists - checking the sign-ins")
    else:
        log("OpenLIT: adding the stack admin and locking the upstream seed login...")
    admin_hash = None if has_admin else _openlit_hash(run, password, log)
    # The lock: a bcrypt hash of a random secret that is never stored or shown.
    lock_hash = None if locked else _openlit_hash(run, secrets.token_urlsafe(32), log)
    # The login compose hands OpenLIT as INIT_DB_USERNAME / INIT_DB_PASSWORD.
    ch_login = ((env.get("CLICKHOUSE_USER") or "").strip() or "default", env.get("CLICKHOUSE_PASSWORD") or "")
    stmts = openlit_repair_statements(OPENLIT_SEED_EMAIL, admin, admin_id=str(uuid.uuid4()),
                                      admin_hash=admin_hash, lock_hash=lock_hash, db_login=ch_login)
    r = _openlit_node(run, OPENLIT_REPAIR_SCRIPT, stdin=json.dumps({"statements": stmts}))
    if r.returncode != 0:
        err = (getattr(r, "stderr", "") or "")[-300:]
        for h in (admin_hash, lock_hash, ch_login[1]):
            if h:
                err = err.replace(h, "***")
        raise PostEnableFailed(
            "openlit post-start: OpenLIT's user table could not be updated (nothing was changed) - "
            f"its public default login {OPENLIT_SEED_EMAIL} may still work: " + err)
    m = re.search(r"^RZFZ-OPENLIT-REPAIRED (\[.*\])$", getattr(r, "stdout", "") or "", re.M)
    if m and any(json.loads(m.group(1))):
        log(f"OpenLIT: rows changed {json.loads(m.group(1))}")
    after = {x.get("email") for x in (_openlit_rows(run) or [])}
    if admin not in after:
        raise PostEnableFailed(f"openlit post-start: the stack admin {admin} is missing from OpenLIT "
                               "after the update. Run: rzfz post-install --refresh")
    seed_v, admin_v = _openlit_signin(
        run, [{"email": OPENLIT_SEED_EMAIL, "password": OPENLIT_SEED_PASSWORD},
              {"email": admin, "password": password}], sleep, wait)
    if seed_v == "accepted":
        raise PostEnableFailed(
            f"openlit post-start: OpenLIT still accepts its public default login {OPENLIT_SEED_EMAIL} "
            "after the seed was locked. Disable the observability module until this is resolved, and "
            "report the OpenLIT version (OBSERVABILITY_VERSION)")
    if admin_v != "accepted" or seed_v != "refused":
        why = ("refused the stack admin's password" if admin_v == "refused"
               else "did not answer the sign-in check")
        raise PostEnableFailed(
            f"openlit post-start: OpenLIT {why} for {admin}, so it is not proven that the public "
            f"default login {OPENLIT_SEED_EMAIL} is closed. If {admin}'s password was changed inside "
            "OpenLIT, set it back to the stack admin password; then run: rzfz post-install --refresh")
    log(f"OpenLIT admin aligned ({admin}); the public default login is refused")


# -------------------------------------------------------------------- cognee

#: cognee ships a development account whose password is published upstream
#: (#2404, #2906). It is a SUPERUSER and cognee's API is reachable without
#: Authentik. It is deactivated, never deleted: cognee's data rows may
#: reference it, and an inactive fastapi-users row cannot log in.
COGNEE_SEED_EMAIL = "default_user@example.com"
COGNEE_SEED_PASSWORD = "default_password"   # public upstream default; never printed
COGNEE_WAIT_SECONDS = 240

#: Runs INSIDE the cognee container (`python3 -c`). stdin: "<email>\n<password>".
#: Prints ONLY the HTTP status of the login (000 when cognee did not answer):
#: no body, so no token can reach a log.
COGNEE_PROBE_SCRIPT = r"""
import sys, urllib.error, urllib.parse, urllib.request
em, _, pw = sys.stdin.read().partition("\n")
req = urllib.request.Request("http://localhost:8000/api/v1/auth/login", method="POST",
    data=urllib.parse.urlencode({"username": em, "password": pw}).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"})
try:
    code = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15).status
except urllib.error.HTTPError as e:
    code = e.code
except Exception:
    code = 0
sys.stdout.write("RZFZ-COGNEE-LOGIN %03d\n" % code)
"""


def _cognee_psql(run, env, sql):
    user = (env.get("POSTGRES_USER") or "docker").strip() or "docker"
    db = (env.get("COGNEE_DB") or "cognee_db").strip() or "cognee_db"
    return _call(run, ["docker", "exec", "postgres", "psql", "-U", user, "-d", db, "-tAc", sql], timeout=60)


def _cognee_users(run, env):
    """[(email, superuser, active)] or None when the table is not readable."""
    r = _cognee_psql(run, env, "SELECT email || '|' || is_superuser || '|' || is_active FROM users ORDER BY email;")
    if r.returncode != 0:
        return None
    out = []
    for ln in (getattr(r, "stdout", "") or "").splitlines():
        em, _, rest = ln.strip().partition("|")
        su, _, act = rest.partition("|")
        if em:
            # Postgres prints `bool || text` as true/false, not t/f (#2404).
            out.append((em, su in ("t", "true"), act in ("t", "true")))
    return out or None


def _cognee_login_status(run):
    """HTTP status of a login with the PUBLIC seed pair; 0 = no answer."""
    r = _call(run, ["docker", "exec", "-i", "cognee", "python3", "-c", COGNEE_PROBE_SCRIPT],
              timeout=60, stdin=f"{COGNEE_SEED_EMAIL}\n{COGNEE_SEED_PASSWORD}")
    m = re.search(r"RZFZ-COGNEE-LOGIN (\d{3})", getattr(r, "stdout", "") or "")
    return int(m.group(1)) if m else 0


def cognee_post_start(ctx):
    """Deactivate cognee's public seed account (idempotent) and PROVE it cannot
    log in. FAILS the enable otherwise (#2906): a superuser session with a
    published password, reachable without Authentik. Other superusers are
    reported, never changed. One implementation for the Portal enable and
    `rzfz post-install`."""
    run, env, log = ctx["run"], ctx["env"], ctx["log"]
    sleep = ctx.get("sleep", time.sleep)
    limit = ctx.get("wait", COGNEE_WAIT_SECONDS)
    stack_id = f"razzfazz-ai-admin@{(env.get('MAIN_DOMAIN') or '').strip()}"
    waited, users = 0, None
    while True:
        users = _cognee_users(run, env)
        if users:
            break
        if waited >= limit:
            raise PostEnableFailed(
                f"cognee post-start: the cognee users table is not readable (postgres or cognee not up, "
                f"or no user row yet) — its default login {COGNEE_SEED_EMAIL} could not be checked. "
                "Run: rzfz post-install --refresh")
        sleep(5)
        waited += 5
    seed = next((u for u in users if u[0] == COGNEE_SEED_EMAIL), None)
    if seed and (seed[1] or seed[2]):
        log(f"cognee: deactivating the upstream development account {COGNEE_SEED_EMAIL} "
            f"(superuser={seed[1]}, active={seed[2]}) and removing superuser...")
        r = _cognee_psql(run, env, "UPDATE users SET is_active = false, is_superuser = false "
                                   f"WHERE email = '{COGNEE_SEED_EMAIL}';")
        if r.returncode != 0:
            raise PostEnableFailed(
                f"cognee post-start: could not deactivate {COGNEE_SEED_EMAIL} — cognee still accepts its "
                "public default login: " + (getattr(r, "stderr", "") or "")[-300:])
    others = sorted(e for e, su, _ in users if su and e not in (COGNEE_SEED_EMAIL, stack_id))
    if others:
        log(f"cognee: superuser(s) besides the stack identity {stack_id}: {', '.join(others)} — not changed; "
            "remove or downgrade them by hand if they are not yours")
    # The effect, not the SQL exit code: log in with the public pair.
    status = _cognee_login_status(run)
    while status == 0 and waited < limit:      # cognee binds its port before it serves
        sleep(5)
        waited += 5
        status = _cognee_login_status(run)
    if status == 200:
        raise PostEnableFailed(
            f"cognee post-start: {COGNEE_SEED_EMAIL} STILL authenticates after the update (HTTP 200) — cognee "
            "hands out a superuser session with its public default password, without Authentik. "
            f"Deactivate the row by hand: UPDATE users SET is_active=false, is_superuser=false WHERE email='{COGNEE_SEED_EMAIL}';")
    if status == 0:
        raise PostEnableFailed(
            f"cognee post-start: cognee did not answer the login probe, so the refusal of {COGNEE_SEED_EMAIL} "
            "is unverified. Run: rzfz post-install --refresh")
    log(f"cognee: the upstream pair is refused (HTTP {status})")


# ----------------------------------------------------------------------- mcp

#: mcp-manager's read-only Authentik token (see mcp_directory_token.py). Without
#: it mcp-manager runs in 2026.09 compatibility mode and derives a new user's
#: personal memory account from the USERNAME, so a reused username inherits the
#: former owner's memory. `rzfz post-install` (install, --refresh, upgrade)
#: minted it; a module enabled in the Portal after install never got it.
MCP_TOKEN_KEY = "MCP_MANAGER_AUTHENTIK_READONLY_TOKEN"
MCP_TOKEN_ARGV = ["docker", "exec", "-i", "razzfazz-config", "python", "-m",
                  "app.services.mcp_directory_token"]
_MCP_KEY = re.compile(r"^[A-Za-z0-9._-]{32,256}$")


def _env_file_setter(stack_root):
    """set_env(key, value) for the host CLI: the stack's .env, rewritten IN
    PLACE (same inode, so the Portal's single-file bind stays valid)."""
    path = os.path.join(stack_root, ".env")

    def set_env(key, value):
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines(True)
        except FileNotFoundError:
            lines = []
        pat = re.compile(r"^\s*(?:export\s+)?" + re.escape(key) + "=")
        out, seen = [], False
        for ln in lines:
            m = pat.match(ln)
            if m:
                out.append(f"{m.group(0)}{value}\n")
                seen = True
            else:
                out.append(ln)
        if not seen:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"{key}={value}\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(out)
    return set_env


def mcp_post_start(ctx):
    """Mint (or keep) mcp-manager's read-only Authentik token, write it to
    .env and recreate mcp-manager so it runs with it. The same mint-and-prove
    step `rzfz post-install` runs (razzfazz-config's
    `python -m app.services.mcp_directory_token`, current key on stdin).
    FAILS the enable when no proven key can be set: the alternative is the
    username-keyed memory accounts of compatibility mode. The key travels on
    stdin and in captured output only; it is never logged."""
    run, env, log = ctx["run"], ctx["env"], ctx["log"]
    set_env = ctx.get("set_env") or _env_file_setter(ctx["stack_root"])
    current = (env.get(MCP_TOKEN_KEY) or "").strip()
    retry = "Run: rzfz post-install --refresh"
    r = _call(run, MCP_TOKEN_ARGV, timeout=300, stdin=current)
    if r.returncode == 3:
        raise PostEnableFailed(
            "mcp post-start: the read-only Authentik token for mcp-manager did not prove read-only, "
            "so it was deleted and not written. mcp-manager runs in compatibility mode (memory "
            f"accounts keyed by username) until a proven token is set. {retry}")
    if r.returncode != 0:
        raise PostEnableFailed(
            "mcp post-start: the read-only Authentik token for mcp-manager could not be provisioned "
            f"(exit {r.returncode}; is Authentik up?). mcp-manager runs in compatibility mode "
            f"(memory accounts keyed by username) until it is set. {retry}")
    status = key = ""
    for line in (getattr(r, "stdout", "") or "").splitlines():
        if line.startswith("STATUS "):
            status = line[len("STATUS "):].strip()
        elif line.startswith("KEY "):
            key = line[len("KEY "):].strip()
    if not _MCP_KEY.match(key):
        raise PostEnableFailed("mcp post-start: unusable answer from the token step, nothing written. "
                               + retry)
    if status == "kept" and key == current:
        log("mcp-manager's read-only Authentik token is present and proven read-only")
    else:
        set_env(MCP_TOKEN_KEY, key)
        log("Minted mcp-manager's read-only Authentik token (view users only; writes refused)")
    # The effect: the RUNNING mcp-manager carries the key. It reads it from its
    # environment, fixed when the container was created.
    def carried():
        out = _call(run, ["docker", "inspect", "--type", "container", "--format",
                          "{{range .Config.Env}}{{println .}}{{end}}", "mcp-manager"])
        return out.returncode == 0 and f"{MCP_TOKEN_KEY}={key}" in (getattr(out, "stdout", "") or "").splitlines()
    if carried():
        return
    log("Recreating mcp-manager so it reads the token...")
    rc = _call(run, ctx["compose"]("up", "-d", "--no-deps", "--force-recreate", "mcp-manager"),
               timeout=300, cwd=ctx["stack_root"])
    if rc.returncode != 0 or not carried():
        raise PostEnableFailed(
            "mcp post-start: the token is in .env but mcp-manager was not recreated with it "
            f"(exit {rc.returncode}); it stays in compatibility mode until it is. Run: "
            "docker compose up -d --no-deps --force-recreate mcp-manager")
    log("mcp-manager runs with the read-only Authentik token")


def observability_post_start(ctx):
    """The ClickHouse backup volume owner, then OpenLIT's logins. The volume
    step runs first and a failure of it does not skip the OpenLIT step (a
    public login must never stay open because of a backup disk); both
    failures are reported together."""
    failures = []
    for step in (clickhouse_backup_owner, openlit_post_start):
        try:
            step(ctx)
        except PostEnableFailed as e:
            failures.append(str(e))
    if failures:
        raise PostEnableFailed("; ".join(failures))


#: profile -> its post-start hook. Every module whose enable needs a step after
#: `up` registers it HERE, so both enable paths run it (#2681).
POST_START_HOOKS = {
    "matrix": matrix_post_start,
    "observability": observability_post_start,
    "cognee": cognee_post_start,
    "mcp": mcp_post_start,
}


def _compose_argv(stack_root):
    """The #1226 chokepoint (compose_allowlist.compose_argv), loaded from this
    file's directory so the host CLI uses it too."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rzfz_compose_allowlist", os.path.join(os.path.dirname(os.path.abspath(__file__)), "compose_allowlist.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return lambda *a: mod.compose_argv(stack_root, *a)


def run_post_start(profile_id, stack_root, env, *, run=subprocess.run, compose=None, log=print,
                   set_env=None):
    """Run the profile's post-start hook, if it has one. Raises PostEnableFailed.
    `compose(*args)` builds a `docker compose` argv; default: the #1226
    chokepoint for `stack_root`. `set_env(key, value)` writes one .env value;
    default: the stack's .env, in place."""
    hook = POST_START_HOOKS.get(profile_id)
    if hook is None:
        return False
    log(f"Running the {profile_id} post-start steps...")
    hook({"stack_root": stack_root, "env": env, "run": run, "log": log,
          "compose": compose or _compose_argv(stack_root),
          "set_env": set_env or _env_file_setter(stack_root)})
    return True


# ------------------------------------------------------------------- bindings

def _table(stack_root, name):
    try:
        with open(os.path.join(stack_root, BINDINGS_SCRIPT), encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        raise PostEnableFailed(f"cannot read the binding table {BINDINGS_SCRIPT}: {e}") from e
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise PostEnableFailed(f"{BINDINGS_SCRIPT} has no {name} table")


def app_slugs(profile_id, stack_root):
    """The Applications the binding pass gates for this profile — read from the
    script's own table, so the check and the pass cannot disagree."""
    open_ok = set(_table(stack_root, "UNBOUND_BY_DESIGN"))
    out = []
    for slug, prof in _table(stack_root, "APP_PROFILES").items():
        profs = (prof,) if isinstance(prof, str) else tuple(prof or ())
        if profile_id in profs and slug not in open_ok:
            out.append(slug)
    return sorted(out)


#: Runs INSIDE authentik-worker (`python -c`), against Authentik's ORM — the
#: same place apply-policy-bindings.py runs. argv[1] = JSON list of slugs,
#: argv[2] = the admin group (the script's SUPER_ADMINS_GROUP).
CHECK_SCRIPT = r'''
import json, os, sys
sys.path.append("/")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
import django
django.setup()
from authentik.core.models import Application, Group
from authentik.policies.models import PolicyBinding
admins = Group.objects.filter(name=sys.argv[2]).first()
out = {}
for slug in json.loads(sys.argv[1]):
    app = Application.objects.filter(slug=slug).first()
    if app is None:
        out[slug] = "absent"
    elif PolicyBinding.objects.filter(target_id=app.pk, enabled=True).exists():
        out[slug] = "bound"
    elif admins is None:
        out[slug] = "open"
    else:
        # Deny-by-default until the binding pass binds the app's own group:
        # Super Admins are in every app's binding set, so the pass keeps this
        # binding and only adds to it (ensure_binding re-orders it).
        PolicyBinding.objects.create(target_id=app.pk, group=admins, enabled=True, order=100)
        out[slug] = "closed"
print("RZFZ-BINDINGS " + json.dumps(out, sort_keys=True))
'''


def wait_for_authentik_init(*, run=subprocess.run, log=print, timeout=INIT_WAIT_SECONDS):
    """authentik-init applies the blueprints and the binding pass; it was just
    recreated detached. The check below means nothing before it has exited."""
    log("Waiting for authentik-init (blueprints + policy bindings) to finish...")
    try:
        r = run(["docker", "wait", "authentik-init"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"  authentik-init still running after {timeout}s — checking the bindings as they are")
        return None
    except OSError as e:
        raise PostEnableFailed(f"cannot wait for authentik-init: {e}") from e
    code = _out(r)
    log(f"  authentik-init exited {code or '?'}")
    return code


def close_unbound_apps(profile_id, stack_root, *, run=subprocess.run, log=print):
    """Every gated Application of the profile must carry an enabled binding.
    Returns {slug: state}. Raises PostEnableFailed when one had none (it is
    bound to Super Admins first) or when the check could not run."""
    slugs = app_slugs(profile_id, stack_root)
    if not slugs:
        return {}
    admins = _table(stack_root, "SUPER_ADMINS_GROUP")
    r = _call(run, ["docker", "exec", "authentik-worker", "python", "-c", CHECK_SCRIPT,
                    json.dumps(slugs), admins], timeout=180)
    line = next((ln for ln in (getattr(r, "stdout", "") or "").splitlines()
                 if ln.startswith("RZFZ-BINDINGS ")), None)
    if r.returncode != 0 or line is None:
        raise PostEnableFailed(
            f"could not check the Authentik policy bindings of {', '.join(slugs)} (exit "
            f"{r.returncode}): {(getattr(r, 'stderr', '') or '')[-300:]} — an unbound app is open to "
            "every signed-in user. Check them, then run: rzfz post-install --refresh")
    states = json.loads(line[len("RZFZ-BINDINGS "):])
    for slug in slugs:
        log(f"  Authentik app {slug}: {states.get(slug, 'unknown')}")
    bad = sorted(s for s, st in states.items() if st in ("closed", "open"))
    if bad:
        raise PostEnableFailed(
            f"Authentik application(s) {', '.join(bad)} had NO policy binding after the enable — "
            "open to every signed-in user. "
            + ("They are now restricted to Super Admins. " if all(states[s] == "closed" for s in bad)
               else "The Super Admins group is missing, so they could NOT be closed. ")
            + "Re-run the binding pass (rzfz post-install --refresh), then enable again.")
    return states


# ------------------------------------------------------------------ host CLI

def _read_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.rstrip("\n").partition("=")
                if sep and k and not k.startswith("#") and k not in env:
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def main(argv=None):
    """rzfz module enable: `post-start <profile> --stack DIR` and
    `bindings <profile> --stack DIR`. Exit 0 done, 1 failed, 2 usage."""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) != 4 or a[0] not in ("post-start", "bindings") or a[2] != "--stack":
        print("usage: module_hooks.py {post-start|bindings} <profile> --stack <dir>", file=sys.stderr)
        return 2
    verb, profile, stack = a[0], a[1], a[3]
    # The binding table comes from the tree this file ships in (on a box the
    # same directory as the stack); the .env from the stack.
    tree = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))

    def log(msg):
        print(f"  → {msg}", flush=True)
    try:
        if verb == "post-start":
            run_post_start(profile, stack, _read_env(os.path.join(stack, ".env")), log=log)
        else:
            if app_slugs(profile, tree):
                wait_for_authentik_init(log=log)
            close_unbound_apps(profile, tree, log=log)
    except PostEnableFailed as e:
        print(f"[✗] {e}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
