# Gotenberg

Gotenberg is a stateless document-conversion API in your stack. It converts
HTML, Markdown and Office files (Word, Excel, PowerPoint, …) into PDF using a
headless Chromium and LibreOffice, and can merge or manipulate PDFs. It is a
**backend service**, used by other modules — notably Dify workflows and Open
WebUI — to produce documents; it has no interactive web UI of its own.

## How to reach it

Gotenberg is an internal API and is **not exposed on a public subdomain**. It is
reachable only from inside the stack's Docker network:

- From another container: `http://gotenberg:3000`
- From the host machine: `http://127.0.0.1:3000`

Outbound private-IP fetches from Gotenberg's Chromium are blocked
(`--chromium-deny-private-ips`) so a malicious URL embedded in a converted
document cannot reach your internal network.

## First steps

1. Convert Markdown or HTML to PDF by POSTing your files to the Chromium routes,
   e.g. `POST http://gotenberg:3000/forms/chromium/convert/html`.
2. Convert an Office document with the LibreOffice route,
   e.g. `POST http://gotenberg:3000/forms/libreoffice/convert`.
3. Check liveness at `GET http://gotenberg:3000/health`.

Most users consume Gotenberg indirectly — for example, from a Dify "HTTP
Request" node in a document-generation workflow — rather than calling it by hand.

## Full upstream documentation

For the full route reference and request options, see the official Gotenberg
documentation: [https://gotenberg.dev/docs/getting-started/introduction](https://gotenberg.dev/docs/getting-started/introduction)
