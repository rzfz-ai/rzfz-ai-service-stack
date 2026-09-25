#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2435 — is every RAG index in the same vector space as the engine that queries it?

An embedding engine whose pooling (or model) differs from the one an index was
built with still answers, and retrieval still returns neighbours — at flat,
near-uniform scores, ranking zinc level with coffee (0.208, 2026-09-23). Only
re-embedding stored chunks and comparing them with their stored vectors shows
it. This does exactly that, per index, straight through the box's LLM endpoint:
never through Dify, whose CacheEmbedding would hand back the vectors computed at
index time and make every index "match".

Output: one line per index, `VERDICT|text`, VERDICT in PASS FAIL WARN INFO.
Environment: POSTGRES_USER, DIFY_DB, OPENWEBUI_DB, LIGHTRAG_DB, COMPOSE_PROFILES,
EMBED_KEY, EMBED_URL, DEFAULT_EMBED_MODEL, LIGHTRAG_EMBEDDING_MODEL,
SAMPLES (default 3), MAX_INDEXES (default 10), MATCH (default 0.99).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import urllib.request

IDENT = re.compile(r"^[A-Za-z0-9_]{1,200}$")
ENV = os.environ


def psql(db: str, sql: str) -> str | None:
    try:
        r = subprocess.run(["docker", "exec", "postgres", "psql", "-U", ENV.get("POSTGRES_USER") or "docker",
                            "-d", db, "-tAc", sql], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def samples(db: str, table: str, text_col: str, vec_col: str, where: str = "") -> list[tuple[str, list[float]]]:
    n = int(ENV.get("SAMPLES") or 3)
    cond = f"{text_col} IS NOT NULL AND length({text_col}) > 0" + (f" AND {where}" if where else "")
    out = psql(db, f"SELECT coalesce(json_agg(json_build_object('t', t, 'v', v)), '[]') FROM "
                   f"(SELECT {text_col} AS t, {vec_col}::text AS v FROM {table} WHERE {cond} "
                   f"ORDER BY random() LIMIT {n}) s;")
    if not out:
        return []
    rows = []
    for r in json.loads(out):
        try:
            rows.append((r["t"], [float(x) for x in json.loads(r["v"])]))
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def embed(model: str, text: str) -> list[float] | None:
    body = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(ENV.get("EMBED_URL") or "", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {ENV.get('EMBED_KEY', '')}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return [float(x) for x in json.load(r)["data"][0]["embedding"]]
    except Exception:
        return None


def cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def verdict(label: str, model: str, rows: list[tuple[str, list[float]]]) -> str:
    if not rows:
        return f"INFO|{label}: no stored chunks to compare"
    got = []
    for text, stored in rows:
        fresh = embed(model, text)
        if fresh is None:
            return f"WARN|{label}: {model} did not answer an embedding request — space UNVERIFIED"
        if len(fresh) != len(stored):
            return (f"FAIL|{label}: stored vectors are {len(stored)}-dim, {model} emits {len(fresh)} — "
                    f"built with another model; re-index it (#2435)")
        got.append(cos(stored, fresh))
    lo, hi, match = min(got), max(got), float(ENV.get("MATCH") or 0.99)
    if lo >= match:
        return f"PASS|{label}: stored vectors reproduce under {model} (cos {lo:.4f}–{hi:.4f})"
    return (f"FAIL|{label}: built in another vector space than {model} serves now (cos {lo:.2f}–{hi:.2f}) — "
            f"retrieval is degraded; re-index it, or set the embedding deployment's pooling to the one "
            f"it was built with (#2435)")


def indexes() -> list[tuple[str, str, str, str, str, str, str]]:
    """(label, model, db, table, text_col, vec_col, where) per index, bounded."""
    profiles = set((ENV.get("COMPOSE_PROFILES") or "").split(","))
    default = ENV.get("DEFAULT_EMBED_MODEL") or "qwen3-embedding"
    found: list = []
    if "dify" in profiles:
        db = ENV.get("DIFY_DB") or "dify_db"
        out = psql(db, "SELECT coalesce(json_agg(json_build_object('n', name, 'm', embedding_model, 'i', index_struct)), '[]') "
                       "FROM datasets WHERE indexing_technique = 'high_quality';") or "[]"
        for d in json.loads(out):
            try:
                prefix = (json.loads(d.get("i") or "{}").get("vector_store") or {}).get("class_prefix") or ""
            except ValueError:
                prefix = ""
            if not IDENT.match(prefix):
                found.append((f"Dify knowledge base '{d['n']}'", "", "", "", "", "", "NOINDEX"))
                continue
            found.append((f"Dify knowledge base '{d['n']}'", d.get("m") or default, db,
                          f"embedding_{prefix.lower()}", "text", "embedding", ""))
    if "chat" in profiles:
        db = ENV.get("OPENWEBUI_DB") or "openwebui_db"
        if psql(db, "SELECT to_regclass('public.document_chunk') IS NOT NULL;") == "t":
            for c in (psql(db, "SELECT DISTINCT collection_name FROM document_chunk LIMIT 50;") or "").splitlines():
                if IDENT.match(c.replace("-", "_")):
                    found.append((f"Open WebUI collection '{c}'", default, db, "document_chunk", "text", "vector",
                                  f"collection_name = '{c}'"))
    if "lightrag" in profiles:
        db = ENV.get("LIGHTRAG_DB") or "lightrag_db"
        for t in (psql(db, "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'lightrag_vdb_chunks%';") or "").splitlines():
            if IDENT.match(t):
                found.append((f"LightRAG table {t}", ENV.get("LIGHTRAG_EMBEDDING_MODEL") or default, db, t,
                              "content", "content_vector", ""))
    return found


def pooling_lines() -> list[str]:
    """How each embedding deployment pools, from GET /api/deployments (if given)."""
    try:
        deps = json.loads(ENV.get("DEPLOYMENTS_JSON") or "[]")
    except ValueError:
        return []
    deps = deps if isinstance(deps, list) else (deps.get("deployments") or deps.get("items") or [])
    out = []
    for d in deps:
        if not isinstance(d, dict) or (d.get("task") or "").strip().lower() not in ("embed", "embedding", "embeddings"):
            continue
        params = d.get("params") or {}
        pin = next((v for k, v in params.items() if str(k).lstrip("-").replace("_", "-").lower() == "pooling"), None)
        how = f"pooling {pin} (set on the deployment)" if pin else "the model's own pooling"
        out.append(f"INFO|{d.get('model_name') or '?'}: embeds with {how}")
    return out


def main() -> int:
    for line in pooling_lines():
        print(line)
    if not ENV.get("EMBED_KEY") or not ENV.get("EMBED_URL"):
        print("WARN|embedding spaces: no LLM endpoint key on this box — per-index check UNVERIFIED")
        return 0
    limit = int(ENV.get("MAX_INDEXES") or 10)
    checked = 0
    for label, model, db, table, tcol, vcol, where in indexes():
        if where == "NOINDEX":
            print(f"INFO|{label}: not indexed (no vector collection) — it cannot retrieve")
            continue
        if checked >= limit:
            print(f"INFO|further indexes not checked (limit {limit})")
            break
        checked += 1
        print(verdict(label, model, samples(db, table, tcol, vcol, where)))
    if "cognee" in (ENV.get("COMPOSE_PROFILES") or "").split(","):
        print("INFO|cognee: embedding space not checked by this row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
