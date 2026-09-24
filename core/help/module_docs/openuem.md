# OpenUEM

## What is OpenUEM

OpenUEM is unified endpoint management for the devices in your organisation:
hardware and software inventory, software deployment, remote assistance and
update control for Windows, Linux and macOS endpoints that run its agent. On
your box it ships as the `openuem` profile in the *Security* category
(experimental in 2026.09). The console runs at
`https://openuem.<your-domain>` (`OPENUEM_DOMAIN`) behind the box's single
sign-on; the agents talk to the box over two dedicated channels — a NATS
message bus and an OCSP responder for the certificates every agent carries.

## How to use it on this box

1. **Enable it.** Add `openuem` to `COMPOSE_PROFILES` and run `rzfz setup`.
   The first start generates an internal certificate authority from the
   `OPENUEM_ORG*` fields and issues the console, NATS, OCSP and agent
   certificates from it — get the organisation fields right *before* the
   first start: changing them later leaves the certificates with the old
   subjects, the broker denies every component and agent, and the only way
   out is wiping the PKI volumes and re-enrolling every agent.
2. **First login.** Open the console; you land in it with your box account.
   The Enterprise guide *OpenUEM — manage the endpoints in your fleet*
   (Help → Guides) walks through the first screens.
3. **Enrol the first agent.** Agents need to reach the box on
   `OPENUEM_NATS_HOST:OPENUEM_NATS_PORT` (default `openuem-nats.<domain>:4433`)
   and `OPENUEM_OCSP_HOST:OPENUEM_OCSP_PORT` (`openuem-ocsp.<domain>:8202`).
   Both are bound to `127.0.0.1` out of the box — set
   `OPENUEM_NATS_HOST_BIND` and `OPENUEM_OCSP_HOST_BIND` to `0.0.0.0` (or a LAN
   address) and make the two host names resolve in your network before you
   install the first agent. The agent needs the shared enrolment certificate
   the bootstrap produced (`docker cp openuem-certs:/certificates/agents/…`);
   the Enterprise guide's *Enrol the first agent* lists the exact steps.
4. **Email.** Notifications and the agent-installer mails go through the
   box's internal `smtp-relay` (`OPENUEM_SMTP_HOST=smtp-relay`,
   `OPENUEM_SMTP_PORT=587`); no separate mail account is needed.
5. **Backup.** The `openuem_db` database and the certificate volumes are part
   of the box backup — a restore brings the agents back without re-enrolment,
   which is exactly why the CA must not be regenerated casually.

## Configuration

| Key | Meaning |
|---|---|
| `OPENUEM_DOMAIN` | Console subdomain, defaults to `openuem.${MAIN_DOMAIN}` |
| `OPENUEM_VERSION` | Pinned OpenUEM release for console, NATS, OCSP and the agent installers |
| `OPENUEM_DB` / `OPENUEM_DB_USER` / `OPENUEM_DB_PASSWORD` | Database on the shared core Postgres (`openuem_db`) |
| `OPENUEM_CONSOLE_JWT_KEY` / `OPENUEM_CLIENT_SECRET` | Console session signing key and the Authentik OIDC client secret — minted at install |
| `OPENUEM_ORGNAME` / `OPENUEM_ORGPROVINCE` / `OPENUEM_ORGLOCALITY` / `OPENUEM_ORGADDRESS` / `OPENUEM_ORGCOUNTRY` | Subject of the internal CA and every certificate it issues |
| `OPENUEM_NATS_HOST` / `OPENUEM_NATS_PORT` / `OPENUEM_NATS_HOST_BIND` | Agent message bus (`openuem-nats.<domain>`, `4433`, bound to `127.0.0.1`) |
| `OPENUEM_OCSP_HOST` / `OPENUEM_OCSP_PORT` / `OPENUEM_OCSP_HOST_BIND` | Certificate status responder the agents check (`openuem-ocsp.<domain>`, `8202`, bound to `127.0.0.1`) |
| `OPENUEM_SMTP_HOST` / `OPENUEM_SMTP_PORT` | Outgoing mail — the box's `smtp-relay` on `587` |
| `OPENUEM_RESET_OPENUEM_USER` | Break-glass: `true` for ONE start regenerates the built-in `openuem` account's password (printed to the console log), then set it back to empty |

Apply a change with `docker compose up -d --force-recreate openuem-console`
(and `openuem-nats` / `openuem-ocsp` when you touched their hosts or binds).

## Troubleshooting

- **An agent installs but never appears in the console.** The agent cannot
  reach NATS — check the `*_HOST_BIND` values, that `openuem-nats.<domain>`
  resolves from the endpoint, and that `4433/tcp` is open on the way.
- **Agents are denied at the broker after a restore or a re-install.** The
  CA or the organisation fields changed; restore the certificate volumes from
  the same backup as the database, or wipe the PKI volumes and re-enrol.
- **No notification mails.** `docker logs openuem-console | grep -i smtp`
  tells you whether the relay accepted the message; the relay's own log
  (`docker logs smtp-relay`) shows whether it delivered.
- **You cannot sign in with your box account.** Compare `OPENUEM_CLIENT_SECRET`
  with the *openuem* provider in Authentik (Admin → Applications → Providers)
  and recreate `openuem-console`; the built-in `openuem` account is the
  break-glass path (see `OPENUEM_RESET_OPENUEM_USER`).
