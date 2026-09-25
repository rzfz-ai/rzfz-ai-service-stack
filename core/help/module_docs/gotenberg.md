# Gotenberg

## What is Gotenberg

Gotenberg is a stateless document-conversion API in your stack. It converts
HTML, Markdown and Office files (Word, Excel, PowerPoint, …) into PDF using a
headless Chromium and LibreOffice under the hood, and can merge or otherwise
manipulate PDFs. It is a **backend service** with no interactive web UI of
its own — other modules call it. On this box, Dify workflows and Open WebUI
are the typical callers, converting generated content to a downloadable PDF.

## How to use it on this box

Gotenberg is an internal API and is **not exposed on a public subdomain** —
there's nothing to sign into. It's reachable only from inside the stack's
Docker network:

- From another container (e.g. a Dify HTTP Request node): `http://gotenberg:3000`
- From the host machine, via the bound loopback port: `http://127.0.0.1:3005`
  (the `GOTENBERG_PORT` setting below maps to the container's internal `3000`)

Typical calls:

1. **Convert HTML/Markdown to PDF** via the Chromium route, e.g.
   `POST http://gotenberg:3000/forms/chromium/convert/html` with your HTML
   file(s) as multipart form fields.
2. **Convert an Office document** via the LibreOffice route, e.g.
   `POST http://gotenberg:3000/forms/libreoffice/convert` with a `.docx`,
   `.xlsx`, or `.pptx` file.
3. **Check liveness** with `GET http://gotenberg:3000/health` — useful when
   diagnosing why a Dify document-generation workflow is failing.

Most users never call Gotenberg by hand — you'll meet it as one node in a
Dify workflow ("convert this Markdown report to PDF and email it") or as the
document engine behind another module's "export as PDF" button.

## Configuration

| Key | Meaning |
|---|---|
| `GOTENBERG_VERSION` | Pinned `gotenberg/gotenberg` image tag (default `8.37.0`) |
| `GOTENBERG_PORT` | Host-loopback port the container's internal `:3000` is published on (default `3005`) |

The container is started with `--chromium-deny-private-ips`. Gotenberg
8.32.0 reverted the strict-by-default SSRF guard that 8.31.0 had introduced
for private-IP fetches during HTML→PDF conversion (relevant when converting
HTML that embeds a remote `<img src>` or stylesheet); this box explicitly
opts back **in** to that guard so a malicious URL embedded in a converted
document can't be used to probe your internal network. Traffic that's
deliberately routed *through* the stack's SSRF proxy (e.g. Dify →
`caddy:8195` → Gotenberg → external) keeps working — only Gotenberg's own
direct outbound fetches to private IPs are blocked.

There is no database, no persistent volume, and no authentication of its
own — Gotenberg is stateless by design, converts the request it's given, and
returns the result. Restart with
`docker compose up -d --force-recreate gotenberg` after changing
`GOTENBERG_VERSION` or `GOTENBERG_PORT`.

## Troubleshooting

- **A Dify workflow's PDF-export node fails.** Confirm `gotenberg` is a
  running container (`docker compose ps gotenberg`) and that `gotenberg` is
  in the caller's SSRF allow-list if the call goes through the proxy.
- **`GET /health` from the host times out.** Check `GOTENBERG_PORT` isn't
  colliding with another service bound to the same host port, and that the
  container hasn't restarted into a crash loop
  (`docker compose logs gotenberg`).
