#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2427 — plan and (on request) apply the re-embedding of Dify knowledge bases
whose embedding model no configured provider serves.

Runs INSIDE dify-api (`docker exec -i dify-api /app/api/.venv/bin/python - …`,
this file on stdin). `plan()`, `retrieval_patch()` and `apply_all()` are pure and
unit-tested; `DifyEnv` is the only part that touches Dify.

Dify 1.17.1, checked against the source at that tag:
  * `DatasetService.update_dataset(dataset_id, data, user, *, session)` — the
    keyword-only `session=` is required since 1.17 (#2662); a bare 3-argument
    call raises TypeError.
  * It authorises with `check_dataset_permission`: for permission=only_me the
    user must be `dataset.maintainer` (NOT created_by) unless the user holds the
    workspace OWNER role; the user's `current_tenant_id` must equal the
    dataset's tenant. The console PATCH as another admin is refused for that
    reason. So this acts as the maintainer (created_by when no maintainer), with
    the tenant context set from the dataset, and never falls back to another
    identity — an owner who left the workspace is reported.
  * It flushes the new provider/model/collection binding, then enqueues
    `deal_dataset_vector_index_task(dataset_id, "update")` (and the summary
    regeneration) INSIDE the caller's open transaction. The worker picks the
    task up within ~60 ms and reads the still-committed OLD row: it drops the
    old vectors, fails every document on the retired provider and reports
    success. So the adapter holds every `.delay` until its commit has landed
    (`commit_then_enqueue`), then replays them in order.
  * The "update" task re-embeds only documents in status "completed" and
    marks a failing one "error"; `update_dataset` with an unchanged model
    enqueues nothing. A dataset already on a served model with documents in
    "error" (a box the race hit) is planned "reembed" as a repair: those
    documents are set back to "completed" and the task is enqueued directly,
    after that commit.
  * After the commit a re-run sees the dataset on the served model and plans
    "ok" unless documents ended in "error": idempotent and resumable. Source
    documents/segments are never deleted here; the Dify task re-embeds the
    existing segments. --apply waits (bounded by --wait seconds) for the
    worker and reports documents left in "error" as a failure.

Nothing secret is read or printed: the report is one JSON object per dataset
with ids, names and model names; exceptions are reported by class name only
(provider errors can echo URLs and keys)."""
from __future__ import annotations

import copy
import json
import sys
import time


def plan(datasets, served, active_provider, default_embedding):
    rows = []
    for d in datasets:
        prov, model = d.get("embedding_model_provider"), d.get("embedding_model")
        # Dify authorises by dataset.maintainer; created_by is the fallback.
        owner = d.get("maintainer") or d.get("created_by")
        row = {"id": d["id"], "name": d["name"], "owner": owner,
               "from": f"{prov}/{model}", "to": "", "action": "ok", "reason": ""}
        if d.get("indexing_technique") != "high_quality" or not prov:
            rows.append({**row, "reason": "no embedding model (economy index)"})
            continue
        if model in served.get(prov, set()):
            errors = d.get("error_documents") or 0
            if errors:
                rows.append({**row, "action": "reembed", "repair": True, "to": f"{prov}/{model}",
                             "reason": f"{errors} documents in error after a re-embedding — re-index them with {model}"})
                continue
            rows.append({**row, "reason": "served as configured"})
            continue
        if model in served.get(active_provider, set()):
            rows.append({**row, "action": "repoint", "to": f"{active_provider}/{model}",
                         "reason": "same model under the active provider — repoint, vectors stay valid (#2415)"})
            continue
        if not default_embedding or default_embedding not in served.get(active_provider, set()):
            rows.append({**row, "action": "refuse",
                         "reason": "no served default embedding model on the active provider — set the Dify default first"})
            continue
        rows.append({**row, "action": "reembed", "to": f"{active_provider}/{default_embedding}",
                     "reason": f"{model} is served by no provider — re-embed with {default_embedding} as the owner"})
    return rows


def retrieval_patch(retrieval_model, provider, model, served_rerank, default_rerank):
    """The retrieval settings that name the retired provider, repointed.

    Hybrid weights carry the embedding model (`weights.vector_setting`); a
    reranker whose (provider, model) no provider serves is replaced by the
    workspace default reranker, and left alone when there is none (we do not
    invent one). Returns a copy; the input is not mutated."""
    if not retrieval_model:
        return retrieval_model
    out = copy.deepcopy(retrieval_model)
    vs = (out.get("weights") or {}).get("vector_setting")
    if isinstance(vs, dict):
        vs["embedding_provider_name"], vs["embedding_model_name"] = provider, model
    rr = out.get("reranking_model") or {}
    rp, rn = rr.get("reranking_provider_name"), rr.get("reranking_model_name")
    if rp and rn and rn not in served_rerank.get(rp, set()) and default_rerank:
        out["reranking_model"] = {"reranking_provider_name": default_rerank[0],
                                  "reranking_model_name": default_rerank[1]}
    return out


def apply_all(rows, datasets_by_id, env, provider, model, served_rerank, default_rerank):
    """Re-embed every "reembed" row as its owner; other rows pass through.

    `env` supplies `load_owner(owner_id, tenant_id)`,
    `update_dataset(dataset_id, data, owner)` and `repair_dataset(dataset_id)`.
    One failure never stops the rest and never swaps in another identity."""
    out = []
    for r in rows:
        if r["action"] != "reembed":
            out.append(r)
            continue
        d = datasets_by_id[r["id"]]
        try:
            if r.get("repair"):
                env.repair_dataset(r["id"])
                out.append({**r, "applied": True})
                continue
            if not r["owner"]:
                raise LookupError("dataset has no owner")
            owner = env.load_owner(r["owner"], d.get("tenant_id"))
            data = {"indexing_technique": "high_quality",
                    "embedding_model_provider": provider, "embedding_model": model}
            rm = retrieval_patch(d.get("retrieval_model"), provider, model, served_rerank, default_rerank)
            if rm:
                data["retrieval_model"] = rm
            env.update_dataset(r["id"], data, owner)
            out.append({**r, "applied": True})
        except Exception as exc:  # report the class only: provider errors can echo credentials
            out.append({**r, "applied": False, "error": type(exc).__name__})
    return out


def await_indexing(rows, env, limit, interval=5):
    """Bounded wait for the worker on every applied row.

    `env.index_state(dataset_id)` returns (pending, errors): documents the
    worker has not finished since our commit, and documents it left in
    "error". Each applied row gets "indexed": completed | error | pending."""
    ids = [r["id"] for r in rows if r.get("applied")]
    waited, states = 0, {}
    while ids:
        for i in ids:
            try:
                states[i] = env.index_state(i)
            except Exception as exc:  # class only, as above
                states[i] = exc
        if all(not isinstance(s, Exception) and s[0] == 0 for s in states.values()) or waited >= limit:
            break
        time.sleep(interval)
        waited += interval
    out = []
    for r in rows:
        s = states.get(r["id"])
        if s is None:
            out.append(r)
        elif isinstance(s, Exception):
            out.append({**r, "indexed": "unknown", "error": type(s).__name__})
        elif s[1]:
            out.append({**r, "indexed": "error", "error_documents": s[1]})
        elif s[0]:
            out.append({**r, "indexed": "pending", "pending_documents": s[0]})
        else:
            out.append({**r, "indexed": "completed"})
    return out


def commit_then_enqueue(module, work, commit):
    """Run `work()` with every Celery task `module` imports held back, call
    `commit()`, then enqueue what `work` asked for, in order.

    Dify enqueues from inside the caller's transaction; a worker that runs
    before the commit sees the old row. Tasks are found by attribute: a name
    ending in `_task` with a `.delay`. Nothing is enqueued when `work` or
    `commit` raises; the original `.delay` is restored either way."""
    tasks, seen = [], set()
    for name in dir(module):
        t = getattr(module, name, None) if name.endswith("_task") else None
        if t is not None and id(t) not in seen and callable(getattr(t, "delay", None)):
            seen.add(id(t))
            tasks.append(t)
    held, patched = [], []
    try:
        for t in tasks:
            original, own = t.delay, "delay" in getattr(t, "__dict__", {})
            t.delay = (lambda orig: lambda *a, **kw: held.append((orig, a, kw)))(original)
            patched.append((t, original, own))
        result = work()
        commit()
    finally:
        for t, original, own in reversed(patched):
            if own:
                t.delay = original
            else:
                del t.delay
    for orig, a, kw in held:
        orig(*a, **kw)
    return result


class DifyEnv:  # pragma: no cover - box only
    def __init__(self):
        self.since = {}  # dataset id -> DB time of our commit, for index_state

    @staticmethod
    def _db_now(session):
        from sqlalchemy import text
        return session.execute(text("SELECT LOCALTIMESTAMP")).scalar()

    def load_owner(self, owner_id, tenant_id):
        import flask
        from extensions.ext_database import db
        from models.account import Account
        account = db.session.get(Account, owner_id)
        if account is None:
            raise LookupError("owner account not found")
        # silently a no-op for a non-member. Dify 1.18 renamed it and made the
        # session explicit (set_tenant_id_with_session(tenant_id, *, session)).
        if hasattr(account, "set_tenant_id_with_session"):
            account.set_tenant_id_with_session(tenant_id, session=db.session)
        else:
            account.set_tenant_id(tenant_id)
        if account.current_tenant_id != tenant_id:
            raise LookupError("owner is not a member of the dataset's workspace")
        flask.g._login_user = account  # what libs.login.current_user resolves
        return account

    def update_dataset(self, dataset_id, data, owner):
        from extensions.ext_database import db
        try:  # Dify 1.18 moved the module into the services.knowledge package
            from services.knowledge import dataset_service
        except ImportError:
            from services import dataset_service
        from sqlalchemy.orm import Session
        # 1.17.1: session= is keyword-only and required; update_dataset only
        # flushes and enqueues the re-index before returning, so the commit is
        # ours and the enqueue waits for it (#2427).
        with Session(db.engine, expire_on_commit=False) as session:
            def commit():
                session.commit()
                self.since[dataset_id] = self._db_now(session)

            commit_then_enqueue(
                dataset_service,
                lambda: dataset_service.DatasetService.update_dataset(dataset_id, data, owner, session=session),
                commit)

    def repair_dataset(self, dataset_id):
        """Re-index a dataset already on a served model whose documents the
        race left in "error". Only documents that once finished indexing
        (completed_at set, so their segments exist) are reset; the "update"
        task re-embeds the existing segments of every completed document."""
        from extensions.ext_database import db
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from tasks.deal_dataset_vector_index_task import deal_dataset_vector_index_task
        with Session(db.engine, expire_on_commit=False) as session:
            session.execute(text(
                "UPDATE documents SET indexing_status = 'completed', error = NULL "
                "WHERE dataset_id::text = :d AND indexing_status = 'error' AND completed_at IS NOT NULL "
                "AND enabled AND NOT archived"), {"d": dataset_id})
            session.commit()
            self.since[dataset_id] = self._db_now(session)
        deal_dataset_vector_index_task.delay(dataset_id, "update")

    def index_state(self, dataset_id):
        """(pending, errors) since our commit. Dify's Document.updated_at is
        bumped (onupdate) by every status change the worker writes; a
        "completed" document not touched since our commit is still queued."""
        if dataset_id not in self.since:
            return 0, 0
        from extensions.ext_database import db
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        with Session(db.engine) as session:
            r = session.execute(text(
                "SELECT count(*) FILTER (WHERE indexing_status NOT IN ('completed', 'error') "
                "OR (indexing_status = 'completed' AND updated_at < :since)) AS pending, "
                "count(*) FILTER (WHERE indexing_status = 'error' AND updated_at >= :since) AS errors "
                "FROM documents WHERE dataset_id::text = :d AND enabled AND NOT archived"),
                {"d": dataset_id, "since": self.since[dataset_id]}).one()
            return r.pending, r.errors


def collect():  # pragma: no cover - box only
    from extensions.ext_database import db
    from sqlalchemy import text
    ds = []
    for r in db.session.execute(text(
            "SELECT id::text AS id, name, tenant_id::text AS tenant_id, created_by::text AS created_by, "
            "maintainer::text AS maintainer, indexing_technique, embedding_model_provider, embedding_model, "
            "retrieval_model, "
            # documents a re-embedding left in error (they had finished indexing once)
            "(SELECT count(*) FROM documents doc WHERE doc.dataset_id = datasets.id "
            " AND doc.indexing_status = 'error' AND doc.completed_at IS NOT NULL "
            " AND doc.enabled AND NOT doc.archived) AS error_documents "
            "FROM datasets")):
        d = dict(r._mapping)
        if isinstance(d.get("retrieval_model"), str):
            d["retrieval_model"] = json.loads(d["retrieval_model"])
        ds.append(d)

    def served(kind):
        out = {}
        for r in db.session.execute(text("SELECT provider_name, model_name FROM provider_models WHERE model_type = :t"),
                                    {"t": kind}):
            out.setdefault(r.provider_name, set()).add(r.model_name)
        return out

    def default(kind):
        r = db.session.execute(text("SELECT provider_name, model_name FROM tenant_default_models "
                                    "WHERE model_type = :t LIMIT 1"), {"t": kind}).first()
        return (r.provider_name, r.model_name) if r else None

    emb, rer = default("text-embedding"), default("rerank")
    return ds, served("text-embedding"), served("rerank"), emb, rer


def main(argv):
    only = argv[argv.index("--dataset") + 1] if "--dataset" in argv else None
    do_apply = "--apply" in argv
    wait = int(argv[argv.index("--wait") + 1]) if "--wait" in argv else 300
    from app_factory import create_app
    # Dify >= 1.14: create_app() returns (socketio_app, app), as everywhere in post-install.sh
    _, app = create_app()
    with app.app_context(), app.test_request_context():
        ds, served_emb, served_rr, emb, rer = collect()
        active, dflt = emb if emb else ("", "")
        rows = [r for r in plan(ds, served_emb, active, dflt) if not only or r["id"] == only]
        if do_apply:
            env = DifyEnv()
            rows = apply_all(rows, {d["id"]: d for d in ds}, env, active, dflt, served_rr, rer)
            rows = await_indexing(rows, env, wait)
        for r in rows:
            print(json.dumps(r, sort_keys=True))
    if do_apply:
        failed = any(r.get("applied") is False or r.get("indexed") in ("error", "unknown") for r in rows)
        return 4 if failed else 0
    return 3 if any(r["action"] == "reembed" for r in rows) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
