#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# =============================================================================
# validate-module.sh — touchpoint coverage probe for one module
#
# Used by the add-module skill (M023-S01.4) as the C3 validator gate. Also
# runnable standalone to audit modules added in earlier releases.
#
# Usage:  scripts/validate-module.sh <module_id> [--json]
# Exit:   0 if no FAIL; 1 if any FAIL.
#
# Emits structured lines:
#   PASS: <touchpoint> — <one-line evidence>
#   WARN: <touchpoint> — <reason>
#   FAIL: <touchpoint> — <reason>
#   INFO: <touchpoint> — <note>
#   SKIP: <touchpoint> — <why>
#
# Touchpoints (per S01.1 audit + S01.3 plan):
#    1. compose.yml present at <group>/<module>/ or <module>/
#    2. .env.example contains <ID>_VERSION
#    3. config/manifests/versions.json has the module under images/hardcoded/custom_built
#    4. config/manifests/VERSIONS.md mentions the module
#    5. config/migrations/env-changes.json has add-rule for <ID>_* in current release
#    6. core/config/profiles.yaml has the profile entry
#    7. core/Caddy/Caddyfile.tpl has subdomain block (only if has_ui)
#    8. core/Authentik/blueprints/base/<NN>-<module>.yaml exists (only if has_ui)
#    9. core/init-db.sh has CREATE DATABASE <module>_db (only if needs_postgres)
#   10. razzfazz-init.sh preset wiring (best-effort: presence in any preset list)
#   11. razzfazz-upgrade.sh data-migration block (best-effort grep)
#   12. razzfazz-post-install.sh post-deploy block (best-effort grep)
#   13. core/help/mirror_config.json has the entry
#  13b. core/help/own_docs/<module>.md exists
#   14. core/init-authentik.sh registers icon (only if has_ui)
#   15. core/Authentik/media/<module>_icon.png exists (only if has_ui)
#   16. core/licenses/download_licenses.py has LICENSE_URLS entry
#   18. razzfazz-config: nav + status-card + image-checker (best-effort grep)
#   19. README.md has module table row; CLAUDE.md has module table row
#   21. top-level compose.yml include line
#
# Plus integration validators:
#   I1. per-module `docker compose config` succeeds
#   I2. top-level `docker compose config` succeeds
#   I3. scripts/lint-ports.sh passes
#   I4. scripts/prepare-release.sh --check passes (sections 4 + 4b + 5)
#   I5. razzfazz-status.sh --short includes the module under [3] (WARN-only)
# =============================================================================

set -o pipefail

MODULE_ID="${1:-}"
JSON_OUTPUT=false
[ "${2:-}" = "--json" ] && JSON_OUTPUT=true

if [ -z "$MODULE_ID" ]; then
    cat >&2 <<EOF
Usage: $0 <module_id> [--json]

Examples:
  $0 vaultwarden        # audit one module
  $0 crawl4ai --json    # JSON output for skill consumption

Module IDs are kebab-case profile names from core/config/profiles.yaml.
EOF
    exit 2
fi

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 2

# Derive ID (uppercase, hyphens → underscores)
ID="$(echo "$MODULE_ID" | tr '[:lower:]-' '[:upper:]_')"

PASS=0
WARN=0
FAIL=0
INFO=0
SKIP=0
RESULTS=()

emit() {
    local sev="$1"; shift
    local tp="$1"; shift
    local msg="$*"
    RESULTS+=("${sev}|${tp}|${msg}")
    case "$sev" in
        PASS) PASS=$((PASS+1)) ;;
        WARN) WARN=$((WARN+1)) ;;
        FAIL) FAIL=$((FAIL+1)) ;;
        INFO) INFO=$((INFO+1)) ;;
        SKIP) SKIP=$((SKIP+1)) ;;
    esac
}

# -----------------------------------------------------------------------------
# Per-user agent types — provisioned dynamically by agent-manager (M020 S07);
# NOT compose profiles. Skip-and-explain instead of FAILing every probe.
# -----------------------------------------------------------------------------
PER_USER_AGENT_TYPES=("hermes" "moltis" "coding-tools")
is_per_user_agent=false
for t in "${PER_USER_AGENT_TYPES[@]}"; do
    if [ "$MODULE_ID" = "$t" ]; then
        is_per_user_agent=true
        break
    fi
done

if $is_per_user_agent; then
    cat <<EOF
=== validate-module: $MODULE_ID ===
  → '$MODULE_ID' is a PER-USER AGENT TYPE, not a compose-profile module.
    It is provisioned dynamically by agent-manager (see
    modules/agents/manager/app/services/catalog.py); image pinning via
    'version' there. Module-level static analysis does not apply.
    Use 'check-and-bump-versions' for catalog version pins (Phase 1c).

SKIP  validate-module               not a compose-profile module (per-user via agent-manager)
EOF
    exit 0
fi

# -----------------------------------------------------------------------------
# Locate compose file (post-S00: <group>/<module>/compose.yml; legacy: <module>/compose.yml)
# -----------------------------------------------------------------------------
COMPOSE_PATH=""
for candidate in \
    "modules/apps/$MODULE_ID/compose.yml" \
    "modules/knowledge/$MODULE_ID/compose.yml" \
    "modules/doc-processing/$MODULE_ID/compose.yml" \
    "modules/search/$MODULE_ID/compose.yml" \
    "core/$MODULE_ID/compose.yml" \
    "modules/llm/$MODULE_ID/compose.yml" \
    "modules/agents/$MODULE_ID/compose.yml" \
    "modules/chat/$MODULE_ID/compose.yml" \
    "modules/monitor/$MODULE_ID/compose.yml" \
    "modules/stts/$MODULE_ID/compose.yml" \
    "modules/doc-processing/$MODULE_ID-ngx/compose.yml" \
    "$MODULE_ID/compose.yml"
do
    if [ -f "$candidate" ]; then
        COMPOSE_PATH="$candidate"
        break
    fi
done
# Fallback: recursive search
if [ -z "$COMPOSE_PATH" ]; then
    COMPOSE_PATH=$(find . -name compose.yml -not -path './.git/*' -not -path './.claude/*' \
        -exec grep -l "^\s*$MODULE_ID:\s*$" {} \; 2>/dev/null | head -1 | sed 's|^\./||')
fi

if [ -z "$COMPOSE_PATH" ]; then
    emit FAIL "1.compose.yml" "no compose.yml found defining service '$MODULE_ID' (searched standard groups + recursive grep)"
else
    emit PASS "1.compose.yml" "$COMPOSE_PATH"
fi

# -----------------------------------------------------------------------------
# Probe questionnaire-derived flags from profiles.yaml so we know what to expect
# -----------------------------------------------------------------------------
HAS_UI="unknown"
NEEDS_PG="unknown"
INTERNAL_ONLY="unknown"
if [ -f core/config/profiles.yaml ]; then
    HAS_UI=$(MODULE="$MODULE_ID" python3 - <<'PY' 2>/dev/null
import os, yaml
m = os.environ['MODULE']
try:
    p = yaml.safe_load(open('core/config/profiles.yaml')) or {}
    e = (p.get('profiles') or {}).get(m)
    print(str(bool(e and e.get('has_ui'))).lower() if e else "missing")
except Exception:
    print("error")
PY
)
    NEEDS_PG=$(MODULE="$MODULE_ID" python3 - <<'PY' 2>/dev/null
import os, yaml
m = os.environ['MODULE']
try:
    p = yaml.safe_load(open('core/config/profiles.yaml')) or {}
    e = (p.get('profiles') or {}).get(m)
    if not e:
        print("missing"); raise SystemExit
    dbs = e.get('databases')
    if isinstance(dbs, list) and dbs:
        print("true")
    else:
        print("false")
except Exception:
    print("error")
PY
)
    INTERNAL_ONLY=$(MODULE="$MODULE_ID" python3 - <<'PY' 2>/dev/null
import os, yaml
m = os.environ['MODULE']
try:
    p = yaml.safe_load(open('core/config/profiles.yaml')) or {}
    e = (p.get('profiles') or {}).get(m)
    if not e:
        print("missing"); raise SystemExit
    print("true" if e.get('internal_only') else "false")
except Exception:
    print("error")
PY
)
fi

# -----------------------------------------------------------------------------
# Touchpoint 2: .env.example contains <ID>_VERSION
# -----------------------------------------------------------------------------
# Some legacy modules use a shortened ID (e.g. PAPERLESS_* for paperless-ngx).
# Build a list of acceptable prefixes: full ID first, then progressively
# shortened forms by dropping trailing _<segment> chunks.
acceptable_ids=("$ID")
short="$ID"
while [[ "$short" == *_* ]]; do
    short="${short%_*}"
    acceptable_ids+=("$short")
done

if [ -f config/.env.example ]; then
    # Determine whether this module is env-var-driven (in `images:` section)
    # or pinned (in `hardcoded:` / `custom_built:`). Env-var-driven modules MUST
    # have <ID>_VERSION; the others don't (version baked into compose.yml).
    manifest_section=$(MODULE="$MODULE_ID" python3 - <<'PY' 2>/dev/null
import os, json
m = os.environ['MODULE']
try:
    mf = json.load(open('config/manifests/versions.json'))
except Exception:
    print(''); raise SystemExit
hits = []
for sec in ('images', 'hardcoded', 'custom_built'):
    s = mf.get(sec) or {}
    if not isinstance(s, dict):
        continue
    if m in s:
        hits.append(sec); continue
    # Multi-service: check substring match
    for k in s:
        if m in k or k in m:
            hits.append(sec); break
print(",".join(sorted(set(hits))))
PY
)
    matched_prefix=""
    matched_version=""
    for pref in "${acceptable_ids[@]}"; do
        if grep -qE "^${pref}_VERSION=" config/.env.example; then
            matched_prefix="$pref"
            matched_version=true
            break
        elif grep -qE "^${pref}_" config/.env.example && [ -z "$matched_prefix" ]; then
            matched_prefix="$pref"
        fi
    done

    is_env_driven=false
    case ",$manifest_section," in
        *,images,*) is_env_driven=true ;;
    esac

    if [ "$matched_version" = "true" ] && [ "$matched_prefix" = "$ID" ]; then
        emit PASS "2.env.example" "${ID}_VERSION declared"
    elif [ "$matched_version" = "true" ]; then
        emit WARN "2.env.example" "${matched_prefix}_VERSION declared (uses shortened ID prefix vs canonical $ID — legacy naming)"
    elif [ -n "$matched_prefix" ] && ! $is_env_driven; then
        emit PASS "2.env.example" "${matched_prefix}_* present (manifest section: ${manifest_section:-?}; version pinned in compose, no env-var override needed)"
    elif [ -n "$matched_prefix" ]; then
        emit WARN "2.env.example" "${matched_prefix}_* present but no ${matched_prefix}_VERSION (nonstandard for env-var-driven module)"
    elif ! $is_env_driven && [ -n "$manifest_section" ]; then
        emit PASS "2.env.example" "no ${ID}_* entries (manifest section: $manifest_section; module pinned in compose, no env vars required)"
    else
        emit FAIL "2.env.example" "no ${ID}_* (or shortened-prefix) entries"
    fi
else
    emit FAIL "2.env.example" ".env.example missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 3: config/manifests/versions.json
# -----------------------------------------------------------------------------
if [ -f config/manifests/versions.json ]; then
    in_manifest=$(MODULE="$MODULE_ID" COMPOSE_PATH="$COMPOSE_PATH" python3 - <<'PY' 2>/dev/null
import os, json, re
m = os.environ['MODULE']
cp = os.environ.get('COMPOSE_PATH', '')
mf = json.load(open('config/manifests/versions.json'))

# Collect all (key, image-repo) pairs from manifest
manifest_entries = {}  # key -> (section, image_repo)
for sec in ('images', 'hardcoded', 'custom_built'):
    s = mf.get(sec) or {}
    if not isinstance(s, dict):
        continue
    for k, v in s.items():
        img_repo = ''
        if isinstance(v, dict):
            img = v.get('image', '')
            img_repo = img.split(':', 1)[0] if img else ''
        manifest_entries[k] = (sec, img_repo)

hits = set()

# Strategy 1: exact module_id match
if m in manifest_entries:
    hits.add(f"{manifest_entries[m][0]}:{m}")

# Strategy 2: substring match (covers paperless-ngx → paperless-ngx, presidio → presidio-*)
for k in manifest_entries:
    if m == k:
        continue
    if m in k or k in m:
        hits.add(f"{manifest_entries[k][0]}:{k}")

# Strategy 3: razzfazz-<module> custom-built convention
custom_key = f"razzfazz-{m}"
if custom_key in manifest_entries:
    hits.add(f"{manifest_entries[custom_key][0]}:{custom_key}")

# Strategy 4: parse the module's compose.yml and match image refs
if cp and os.path.isfile(cp):
    try:
        import yaml
        d = yaml.safe_load(open(cp)) or {}
        for svc, sd in (d.get('services') or {}).items():
            if not isinstance(sd, dict):
                continue
            img = sd.get('image', '')
            # strip ${VAR:-default} envs and tag
            img = re.sub(r':\$\{[^}]+\}', '', img).split(':', 1)[0]
            if not img:
                continue
            for k, (sec, repo) in manifest_entries.items():
                if repo == img:
                    hits.add(f"{sec}:{k} (compose-image:{img})")
    except Exception:
        pass

print(";".join(sorted(hits)) if hits else "")
PY
)
    if [ -n "$in_manifest" ]; then
        emit PASS "3.manifest" "found: $in_manifest"
    else
        emit FAIL "3.manifest" "no entry in versions.json (also tried substring + razzfazz-prefix + compose-image match)"
    fi
else
    emit FAIL "3.manifest" "config/manifests/versions.json missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 4: config/manifests/VERSIONS.md
# -----------------------------------------------------------------------------
if [ -f config/manifests/VERSIONS.md ]; then
    # Match any of:
    #   - Direct row: | <module> | ...
    #   - Profile heading: ## Profile: <module>
    #   - Custom-built row: | razzfazz-<module> | ...
    #   - Multi-service: any service-name from the module's compose.yml
    found=false
    if grep -qiE "^\|\s*\*?\*?${MODULE_ID}\*?\*?\s*\|" config/manifests/VERSIONS.md; then
        found=true
    elif grep -qiE "^##\s+Profile:\s+${MODULE_ID}\s*$" config/manifests/VERSIONS.md; then
        found=true
    elif grep -qiE "^\|\s*razzfazz-${MODULE_ID}\s*\|" config/manifests/VERSIONS.md; then
        found=true
    elif [ -n "$COMPOSE_PATH" ] && [ -f "$COMPOSE_PATH" ]; then
        svc_names=$(MODULE_COMPOSE="$COMPOSE_PATH" python3 - <<'PY' 2>/dev/null
import os, yaml
try:
    d = yaml.safe_load(open(os.environ['MODULE_COMPOSE'])) or {}
    print(" ".join((d.get('services') or {}).keys()))
except Exception:
    pass
PY
)
        for svc in $svc_names; do
            if grep -qiE "^\|\s*${svc}\s*\|" config/manifests/VERSIONS.md; then
                found=true
                break
            fi
        done
    fi
    if $found; then
        emit PASS "4.VERSIONS.md" "row/heading/service-row present"
    else
        emit WARN "4.VERSIONS.md" "no row matching $MODULE_ID, ## Profile: $MODULE_ID heading, or razzfazz-$MODULE_ID custom row (may need regen)"
    fi
else
    emit WARN "4.VERSIONS.md" "config/manifests/VERSIONS.md missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 5: config/migrations/env-changes.json
# -----------------------------------------------------------------------------
if [ -f config/migrations/env-changes.json ]; then
    matched_prefix=""
    for pref in "${acceptable_ids[@]}"; do
        if grep -qE "\"${pref}_" config/migrations/env-changes.json; then
            matched_prefix="$pref"
            break
        fi
    done
    if [ "$matched_prefix" = "$ID" ]; then
        emit PASS "5.migrations" "${ID}_* migration entry present"
    elif [ -n "$matched_prefix" ]; then
        emit PASS "5.migrations" "${matched_prefix}_* migration entry present (shortened prefix vs canonical $ID — legacy)"
    else
        emit WARN "5.migrations" "no ${ID}_* (or shortened-prefix) migration entry (acceptable if module pre-dates migration system)"
    fi
else
    emit WARN "5.migrations" "config/migrations/env-changes.json missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 6: core/config/profiles.yaml
# -----------------------------------------------------------------------------
case "$HAS_UI" in
    missing) emit FAIL "6.profiles.yaml" "no entry for $MODULE_ID" ;;
    error)   emit WARN "6.profiles.yaml" "could not parse profiles.yaml" ;;
    *)       emit PASS "6.profiles.yaml" "entry present (has_ui=$HAS_UI)" ;;
esac

# -----------------------------------------------------------------------------
# Touchpoint 7: core/Caddy/Caddyfile.tpl (only if has_ui)
# -----------------------------------------------------------------------------
caddy_file=""
for f in core/Caddy/Caddyfile.tpl core/Caddy/Caddyfile; do
    [ -f "$f" ] && caddy_file="$f" && break
done
if [ "$INTERNAL_ONLY" = "true" ]; then
    emit SKIP "7.caddy" "internal_only=true (module not exposed via Caddy by design)"
elif [ "$HAS_UI" = "true" ]; then
    found=false
    if [ -n "$caddy_file" ]; then
        # Match any of:
        #   <module>.{$MAIN_DOMAIN}       — direct subdomain pattern
        #   {$<MODULE_UPPER>_DOMAIN}      — env-var-driven subdomain pattern
        #   reverse_proxy <module>:       — module is the upstream of some route
        if grep -qE "^${MODULE_ID}\.\{|^\{\\\$${ID}_DOMAIN\}|reverse_proxy[[:space:]]+${MODULE_ID}:" "$caddy_file"; then
            found=true
        fi
    fi
    if $found; then
        emit PASS "7.caddy" "route found in $caddy_file"
    else
        emit FAIL "7.caddy" "no route for $MODULE_ID in $caddy_file (looked for <mod>.{\$MAIN_DOMAIN}, {\$${ID}_DOMAIN}, or reverse_proxy ${MODULE_ID}:)"
    fi
elif [ "$HAS_UI" = "false" ]; then
    emit SKIP "7.caddy" "has_ui=false"
else
    emit SKIP "7.caddy" "has_ui=$HAS_UI (cannot determine)"
fi

# -----------------------------------------------------------------------------
# Touchpoint 8: Authentik blueprint
# -----------------------------------------------------------------------------
if [ "$INTERNAL_ONLY" = "true" ]; then
    emit SKIP "8.authentik-blueprint" "internal_only=true (no end-user route, no Authentik blueprint by design)"
elif [ "$HAS_UI" = "true" ]; then
    bp_hits=()
    # Direct match: NN-<module_id>.yaml
    while IFS= read -r f; do
        [ -n "$f" ] && bp_hits+=("$f")
    done < <(find core/Authentik/blueprints/base -maxdepth 1 -type f -name "*-${MODULE_ID}.yaml" 2>/dev/null)

    # Multi-service modules: also accept blueprints matching any service name from
    # the module's compose.yml (e.g. matrix → synapse, element-web).
    if [ ${#bp_hits[@]} -eq 0 ] && [ -n "$COMPOSE_PATH" ] && [ -f "$COMPOSE_PATH" ]; then
        svc_names=$(MODULE_COMPOSE="$COMPOSE_PATH" python3 - <<'PY' 2>/dev/null
import os, yaml
try:
    d = yaml.safe_load(open(os.environ['MODULE_COMPOSE'])) or {}
    print(" ".join((d.get('services') or {}).keys()))
except Exception:
    pass
PY
)
        for svc in $svc_names; do
            while IFS= read -r f; do
                [ -n "$f" ] && bp_hits+=("$f")
            done < <(find core/Authentik/blueprints/base -maxdepth 1 -type f -name "*-${svc}.yaml" -o -name "*-${svc}-*.yaml" 2>/dev/null)
        done
    fi

    # Strategy 3: consolidated blueprint files (e.g. 03-providers-apps.yaml
    # bundles dify, chat, llm-management, etc. by slug/name rather than per-
    # module file). Grep blueprint contents for the module_id, slug-style
    # variants, or any service name from the compose.
    if [ ${#bp_hits[@]} -eq 0 ]; then
        consolidated=$(grep -lEi "(\b${MODULE_ID}\b|slug:[[:space:]]*${MODULE_ID}|to[[:space:]]+${MODULE_ID})" \
                       core/Authentik/blueprints/base/*.yaml 2>/dev/null | head -3)
        if [ -n "$consolidated" ]; then
            for f in $consolidated; do bp_hits+=("$f (consolidated)"); done
        fi
    fi

    if [ ${#bp_hits[@]} -gt 0 ]; then
        emit PASS "8.authentik-blueprint" "${bp_hits[*]}"
    else
        emit FAIL "8.authentik-blueprint" "no NN-${MODULE_ID}.yaml (or per-service / consolidated blueprint) in core/Authentik/blueprints/base/"
    fi
else
    emit SKIP "8.authentik-blueprint" "has_ui=$HAS_UI"
fi

# -----------------------------------------------------------------------------
# Touchpoint 9: core/init-db.sh CREATE DATABASE (only if needs_postgres)
# -----------------------------------------------------------------------------
if [ "$NEEDS_PG" = "true" ]; then
    if [ -f core/init-db.sh ] && grep -qE "${MODULE_ID//-/_}|${MODULE_ID}" core/init-db.sh; then
        emit PASS "9.init-db" "creates DB for $MODULE_ID"
    else
        emit FAIL "9.init-db" "needs_postgres=true but no entry in core/init-db.sh"
    fi
else
    emit SKIP "9.init-db" "needs_postgres=$NEEDS_PG"
fi

# -----------------------------------------------------------------------------
# Touchpoint 10: razzfazz-init.sh preset wiring (best-effort)
# -----------------------------------------------------------------------------
if [ -f razzfazz-init.sh ]; then
    if grep -qE "['\",](${MODULE_ID})['\",]|profiles.*${MODULE_ID}|\"${MODULE_ID}\"" razzfazz-init.sh; then
        emit PASS "10.init.sh-preset" "found in preset(s)"
    else
        emit INFO "10.init.sh-preset" "not in any preset (intentional if operator-only-enable)"
    fi
else
    emit WARN "10.init.sh-preset" "razzfazz-init.sh missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 11: razzfazz-upgrade.sh data-migration (best-effort)
# -----------------------------------------------------------------------------
if [ -f razzfazz-upgrade.sh ] && grep -qE "${MODULE_ID}|${ID}" razzfazz-upgrade.sh; then
    emit PASS "11.upgrade.sh-migration" "module mentioned"
else
    emit INFO "11.upgrade.sh-migration" "no migration block (acceptable if no first-run state needed)"
fi

# -----------------------------------------------------------------------------
# Touchpoint 12: razzfazz-post-install.sh
# -----------------------------------------------------------------------------
if [ -f razzfazz-post-install.sh ] && grep -qE "${MODULE_ID}|${ID}" razzfazz-post-install.sh; then
    emit PASS "12.post-install" "module mentioned"
else
    emit INFO "12.post-install" "no post-install block (acceptable if not needed)"
fi

# -----------------------------------------------------------------------------
# Touchpoint 13: core/help/mirror_config.json
# -----------------------------------------------------------------------------
if [ -f core/help/mirror_config.json ]; then
    found=false
    if grep -qE "\"${MODULE_ID}\"" core/help/mirror_config.json; then
        found=true
    elif [ -n "$COMPOSE_PATH" ] && [ -f "$COMPOSE_PATH" ]; then
        # Multi-service: accept entries keyed on any service name from the module
        svc_names=$(MODULE_COMPOSE="$COMPOSE_PATH" python3 - <<'PY' 2>/dev/null
import os, yaml
try:
    d = yaml.safe_load(open(os.environ['MODULE_COMPOSE'])) or {}
    print(" ".join((d.get('services') or {}).keys()))
except Exception:
    pass
PY
)
        for svc in $svc_names; do
            if grep -qE "\"${svc}\"" core/help/mirror_config.json; then
                found=true
                break
            fi
        done
    fi
    if $found; then
        emit PASS "13.mirror_config" "entry present (module or one of its services)"
    else
        emit WARN "13.mirror_config" "no entry for $MODULE_ID or any of its services"
    fi
else
    emit WARN "13.mirror_config" "core/help/mirror_config.json missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 13b: core/help/own_docs/<module>.md
# -----------------------------------------------------------------------------
if [ -f "core/help/own_docs/${MODULE_ID}.md" ]; then
    emit PASS "13b.own_docs" "core/help/own_docs/${MODULE_ID}.md"
else
    emit WARN "13b.own_docs" "no own_docs/${MODULE_ID}.md (operator-facing description missing)"
fi

# -----------------------------------------------------------------------------
# Touchpoints 14 + 15: Authentik icon
#
# Real model (verified 2026-04-29):
#   - profiles.yaml `app_icon:` names the icon file (concept-keyed, not
#     module-keyed: razzfazz-ai_<concept>_icon.png — e.g. infisical and
#     vaultwarden share `razzfazz-ai_secrets_icon.png`).
#   - core/Authentik/media/ stores the PNG (bulk-copied by init-authentik.sh
#     into /data/media on first run; no per-module `register_app_icon` line —
#     init-authentik.sh handling is one-time bulk copy of the entire media dir).
#   - The Authentik blueprint's `meta_icon:` field references
#     /media/public/application-icons/... (path resolved at apply time).
#
# Probe logic:
#   - 14 (init-authentik): PASS if init-authentik.sh exists and has the bulk
#     media-copy block; this is one-shot, not per-module.
#   - 15 (icon file present): resolve profiles.yaml `app_icon`, then check
#     core/Authentik/media/<that-filename> exists.
# -----------------------------------------------------------------------------
APP_ICON=$(MODULE="$MODULE_ID" python3 - <<'PY' 2>/dev/null
import os, yaml
m = os.environ['MODULE']
try:
    p = yaml.safe_load(open('core/config/profiles.yaml')) or {}
    e = (p.get('profiles') or {}).get(m)
    print((e or {}).get('app_icon') or '')
except Exception:
    print('')
PY
)

if [ "$INTERNAL_ONLY" = "true" ]; then
    emit SKIP "14.init-authentik-icon" "internal_only=true"
elif [ "$HAS_UI" = "true" ]; then
    if [ -f core/init-authentik.sh ] && grep -qE 'cp .*SOURCE_MEDIA|copy.*media' core/init-authentik.sh; then
        emit PASS "14.init-authentik-icon" "bulk media-copy block present (icons registered en bloc, not per-module)"
    elif [ -f core/init-authentik.sh ]; then
        emit WARN "14.init-authentik-icon" "core/init-authentik.sh present but no media-copy block found"
    else
        emit WARN "14.init-authentik-icon" "core/init-authentik.sh missing"
    fi
else
    emit SKIP "14.init-authentik-icon" "has_ui=$HAS_UI"
fi

if [ "$INTERNAL_ONLY" = "true" ]; then
    emit SKIP "15.authentik-icon" "internal_only=true"
elif [ "$HAS_UI" = "true" ]; then
    if [ -z "$APP_ICON" ] || [ "$APP_ICON" = "None" ]; then
        emit WARN "15.authentik-icon" "profiles.yaml has no app_icon: field for $MODULE_ID"
    elif [ -f "core/Authentik/media/${APP_ICON}" ]; then
        emit PASS "15.authentik-icon" "core/Authentik/media/${APP_ICON} (via profiles.yaml app_icon)"
    else
        emit FAIL "15.authentik-icon" "profiles.yaml app_icon=${APP_ICON} but file core/Authentik/media/${APP_ICON} not found"
    fi
else
    emit SKIP "15.authentik-icon" "has_ui=$HAS_UI"
fi

# -----------------------------------------------------------------------------
# Touchpoint 16: licenses
# -----------------------------------------------------------------------------
if [ -f core/licenses/download_licenses.py ]; then
    # The dict is keyed by display name (e.g. "Vaultwarden", "paperless-ngx",
    # "OpenHands"), not module_id. Match case-insensitively against the module_id
    # AND against display-name candidates derived from profiles.yaml; for
    # multi-service modules also check service names.
    # Build newline-separated candidate list (preserves multi-word names like "Apache Tika")
    candidates_file=$(mktemp)
    printf '%s\n' "$MODULE_ID" > "$candidates_file"
    MODULE="$MODULE_ID" python3 - <<'PY' >> "$candidates_file" 2>/dev/null
import os, yaml, re
m = os.environ['MODULE']
try:
    p = yaml.safe_load(open('core/config/profiles.yaml')) or {}
    e = (p.get('profiles') or {}).get(m) or {}
    name = e.get('name', '')
    if isinstance(name, str) and name:
        # Full name first
        print(name)
        # Then any single-word chunks (e.g. "Search (SearXNG)" → "Search", "SearXNG")
        for c in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", name):
            print(c)
except Exception:
    pass
PY
    if [ -n "$COMPOSE_PATH" ] && [ -f "$COMPOSE_PATH" ]; then
        MODULE_COMPOSE="$COMPOSE_PATH" python3 - <<'PY' >> "$candidates_file" 2>/dev/null
import os, yaml
try:
    d = yaml.safe_load(open(os.environ['MODULE_COMPOSE'])) or {}
    for s in (d.get('services') or {}).keys():
        print(s)
except Exception:
    pass
PY
    fi
    found=false
    matched=""
    while IFS= read -r c; do
        [ -z "$c" ] && continue
        # Use grep -F (fixed string) to handle multi-word names safely
        if grep -qiF "\"${c}\"" core/licenses/download_licenses.py; then
            found=true
            matched="$c"
            break
        fi
    done < "$candidates_file"
    rm -f "$candidates_file"
    if $found; then
        emit PASS "16.licenses" "entry present (matched: \"$matched\")"
    else
        emit WARN "16.licenses" "no LICENSE_URLS entry for $MODULE_ID (tried: $candidates)"
    fi
else
    emit WARN "16.licenses" "core/licenses/download_licenses.py missing"
fi

# -----------------------------------------------------------------------------
# Touchpoint 18: razzfazz-config (Configuration Portal)
#
# Verified 2026-04-29: the Config UI is fully data-driven from
# core/config/profiles.yaml (profile_manager.get_all_profiles() reads it; the
# image_checker, resource_monitor, and dashboard all consume the result). No
# per-module code change is needed in core/config/app/. Probe 6 (profiles.yaml
# entry) is the prerequisite; if that PASSes, Config UI auto-picks-up the module.
#
# Probe is INFO-only (audit visibility): notes the data-driven path.
# -----------------------------------------------------------------------------
emit INFO "18.razzfazz-config" "data-driven via profiles.yaml; no code change needed (probe 6 covers prerequisite)"

# -----------------------------------------------------------------------------
# Touchpoint 19: README.md + CLAUDE.md module tables
# -----------------------------------------------------------------------------
readme_hit=false
claude_hit=false
[ -f README.md ]   && grep -qE "^\|\s*\`?${MODULE_ID}\`?\s*\|" README.md && readme_hit=true
[ -f CLAUDE.md ]   && grep -qE "^\|\s*\`?${MODULE_ID}\`?\s*\|" CLAUDE.md && claude_hit=true
if $readme_hit && $claude_hit; then
    emit PASS "19.docs-tables" "in both README.md and CLAUDE.md"
elif $readme_hit; then
    emit WARN "19.docs-tables" "in README.md only (missing from CLAUDE.md)"
elif $claude_hit; then
    emit WARN "19.docs-tables" "in CLAUDE.md only (missing from README.md)"
else
    emit WARN "19.docs-tables" "not in README.md or CLAUDE.md module table"
fi

# -----------------------------------------------------------------------------
# Touchpoint 21: top-level compose.yml include
# -----------------------------------------------------------------------------
if [ -f compose.yml ] && [ -n "$COMPOSE_PATH" ]; then
    # Match either `- ./path/to/compose.yml`, `- path/to/compose.yml`,
    # or the `path:`-keyed form `- path: ./path/to/compose.yml`
    if grep -qE "^\s*-\s*(path:[[:space:]]+)?\.?/?${COMPOSE_PATH//\//\\/}\b" compose.yml; then
        emit PASS "21.top-level-include" "$COMPOSE_PATH included"
    else
        emit FAIL "21.top-level-include" "$COMPOSE_PATH not in top-level compose.yml include list"
    fi
elif [ -z "$COMPOSE_PATH" ]; then
    emit SKIP "21.top-level-include" "no module compose.yml found"
else
    emit FAIL "21.top-level-include" "top-level compose.yml missing"
fi

# -----------------------------------------------------------------------------
# Integration validators
# -----------------------------------------------------------------------------

# I1: compose config — preferred path is top-level `docker compose --profile
# <mod> config`, which loads the include chain (core services like postgres
# are visible to depends_on resolvers). Per-module standalone fails for any
# module that has cross-include depends_on.
#
# Env-file resolution: docker compose auto-loads .env when it exists at repo
# root. In a worktree without .env, we fall back to .env.example (which has
# every variable defined as a placeholder) so the parse succeeds.
if [ -n "$COMPOSE_PATH" ]; then
    env_args=()
    if [ -f .env ]; then
        :  # docker compose auto-loads
    elif [ -f config/.env.example ]; then
        env_args=(--env-file config/.env.example)
    fi
    if docker compose "${env_args[@]}" --profile "$MODULE_ID" config >/dev/null 2>&1; then
        emit PASS "I1.module-compose-config" "ok (top-level compose, profile $MODULE_ID)"
    elif docker compose "${env_args[@]}" -f "$COMPOSE_PATH" config >/dev/null 2>&1; then
        emit WARN "I1.module-compose-config" "ok standalone but failed under top-level compose — possibly missing from compose.yml include list"
    else
        emit FAIL "I1.module-compose-config" "compose config failed for profile $MODULE_ID (env: ${env_args[*]:-auto})"
    fi
else
    emit SKIP "I1.module-compose-config" "no compose.yml found"
fi

# I3: lint-ports
if [ -x scripts/lint-ports.sh ]; then
    if scripts/lint-ports.sh >/dev/null 2>&1; then
        emit PASS "I3.lint-ports" "ok"
    else
        emit FAIL "I3.lint-ports" "host port collision"
    fi
else
    emit SKIP "I3.lint-ports" "scripts/lint-ports.sh missing or not executable"
fi

# I4: prepare-release.sh --check
if [ -x scripts/prepare-release.sh ] && [ -f VERSION ]; then
    ver=$(tr -d '[:space:]' < VERSION)
    if scripts/prepare-release.sh "$ver" --check >/dev/null 2>&1; then
        emit PASS "I4.prepare-release" "all checks pass"
    else
        emit FAIL "I4.prepare-release" "scripts/prepare-release.sh $ver --check exited non-zero"
    fi
else
    emit SKIP "I4.prepare-release" "scripts/prepare-release.sh or VERSION missing"
fi

# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------
if $JSON_OUTPUT; then
    printf '{"module":"%s","compose_path":"%s","has_ui":"%s","needs_postgres":"%s","summary":{"pass":%d,"warn":%d,"fail":%d,"info":%d,"skip":%d},"results":[' \
        "$MODULE_ID" "$COMPOSE_PATH" "$HAS_UI" "$NEEDS_PG" "$PASS" "$WARN" "$FAIL" "$INFO" "$SKIP"
    first=true
    for r in "${RESULTS[@]}"; do
        sev="${r%%|*}"; rest="${r#*|}"
        tp="${rest%%|*}"; msg="${rest#*|}"
        msg_esc=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
        $first || printf ','
        first=false
        printf '{"sev":"%s","touchpoint":"%s","msg":"%s"}' "$sev" "$tp" "$msg_esc"
    done
    printf ']}\n'
else
    echo "=== validate-module: $MODULE_ID ==="
    echo "  compose_path: ${COMPOSE_PATH:-<none>}"
    echo "  has_ui:       $HAS_UI"
    echo "  needs_pg:     $NEEDS_PG"
    echo
    for r in "${RESULTS[@]}"; do
        sev="${r%%|*}"; rest="${r#*|}"
        tp="${rest%%|*}"; msg="${rest#*|}"
        printf '%-5s %-30s %s\n' "$sev" "$tp" "$msg"
    done
    echo
    printf 'Summary: PASS=%d  WARN=%d  FAIL=%d  INFO=%d  SKIP=%d\n' "$PASS" "$WARN" "$FAIL" "$INFO" "$SKIP"
fi

[ "$FAIL" -eq 0 ]
