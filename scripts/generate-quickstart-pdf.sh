#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai — Generate Quick Start Reference (PDF)
# ==============================================================================
# Creates a compact 2-page A4 PDF using the running Gotenberg container.
# Same content as generate-getting-started-pdf.sh but with a compact
# two-column layout suitable for printing as a quick reference sheet.
#
# Usage:  ./scripts/generate-quickstart-pdf.sh
# Output: quickstart.pdf in the project root
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
OUTPUT_PDF="${SCRIPT_DIR}/quickstart.pdf"
TMP_HTML="$(mktemp /tmp/quickstart-XXXXXX.html)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# ── Load configuration ───────────────────────────────────────────────────────

if [[ ! -f "$ENV_FILE" ]]; then
    echo -e "${RED}ERROR: .env file not found at ${ENV_FILE}${NC}"
    exit 1
fi

# Read only the specific variables we need (avoids eval of shell references)
DOMAIN="$(grep -m1 '^MAIN_DOMAIN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")"
# #1148 review (Nachtrag): this sheet is HANDED TO THE OPERATOR with the box.
# It printed `akadmin` as the login name — the pre-#1148 bootstrap account. On a
# new box that name does not exist, so the first thing the operator reads is a
# credential that cannot log in. Read the same key everything else reads.
ADMIN_USER="$(grep -m1 '^RAZZFAZZ_ADMIN_USERNAME=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")"
ADMIN_USER="${ADMIN_USER:-rzfz-admin}"
COMPOSE_PROFILES="$(grep -m1 '^COMPOSE_PROFILES=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")"

if [[ -z "$DOMAIN" ]]; then
    echo -e "${RED}ERROR: MAIN_DOMAIN not found in .env${NC}"
    exit 1
fi

# ── Check Gotenberg is running ───────────────────────────────────────────────

if ! docker ps --format '{{.Names}}' | grep -q '^gotenberg$'; then
    echo -e "${RED}ERROR: Gotenberg container is not running.${NC}"
    echo "Start it with: docker compose --profile gotenberg up -d"
    exit 1
fi

# ── Encode logo as base64 data URI ──────────────────────────────────────────
LOGO_FILE="${SCRIPT_DIR}/core/Authentik/media/razzfazz-ai-logo.png"
LOGO_B64=""
if [ -f "$LOGO_FILE" ]; then
    LOGO_B64=$(base64 -w0 "$LOGO_FILE")
fi

echo "Generating Quick Start PDF for domain: ${DOMAIN} ..."

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

# ── Determine active services ────────────────────────────────────────────────

PROFILES="${COMPOSE_PROFILES:-}"
has_profile() { echo ",$PROFILES," | grep -q ",$1,"; }

# ── Generate HTML ────────────────────────────────────────────────────────────

cat > "$TMP_HTML" <<'HTMLEOF'
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<style>
  @page { size: A4 landscape; margin: 14mm 16mm 14mm 16mm; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
         font-size: 9.5pt; line-height: 1.45; color: #1a1a1a; }
  h1 { font-size: 16pt; color: #CD1719; margin-bottom: 4px; border-bottom: 2px solid #CD1719; padding-bottom: 4px; }
  h2 { font-size: 11pt; color: #CD1719; margin: 10px 0 4px 0; }
  h3 { font-size: 9.5pt; margin: 6px 0 2px 0; }
  p, li { margin-bottom: 2px; }
  ul { padding-left: 16px; }
  li { margin-bottom: 1px; }
  code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 8.5pt; font-family: 'Consolas', 'Courier New', monospace; }
  .subtitle { font-size: 10pt; color: #666; margin-bottom: 10px; }
  .url { color: #CD1719; font-weight: bold; }
  table { width: 100%; border-collapse: collapse; margin: 4px 0 8px 0; font-size: 9pt; }
  th, td { border: 1px solid #ccc; padding: 3px 6px; text-align: left; }
  th { background: #f5f5f5; font-weight: 600; }
  .step { margin-bottom: 8px; }
  .step-num { display: inline-block; width: 18px; height: 18px; background: #CD1719; color: white;
              border-radius: 50%; text-align: center; line-height: 18px; font-size: 8pt; font-weight: 700;
              margin-right: 4px; vertical-align: middle; }
  .hint { background: #fff8e1; border-left: 3px solid #f9a825; padding: 4px 8px; margin: 6px 0; font-size: 8.5pt; }
  .hint-blue { background: #e3f2fd; border-left: 3px solid #1976d2; padding: 4px 8px; margin: 6px 0; font-size: 8.5pt; }
  .warn { background: #FFF3F3; border-left: 3px solid #CD1719; padding: 4px 8px; margin: 6px 0; font-size: 8.5pt; }
  .cols { display: flex; gap: 14px; }
  .col { flex: 1; }
  .footer { margin-top: 10px; text-align: center; font-size: 8pt; color: #999; border-top: 1px solid #ddd; padding-top: 4px; }
</style>
</head>
<body>
HTMLEOF

# ── Inject dynamic header (with logo) ────────────────────────────────────────
cat >> "$TMP_HTML" <<HEADEREOF
<div style="display:flex; justify-content:space-between; align-items:flex-start;">
<div>
<h1>🚀 razzfazz.ai — Quick Start</h1>
<p class="subtitle">Kurzanleitung zur Inbetriebnahme · Domain: <code>${DOMAIN}</code></p>
</div>
$([ -n "$LOGO_B64" ] && echo "<img src=\"data:image/png;base64,${LOGO_B64}\" alt=\"razzfazz.ai\" style=\"height:36px; margin-top:2px;\" />")
</div>

<div class="cols">
<div class="col">

<div class="step">
<h2><span class="step-num">1</span> Rechner starten &amp; anmelden</h2>
<p>Den Rechner starten und mit folgendem Benutzer am Betriebssystem anmelden:</p>
<ul>
  <li>Benutzer: <code>razzfazz-ai-admin</code></li>
  <li>Passwort: <strong>siehe Aufkleber auf dem Gerät</strong></li>
</ul>
<p>Alle Services starten automatisch beim Hochfahren.</p>
</div>

<div class="step">
<h2><span class="step-num">2</span> Netzwerk &amp; IP-Adresse</h2>
<p>LAN-Kabel anschließen oder WLAN verbinden. IP-Adresse ermitteln:</p>
<p><code>ip addr show | grep "inet "</code></p>
<p>Die angezeigte IP-Adresse (z.B. <code>192.168.1.100</code>) wird im nächsten Schritt benötigt.</p>
</div>

<div class="step">
<h2><span class="step-num">3</span> DNS / hosts konfigurieren</h2>
<p>Damit die Web-Oberflächen erreichbar sind, müssen die Subdomains auf die IP zeigen:</p>

<p><strong>Option A: hosts-Datei</strong> (auf jedem Client-PC bearbeiten)</p>
<table>
<tr><th>Betriebssystem</th><th>Datei</th></tr>
<tr><td>Windows</td><td><code>C:\Windows\System32\drivers\etc\hosts</code></td></tr>
<tr><td>Linux / macOS</td><td><code>/etc/hosts</code></td></tr>
</table>
<p>Einträge (IP durch die tatsächliche ersetzen):</p>
<p style="font-size:8pt"><code>192.168.x.x &nbsp; auth.${DOMAIN} chat.${DOMAIN} dify.${DOMAIN} llm.${DOMAIN} admin.${DOMAIN} settings.${DOMAIN} config.${DOMAIN} help.${DOMAIN} backup.${DOMAIN} git.${DOMAIN} license.${DOMAIN}</code></p>

<p><strong>Option B: DNS-Server</strong> — A-Records oder Wildcard <code>*.${DOMAIN}</code> auf die Box-IP setzen.</p>

<p><strong>Auf der Box selbst:</strong> Die <code>HOST_IP</code> Variable in der <code>.env</code>-Datei auf die aktuelle IP setzen und mit <code>docker compose up -d --force-recreate</code> übernehmen.</p>
</div>

</div><!-- col -->
<div class="col">

<div class="step">
<h2><span class="step-num">4</span> Web-UIs aufrufen &amp; anmelden</h2>
<p>Alle UIs sind über HTTPS erreichbar (selbstsigniertes Zertifikat — Browserwarnung bestätigen).</p>

<table>
<tr><th>Dienst</th><th>URL</th><th>Benutzer</th><th>Passwort</th></tr>
<tr><td><strong>Authentik</strong></td><td class="url">https://auth.${DOMAIN}</td><td><code>${ADMIN_USER}</code></td><td rowspan="6" style="text-align:center; vertical-align:middle; font-size:8pt;">Standard-Passwort<br>(auf dem Gerät<br>aufgeklebt)</td></tr>
<tr><td><strong>Open WebUI</strong></td><td class="url">https://chat.${DOMAIN}</td><td style="font-size:8pt"><code>razzfazz-ai-admin@${DOMAIN}</code></td></tr>
<tr><td><strong>Dify</strong></td><td class="url">https://dify.${DOMAIN}</td><td style="font-size:8pt"><code>razzfazz-ai-admin@${DOMAIN}</code></td></tr>
<tr><td><strong>Komodo</strong></td><td class="url">https://admin.${DOMAIN}</td><td><code>admin</code></td></tr>
<tr><td><strong>GPUStack</strong></td><td class="url">https://llm.${DOMAIN}</td><td><code>admin</code></td></tr>
<tr><td><strong>Gitea</strong></td><td class="url">https://git.${DOMAIN}</td><td><code>admin</code></td></tr>
</table>

<p><strong>Erster Test:</strong> Auf <span class="url">https://chat.${DOMAIN}</span> anmelden und eine erste Frage an das KI-Modell stellen.</p>

<div class="warn" style="margin: 6px 0; padding: 6px 8px; background: #fff8e1; border-left: 3px solid #f9a825; font-size: 8pt;">
<strong>ℹ Erster Login — zwei Schritte:</strong>
<strong>①</strong> <strong>Authentik SSO</strong>: <code>${ADMIN_USER}</code> + Admin-Passwort &nbsp;|&nbsp;
<strong>②</strong> <strong>Open WebUI</strong>: <code>razzfazz-ai-admin@${DOMAIN}</code> + Admin-Passwort.<br>
Nach dem ersten Login bleibt die Authentik-Session aktiv.
</div>
</div>

<div class="step">
<h2><span class="step-num">5</span> Unterstützende Dienste</h2>
<table>
<tr><th>Dienst</th><th>URL</th><th>Funktion</th></tr>
<tr><td>Konfigurations-Portal</td><td class="url">https://settings.${DOMAIN}</td><td>Module, Einstellungen, Secrets, Backup, Governance</td></tr>
<tr><td>Backup UI</td><td class="url">https://backup.${DOMAIN}</td><td>Backup-Verwaltung &amp; Zeitplanung</td></tr>
<tr><td>Help Center</td><td class="url">https://help.${DOMAIN}</td><td>Dokumentation aller Dienste</td></tr>
<tr><td>Lizenzen</td><td class="url">https://license.${DOMAIN}</td><td>Open-Source-Lizenzübersicht</td></tr>
<tr><td>SearXNG</td><td colspan="2">Interne Metasuchmaschine (von Chat &amp; Dify genutzt)</td></tr>
<tr><td>Speaches</td><td colspan="2">Sprache-zu-Text / Text-zu-Sprache (von Chat genutzt)</td></tr>
<tr><td>Gotenberg</td><td colspan="2">PDF-/Dokumentenkonvertierung (von Dify genutzt)</td></tr>
</table>
</div>

<div class="step">
<h2><span class="step-num">6</span> Backup sichern</h2>
<div class="warn">⚠️ <strong>Wichtig:</strong> Automatische Backups liegen unter <code>backups/</code> – zusätzlich regelmäßig auf externes Medium sichern!</div>
<ul>
  <li>Backup-Status: <span class="url">https://backup.${DOMAIN}</span></li>
  <li>Manuell: <code>rzfz backup backup</code></li>
  <li>Automatisch: täglich um 03:00 Uhr</li>
</ul>
</div>

</div><!-- col -->
</div><!-- cols -->
HEADEREOF

# ── Inject deployed models section (if available) ────────────────────────────
if [ -n "$MODELS_HTML" ]; then
    cat >> "$TMP_HTML" <<MODELSEOF

<h2>Installierte KI-Modelle</h2>
<p>Folgende Modelle sind auf dieser Box installiert. Verwaltung unter <span class="url">https://llm.${DOMAIN}</span></p>
<table>
<tr><th>Modell</th><th>Typ</th><th>Größe</th><th>Backend</th><th>Status</th></tr>
${MODELS_HTML}
</table>
MODELSEOF
else
    cat >> "$TMP_HTML" <<NOMMODELSEOF

<h2>Installierte KI-Modelle</h2>
<p><strong>Die Modell-Inventur war zum Zeitpunkt der Erstellung nicht abrufbar</strong> (weder der LLM Manager noch GPUStack antworteten). Aktuelle Liste unter <span class="url">https://llm.${DOMAIN}</span> — dieses Dokument nach <code>rzfz post-install --refresh</code> neu erzeugen.</p>
NOMMODELSEOF
fi

cat >> "$TMP_HTML" <<FOOTEREOF

<div class="hint-blue">
<strong>📖 Weitere Hilfe:</strong> Ausführliche Dokumentation zu allen Services unter <span class="url">https://help.${DOMAIN}</span>
</div>

<div class="footer">razzfazz.ai Quick Start · ${DOMAIN} · Generiert am $(date '+%d.%m.%Y %H:%M') · Commit: ${GIT_COMMIT} · Checksum: ${INSTALL_CHECKSUM}</div>
</body>
</html>
FOOTEREOF

# ── Convert to PDF via Gotenberg ─────────────────────────────────────────────

echo "Generating PDF via Gotenberg..."

HTTP_CODE=$(docker exec -i gotenberg sh -c 'cat > /tmp/index.html' < "$TMP_HTML" && \
  docker exec gotenberg curl -s -o /tmp/output.pdf -w '%{http_code}' \
    --request POST http://localhost:3000/forms/chromium/convert/html \
    --form files=@/tmp/index.html \
    --form paperWidth=11.69 \
    --form paperHeight=8.27 \
    --form marginTop=0 \
    --form marginBottom=0 \
    --form marginLeft=0 \
    --form marginRight=0 \
    --form preferCssPageSize=true \
    --form printBackground=true)

if [[ "$HTTP_CODE" == "200" ]]; then
    docker cp gotenberg:/tmp/output.pdf "$OUTPUT_PDF"
    docker exec gotenberg rm -f /tmp/index.html /tmp/output.pdf
    rm -f "$TMP_HTML"
    echo "✓ PDF created: ${OUTPUT_PDF}"
    echo "  Size: $(du -h "$OUTPUT_PDF" | cut -f1)"
else
    rm -f "$TMP_HTML"
    echo "ERROR: Gotenberg returned HTTP ${HTTP_CODE}"
    docker exec gotenberg cat /tmp/output.pdf 2>/dev/null || true
    exit 1
fi
