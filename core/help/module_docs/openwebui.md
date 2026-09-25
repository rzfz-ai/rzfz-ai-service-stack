# Chat (Open WebUI)

## What is Chat

Chat is the box's ChatGPT-style interface, built on Open WebUI (0.11.x in
2026.09). You talk to the models your box serves, attach documents, build
**Knowledge** collections for retrieval-augmented answers, search the web
through the box's own SearXNG, dictate and listen with the local
speech services, and run tools and functions an administrator has approved.
It runs as the `chat` profile at `https://chat.<your-domain>`
(`OPENWEBUI_DOMAIN`) and you sign in with your box account.

## How to use it on this box

1. **Where the models come from.** Every entry in the model picker is a
   deployment of the box's **LLM Manager**; Chat reaches it at
   `OWUI_OPENAI_BASE_URLS=http://llm:8080/v1` with its own API key
   (`LLM_MANAGER_OWUI_KEY`, minted by post-install). Deploy or remove a model
   in the LLM Console and the picker follows. The pre-2026.09 direct GPUStack
   endpoint is a legacy provider now — see *Migrate your OpenAI-compatible API
   access (2026.09)* in Help → Guides if you still use it.
2. **Knowledge and documents.** Uploads are converted by the box's document
   pipeline (`OWUI_CONTENT_EXTRACTION_ENGINE`, Docling when the `docling`
   profile is on) and embedded through the LLM Manager
   (`OWUI_RAG_OPENAI_BASE_URL` / `OWUI_RAG_OPENAI_KEY`). Your collections
   are yours; sharing is a workspace permission.
3. **Web search, voice, workflows.** Web search uses the box's SearXNG (the
   *Private web-search-augmented chat* tutorial); microphone and read-aloud use the `stts`
   profile; Dify workflows appear as models through the Dify pipe — *Open
   WebUI Integrations* in Help → Guides explains the picker entries and the
   admin-only Function/Tool policy.
4. **Users and roles.** Sign-in is native OIDC against Authentik
   (`ENABLE_OPENWEBUI_OIDC=true`); the groups in `OWUI_OAUTH_ALLOWED_ROLES`
   may enter, and `OWUI_DEFAULT_USER_ROLE` decides whether a first-time user
   is active or waits for approval.
5. **Per-user agents.** With the `agents` profile, your personal Hermes,
   Moltis or coding workspace shows up in the same picker as `…: Personal`
   entries — *Per-User Agents* in Help → Guides.

## Configuration

| Key | Meaning |
|---|---|
| `OPENWEBUI_DOMAIN` | Public subdomain, defaults to `chat.${MAIN_DOMAIN}`; `WEBUI_URL` is derived from it |
| `OPENWEBUI_VERSION` | Pinned `ghcr.io/open-webui/open-webui` tag |
| `OWUI_OPENAI_BASE_URLS` / `OWUI_OPENAI_KEYS` | Model providers, `;`-separated — the LLM Manager first (`http://llm:8080/v1` with `LLM_MANAGER_OWUI_KEY`) |
| `OWUI_RAG_OPENAI_BASE_URL` / `OWUI_RAG_OPENAI_KEY` | Embedding endpoint for Knowledge; empty = same as the chat provider |
| `OWUI_CONTENT_EXTRACTION_ENGINE` / `OWUI_DOCLING_SERVER_URL` | Document conversion backend (`docling` → `http://docling:5001`) |
| `ENABLE_OPENWEBUI_OIDC` / `OPENWEBUI_OIDC_CLIENT_ID` / `OPENWEBUI_OIDC_CLIENT_SECRET` | Native single sign-on against Authentik |
| `OWUI_OAUTH_ALLOWED_ROLES` | Authentik groups that may sign in (`razzfazz.ai Chat Users,razzfazz.ai Super Admins`) |
| `OWUI_DEFAULT_USER_ROLE` | Role of a user created by single sign-on; install and upgrade set `user` on SSO boxes, empty = Open WebUI's default `pending` |
| `OPENWEBUI_DB` / `OPENWEBUI_DB_USER` / `OPENWEBUI_DB_PASSWORD` | Database on the shared core Postgres (`openwebui_db`) |
| `WEBUI_SECRET_KEY` | Session signing key — rotating it signs everyone out |
| `OPENWEBUI_PORT` | Internal container port (`8080`) |

Apply a change with `docker compose up -d --force-recreate openwebui`.

## Troubleshooting

- **The model picker is empty.** In Admin → Settings → Connections the
  manager entry must show green; otherwise the key in `OWUI_OPENAI_KEYS` is
  not the one the LLM Manager knows — `rzfz post-install` re-mints it.
- **A new colleague sees "account pending".** `OWUI_DEFAULT_USER_ROLE` is
  `pending` or they are not in an allowed group; approve them under Admin →
  Users or add them to *razzfazz.ai Chat Users* in Authentik.
- **A document upload fails or yields empty text.** `docker logs docling`
  first — a converter restart fixes most of it; scanned PDFs need OCR, which
  Docling does but slowly on CPU-only boxes.
- **After an upgrade the first page load takes long.** Open WebUI runs its
  database migrations on the first start of a new version; wait for
  `docker logs openwebui` to report the server is up before you retry.
