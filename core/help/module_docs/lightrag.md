# LightRAG

## What is LightRAG

LightRAG is the graph-based retrieval-augmented-generation knowledge base in
your stack. It combines a knowledge graph with dual-level (local + global)
retrieval, so answers draw on both fine-grained facts and the broader
relationships between them — giving richer, better-connected responses than
plain vector-similarity RAG alone. It runs as a single container behind the
`lightrag` profile (marked EXPERIMENTAL), with its own `lightrag_db`
database on the box's shared Postgres.

## How to use it on this box

1. Open `https://rag.<domain>` (the `LIGHTRAG_DOMAIN` setting below, on the
   `rag` subdomain) and sign in. LightRAG's WebUI has its own login on top
   of the SSO gate, using the account(s) configured via
   `LIGHTRAG_AUTH_ACCOUNTS` — Authentik gets you to the app, LightRAG's own
   auth gets you into the app.
2. **Insert documents** into LightRAG (via the web UI or its API) — it
   extracts entities and relationships and builds the knowledge graph as it
   indexes, rather than only building flat vector embeddings.
3. **Query** the knowledge base and pick a retrieval mode:
   - `local` — focused, entity-level answers (good for "what is X").
   - `global` — broad, theme-level answers (good for "what are the main
     themes across these documents").
   - `hybrid` — blends both, LightRAG's usual recommended default.
4. For programmatic access (from a script, or from a Dify HTTP Request node),
   authenticate with the `X-API-Key` header using the `LIGHTRAG_API_KEY`
   value from `.env`.
5. Chat, embedding (and, if enabled, rerank) all run against this box's own
   GPUStack — no external LLM key is required, and it works fully offline.

## Configuration

| Key | Meaning |
|---|---|
| `LIGHTRAG_DOMAIN` | Public subdomain, defaults to `rag.${MAIN_DOMAIN}` |
| `LIGHTRAG_PORT` | Internal container port (default `9621`) |
| `LIGHTRAG_VERSION` | Pinned `hkuds/lightrag` image tag |
| `LIGHTRAG_DB` | Postgres database name (`lightrag_db`) on the shared core Postgres |
| `LIGHTRAG_API_KEY` | Generated at install time by `razzfazz-init.sh`; required in the `X-API-Key` header for programmatic (non-WebUI) access |
| `LIGHTRAG_TOKEN_SECRET` / `LIGHTRAG_AUTH_ACCOUNTS` | LightRAG WebUI's own login credentials, separate from Authentik SSO |
| `LIGHTRAG_LLM_MODEL` / `LIGHTRAG_EMBEDDING_MODEL` | Model names as deployed in GPUStack (defaults match `razzfazz-post-install.sh`'s standard preset: `qwen3.6` for chat, `qwen3-embedding` for embeddings) |
| `LIGHTRAG_EMBEDDING_DIM` | Embedding vector dimension — must match the deployed embedding model |
| `LIGHTRAG_RERANK_BINDING` | Set to `null` to disable reranking (the default); set to a rerank model to enable it |
| `LIGHTRAG_LLM_ENDPOINT` / `LIGHTRAG_EMBEDDING_ENDPOINT` / `LIGHTRAG_RERANK_ENDPOINT` | Override targets if you point LightRAG at something other than this box's own GPUStack |
| `LIGHTRAG_EMBEDDING_TIMEOUT` / `LIGHTRAG_LLM_TIMEOUT` | Request timeouts in seconds (defaults `120` / `300`) — worth raising on slower hardware for large documents |

Apply a change with `docker compose up -d --force-recreate lightrag`.

## Troubleshooting

- **WebUI prompts for credentials you don't have.** That's LightRAG's own
  login layer (`LIGHTRAG_AUTH_ACCOUNTS`), separate from the SSO screen —
  check `.env` for the generated account, or `rzfz setup --status` if it's
  unclear which credentials were provisioned.
- **Indexing seems to hang or time out on large documents.** Raise
  `LIGHTRAG_EMBEDDING_TIMEOUT` / `LIGHTRAG_LLM_TIMEOUT` — the defaults are
  tuned for typical hardware, not necessarily this box's.
- **Query results seem to ignore relationships between entities.** Try
  `global` or `hybrid` mode instead of `local` — `local` deliberately favors
  precise, narrow matches over the broader graph structure.
