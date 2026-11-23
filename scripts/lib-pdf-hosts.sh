# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2546: the hosts, LLM rows and logo the two Day-1 PDFs print, from the box's
# profiles. Sourced by scripts/generate-{getting-started,quickstart}-pdf.sh.
# No top-level `set` (sourced library).
#
#   razzfazz_pdf_has_gpustack <compose_profiles>          0 on an llm-legacy box
#   razzfazz_pdf_hosts <domain> <compose_profiles>        one line, space-separated hosts of
#                                                         core + the enabled profiles (profiles.yaml)
#   razzfazz_pdf_llm_rows <domain> <compose_profiles> <admin_user>
#                                                         the <tr> rows for llm. (+ gpustack.)
#   razzfazz_pdf_logo_b64 <stack_root>                    base64 of the rzfz.ai raster;
#                                                         nothing and rc 1 when it is missing
#   razzfazz_pdf_dns_options <domain>                     #2800: the DNS-server and dnsmasq options (HTML)
#   razzfazz_pdf_tls_note <domain> <tls_mode>             #2800: what the clients need for HTTPS (HTML)
#   razzfazz_pdf_service_rows <domain> <compose_profiles> <admin_user> [small]
#                                                         #2770: the <tr> rows of the optional
#                                                         apps, only for the enabled profiles,
#                                                         each with the login the box really has

razzfazz_pdf_has_gpustack() {
    case ",${1:-}," in *,llm-legacy,*|*,gpustack,*) return 0 ;; esac
    return 1
}

# The profiles manifest, relative to this library (scripts/ -> core/config/).
_RAZZFAZZ_PDF_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"

# The hosts are the box's ENABLED profiles mapped through each profile's own
# urls[].subdomain in core/config/profiles.yaml — core always, an always_on
# profile always, every other profile only when COMPOSE_PROFILES names it. No
# hand-kept list: a box measured without search./cognee./docling. in the line.
# config.<domain> is not in core's urls (a redirect to settings.<domain>).
# An unreadable manifest is loud and non-zero: a short line is the defect.
razzfazz_pdf_hosts() {
    local d="$1" p="${2:-}"
    local yml="${RAZZFAZZ_PDF_PROFILES_YAML:-${_RAZZFAZZ_PDF_LIB_DIR}/../core/config/profiles.yaml}"
    PDF_DOMAIN="$d" PDF_PROFILES="$p" PDF_YAML="$yml" python3 - <<'PYEOF'
import os, re, sys
path = os.environ["PDF_YAML"]


def _plain(text):
    """profiles.yaml without PyYAML (a host python3 may lack it): the profile
    ids, always_on and urls[].subdomain, read from the file's fixed layout."""
    out, sect, pid, in_urls = {}, None, None, False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip())
        if ind == 0:
            sect = line.split(":", 1)[0].strip()
            continue
        if sect != "profiles":
            continue
        if ind == 2:
            pid = line.strip().rstrip(":").strip("\"'")
            out[pid] = {"urls": []}
            in_urls = False
        elif ind == 4 and pid:
            key, _, val = line.strip().partition(":")
            in_urls = key == "urls"
            if key == "always_on":
                out[pid]["always_on"] = val.strip().lower() == "true"
        elif in_urls and pid:
            m = re.match(r"-?\s*subdomain:\s*[\"']?([A-Za-z0-9.-]+)", line.strip())
            if m:
                out[pid]["urls"].append({"subdomain": m.group(1)})
    return out


try:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        import yaml
        profiles = (yaml.safe_load(text) or {}).get("profiles") or {}
    except ImportError:
        profiles = _plain(text)
except Exception as exc:  # noqa: BLE001 - any failure means no trustworthy line
    print(f"[!] cannot read {path} ({exc.__class__.__name__}); profiles.yaml is needed for the hosts line",
          file=sys.stderr)
    sys.exit(1)
if not profiles:
    print(f"[!] no profiles in {path}; profiles.yaml is needed for the hosts line", file=sys.stderr)
    sys.exit(1)
enabled = {x.strip() for x in os.environ["PDF_PROFILES"].split(",") if x.strip()}
if "gpustack" in enabled:          # the pre-rename profile name (razzfazz_pdf_has_gpustack)
    enabled.add("llm-legacy")
dom = os.environ["PDF_DOMAIN"]
order = ["core"] + [k for k in profiles if k != "core"]
# #2800: the bare domain first: the start redirect and the CA download
# (https://<domain>/caddy-ca.crt) live there, and no subdomain covers it.
out = [dom]
for pid in order:
    e = profiles.get(pid)
    if not isinstance(e, dict):
        continue
    if pid != "core" and not e.get("always_on") and pid not in enabled:
        continue
    for u in e.get("urls") or []:
        sub = u.get("subdomain") if isinstance(u, dict) else None
        h = f"{sub}.{dom}" if sub else ""
        if h and h not in out:
            out.append(h)
print(" ".join(out))
PYEOF
}

# #2546/#2480: the rzfz.ai raster of the single-source SVG
# (core/Authentik/media/rzfz-ai-logo.svg, rasterised by
# scripts/branding/rasterise-pdf-logo.py), as base64 for a data URI.
razzfazz_pdf_logo_b64() {
    local f="${1:?stack root}/core/Authentik/media/rzfz-ai-logo-pdf.png"
    [ -f "$f" ] || return 1
    base64 -w0 "$f"
}

razzfazz_pdf_llm_rows() {
    local d="$1" p="${2:-}" user="${3:-rzfz-admin}"
    printf '<tr><td><strong>LLM Manager</strong> (Modelle &amp; Worker)</td><td class="url">https://llm.%s</td><td><code>%s</code></td></tr>\n' "$d" "$user"
    if razzfazz_pdf_has_gpustack "$p"; then
        printf '<tr><td><strong>GPUStack</strong> (Legacy)</td><td class="url">https://gpustack.%s</td><td><code>admin</code></td></tr>\n' "$d"
    fi
}

# #2800: name resolution is the customer admin's job. A wildcard does not cover the
# domain itself (the start redirect and the CA download live there), so the DNS option
# names both records; dnsmasq covers the domain and every subdomain in one line.
razzfazz_pdf_dns_options() {
    local d="$1"
    printf '<p><strong>Option B: DNS-Server</strong> — Wildcard-Eintrag <code>*.%s</code> <strong>und</strong> ein Eintrag für <code>%s</code> selbst auf die IP der Box (der Wildcard deckt die Domain selbst nicht ab).</p>\n' "$d" "$d"
    printf '<p><strong>Option C: dnsmasq</strong> (auf jedem Client, der die Box nutzt) — eine Zeile deckt die Domain und alle Subdomains ab: <code>address=/%s/&lt;IP-der-Box&gt;</code></p>\n' "$d"
}

# #2800: what the clients need for HTTPS, by the box's TLS_MODE. internal/selfsigned:
# the box's own root CA, served by Caddy on the main domain (/caddy-ca.crt), to be
# installed as a trusted root on every client. letsencrypt/empty and certificate:
# a certificate the clients already trust; nothing to confirm.
razzfazz_pdf_tls_note() {
    local d="$1" mode="${2:-}"
    case "$mode" in
        internal|selfsigned|self-signed)
            printf 'Alle UIs laufen über HTTPS mit einem Zertifikat der <strong>internen CA der Box</strong>. Das Root-Zertifikat steht unter <span class="url">https://%s/caddy-ca.crt</span> zum Download; es muss auf jedem Client als vertrauenswürdige Stammzertifizierungsstelle installiert werden, sonst zeigt jeder Browser eine Zertifikatswarnung.' "$d" ;;
        *)
            printf 'Alle UIs laufen über HTTPS mit einem Zertifikat, dem die Clients bereits vertrauen.' ;;
    esac
}

razzfazz_pdf_has_profile() {   # <profile> <compose_profiles>
    case ",${2:-}," in *",$1,"*) return 0 ;; esac
    return 1
}

# #2770: a customer handover PDF told the operator to log in to Open WebUI and
# Gitea with a local account + password, while both log in through Authentik
# (Open WebUI's OIDC provider is always configured in modules/chat/compose.yml;
# init adds the Authentik OAuth source to Gitea), and it listed Komodo / Gitea
# on a box without them. One row per ENABLED app, with the login it really has.
razzfazz_pdf_service_rows() {
    local d="$1" p="${2:-}" user="${3:-rzfz-admin}" small="${4:-}" st=""
    [ -n "$small" ] && st=' style="font-size:8pt"'
    if razzfazz_pdf_has_profile chat "$p"; then
        printf '<tr><td><strong>Open WebUI</strong> (Chat)</td><td class="url">https://chat.%s</td><td%s>„Mit Authentik fortfahren“ (SSO, <code>%s</code>)</td></tr>\n' "$d" "$st" "$user"
    fi
    if razzfazz_pdf_has_profile dify "$p"; then
        printf '<tr><td><strong>Dify</strong> (Workflows)</td><td class="url">https://dify.%s</td><td%s><code>razzfazz-ai-admin@%s</code></td></tr>\n' "$d" "$st" "$d"
    fi
    if razzfazz_pdf_has_profile monitor "$p"; then
        printf '<tr><td><strong>Komodo</strong> (Monitoring)</td><td class="url">https://admin.%s</td><td%s><code>admin</code></td></tr>\n' "$d" "$st"
    fi
    if razzfazz_pdf_has_profile gitea "$p"; then
        printf '<tr><td><strong>Gitea</strong> (Git)</td><td class="url">https://git.%s</td><td%s>Button „Authentik“ (SSO, <code>%s</code>)<br><small>lokal: <code>admin</code> (Notfallzugang)</small></td></tr>\n' "$d" "$st" "$user"
    fi
}
