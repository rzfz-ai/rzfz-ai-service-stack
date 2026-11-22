# Wazuh

## What is Wazuh

Wazuh is the security monitoring module (SIEM/XDR) of your box: a **manager**
that receives events from agents and syslog sources, an **indexer**
(OpenSearch) that stores them, and a **dashboard** where you read alerts,
file-integrity changes, vulnerability findings and compliance mappings
(CIS, NIST, PCI). It ships as the `wazuh` profile in the *Security* category
and is marked experimental in 2026.09: it works, but its resource appetite
(the indexer wants several GB of RAM) and its network exposure are yours to
weigh before you turn it on.

The dashboard lives at `https://wazuh.<your-domain>` (`WAZUH_DOMAIN`). You
reach it through your normal box login (Authentik's gate at the edge) and then
sign in to the dashboard itself with the local admin account: user `admin`,
password `WAZUH_INDEXER_PASSWORD` from the box's `.env`. That is `WAZUH_AUTH_MODE=forward_auth`, the shipped
default: Authentik decides who reaches the dashboard, and inside it everyone
uses the one local `admin` account.

`native_oidc` makes Authentik the login inside the dashboard as well: after the
box login you land on the Wazuh overview with no second password, as your own
account (Super Admins with full access, members of *razzfazz.ai Wazuh Users*
with read access). It is opt-in, because it changes who can do what in Wazuh.
To switch, set `WAZUH_AUTH_MODE=native_oidc` in `.env` and run
`docker compose up -d --force-recreate --no-deps wazuh-securityconfig-init wazuh-securityadmin wazuh-dashboard-config-init wazuh-dashboard`
(or switch the module off and on in the Config Portal). In this mode every
Super Admin appears in Wazuh as `admin`, which is the name Wazuh's API gives
its administrator role; Authentik's event log shows which person signed in.

## How to use it on this box

1. **Read the Enterprise guide first.** *Wazuh — Security Monitoring* (Help →
   Guides) explains what the module costs, what it exposes and where the
   trust boundary is. Enabling is `wazuh` in `COMPOSE_PROFILES` followed by
   `rzfz setup`; the first start takes a few minutes while the indexer
   initialises its security index.
2. **The box monitors itself.** The host is enrolled as the first agent with
   `scripts/install-wazuh-agent.sh`; it also prepares rootcheck for Debian 13,
   whose coreutils are the Rust `uutils` binaries — without that step every
   `ls`, `cat` and `ps` on the host raised a "trojaned version of file" alert
   (fixed in 2026.09).
3. **Enrol other hosts.** The manager's agent ports (`1514` events, `1515`
   enrolment) are bound to `127.0.0.1` by default. To accept agents from your
   network, set `WAZUH_MANAGER_HOST_BIND=0.0.0.0`, list the agents' addresses
   in `WAZUH_ALLOWED_IPS`, and hand the enrolment password
   (`WAZUH_AUTHD_PASSWORD`) to the agent installer — `WAZUH_AGENT_AUTO_ENROLL`
   stays `false`, so an unknown host cannot register itself.
4. **Feed syslog in.** Port `514` follows the same pattern
   (`WAZUH_SYSLOG_HOST_BIND`, `WAZUH_SYSLOG_ALLOWED_IPS`) for firewalls,
   switches and appliances that cannot run an agent.
5. **Get told.** Since 2026.09 the manager mails every alert at or above
   `WAZUH_EMAIL_ALERT_LEVEL` (default `12`) to `RAZZFAZZ_OPERATOR_EMAIL`
   (falling back to `ADMIN_CONTACT_EMAIL`) through the box's internal
   `smtp-relay`. An empty recipient keeps alert mail off — and the render
   step says so in its log, because a SIEM nobody hears from is the defect.
6. **Restore.** Events and the indexer state are part of the box backup; the
   Enterprise guide's *Operator recipes* say what a restore gives you back.

## Configuration

| Key | Meaning |
|---|---|
| `WAZUH_DOMAIN` | Public subdomain of the dashboard, defaults to `wazuh.${MAIN_DOMAIN}` |
| `WAZUH_VERSION` | Pinned Wazuh release for manager, indexer and dashboard |
| `WAZUH_AUTH_MODE` | `forward_auth` (default) — Authentik gate at the edge, local `admin` + `WAZUH_INDEXER_PASSWORD` inside; `native_oidc` — Authentik as the dashboard's own login, per-user roles (opt-in); `WAZUH_OIDC_CLIENT_ID` / `WAZUH_OIDC_CLIENT_SECRET` / `WAZUH_CLIENT_SECRET` are minted at install either way |
| `WAZUH_MANAGER_HOST_BIND` / `WAZUH_MANAGER_EVENTS_PORT` / `WAZUH_MANAGER_ENROLL_PORT` | Where agents reach the manager (`127.0.0.1`, `1514`, `1515`) |
| `WAZUH_ALLOWED_IPS` | Addresses or CIDRs allowed to talk to the agent ports once the bind is opened |
| `WAZUH_AGENT_AUTO_ENROLL` | `false` — enrolment needs `WAZUH_AUTHD_PASSWORD` |
| `WAZUH_AGENT_MANAGER_ADDRESS` | The manager this box's own agent enrols against; empty = the local manager, a worker box without one sets the fleet manager's LAN address |
| `WAZUH_AGENT_CONTAINERS` | Container names whose stdout the box agent ships besides the host logs; empty = `caddy,postgres,authentik-server` |
| `WAZUH_SYSLOG_HOST_BIND` / `WAZUH_MANAGER_SYSLOG_PORT` / `WAZUH_SYSLOG_ALLOWED_IPS` | Syslog intake (`127.0.0.1`, `514`) and its allow-list |
| `WAZUH_EMAIL_ALERT_LEVEL` | Alert level from which the manager mails the operator (default `12`) |
| `RAZZFAZZ_OPERATOR_EMAIL` | Recipient of alert mail; falls back to `ADMIN_CONTACT_EMAIL`, empty = off |
| `WAZUH_AWS_*` | Optional AWS CloudTrail / S3 log intake (`WAZUH_AWS_S3_LOG_TYPE`, `WAZUH_AWS_REGION`, `WAZUH_AWS_PROFILE`) |
| `WAZUH_INDEXER_PASSWORD` / `WAZUH_DASHBOARD_PASSWORD` / `WAZUH_API_PASSWORD` | Component-to-component credentials, generated at install — not your login |

Apply a change with `docker compose up -d --force-recreate wazuh-manager
wazuh-indexer wazuh-dashboard`; the manager re-renders its `ossec.conf` from
`.env` on every start.

## OpenBao audit events

With the OpenBao module enabled, the box agent also ships the `openbao` container,
whose stdout is OpenBao's audit log (JSON, secrets HMAC'd by the audit device).
Rules 100500-100507 raise: a refused request (5) and five of them from one
address within two minutes (10), a root token in use (12), a change to
policies, auth methods, audit devices or mounts (8), an audit device disabled
(12), a token created with the root policy (12) and an agent identity refused
outside its own secret path (7). Re-run the agent install after enabling the
module (`rzfz post-install --refresh`) so the container is on the list.

OpenBao refuses requests while its audit device cannot write. The device writes to
the container's stdout, which Docker rotates (10 MB x 3 files), so a full log
file cannot block OpenBao; keep that logging option when you edit the compose
file. The post-install run also creates the first policies, so expect a few
level-8 alerts and one root-token alert then.

## Troubleshooting

- **The dashboard shows a Wazuh login form instead of the box login.** The
  OIDC client is missing or its secret changed — compare
  `WAZUH_OIDC_CLIENT_SECRET` with the *wazuh* provider in Authentik
  (Admin → Applications → Providers) and recreate `wazuh-dashboard`.
- **An agent reports "unable to connect to enrollment service".** In this
  order: is `WAZUH_MANAGER_HOST_BIND` still `127.0.0.1`, is the agent's
  address in `WAZUH_ALLOWED_IPS`, does the agent use the current
  `WAZUH_AUTHD_PASSWORD`?
- **The indexer stays red after a start.** It needs about 2 GB of RAM and
  `vm.max_map_count >= 262144` on the host; the stack ships that sysctl in
  `core/sysctl/99-razzfazz-stability.conf` and `rzfz status` reports the
  drop-in as **STALE** on a box installed before it landed.
- **No alert mail arrives.** `docker logs wazuh-manager | grep -i mail` shows
  whether the recipient was empty at render time; then check that
  `smtp-relay` can deliver (`docker logs smtp-relay`).
- **"Trojaned version of file" alerts for `/usr/bin/ls` and friends.** That
  is the Debian 13 uutils false positive; re-run `scripts/install-wazuh-agent.sh`
  on the host to install the 2026.09 rootcheck ignore list.
