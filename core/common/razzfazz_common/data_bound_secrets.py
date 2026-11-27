# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2471 — secrets whose value encrypts a module's stored data.

Enabling a module whose database already held data while such a secret was
empty generated a NEW key and started the app on it: Infisical crash-looped
("Unsupported state or unable to authenticate data"), the enable reported
success, and the data was lost for good (a demo box, 2026-09-24). Every path that
fills an empty secret asks this module first: the Configuration Portal's
enable, `rzfz upgrade`'s _gen_if_empty, `rzfz status` and `rzfz init --force`
(`remint`: every key at once). The list lives next to
the module declaration, core/config/profiles.yaml `data_bound_secrets:`.

The secret store is a parameter (`get_secret`). Today the value comes from
.env. Once OpenBao (#2476) holds a module's secrets, the caller passes
`chained_getter(openbao_getter, env_getter(env))`, and the check reads the
store the module actually starts from.

Fail closed: a database whose state cannot be read counts as holding data.
"""
from __future__ import annotations

import argparse
import enum
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

_DB_RE = re.compile(r"\A[a-z0-9_]+\Z")
_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")

RunSql = Callable[[str, str], Optional[str]]


class DbState(enum.Enum):
    ABSENT = "absent"
    EMPTY = "empty"
    HAS_DATA = "holds data"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DataBound:
    database: str
    keys: tuple


_VOL_RE = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,62}\Z")
_FILE_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
RunLs = Callable[[str], Optional[str]]   # compose volume name -> `ls -A` of it, "" absent, None on error


@dataclass(frozen=True)
class DataBoundVolume:
    """A module whose data lives in a volume and whose key is a FILE in another
    volume (OpenBao's static seal, #2476). No .env keys: `all`/`keys` print nothing."""
    volume: str
    key_volume: str
    key_files: tuple
    keys: tuple = ()


def declared(profile_cfg: Mapping):
    """The profile's declaration: DataBound (database form), DataBoundVolume
    (volume form) or None. ValueError on a malformed or mixed block."""
    block = (profile_cfg or {}).get("data_bound_secrets")
    if not block:
        return None
    if "volume" in block:
        vol, kvol = block.get("volume"), block.get("key_volume")
        files = tuple(block.get("key_files") or ())
        if ("database" in block or "keys" in block
                or not all(isinstance(v, str) and _VOL_RE.match(v) for v in (vol, kvol))
                or not files or not all(isinstance(f, str) and _FILE_RE.match(f) and f not in (".", "..")
                                        for f in files)):
            raise ValueError(f"malformed data_bound_secrets block: {block!r}")
        return DataBoundVolume(vol, kvol, files)
    db = block.get("database")
    keys = tuple(block.get("keys") or ())
    if (not isinstance(db, str) or not _DB_RE.match(db) or not keys
            or not all(isinstance(k, str) and _KEY_RE.match(k) for k in keys)):
        raise ValueError(f"malformed data_bound_secrets block: {block!r}")
    return DataBound(db, keys)


def probe_database(database: str, run_sql: RunSql) -> DbState:
    if not _DB_RE.match(database):
        raise ValueError(f"not a database name: {database!r}")
    exists = run_sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{database}'")
    if exists is None:
        return DbState.UNKNOWN
    if exists.strip() == "0":
        return DbState.ABSENT
    tables = run_sql(database, "SELECT count(*) FROM information_schema.tables "
                               "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')")
    if tables is None:
        return DbState.UNKNOWN
    try:
        n = int(tables.strip())
    except ValueError:
        return DbState.UNKNOWN
    return DbState.HAS_DATA if n > 0 else DbState.EMPTY


def probe_volume(volume: str, run_ls: RunLs) -> DbState:
    out = run_ls(volume)
    if out is None:
        return DbState.UNKNOWN
    return DbState.HAS_DATA if out.strip() else DbState.EMPTY


@dataclass(frozen=True)
class VolumeRefusal:
    profile: str
    volume: str
    key_volume: str
    missing: tuple
    state: DbState

    def message(self) -> str:   # Portal-visible: no issue numbers (#2405)
        files = ", ".join(self.missing)
        if self.state is DbState.UNKNOWN:
            return (f"Refusing to enable '{self.profile}': cannot read the volumes {self.volume} / "
                    f"{self.key_volume}. A new key over existing data makes it unreadable for good. "
                    f"Retry once docker answers.")
        return (f"Refusing to enable '{self.profile}': {self.volume} holds data, but {files} is "
                f"missing from {self.key_volume}. A new key would make that data unreadable for "
                f"good. Restore the key volume from the backup (keys.tar.enc, "
                f"scripts/openbao-restore.sh) before enabling it again.")


def check_volume(profile_id: str, bound: DataBoundVolume,
                 run_ls: Optional[RunLs]) -> Optional[VolumeRefusal]:
    if run_ls is None:
        return VolumeRefusal(profile_id, bound.volume, bound.key_volume, bound.key_files, DbState.UNKNOWN)
    state = probe_volume(bound.volume, run_ls)
    if state is DbState.EMPTY:
        return None
    if state is DbState.UNKNOWN:
        return VolumeRefusal(profile_id, bound.volume, bound.key_volume, bound.key_files, state)
    keys_out = run_ls(bound.key_volume)
    if keys_out is None:
        return VolumeRefusal(profile_id, bound.volume, bound.key_volume, bound.key_files, DbState.UNKNOWN)
    present = set(keys_out.split())
    missing = tuple(f for f in bound.key_files if f not in present)
    return VolumeRefusal(profile_id, bound.volume, bound.key_volume, missing, state) if missing else None


def docker_volume_lister() -> RunLs:
    """Host or Portal side: resolve the compose volume by its label (never
    creates it) and list it read-only from a throwaway container of the box's
    postgres image (always present; no pull, #2544's lesson)."""
    def run(volume: str) -> Optional[str]:
        try:
            name = subprocess.run(["docker", "volume", "ls", "-q", "--filter",
                                   f"label=com.docker.compose.volume={volume}"],
                                  capture_output=True, text=True, timeout=30)
            if name.returncode != 0:
                return None
            vol = (name.stdout.split() or [""])[0]
            if not vol:
                return ""
            img = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", "postgres"],
                                 capture_output=True, text=True, timeout=30)
            if img.returncode != 0:
                return None
            ls = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{vol}:/v:ro",
                                 "--entrypoint", "ls", img.stdout.strip(), "-A", "/v"],
                                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return ls.stdout if ls.returncode == 0 else None
    return run


@dataclass(frozen=True)
class Refusal:
    profile: str
    database: str
    empty_keys: tuple
    state: DbState

    def message(self) -> str:
        keys = ", ".join(self.empty_keys)
        if self.state is DbState.UNKNOWN:
            return (f"Refusing to generate {keys} for '{self.profile}': cannot tell whether "
                    f"{self.database} already holds data (postgres did not answer). A new key "
                    f"over existing data makes it unreadable for good. Retry once "
                    f"postgres is up.")
        return (f"Refusing to generate {keys} for '{self.profile}': {self.database} already "
                f"holds data, encrypted with a key that is no longer in the secret store. A NEW "
                f"key would make that data unreadable for good. Either put the original "
                f"value(s) of {keys} back (an env snapshot, `scripts/env-snapshot.sh list`, or a "
                f"backup's .env may hold them), or reset the module's data explicitly: "
                f"rzfz module reset-data {self.profile} --i-understand-data-loss")


def env_getter(env: Mapping[str, str]) -> Callable[[str], str]:
    return lambda key: env.get(key) or ""


def chained_getter(*getters: Callable[[str], str]) -> Callable[[str], str]:
    def get(key: str) -> str:
        for g in getters:
            v = g(key)
            if v:
                return v
        return ""
    return get


def check_generation(profile_id: str, profile_cfg: Mapping,
                     get_secret: Callable[[str], str], run_sql: RunSql,
                     run_ls: Optional[RunLs] = None):
    """None: every data-bound key is present, or there is no data to lose.
    A Refusal: at least one is empty while the database holds data (or its
    state cannot be read). A volume form goes to check_volume (VolumeRefusal)."""
    bound = declared(profile_cfg)
    if bound is None:
        return None
    if isinstance(bound, DataBoundVolume):
        return check_volume(profile_id, bound, run_ls)
    empty = tuple(k for k in bound.keys if not get_secret(k))
    if not empty:
        return None
    state = probe_database(bound.database, run_sql)
    if state in (DbState.ABSENT, DbState.EMPTY):
        return None
    return Refusal(profile_id, bound.database, empty, state)


def docker_psql_runner(pg_user: str, pg_password: str) -> RunSql:
    """The password travels in the subprocess environment (`-e PGPASSWORD`
    without a value), never in docker's argv where `ps` shows it."""
    def run(db: str, sql: str) -> Optional[str]:
        try:
            r = subprocess.run(["docker", "exec", "-e", "PGPASSWORD", "postgres",
                                "psql", "-U", pg_user, "-d", db, "-tAc", sql],
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "PGPASSWORD": pg_password})
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None
    return run


def load_profiles(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("profiles") or {}


def _cli(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m razzfazz_common.data_bound_secrets")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("all", "keys", "database", "check", "remint"):
        s = sub.add_parser(name)
        s.add_argument("--profiles-yaml", required=True)
        if name not in ("all", "remint"):
            s.add_argument("--profile", required=True)
        if name in ("check", "remint"):
            s.add_argument("--env", required=True)
    a = p.parse_args(argv)
    try:
        profiles = load_profiles(a.profiles_yaml)
        if a.cmd == "all":
            for name, cfg in profiles.items():
                bound = declared(cfg if isinstance(cfg, dict) else {})
                for k in (bound.keys if bound else ()):
                    print(f"{name} {k}")
            return 0
        if a.cmd == "remint":
            bound_profiles = [(n, c) for n, c in profiles.items()
                              if isinstance(c, dict) and isinstance(declared(c), DataBound)]
        elif a.profile not in profiles:
            print(f"ERROR: unknown profile '{a.profile}' (not in {a.profiles_yaml}); refusing",
                  file=sys.stderr)
            return 5
        else:
            cfg = profiles.get(a.profile) or {}
            bound = declared(cfg)
    except Exception as e:  # no yaml, no file, malformed block: fail closed
        print(f"ERROR: cannot read the data-bound secret declaration ({e}); refusing",
              file=sys.stderr)
        return 4
    if a.cmd == "keys":
        for k in (bound.keys if bound else ()):
            print(k)
        return 0
    if a.cmd == "database":
        if isinstance(bound, DataBound):   # a volume form has no database
            print(bound.database)
        return 0
    from razzfazz_common.env_utils import read_env_file
    env = read_env_file(a.env)
    if a.cmd == "remint":
        return _remint(bound_profiles, docker_psql_runner(
            env.get("POSTGRES_USER") or "docker", env.get("POSTGRES_PASSWORD", "")))
    refusal = check_generation(a.profile, cfg, env_getter(env), docker_psql_runner(
        env.get("POSTGRES_USER") or "docker", env.get("POSTGRES_PASSWORD", "")),
        run_ls=docker_volume_lister())
    if refusal:
        print(refusal.message())
        return 3
    return 0


def remint_message(refusal: Refusal) -> str:
    """`rzfz init --force` generates EVERY secret anew: the keys are still in
    .env, but about to be replaced. Operator-visible: no issue numbers."""
    keys = ", ".join(refusal.empty_keys)
    if refusal.state is DbState.UNKNOWN:
        return (f"'{refusal.profile}': cannot tell whether {refusal.database} holds data "
                f"(postgres did not answer). New secrets would replace {keys}; over existing "
                f"data that makes it unreadable for good. Start the stack (rzfz start) and "
                f"re-run, or keep the secrets.")
    return (f"'{refusal.profile}': {refusal.database} holds data encrypted with {keys}. New "
            f"secrets would replace {keys} and make that data unreadable for good.")


def _remint(bound_profiles, run_sql: RunSql) -> int:
    """Every database-form profile, as if its keys were blanked (= what a
    re-mint does to the data). Volume-form keys are files, never re-minted from
    .env. 3 + one line per refusal; 0 when no declared database holds data."""
    found = False
    for name, cfg in bound_profiles:
        refusal = check_generation(name, cfg, lambda _k: "", run_sql)
        if refusal:
            print(remint_message(refusal))
            found = True
    return 3 if found else 0


if __name__ == "__main__":
    sys.exit(_cli())
