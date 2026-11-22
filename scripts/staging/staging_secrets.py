#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""scripts/staging/staging_secrets.py — #2524: the two secret classes on staging.

  --carry     copy carry_from_prod_keys from production's .env copy into staging's
              .env: keys that DECRYPT restored data (without them staging
              cannot read what it restored).
  --carry --db-roles
              also copy every DB-role password key (POSTGRES_PASSWORD, every
              *_DB_PASSWORD, POSTGRES_EXPORTER_PASSWORD), derived from the
              stack's .env template. The full restore replays production's
              cluster dump, role passwords included, so after it only
              production's passwords authenticate. Only AFTER the restore: before
              it, staging's cluster still holds staging's own roles. A key
              production's .env does not set (absent or empty) keeps staging's own.
              Also every restored_account_password_keys entry of the config: the
              app admin credentials whose account lives in a restored database
              (the restore brings production's accounts with production's
              passwords), under the same rule.
  --distinct  for external_secret_keys: a staging value EQUAL to production's is
              blanked (staging's own value comes from the secret store; the
              restore must never leave production's outward credential on staging).
              With --check: report, change nothing, exit 1 if any is equal.

Prints key NAMES only. Never a value. Exit 2 (nothing written) when production's
.env copy is missing or lacks a carry key; a carried value keeps its quoting.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "common"))
from razzfazz_common.env_utils import read_env_file, write_env_values_locked  # noqa: E402


ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "config" / ".env.example"
# The superuser (POSTGRES_PASSWORD), the monitoring role (POSTGRES_EXPORTER_PASSWORD) and
# every per-service role (<X>_DB_PASSWORD, the pair of the #2401 pre-flight's <X>_DB_USER).
_DB_ROLE_PASSWORD = re.compile(r"^(?:POSTGRES(?:_[A-Z0-9_]+)?|[A-Z0-9_]+_DB)_PASSWORD$")


def db_role_password_keys(env_example: Path, prod_env: Path) -> list:
    """Every DB-role password key the stack's .env template declares, and every one
    production's .env carries (a role a migration added without a template line)."""
    if not env_example.is_file():
        raise ValueError(f"the stack's .env template not found: {env_example}")
    _require_production(prod_env)
    names = set(read_env_file(env_example)) | set(read_env_file(prod_env))
    return sorted(k for k in names if _DB_ROLE_PASSWORD.match(k))


def _write(path: Path, updates: dict) -> None:
    mode = path.stat().st_mode & 0o777
    write_env_values_locked(path, updates)
    os.chmod(path, mode)


def _raw_values(path: Path) -> dict:
    """KEY -> the right-hand side exactly as written (quotes, `$`, ` #` and all).

    The writer puts a value back verbatim, so a parsed value would lose its
    quoting: `"ab #cd$EF gh"` written unquoted reads back as `ab`.
    """
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = line.split("=", 1)
        out[key.strip()] = raw                    # the LAST occurrence wins, as in Compose and read_env_file
    return out


def _require_production(prod_env: Path) -> None:
    if not prod_env.is_file():
        raise ValueError(f"production's .env copy not found: {prod_env}")


def carry(staging_env: Path, prod_env: Path, keys: list, role_keys: tuple = ()) -> tuple:
    """keys: required in production's .env. role_keys: carried when production sets them.

    Returns (the keys changed, the role keys production leaves absent or empty)."""
    _require_production(prod_env)
    s, p, raw = read_env_file(staging_env), read_env_file(prod_env), _raw_values(prod_env)
    missing = sorted(k for k in keys if k not in p)
    if missing:
        raise ValueError(f"production's .env copy lacks carry key(s): {', '.join(missing)}")
    wanted = list(dict.fromkeys([*keys, *(k for k in role_keys if p.get(k))]))
    kept = sorted(k for k in role_keys if k not in wanted)
    updates = {k: raw[k] for k in wanted if s.get(k) != p[k]}
    if updates:
        _write(staging_env, updates)
    return sorted(updates), kept


def enforce_distinct(staging_env: Path, prod_env: Path, keys: list, check_only: bool) -> list:
    _require_production(prod_env)
    s, p = read_env_file(staging_env), read_env_file(prod_env)
    equal = sorted(k for k in keys if s.get(k) and s.get(k) == p.get(k))
    if equal and not check_only:
        _write(staging_env, {k: "" for k in equal})
    return equal


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging-env", required=True, type=Path)
    ap.add_argument("--production-env", required=True, type=Path)
    ap.add_argument("--config", required=True, type=Path)
    m = ap.add_mutually_exclusive_group(required=True)
    m.add_argument("--carry", action="store_true")
    m.add_argument("--distinct", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--db-roles", action="store_true",
                    help="with --carry, after the restore: also carry the DB-role password keys "
                         "and the restored accounts' admin credentials")
    ap.add_argument("--env-example", type=Path, default=ENV_EXAMPLE,
                    help="the stack's .env template the DB-role keys are derived from")
    a = ap.parse_args(argv)
    if a.db_roles and not a.carry:
        ap.error("--db-roles goes with --carry")
    cfg = yaml.safe_load(a.config.read_text(encoding="utf-8"))
    try:
        if a.carry:
            keys = cfg.get("carry_from_prod_keys", [])
            role_keys = (db_role_password_keys(a.env_example, a.production_env)
                         + list(cfg.get("restored_account_password_keys", [])) if a.db_roles else [])
            outward = sorted(set(cfg.get("external_secret_keys", [])) & (set(keys) | set(role_keys)))
            if outward:
                raise ValueError(f"key(s) both carried and outward in the config: {', '.join(outward)}")
            changed, kept = carry(a.staging_env, a.production_env, keys, role_keys)
            if kept:
                print(f"[staging-secrets] kept staging's own (production sets no value): {', '.join(kept)}")
            print(f"[staging-secrets] carried from production: {', '.join(changed) or 'nothing to change'}")
            return 0
        equal = enforce_distinct(a.staging_env, a.production_env, cfg.get("external_secret_keys", []), a.check)
    except ValueError as e:
        print(f"[staging-secrets] ERROR: {e} — nothing written", file=sys.stderr)
        return 2
    if not equal:
        print("[staging-secrets] no outward key equals production's")
        return 0
    verb = "EQUAL to production (not changed, --check)" if a.check else "blanked (was production's value)"
    print(f"[staging-secrets] {verb}: {', '.join(equal)} — set staging's own value from the secret store")
    return 1 if a.check else 0


if __name__ == "__main__":
    sys.exit(main())
