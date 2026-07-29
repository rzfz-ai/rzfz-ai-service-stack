# Cognee

Cognee is the GraphRAG knowledge engine in your stack — it gives your AI an
"AI memory". You feed it documents and data; it builds a knowledge graph (backed
by Kuzu) plus vector embeddings so that later questions are answered with
contextual, connected reasoning instead of isolated snippets.

## How to reach it

Open [https://cognee.<domain>](https://cognee.<domain>) and sign in with your
razzfazz.ai single sign-on. The UI is served in front of the Cognee FastAPI
backend.

## First steps

1. **Add data** to a dataset — upload text or documents through the UI (or the
   API) that you want Cognee to remember.
2. **Cognify** the dataset — this is the processing step that extracts entities
   and relationships and builds the knowledge graph plus embeddings. Wait for it
   to finish (the dataset turns green when ready).
3. **Search / ask** against the dataset once processing completes to get
   graph-grounded answers.
4. The chat, embedding and reasoning models are the local models served by
   GPUStack (`https://llm.<domain>`); no external LLM key is required.

## Troubleshooting: the knowledge graph shows "No graph data available"

After a Cognee upgrade the graph visualization can read empty even though your
data is still there. There are two independent causes; the stack detects both on
start-up and writes a note to the `cognee` container log
(`docker compose logs cognee`).

1. **Old on-disk graph format (after a 0.16 → 0.17 upgrade).** The graph store is
   left in the previous storage format, so the new engine opens it and reads zero
   nodes. On start-up the stack **automatically backs up the store and migrates
   it** to the current format (look for `[cognee-kuzu-migrate] migration OK` in
   the log). If your box has no outbound internet during that one-time migration,
   or you have set `COGNEE_KUZU_AUTO_MIGRATE=false`, the log prints the exact
   `ladybug_migrate …` command to run by hand under a maintenance window. The
   original store is always kept as a `*_old_` / `*_pre186bak_` backup directory,
   so the migration is reversible.

2. **Per-dataset graph is empty under access control.** With access control on
   (the default), the visualization reads a per-user, per-dataset graph store. On
   a box whose data pre-dates access control, that per-dataset store can be empty
   while the real graph lives in the global store — so the viz reads empty and the
   log shows a `WARNING (#186 Defect 2)`. To recover, either:
   - set `COGNEE_ACCESS_CONTROL=false` in `.env` (single-tenant boxes — the viz
     then reads the global graph directly), then
     `docker compose up -d --force-recreate cognee`; or
   - re-ingest the documents into a **fresh** dataset and run **cognify** again
     (re-cognifying an already-processed dataset is skipped as a no-op, so a new
     dataset is required to repopulate the per-dataset store).

## Full upstream documentation

For the complete API reference, pipeline concepts and configuration options,
see the official Cognee documentation: [https://docs.cognee.ai/](https://docs.cognee.ai/)
