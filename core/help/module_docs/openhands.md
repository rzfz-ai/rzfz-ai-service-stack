# OpenHands

## What is OpenHands

OpenHands is the autonomous AI software-development agent in your stack. You
describe a coding task in plain language; the agent plans it, writes and
edits code, runs commands and iterates — all inside a sandboxed
**agent-server** runtime container that OpenHands spawns per conversation
through the host's Docker socket, so its work stays isolated from both the
host and the long-running `openhands` container itself. It runs behind the
`openhands` profile (marked EXPERIMENTAL) and, because it holds Docker-socket
access (root-equivalent on the host), Authentik SSO is its primary access
control — see `D016` in the repo's design-decision log for the rationale.

## How to use it on this box

1. Open `https://openhands.<domain>` (the `OPENHANDS_DOMAIN` setting below)
   and sign in with your razzfazz.ai single sign-on.
2. Start a **new conversation** and describe the task in plain language —
   for example, "add a unit test for module X" or "scaffold a small Flask
   endpoint".
3. The agent uses this box's own LLM Manager as its backend
   (`http://llm:8080/v1` — the canonical address, whichever engine serves it),
   defaulting to the `qwen3.6` coding model and `qwen3-embedding` for
   embeddings — no external API key is needed, and it works fully offline.
4. Watch the agent work in the built-in editor/terminal view; review its
   diffs and approve or redirect as it goes. If you've configured
   `GITEA_API_TOKEN` / `GIT_AUTHOR_*` in `.env`, the agent can commit and
   push against this box's own Gitea as itself.
5. Connect a repository (or work in the persistent sandbox workspace, backed
   by the `openhands-workspace` volume) to have the agent make changes
   against real code that survives container restarts.

Under the hood, each conversation's actual code-editing/command-running work
happens in a separate `ghcr.io/openhands/agent-server` container that the
`openhands` container spawns on demand via the host Docker socket — the
`openhands` container itself is closer to an orchestrator + UI than the
sandbox doing the work.

## Configuration

| Key | Meaning |
|---|---|
| `OPENHANDS_DOMAIN` | Public subdomain, defaults to `openhands.${MAIN_DOMAIN}` |
| `OPENHANDS_PORT` | Host port the `openhands` container listens on (default `3003`); internally read as `SANDBOX_HOST_PORT` |
| `OPENHANDS_VERSION` | Pinned `ghcr.io/openhands/openhands` image tag |
| `OPENHANDS_CLIENT_SECRET` | Authentik OIDC client secret for SSO |
| `GITEA_API_TOKEN` | Optional, not in `.env.example` — add it to `.env` and compose passes it through; lets the agent commit/push against this box's Gitea |
| `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` / `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` | Optional, not in `.env.example` — compose passes them through from `.env`; the Git identity the agent commits as (default "AI Agent" / `agent@localhost`) |

Two settings are baked into `modules/apps/openhands/compose.yml` rather than
`.env`, and are worth knowing if you ever bump the OpenHands version:
`AGENT_SERVER_IMAGE_REPOSITORY` / `AGENT_SERVER_IMAGE_TAG` pin the exact
per-conversation sandbox image, and `SANDBOX_CONTAINER_URL_PATTERN` makes
sandbox URLs route through Caddy at `/sandbox/{port}/` so your browser talks
to them over the box's normal TLS on `:443` instead of trying an unreachable
`localhost:<random>` address.

The `openhands` container requires `/var/run/docker.sock` mounted **read-write**
— this is the one module in the stack with genuine root-equivalent host
access, gated entirely by who Authentik lets reach the login page. Apply a
config change with `docker compose up -d --force-recreate openhands`.

## Troubleshooting

- **Agent can't reach its sandbox / webhook callbacks fail.** OpenHands is
  joined to both the compose `default` bridge and the Docker `bridge`
  network so it can reach spawned sandbox containers directly; if this
  breaks after a Docker network change, check
  `modules/apps/openhands/openhands-monkeypatch.sh` is still applying (its
  job is pinning the sandbox's callback address to the `openhands`
  container's own bridge IP rather than whatever else is listening on that
  port).
- **Agent-server spawns but the UI shows it as unreachable.** Confirm
  `SANDBOX_HOST_PORT` matches `OPENHANDS_PORT` and that no other container
  (Gitea, by default, also lives near port `3000`) is colliding with it.
- **Fresh install: permission errors writing to `/opt/workspace` or
  `/.openhands`.** The container's custom entrypoint `chown`s both paths for
  the sandbox's uid at every start — check
  `docker compose logs openhands` for the chown step if this doesn't clear.
