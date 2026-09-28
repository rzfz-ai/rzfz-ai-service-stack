# Cognee

## What is Cognee

Cognee is the GraphRAG knowledge engine in your stack — it gives your AI an
"AI memory". You feed it documents and data; it builds a knowledge graph
(backed by an embedded graph database — ladybug, cognee's successor to Kuzu) plus vector embeddings so that
later questions are answered with contextual, connected reasoning instead of
isolated snippets. It runs as two containers behind the `cognee` profile —
the Cognee FastAPI backend plus a separate frontend — and is **on by default
(#520)** in every install preset except `worker-box`.

## How to use it on this box

1. Open `https://cognee.<domain>` and sign in with your razzfazz.ai single
   sign-on. The UI is served in front of the Cognee FastAPI backend.
2. **Add data** to a dataset — upload text or documents through the UI (or
   the API) that you want Cognee to remember.
3. **Cognify** the dataset — this is the processing step that extracts
   entities and relationships and builds the knowledge graph plus embeddings.
   Wait for it to finish (the dataset turns green when ready).
4. **Search / ask** against the dataset once processing completes to get
   graph-grounded answers.
5. The chat, embedding and reasoning models are the local models served by
   this box's own GPUStack (`https://gpustack.<domain>`); no external LLM key is
   required, and it works fully offline.

## Configuration

| Key | Meaning |
|---|---|
| `COGNEE_DOMAIN` | Public subdomain, defaults to `cognee.${MAIN_DOMAIN}` |
| `COGNEE_PORT` / `COGNEE_FRONTEND_PORT` | Internal container ports (defaults `8011` / `8012`) |
| `COGNEE_DB` | Postgres database name (`cognee_db`) on the shared core Postgres |
| `COGNEE_LLM_MODEL` / `COGNEE_EMBEDDING_MODEL` | Model names as served by this box's LLM Manager. `rzfz post-install` writes the fleet defaults here (`openai/qwen3.6` for chat, `qwen3-embedding` for embeddings); the `openai/` prefix is what litellm needs for routing |
| `COGNEE_EMBEDDING_DIM` | Embedding vector dimension — must match the deployed embedding model |
| `COGNEE_LLM_ENDPOINT` / `COGNEE_EMBEDDING_ENDPOINT` (+ `*_API_KEY`) | Override targets if you point Cognee at something other than this box's own LLM Manager |
| `COGNEE_ADMIN_PASSWORD` | Initial admin password, set at install time |
| `COGNEE_CLIENT_SECRET` | Authentik OIDC client secret for SSO |
| `COGNEE_MCP_API_KEY` | API key for the Cognee MCP server (used by coding agents that read/write Cognee's memory via MCP) |
| `COGNEE_ACCESS_CONTROL` | `true` (default) enables per-user, per-dataset graph isolation; `false` makes every user share one global graph — see the migration note below for why this matters |
| `COGNEE_KUZU_AUTO_MIGRATE` | `true` (default) lets the stack auto-migrate the on-disk Kuzu store across format-incompatible Cognee upgrades on start-up |
| `COGNEE_LOG_LEVEL` | Container log verbosity |

Apply a change with `docker compose up -d --force-recreate cognee cognee-frontend`.

## Troubleshooting: the knowledge graph shows "No graph data available"

After a Cognee upgrade the graph visualization can read empty even though
your data is still there. There are two independent causes; the stack
detects both on start-up and writes a note to the `cognee` container log
(`docker compose logs cognee`).

1. **Old on-disk graph format (after a 0.16 → 0.17 upgrade).** The graph
   store is left in the previous storage format, so the new engine opens it
   and reads zero nodes. On start-up the stack **automatically backs up the
   store and migrates it** to the current format (look for
   `[cognee-kuzu-migrate] migration OK` in the log). If your box has no
   outbound internet during that one-time migration, or you have set
   `COGNEE_KUZU_AUTO_MIGRATE=false`, the log prints the exact
   `ladybug_migrate …` command to run by hand under a maintenance window.
   The original store is always kept as a `*_old_` / `*_pre186bak_` backup
   directory, so the migration is reversible.

2. **Per-dataset graph is empty under access control.** With access control
   on (the default), the visualization reads a per-user, per-dataset graph
   store. On a box whose data pre-dates access control, that per-dataset
   store can be empty while the real graph lives in the global store — so
   the viz reads empty and the log shows a `WARNING (#186 Defect 2)`. To
   recover, either:
   - set `COGNEE_ACCESS_CONTROL=false` in `.env` (single-tenant boxes — the
     viz then reads the global graph directly), then
     `docker compose up -d --force-recreate cognee`; or
   - re-ingest the documents into a **fresh** dataset and run **cognify**
     again (re-cognifying an already-processed dataset is skipped as a
     no-op, so a new dataset is required to repopulate the per-dataset
     store).
