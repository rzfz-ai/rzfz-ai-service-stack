# Dify

## What is Dify

Dify is the workflow-automation, RAG and agentic-AI platform in your stack.
You build LLM apps visually — chatbots, agents and multi-step workflows —
wiring together prompts, your knowledge bases, tools and models, then publish
the result as an app and/or as an HTTP/OpenAI-compatible API that other
services can call.

On this box Dify is not one container but a small cluster of six, all
launched together by the `dify` compose profile:

- **`dify-api`** — the main backend: app config, workflow execution, the
  console API. `mem_limit: 2g`.
- **`dify-worker`** and **`dify-worker-beat`** — Celery worker + scheduler for
  background jobs (document indexing, scheduled workflow triggers).
- **`dify-web`** — the console/chat frontend the browser talks to.
- **`dify-sandbox`** — the isolated code-execution sandbox used by "Code"
  nodes in workflows.
- **`dify-plugin-daemon`** — runs Dify's plugin/tool ecosystem. On a
  connected box the standard plugins come from Dify's marketplace; on an
  air-gapped box they come from the offline package, which carries the
  plugin packages and the daemon's dependency cache, and the provisioning
  step installs them from there without reaching the internet.
- **`dify-init-permissions`** — a one-shot init container that `chown`s the
  shared storage volume on first start, then exits.

All six share the box's core Postgres (`dify_db`, `dify_plugin_db`) and
Valkey instances — there is no separate Dify database to manage.

## How to use it on this box

1. Open `https://dify.<domain>` (the `DIFY_DOMAIN` setting below) and sign in
   with razzfazz.ai single sign-on — Authentik gates first-time access exactly
   like every other module.
2. From **Studio**, create an app: start from a **Chatbot** or **Agent**
   template, or an empty **Workflow** to build a pipeline node-by-node.
3. Under **Settings → Model Provider**, the box pre-wires the local models
   served by this box's own GPUStack (`https://gpustack.<domain>`, container
   address `http://gpustack:9090`), so you can pick a chat and an embedding
   model without ever pasting an external API key. This works with zero
   internet access — GPUStack is local.
4. Add a **Knowledge** base to enable RAG: upload documents (Dify calls out
   to this box's Gotenberg/Docling/Tika services for extraction, depending on
   what's enabled), let Dify index them, then reference the knowledge base
   from your app for grounded answers.
5. Any outbound HTTP a workflow node performs (an "HTTP Request" node, a
   plugin, a Code node reaching the internet) is routed through the box's
   SSRF proxy (`caddy:8195`) — this is transparent to you, but it's why a
   workflow can reach `gotenberg`, `lightrag`, `cognee`, `searxng` and the
   other in-stack services by name even though they aren't publicly exposed:
   see the `DIFY_DOC_TOOLS_SSRF_ALLOW` allow-list below.
6. **Publish** the app to get a shareable web app and an API endpoint other
   services (or your own scripts) can call.

## Configuration

Dify has its own env file, `.env.dify`, layered on top of the global `.env`.
Key settings that live in `.env` (module-shared with the rest of the stack):

| Key | Meaning |
|---|---|
| `DIFY_DOMAIN` | Public subdomain, defaults to `dify.${MAIN_DOMAIN}` |
| `DIFY_VERSION` | Pinned `langgenius/dify-api` / `dify-web` image tag |
| `DIFY_SANDBOX_VERSION` | Pinned `dify-sandbox` image tag |
| `DIFY_PLUGIN_VERSION` | Pinned `dify-plugin-daemon` image tag |
| `DIFY_DB_USER` / `DIFY_DB_PASSWORD` | Credentials for the `dify_db` Postgres database |
| `DIFY_PLUGIN_DB_USER` / `DIFY_PLUGIN_DB_PASSWORD` | Credentials for the `dify_plugin_db` database |
| `DIFY_CLIENT_SECRET` | Authentik OIDC client secret for SSO |
| `DIFY_ADMIN_PASSWORD` | Initial console admin password, set at install time |
| `DIFY_APPS_JSON` | Populated by `modules/dify/seed-apps.sh` once you've created apps you want auto-provisioned on a fresh install |
| `DIFY_DOC_TOOLS_SSRF_ALLOW` | Allow-list of internal service names Dify's SSRF proxy may reach (`gpustack`, `gotenberg`, `lightrag`, `cognee`, `searxng`, `docling`, `tika`, `presidio-*`, `stirling-pdf`, `gitea`, …) |

Internally, `dify-api` talks to Postgres via `DB_HOST=postgres`, to Valkey via
`REDIS_HOST=valkey`, and routes outbound web requests through
`SSRF_PROXY_HTTP_URL=http://caddy:8195`. Apply any `.env`/`.env.dify` change
with `docker compose up -d --force-recreate` (or the relevant `dify-*`
service names if you want a narrower restart).

## Troubleshooting

- **Console shows the wrong timestamps.** Dify's own default (`LOG_TZ=UTC`)
  is overridden on this box to track your `.env` `TZ` setting; if times still
  look off after a change, restart `dify-api`/`dify-worker` to pick it up.
- **A workflow's HTTP Request node can't reach an internal service.** Check
  the target hostname is present in `DIFY_DOC_TOOLS_SSRF_ALLOW` — the SSRF
  proxy denies anything not on that list by design.
- **Storage permission errors on first start.** `dify-init-permissions` only
  runs once (it drops a flag file in `/app/storage/.init_permissions`); if the
  storage volume was replaced without clearing that flag, permissions won't
  be re-applied — check `docker compose logs dify-init-permissions`.
