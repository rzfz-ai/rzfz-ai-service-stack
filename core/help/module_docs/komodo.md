# Komodo

## What is Komodo

Komodo is the container-infrastructure management and monitoring UI in your
stack. It gives you a single dashboard over the box's Docker environment —
see which containers and stacks are running, inspect their state and
resource use, follow logs, and start, stop or redeploy services. It ships as
four containers behind the `monitor` profile:

- **`komodo-core`** — the web UI and API server.
- **`komodo-periphery`** — the local agent that reads Docker state on this
  host and reports it to `komodo-core`; it mounts the host's Docker socket
  **read-only**.
- **`postgres-komodo`** and **`ferretdb`** — Komodo stores its data in
  MongoDB-shaped documents; `ferretdb` is a MongoDB-API-compatible layer that
  actually persists into its own dedicated Postgres instance
  (`postgres-komodo`), separate from the stack's shared core Postgres.

## How to use it on this box

1. Open `https://admin.<domain>` (the `KOMODO_DOMAIN` setting below, on the
   `admin` subdomain) and sign in with your razzfazz.ai single sign-on.
2. Open the **Servers / Resources** view to see the containers and stacks
   that make up this box.
3. Select a container to inspect its **status, resource usage and logs** —
   this is the fastest way to see why a module is misbehaving without
   dropping to a shell.
4. Use the **actions** (restart / redeploy) on a resource when you need to
   bring a service back or apply a change from the UI directly.

Komodo is an operator tool for observing and nudging the running stack.
Day-to-day stack *configuration* changes (enabling/disabling modules,
editing `.env`) are normally made with the `rzfz` CLI and the Configuration
Portal at `https://settings.<domain>`; Komodo is the live view and manual
control surface on top of what those changes produce.

## Configuration

| Key | Meaning |
|---|---|
| `KOMODO_DOMAIN` | Public subdomain, defaults to `admin.${MAIN_DOMAIN}` |
| `KOMODO_PORT` | Host-loopback port `komodo-core` is published on (default `8180`) |
| `KOMODO_PERIPHERY_PORT` | Host-loopback port `komodo-periphery` is published on (default `8120`) — bound to `127.0.0.1` only, never reachable externally |
| `KOMODO_VERSION` | Pinned `komodo-core`/`komodo-periphery` image tag |
| `KOMODO_DB_VERSION` | Pinned `ferretdb/postgres-documentdb` image tag backing `postgres-komodo` |
| `KOMODO_HOST` | The URL Komodo validates incoming requests against — must match the public URL it's actually served at (`https://${KOMODO_DOMAIN}`) |
| `KOMODO_PASSKEY` | Shared passkey `komodo-core` and `komodo-periphery` authenticate to each other with (mapped internally to `KOMODO_PASSKEYS`/`PERIPHERY_PASSKEYS`, plural, which is what Komodo actually reads) |
| `KOMODO_INIT_ADMIN_USERNAME` / `KOMODO_INIT_ADMIN_PASSWORD` | Initial local-auth admin account |
| `KOMODO_LOCAL_AUTH` | Whether Komodo's own username/password login is enabled alongside SSO |

Komodo's periphery agent talks to the host's Docker daemon via
`/var/run/docker.sock` mounted **read-only** — it can observe container
state but cannot itself start/stop/redeploy containers at the socket level;
those actions go through `komodo-core`'s own API. Apply a config change with
`docker compose up -d --force-recreate komodo-core komodo-periphery`.

## Troubleshooting

- **`komodo-core` can't authenticate to `komodo-periphery` ("invalid public
  key").** This happens if the box still relies on the legacy per-host
  ed25519 key fallback instead of the shared passkey; confirm `KOMODO_PASSKEY`
  is set in `.env` and both containers were recreated after it was set.
- **Backups don't include Komodo's own history.** `postgres-komodo-data` is
  deliberately excluded from the volume tar (it's captured logically via
  `pg_dumpall` into `databases/postgres_komodo.sql` instead) so the nightly
  backup doesn't need to stop Komodo's database — this is expected, not a
  bug.
