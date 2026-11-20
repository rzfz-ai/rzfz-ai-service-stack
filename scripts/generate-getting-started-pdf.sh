#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# Generate Getting Started PDF
# ==============================================================================
# Creates a concise Getting Started guide (max 2 A4 pages) as PDF using the
# Gotenberg container. The PDF is saved to the installation directory.
#
# Usage: ./scripts/generate-getting-started-pdf.sh
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/getting-started.pdf"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# ── Read domain from .env ────────────────────────────────────────────────────
DOMAIN=""
ADMIN_USER=""
if [ -f "${SCRIPT_DIR}/.env" ]; then
    DOMAIN=$(grep '^MAIN_DOMAIN=' "${SCRIPT_DIR}/.env" | head -1 | cut -d= -f2-)
    # #1148 review (Nachtrag): same as the quickstart sheet — this is printed
    # and handed over, and `akadmin` does not exist on a box installed after
    # #1148.
    ADMIN_USER=$(grep '^RAZZFAZZ_ADMIN_USERNAME=' "${SCRIPT_DIR}/.env" | head -1 | cut -d= -f2-)
fi
ADMIN_USER="${ADMIN_USER:-rzfz-admin}"
if [ -z "$DOMAIN" ]; then
    echo -e "${RED}Error: MAIN_DOMAIN not found in .env${NC}"
    exit 1
fi

# ── Verify Gotenberg is running ──────────────────────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -q '^gotenberg$'; then
    echo -e "${RED}Error: gotenberg container is not running.${NC}"
    echo "Start it with: docker compose --profile gotenberg up -d"
    exit 1
fi

# ── Hosts, LLM rows and the rzfz.ai logo (#2546) ────────────────────────────
# The logo is the committed raster of the single-source SVG (#2480,
# scripts/branding/rasterise-pdf-logo.py); a missing raster leaves no image.
source "${SCRIPT_DIR}/scripts/lib-pdf-hosts.sh"
LOGO_B64=$(razzfazz_pdf_logo_b64 "$SCRIPT_DIR") || {
    LOGO_B64=""
    echo "[!] rzfz.ai logo raster missing (core/Authentik/media/rzfz-ai-logo-pdf.png); the PDF has no logo" >&2
}
PDF_PROFILES="$(grep -m1 '^COMPOSE_PROFILES=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")" || PDF_PROFILES=""
HOST_LINE=$(razzfazz_pdf_hosts "$DOMAIN" "$PDF_PROFILES")
# #2800: name resolution and the certificate, from the box's own settings.
PDF_TLS_MODE="$(grep -m1 '^TLS_MODE=' "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")" || PDF_TLS_MODE=""
DNS_OPTIONS_HTML="$(razzfazz_pdf_dns_options "$DOMAIN")"
TLS_NOTE_HTML="$(razzfazz_pdf_tls_note "$DOMAIN" "$PDF_TLS_MODE")"
LLM_ROWS=$(razzfazz_pdf_llm_rows "$DOMAIN" "$PDF_PROFILES" "$ADMIN_USER")
SVC_ROWS=$(razzfazz_pdf_service_rows "$DOMAIN" "$PDF_PROFILES" "$ADMIN_USER")
# The password cell spans every row of the service table (#2770: counted, not hard-coded).
SPAN=$(( 2 + $(printf '%s\n%s\n' "$SVC_ROWS" "$LLM_ROWS" | grep -c '<tr>') ))
HOST_HTML=$(printf '%s' "$HOST_LINE" | sed 's/ / \&ensp; /g')

echo "Generating Getting Started PDF for domain: ${DOMAIN} ..."

# ── Deployed-model inventory (#2317) ────────────────────────────────────────
# LLM Manager first (the canonical front end since #1443/#1445), GPUStack only on
# a legacy box. An empty inventory is said out loud AND written into the PDF —
# a silent two-page document without its model section is what #2317 shipped.
source "${SCRIPT_DIR}/scripts/lib-model-inventory.sh"
razzfazz_model_inventory_load "${SCRIPT_DIR}/.env" || true
MODELS_HTML="$RAZZFAZZ_MODEL_INVENTORY_HTML"
if [ -n "$MODELS_HTML" ]; then
    echo "Model inventory: ${RAZZFAZZ_MODEL_INVENTORY_SOURCE} ($(printf '%s' "$MODELS_HTML" | grep -o '<tr>' | wc -l | tr -d ' ') models)"
else
    echo "[!] Model inventory EMPTY — neither the LLM Manager nor GPUStack answered; the PDF states that instead of omitting the section (#2317)" >&2
fi

# ── Collect metadata for footer ──────────────────────────────────────────────
GIT_COMMIT=$(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || echo "n/a")
INSTALL_CHECKSUM=$(RAZZFAZZ_STACK_ROOT="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/cli/setup_lib/setup.py" --checksum-status 2>/dev/null \
    | grep -oP '[0-9a-f]{64}' | head -1 || echo "n/a")

# ── Build HTML ───────────────────────────────────────────────────────────────
HTML_FILE=$(mktemp /tmp/getting-started-XXXXXX.html)

cat > "$HTML_FILE" <<HTMLEOF
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<style>
  @page { size: A4; margin: 12mm 14mm 11mm 14mm; }
  body {
    font-family: 'Segoe UI', Arial, Helvetica, sans-serif;
    font-size: 9.5pt; line-height: 1.28; color: #222;
    margin: 0; padding: 0;
  }
  h1 { font-size: 16pt; color: #CD1719; margin: 0 0 3px 0; }
  h2 { font-size: 11pt; color: #CD1719; margin: 8px 0 3px 0; border-bottom: 1px solid #ddd; padding-bottom: 1px; }
  h3 { font-size: 10pt; margin: 5px 0 2px 0; }
  p, li { margin: 1px 0; }
  ul { padding-left: 18px; margin: 1px 0; }
  code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 8.5pt; }
  .subtitle { font-size: 10pt; color: #666; margin-bottom: 8px; }
  .url { color: #CD1719; font-weight: bold; }
  table { width: 100%; border-collapse: collapse; margin: 3px 0; font-size: 9pt; }
  th { background: #f0f0f0; text-align: left; padding: 2px 6px; border: 1px solid #ddd; }
  td { padding: 2px 6px; border: 1px solid #ddd; }
  .note { background: #FFF8E1; border-left: 3px solid #FFC107; padding: 5px 10px; margin: 5px 0; font-size: 9pt; }
  .warn { background: #FFF3F3; border-left: 3px solid #CD1719; padding: 5px 10px; margin: 5px 0; font-size: 9pt; }
  .footer { margin-top: 8px; font-size: 8pt; color: #999; text-align: center; border-top: 1px solid #eee; padding-top: 3px; }
  .cols { display: flex; gap: 16px; }
  .cols > div { flex: 1; }
  /* Keep a section's heading with its first content and don't split the small
     blocks across the page boundary — the readable alternative to a forced
     break, which cost us a near-empty page 2. The model TABLE is deliberately
     not break-inside:avoid — it is the one block that can legitimately grow
     past a page, and avoiding a split there would push the whole table down
     and re-create the 3-page layout. Per-row avoid is enough. */
  h2, h3 { break-after: avoid; page-break-after: avoid; }
  .note, .warn { break-inside: avoid; page-break-inside: avoid; }
  tr { break-inside: avoid; page-break-inside: avoid; }
</style>
</head>
<body>

<div style="display:flex; justify-content:space-between; align-items:flex-start;">
<div>
<h1>🚀 razzfazz.ai — Quick Start</h1>
<p class="subtitle">Kurzanleitung zur Inbetriebnahme · Domain: <code>${DOMAIN}</code></p>
</div>
$([ -n "$LOGO_B64" ] && echo "<img src=\"data:image/png;base64,${LOGO_B64}\" alt=\"rzfz.ai\" style=\"height:36px; margin-top:2px;\" />")
</div>

<h2>1 · Rechner starten &amp; anmelden</h2>
<p>Rechner einschalten und mit folgenden Zugangsdaten am Betriebssystem anmelden:</p>
<ul>
  <li><strong>Benutzer:</strong> <code>razzfazz-ai-admin</code></li>
  <li><strong>Passwort:</strong> Das Standard-Passwort, das auf der Box aufgeklebt ist</li>
</ul>

<h2>2 · Netzwerk &amp; IP-Adresse</h2>
<ul>
  <li>Netzwerkkabel anschließen oder WLAN verbinden.</li>
  <li>IP-Adresse ermitteln: <code>ip addr show</code> oder <code>hostname -I</code></li>
  <li>Die IP wird für die DNS/Hosts-Konfiguration im nächsten Schritt benötigt.</li>
</ul>

<h2>3 · DNS / Hosts konfigurieren</h2>
<p>Damit die Web-UIs von anderen Geräten erreichbar sind, muss die Domain <code>${DOMAIN}</code> auf die IP der Box zeigen. <strong>Eine</strong> der folgenden Optionen wählen:</p>

<div class="cols">
<div>
<h3>Option A: hosts-Datei (pro Client)</h3>
<p>In <code>C:\Windows\System32\drivers\etc\hosts</code> (Windows, als Admin) bzw. <code>/etc/hosts</code> (Linux/Mac) eintragen:</p>
<p><code>&lt;IP-der-Box&gt; &ensp; ${HOST_HTML}</code></p>
</div>
<div>
${DNS_OPTIONS_HTML}
</div>
</div>

<h2>4 · Web-UIs aufrufen &amp; anmelden</h2>
<p>${TLS_NOTE_HTML}</p>

<table>
<tr><th>Dienst</th><th>URL</th><th>Benutzer</th><th>Passwort</th></tr>
<tr><td><strong>razzfazz.ai Portal</strong> (Start)</td><td class="url">https://start.${DOMAIN}</td><td><code>${ADMIN_USER}</code></td><td rowspan="${SPAN}" style="text-align:center; vertical-align:middle;">Standard-Passwort<br>(auf der Box aufgeklebt)</td></tr>
<tr><td><strong>Authentik</strong> (SSO &amp; Benutzerverwaltung)</td><td class="url">https://auth.${DOMAIN}</td><td><code>${ADMIN_USER}</code></td></tr>
${LLM_ROWS}
${SVC_ROWS}
</table>

<p><strong>Einstieg:</strong> Rufen Sie das <strong>razzfazz.ai Portal</strong> auf <span class="url">https://start.${DOMAIN}</span> auf — dort finden Sie alle freigeschalteten Anwendungen als Kacheln. <strong>Erster Test:</strong> Auf <span class="url">https://chat.${DOMAIN}</span> anmelden und eine erste Frage an das KI-Modell stellen.</p>

<div class="warn" style="margin: 6px 0; padding: 6px 10px; background: #fff8e1; border-left: 3px solid #f9a825; font-size: 9pt;">
<strong>ℹ Erster Login:</strong> Beim ersten Aufruf eines Dienstes erscheint das <strong>Authentik SSO</strong>-Login (<code>${ADMIN_USER}</code> + Admin-Passwort). In <strong>Open WebUI</strong> danach <strong>„Mit Authentik fortfahren“</strong>, in <strong>Gitea</strong> den Button <strong>„Authentik“</strong> wählen — kein eigenes Passwort. Danach bleibt die Session aktiv.
</div>

<h2>5 · Unterstützende Dienste</h2>
<table>
<tr><th>Dienst</th><th>URL</th><th>Funktion</th></tr>
<tr><td>Konfigurations-Portal</td><td class="url">https://settings.${DOMAIN}</td><td>Module, Einstellungen, Secrets, Backup, Governance</td></tr>
<tr><td>Backup UI</td><td class="url">https://backup.${DOMAIN}</td><td>Backup-Verwaltung &amp; Zeitplanung</td></tr>
<tr><td>Help Center</td><td class="url">https://help.${DOMAIN}</td><td>Dokumentation aller Dienste</td></tr>
<tr><td>Lizenzen</td><td class="url">https://license.${DOMAIN}</td><td>Open-Source-Lizenzübersicht</td></tr>
</table>

<!-- No forced page break here. This document's contract is "max 2 A4 pages"
     (see the header). A page-break-before:always on this heading made it
     THREE: sections 1-5 spill roughly one table row onto page 2, the forced
     break then threw 6-8 onto page 3 and left page 2 holding a single line.
     Let the content flow; keep each section intact instead.
     NB: this file is one big UNQUOTED heredoc (it interpolates ${DOMAIN} etc),
     so backticks here would be executed as command substitution. Don't. -->
<h2>6 · Backup sichern</h2>
<div class="warn">⚠️ <strong>Wichtig:</strong> Die automatischen Backups werden lokal unter <code>backups/</code> im Installationsverzeichnis gespeichert.
Dieses Verzeichnis muss <strong>zusätzlich regelmäßig auf ein externes Speichermedium</strong> (USB, NAS, Cloud) gesichert werden, um Datenverlust bei Hardware-Ausfall zu vermeiden.</div>
<ul>
  <li>Backup-Status &amp; Einstellungen: <span class="url">https://backup.${DOMAIN}</span></li>
  <li>Manuelles Backup per CLI: <code>rzfz backup backup</code></li>
  <li>Automatisches Backup: täglich um 03:00 Uhr (konfigurierbar)</li>
</ul>

HTMLEOF

# ── Inject deployed models section (if available) ────────────────────────────
if [ -n "$MODELS_HTML" ]; then
    cat >> "$HTML_FILE" <<MODELSEOF
<h2>7 · Installierte KI-Modelle</h2>
<p>Folgende Modelle sind auf dieser Box installiert und einsatzbereit. Verwaltung unter <span class="url">https://llm.${DOMAIN}</span></p>
<table>
<tr><th>Modell</th><th>Typ</th><th>Größe (Speicherbedarf)</th><th>Backend</th><th>Status</th></tr>
${MODELS_HTML}
</table>

<h2>8 · Weiterführende Hilfe</h2>
MODELSEOF
else
    cat >> "$HTML_FILE" <<NOMMODELSEOF
<h2>7 · Installierte KI-Modelle</h2>
<p><strong>Die Modell-Inventur war zum Zeitpunkt der Erstellung nicht abrufbar</strong> (weder der LLM Manager noch GPUStack antworteten). Aktuelle Liste unter <span class="url">https://llm.${DOMAIN}</span> — dieses Dokument nach <code>rzfz post-install --refresh</code> neu erzeugen.</p>

<h2>8 · Weiterführende Hilfe</h2>
NOMMODELSEOF
fi

cat >> "$HTML_FILE" <<FOOTEREOF
<p>Ausführliche Dokumentation zu allen Diensten, Konfiguration, Upgrade und Troubleshooting:</p>
<p style="text-align:center; font-size: 11pt;">📖 <span class="url">https://help.${DOMAIN}</span></p>

<div class="footer">razzfazz.ai Service Stack · ${DOMAIN} · Generiert am $(date '+%d.%m.%Y %H:%M') · Commit: ${GIT_COMMIT} · Checksum: ${INSTALL_CHECKSUM}</div>
</body>
</html>
FOOTEREOF

# ── Generate PDF via Gotenberg ───────────────────────────────────────────────
HTTP_CODE=$(docker exec -i gotenberg sh -c 'cat > /tmp/index.html' < "$HTML_FILE" && \
  docker exec gotenberg curl -s -o /tmp/output.pdf -w '%{http_code}' \
    --request POST http://localhost:3000/forms/chromium/convert/html \
    --form files=@/tmp/index.html \
    --form paperWidth=8.27 \
    --form paperHeight=11.69 \
    --form marginTop=0 \
    --form marginBottom=0 \
    --form marginLeft=0 \
    --form marginRight=0 \
    --form preferCssPageSize=true)

if [ "$HTTP_CODE" = "200" ]; then
    docker cp gotenberg:/tmp/output.pdf "$OUTPUT_FILE"
    docker exec gotenberg rm -f /tmp/index.html /tmp/output.pdf
    rm -f "$HTML_FILE"
    echo -e "${GREEN}PDF generated: ${OUTPUT_FILE}${NC}"
    ls -lh "$OUTPUT_FILE"
else
    rm -f "$HTML_FILE"
    echo -e "${RED}Gotenberg returned HTTP ${HTTP_CODE}${NC}"
    docker exec gotenberg cat /tmp/output.pdf 2>/dev/null || true
    exit 1
fi
