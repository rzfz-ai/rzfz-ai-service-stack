#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""scripts/staging/rewrite_domain.py — #2524: move a restored production dataset onto
the staging domain.

A staging box restores production's newest backup. What the Authentik blueprints and
staging's own .env render converges by itself (the init takes the full path on
a domain change, #183). What stays behind are URLs stored as DATA — a launch
URL, Open WebUI's config row, a Dify workflow's HTTP node that calls
https://llm.<production domain>/v1. Left alone, a staging workflow calls PRODUCTION.

Rules:
  * only URL authorities (after `://`) and whole-value host columns are
    rewritten, never free text: `razzfazz.ai Super Admins` is a group NAME;
  * idempotent by construction: a host already under `<label>.<production domain>` does not
    match the pattern, so a second run changes nothing;
  * `--scan` lists every text/json column that still holds a production URL, so the
    target list is completed from a measurement on the first restore.

Usage:
  rewrite_domain.py --config STAGING.yaml --plan    # print the SQL
  rewrite_domain.py --config STAGING.yaml --apply   # rewrite targets
  rewrite_domain.py --config STAGING.yaml --scan    # residual report
Exit: 0 clean; 1 a residual outside the content columns; 2 bad config/arguments.
"""
from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9-]+(\.[a-z0-9-]+)+$")
LABEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")   # a leading digit would read as a backref
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
KINDS = ("url", "host", "json-url")


@dataclasses.dataclass(frozen=True)
class Target:
    db: str
    table: str
    column: str
    kind: str
    cast: str = ""

    def validate(self) -> None:
        for part in (self.db, self.table, self.column):
            if not IDENT_RE.match(part):
                raise ValueError(f"not a plain SQL identifier: {part!r}")
        if self.kind not in KINDS:
            raise ValueError(f"{self.table}.{self.column}: kind {self.kind!r} not in {KINDS}")
        if self.kind == "json-url" and self.cast not in ("json", "jsonb"):
            raise ValueError(f"{self.table}.{self.column}: json-url needs cast: json|jsonb")


@dataclasses.dataclass(frozen=True)
class Residual:
    db: str
    schema: str
    table: str
    column: str
    count: int
    is_content: bool


@dataclasses.dataclass
class Config:
    domain: str
    label: str
    container: str
    pg_user: str
    databases: list
    targets: list
    content: list            # [(db, table, column)]


def patterns(domain: str, label: str) -> tuple[str, str]:
    """(url, host) patterns, valid in Python `re` AND PostgreSQL ARE (>= 9.6)."""
    if not DOMAIN_RE.match(domain):
        raise ValueError(f"not a domain: {domain!r}")
    if not LABEL_RE.match(label):
        raise ValueError(f"not a lowercase DNS label starting with a letter: {label!r}")
    p = domain.replace(".", r"\.")
    lab = label.replace(".", r"\.")
    tail = r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])"      # not a label of a longer name
    labels = r"(?:[A-Za-z0-9-]+\.)*"
    # Not already staging: the label right before the domain is exactly <label>
    # (a whole label, so `mystaging.` is still production). A lookahead from the
    # start of the host, because PostgreSQL's lookbehind cannot see `^`.
    fresh = rf"(?!{labels}{lab}\.{p}{tail})"
    # `://`, also JSON-escaped as `:\/\/`, then optional userinfo (`user:pw@`);
    # userinfo holds no `/?#`, so a mail address in a query string is not one.
    scheme = r"(:(?:\\?/){2}(?:[^/?#@\s\"'\\]*@)?)"
    url = rf"{scheme}{fresh}({labels}){p}{tail}"
    host = rf"^{fresh}({labels}){p}$"
    return url, host


def scan_patterns(domain: str, label: str) -> list:
    """Scan-only forms of a production URL that --apply does not rewrite, so a
    hit is a RESIDUAL to fix by hand (or a new target): a percent-encoded
    `%3A%2F%2F`, a protocol-relative `//host`, and a scheme-less `host/` or
    `host:port`. Same "already staging" exclusion; valid in both engines."""
    url, _ = patterns(domain, label)              # validates domain and label
    p = domain.replace(".", r"\.")
    lab = label.replace(".", r"\.")
    tail = r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])"
    labels = r"(?:[A-Za-z0-9-]+\.)*"
    fresh = rf"(?!{labels}{lab}\.{p}{tail})"
    return [
        rf"%3A%2F%2F{fresh}{labels}{p}{tail}",                    # percent-encoded ://
        rf"(^|[^A-Za-z0-9.-])//{fresh}{labels}{p}{tail}",          # protocol-relative
        rf"(^|[^A-Za-z0-9.@/-]){fresh}{labels}{p}[:/]",            # scheme-less host/ or host:port
    ]


def rewrite_text(value: str, domain: str, label: str) -> str:
    url, _ = patterns(domain, label)
    return re.sub(url, lambda m: f"{m.group(1)}{m.group(2)}{label}.{domain}", value, flags=re.I)


def rewrite_host(value: str, domain: str, label: str) -> str:
    _, host = patterns(domain, label)
    return re.sub(host, lambda m: f"{m.group(1)}{label}.{domain}", value, flags=re.I)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def update_sql(t: Target, domain: str, label: str) -> str:
    t.validate()
    url, host = patterns(domain, label)
    new = f"{label}.{domain}"
    col = _q(t.column)
    if t.kind == "url":
        expr = f"regexp_replace({col}, {_lit(url)}, {_lit(chr(92) + '1' + chr(92) + '2' + new)}, 'gi')"
        where = f"{col} ~* {_lit(url)}"
    elif t.kind == "host":
        expr = f"regexp_replace({col}, {_lit(host)}, {_lit(chr(92) + '1' + new)}, 'i')"
        where = f"{col} ~* {_lit(host)}"
    else:
        expr = (f"(regexp_replace({col}::text, {_lit(url)}, "
                f"{_lit(chr(92) + '1' + chr(92) + '2' + new)}, 'gi'))::{t.cast}")
        where = f"{col}::text ~* {_lit(url)}"
    return (f"WITH u AS (UPDATE {_q(t.table)} SET {col} = {expr} WHERE {where} RETURNING 1) "
            f"SELECT count(*) FROM u;")


COLUMNS_SQL = ("SELECT table_schema, table_name, column_name FROM information_schema.columns "
               "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
               "AND (data_type IN ('text', 'character varying', 'character', 'json', 'jsonb') "
               "OR (data_type = 'ARRAY' AND udt_name IN ('_text', '_varchar', '_bpchar', '_json', '_jsonb', '_citext')) "
               "OR (data_type = 'USER-DEFINED' AND udt_name = 'citext')) "
               "ORDER BY 1, 2, 3;")


def count_sql(schema: str, table: str, column: str, domain: str, label: str) -> str:
    url, host = patterns(domain, label)
    c = f"{_q(column)}::text"
    where = " OR ".join(f"{c} ~* {_lit(rx)}" for rx in [url, host, *scan_patterns(domain, label)])
    return f"SELECT count(*) FROM {_q(schema)}.{_q(table)} WHERE {where};"


def load_config(path) -> Config:
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    targets = [Target(**t) for t in d.get("targets", [])]
    for t in targets:
        t.validate()
    patterns(d["prod_domain"], d["staging_label"])
    return Config(domain=d["prod_domain"], label=d["staging_label"],
                  container=d.get("postgres_container", "postgres"),
                  pg_user=d.get("pg_user", "docker"),
                  databases=list(d.get("databases", [])), targets=targets,
                  content=[(c["db"], c["table"], c["column"]) for c in d.get("content_columns", [])])


def apply_targets(exe, cfg: Config) -> dict:
    out = {}
    for t in cfg.targets:
        rows = exe.query(t.db, update_sql(t, cfg.domain, cfg.label))
        out[f"{t.db}.{t.table}.{t.column}"] = int(rows[0][0]) if rows else 0
    return out


def scan(exe, cfg: Config) -> list:
    found = []
    content = set(cfg.content)
    for db in cfg.databases:
        for schema, table, column in exe.query(db, COLUMNS_SQL, read_only=True):
            n = int(exe.query(db, count_sql(schema, table, column, cfg.domain, cfg.label), read_only=True)[0][0])
            if n:
                found.append(Residual(db, schema, table, column, n, (db, table, column) in content))
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--scan", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except (ValueError, KeyError, TypeError) as e:
        print(f"[rewrite-domain] ERROR: bad config: {e}", file=sys.stderr)
        return 2
    if a.plan:
        for t in cfg.targets:
            print(f"-- {t.db}\n{update_sql(t, cfg.domain, cfg.label)}")
        return 0
    from pg_exec import Psql
    exe = Psql(cfg.container, cfg.pg_user)
    if a.apply:
        for key, n in apply_targets(exe, cfg).items():
            print(f"[rewrite-domain] {key}: {n} row(s) rewritten")
        return 0
    bad = 0
    for r in scan(exe, cfg):
        tag = "content (reported, never rewritten)" if r.is_content else "RESIDUAL"
        print(f"[rewrite-domain] {tag}: {r.db} {r.schema}.{r.table}.{r.column} = {r.count}")
        bad += 0 if r.is_content else 1
    print(f"[rewrite-domain] scan: {bad} residual column(s) outside the content list")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
