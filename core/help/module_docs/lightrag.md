# LightRAG

LightRAG is the graph-based retrieval-augmented-generation knowledge base in
your stack. It combines a knowledge graph with dual-level (local + global)
retrieval, so answers draw on both fine-grained facts and the broader
relationships between them — giving richer, better-connected responses than
plain vector RAG.

## How to reach it

Open [https://rag.<domain>](https://rag.<domain>) and sign in with your
razzfazz.ai single sign-on. (LightRAG lives on the `rag` subdomain.)

## First steps

1. **Insert documents** into LightRAG (via the web UI or the API) — it extracts
   entities and relationships and builds the knowledge graph as it indexes.
2. **Query** the knowledge base and choose a retrieval mode — `local` for
   focused, entity-level answers, `global` for broad, theme-level ones, or
   `hybrid` to blend both.
3. The chat and embedding models are the local models served by GPUStack
   (`https://llm.<domain>`); no external LLM key is required.

## Full upstream documentation

For the full configuration, retrieval-mode reference and API details, see the
official LightRAG documentation: [https://lightrag.github.io/](https://lightrag.github.io/)
