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
if [ -f "${SCRIPT_DIR}/.env" ]; then
    DOMAIN=$(grep '^MAIN_DOMAIN=' "${SCRIPT_DIR}/.env" | head -1 | cut -d= -f2-)
fi
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

# ── Encode logo as base64 data URI ──────────────────────────────────────────
LOGO_FILE="${SCRIPT_DIR}/core/Authentik/media/razzfazz-ai-logo.png"
LOGO_B64=""
if [ -f "$LOGO_FILE" ]; then
    LOGO_B64=$(base64 -w0 "$LOGO_FILE")
fi

echo "Generating Getting Started PDF for domain: ${DOMAIN} ..."

# ── Read GPUStack models ─────────────────────────────────────────────────────
GPUSTACK_API_KEY=""
if [ -f "${SCRIPT_DIR}/.env" ]; then
    GPUSTACK_API_KEY=$(grep '^GPUSTACK_API_KEY=' "${SCRIPT_DIR}/.env" | head -1 | cut -d= -f2-)
fi

MODELS_HTML=""
if [ -n "$GPUSTACK_API_KEY" ] && docker ps --format '{{.Names}}' | grep -q '^gpustack$'; then
    MODELS_JSON=$(docker exec gpustack curl -s http://localhost:9090/v1/models \
        -H "Authorization: Bearer ${GPUSTACK_API_KEY}" 2>/dev/null || true)
    if [ -n "$MODELS_JSON" ]; then
        MODELS_HTML=$(echo "$MODELS_JSON" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    rows = []
    for m in data.get('items', []):
        name = m.get('name', '?')
        cats = ', '.join(m.get('categories', []))
        params_b = m.get('meta', {}).get('n_params', 0) / 1e9
        ready = m.get('ready_replicas', 0)
        total = m.get('replicas', 0)
        status = '✅' if ready >= total and total > 0 else '⏳'
        # Parse backend parameters into readable key=value pairs
        bp = m.get('backend_parameters', [])
        bp_map = {}
        label_map = {'ctx-size':'Ctx','flash-attn':'FlashAttn','cache-type-k':'CacheK','cache-type-v':'CacheV','temp':'Temp','top-k':'TopK','top-p':'TopP','min-p':'MinP','repeat-penalty':'RepPen','presence-penalty':'PresPen','parallel':'Parallel'}
        for p in bp:
            p = p.lstrip('-')
            if '=' in p:
                k, v = p.split('=', 1)
                lbl = label_map.get(k, k)
                bp_map[lbl] = v
        bp_str = ', '.join(f'{k}={v}' for k, v in bp_map.items()) if bp_map else '—'
        # Model descriptions based on category and name
        desc = ''
        # One compact row per model (name · type · size · backend · status). The
        # per-model paragraph descriptions were removed: they doubled the table
        # length and pushed a full module set onto a 3rd page. A quick-start sheet
        # needs the model inventory, not prose — keep it to two A4 pages.
        rows.append(f'<tr><td><strong>{name}</strong></td><td>{cats}</td><td>{params_b:.1f}B</td><td style=\"font-size:8pt;\">{bp_str}</td><td>{status} {ready}/{total}</td></tr>')
    if rows:
        print(''.join(rows))
except Exception:
    pass
" 2>/dev/null)
    fi
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
</style>
</head>
<body>

<div style="display:flex; justify-content:space-between; align-items:flex-start;">
<div>
<h1>🚀 razzfazz.ai — Quick Start</h1>
<p class="subtitle">Kurzanleitung zur Inbetriebnahme · Domain: <code>${DOMAIN}</code></p>
</div>
<img src="data:image/png;base64,${LOGO_B64}" alt="razzfazz.ai" style="height:36px; margin-top:2px;" />
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
<p><code>&lt;IP-der-Box&gt; &ensp; start.${DOMAIN} &ensp; chat.${DOMAIN} &ensp; auth.${DOMAIN} &ensp; dify.${DOMAIN} &ensp; llm.${DOMAIN} &ensp; admin.${DOMAIN} &ensp; config.${DOMAIN} &ensp; help.${DOMAIN} &ensp; backup.${DOMAIN} &ensp; git.${DOMAIN} &ensp; license.${DOMAIN}</code></p>
</div>
<div>
<h3>Option B: DNS-Server</h3>
<p>Wildcard-Eintrag <code>*.${DOMAIN}</code> → IP der Box im internen DNS-Server anlegen.</p>
</div>
</div>

<h2>4 · Web-UIs aufrufen &amp; anmelden</h2>
<p>Alle UIs sind über HTTPS erreichbar (selbstsigniertes Zertifikat — Browserwarnung bestätigen).</p>

<table>
<tr><th>Dienst</th><th>URL</th><th>Benutzer</th><th>Passwort</th></tr>
<tr><td><strong>razzfazz.ai Portal</strong> (Start)</td><td class="url">https://start.${DOMAIN}</td><td><code>akadmin</code></td><td rowspan="7" style="text-align:center; vertical-align:middle;">Standard-Passwort<br>(auf der Box aufgeklebt)</td></tr>
<tr><td><strong>Authentik</strong> (SSO &amp; Benutzerverwaltung)</td><td class="url">https://auth.${DOMAIN}</td><td><code>akadmin</code></td></tr>
<tr><td><strong>Open WebUI</strong> (Chat)</td><td class="url">https://chat.${DOMAIN}</td><td><code>razzfazz-ai-admin@${DOMAIN}</code></td></tr>
<tr><td><strong>Dify</strong> (Workflows)</td><td class="url">https://dify.${DOMAIN}</td><td><code>razzfazz-ai-admin@${DOMAIN}</code></td></tr>
<tr><td><strong>Komodo</strong> (Monitoring)</td><td class="url">https://admin.${DOMAIN}</td><td><code>admin</code></td></tr>
<tr><td><strong>GPUStack</strong> (LLM Mgmt)</td><td class="url">https://llm.${DOMAIN}</td><td><code>admin</code></td></tr>
<tr><td><strong>Gitea</strong> (Git)</td><td class="url">https://git.${DOMAIN}</td><td><code>admin</code></td></tr>
</table>

<p><strong>Einstieg:</strong> Rufen Sie das <strong>razzfazz.ai Portal</strong> auf <span class="url">https://start.${DOMAIN}</span> auf — dort finden Sie alle freigeschalteten Anwendungen als Kacheln. <strong>Erster Test:</strong> Auf <span class="url">https://chat.${DOMAIN}</span> anmelden und eine erste Frage an das KI-Modell stellen.</p>

<div class="warn" style="margin: 6px 0; padding: 6px 10px; background: #fff8e1; border-left: 3px solid #f9a825; font-size: 9pt;">
<strong>ℹ Erster Login — zwei Schritte:</strong> Beim ersten Aufruf von <strong>chat.${DOMAIN}</strong> erscheinen nacheinander <strong>①</strong> das <strong>Authentik SSO</strong>-Login (<code>akadmin</code> + Admin-Passwort) und <strong>②</strong> die <strong>Open WebUI</strong>-Anmeldung (<code>razzfazz-ai-admin@${DOMAIN}</code> + Admin-Passwort). Danach bleibt die Session aktiv (gilt beim Erstaufruf jedes Dienstes).
</div>

<h2>5 · Unterstützende Dienste</h2>
<table>
<tr><th>Dienst</th><th>URL</th><th>Funktion</th></tr>
<tr><td>Konfigurations-Portal</td><td class="url">https://config.${DOMAIN}</td><td>Module, Einstellungen, Secrets, Backup, Governance</td></tr>
<tr><td>Backup UI</td><td class="url">https://backup.${DOMAIN}</td><td>Backup-Verwaltung &amp; Zeitplanung</td></tr>
<tr><td>Help Center</td><td class="url">https://help.${DOMAIN}</td><td>Dokumentation aller Dienste</td></tr>
<tr><td>Lizenzen</td><td class="url">https://license.${DOMAIN}</td><td>Open-Source-Lizenzübersicht</td></tr>
</table>

<h2 style="page-break-before: always;">6 · Backup sichern</h2>
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
<tr><th>Modell</th><th>Typ</th><th>Größe</th><th>Backend Parameter</th><th>Status</th></tr>
${MODELS_HTML}
</table>

<h2>8 · Weiterführende Hilfe</h2>
MODELSEOF
else
    cat >> "$HTML_FILE" <<NOMMODELSEOF
<h2>7 · Weiterführende Hilfe</h2>
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
