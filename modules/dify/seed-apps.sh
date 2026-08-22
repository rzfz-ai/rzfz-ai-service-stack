#!/usr/bin/env bash
# ==============================================================================
# Dify example-app + KB seeder (M019 S03)
# ==============================================================================
# Invoked from razzfazz-post-install.sh after dify_ensure_admin succeeds.
# Idempotent via a flag file at /var/lib/razzfazz/dify-seeded.
#
# What this script DOES (automatable, well-defined Dify console API):
#   1. Provisions a knowledge base "razzfazz.ai Stack Docs" populated from
#      core/help/own_docs/*.md so the example chatflow can demonstrate the
#      retriever_resources → citation-chip path end-to-end.
#   2. If the operator has already created one or more apps in the Dify UI
#      with names matching the targets in TARGET_APPS below, mints a service
#      API key for each found app and populates DIFY_APPS_JSON in .env so
#      the OpenWebUI dify_pipe.py picks them up. Triggers a re-seed of the
#      openwebui-seed sidecar so the pipe valve refreshes.
#
# What this script does NOT do (deliberately — too brittle without empirical
# Dify testing):
#   - Auto-create the apps with hand-crafted DSL (chatflow/workflow/agent
#     graphs are rich JSON-DSL; mistyped node identifiers or missing tool
#     references would silently break on import). Operator designs the apps
#     in the Dify UI at https://dify.<MAIN_DOMAIN> using the seeded KB,
#     then re-runs razzfazz-post-install.sh which calls this script again to
#     pick them up.
#
# Required env (sourced from razzfazz-post-install.sh):
#   - MAIN_DOMAIN, AUTHENTIK_BOOTSTRAP_PASSWORD
#   - POSTGRES_USER, DIFY_DB
#   - DIFY_ACCESS, DIFY_CSRF (set by dify_login from razzfazz-post-install.sh)
#   - SCRIPT_DIR (razzfazz-post-install.sh's own dir)
# ==============================================================================

set -eo pipefail

# 2026-05-09 fix: previously hard-coded /var/lib/razzfazz which requires root
# to mkdir on a fresh box; razzfazz-post-install.sh runs as the operator user
# and saw `mkdir: Keine Berechtigung`. Fall back to the operator's home cache
# dir when /var/lib/razzfazz isn't writable. The flag content is small and
# only needs to survive between razzfazz-post-install.sh invocations on the
# same host — both locations satisfy that.
if [ -w /var/lib/razzfazz ] || [ -w /var/lib ] && mkdir -p /var/lib/razzfazz 2>/dev/null; then
    FLAG_DIR="/var/lib/razzfazz"
else
    FLAG_DIR="${HOME}/.cache/razzfazz"
    mkdir -p "$FLAG_DIR"
fi
KB_FLAG="${FLAG_DIR}/dify-kb-seeded"
APPS_FLAG="${FLAG_DIR}/dify-apps-keys-extracted"

# Names the operator must use when creating apps in the Dify UI for this
# script to pick them up. The pipe id (left of `:`) becomes the manifold
# slug shown in OpenWebUI as `Dify: <name>`.
TARGET_APPS=(
    "allgemein-chat:Allgemein-Chat:chat"
    "summarizer:Document Summarizer:workflow"
    "tool-user:Tool-User:chat"
)

# ----------------------------------------------------------------------------
# Step 1 — KB
# ----------------------------------------------------------------------------
if [ -f "$KB_FLAG" ]; then
    echo "[dify-seed] KB already seeded (flag: $KB_FLAG); skipping."
else
    echo "[dify-seed] Provisioning knowledge base 'razzfazz.ai Stack Docs'..."

    KB_PAYLOAD='{"name":"razzfazz.ai Stack Docs","description":"Self-stack documentation seeded from core/help/own_docs (M019 S03)","permission":"only_me","provider":"vendor","indexing_technique":"high_quality"}'

    KB_RESP=$(dify_api POST "/console/api/datasets" "$KB_PAYLOAD" || true)
    KB_ID=$(echo "$KB_RESP" | python3 -c "import sys,json;
try:
    d = json.load(sys.stdin)
    print(d.get('id') or d.get('dataset', {}).get('id') or '')
except Exception: pass" 2>/dev/null)

    if [ -z "$KB_ID" ]; then
        echo "[dify-seed] WARN — KB creation response had no id. Skipping doc upload."
        echo "[dify-seed] Response: $KB_RESP"
    else
        echo "[dify-seed] KB id: $KB_ID"
        DOC_DIR="${SCRIPT_DIR:-.}/core/help/own_docs"
        for md in "$DOC_DIR"/*.md; do
            [ -f "$md" ] || continue
            name=$(basename "$md" .md)
            content=$(cat "$md" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
            payload="{\"name\":\"${name}\",\"text\":${content},\"indexing_technique\":\"high_quality\",\"process_rule\":{\"mode\":\"automatic\"}}"
            echo "[dify-seed]   ingesting ${name}.md..."
            dify_api POST "/console/api/datasets/${KB_ID}/document/create-by-text" "$payload" >/dev/null || true
        done
        echo "[dify-seed] KB seeded with $(ls "$DOC_DIR"/*.md | wc -l) documents."
        echo "$KB_ID" > "$KB_FLAG"
    fi
fi

# ----------------------------------------------------------------------------
# Step 2 — App API keys (only for apps the operator has already created)
# ----------------------------------------------------------------------------
echo "[dify-seed] Looking for apps with target names in Dify..."

# Fetch all apps in the workspace
APPS_LIST=$(dify_api GET "/console/api/apps?page=1&limit=100" "" || echo '{}')

DIFY_APPS_JSON_VALUE='['
FIRST=true
APPS_FOUND=0
APPS_MISSING=0

for entry in "${TARGET_APPS[@]}"; do
    pipe_id="${entry%%:*}"
    rest="${entry#*:}"
    app_name="${rest%%:*}"
    pipe_type="${rest##*:}"

    # Find the app id by matching name (case-sensitive)
    app_id=$(echo "$APPS_LIST" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for app in data.get('data', []):
        if app.get('name') == sys.argv[1]:
            print(app.get('id', ''))
            break
except Exception: pass
" "$app_name" 2>/dev/null)

    if [ -z "$app_id" ]; then
        echo "[dify-seed]   skip: '${app_name}' not yet created in Dify (operator's next step)"
        APPS_MISSING=$((APPS_MISSING + 1))
        continue
    fi

    # Mint a service API key
    key_resp=$(dify_api POST "/console/api/apps/${app_id}/api-keys" "{}" || echo '{}')
    api_key=$(echo "$key_resp" | python3 -c "
import sys, json
try: print(json.load(sys.stdin).get('token', ''))
except Exception: pass" 2>/dev/null)

    if [ -z "$api_key" ]; then
        echo "[dify-seed]   FAIL: '${app_name}' (id ${app_id}) — could not mint API key"
        echo "[dify-seed]   response: $key_resp"
        continue
    fi

    echo "[dify-seed]   ok: ${pipe_id} = ${app_name} (${app_id}) → key ${api_key:0:14}..."
    APPS_FOUND=$((APPS_FOUND + 1))

    [ "$FIRST" = "false" ] && DIFY_APPS_JSON_VALUE="${DIFY_APPS_JSON_VALUE},"
    DIFY_APPS_JSON_VALUE="${DIFY_APPS_JSON_VALUE}{\"id\":\"${pipe_id}\",\"name\":\"${app_name}\",\"api_key\":\"${api_key}\",\"type\":\"${pipe_type}\"}"
    FIRST=false
done

DIFY_APPS_JSON_VALUE="${DIFY_APPS_JSON_VALUE}]"

if [ "$APPS_FOUND" -gt 0 ]; then
    echo "[dify-seed] Writing DIFY_APPS_JSON ($APPS_FOUND apps) into .env..."
    # update_env_value is provided by razzfazz-post-install.sh
    update_env_value "${SCRIPT_DIR}/.env" "DIFY_APPS_JSON" "$DIFY_APPS_JSON_VALUE"

    # Refresh the dify_pipe valve in OpenWebUI by re-running the seeder sidecar.
    if docker ps --format '{{.Names}}' | grep -q '^openwebui$'; then
        echo "[dify-seed] Triggering openwebui-seed re-run to refresh dify_pipe valve..."
        (cd "${SCRIPT_DIR}" && \
            RAZZFAZZ_FORCE_RESEED=1 docker compose run --rm openwebui-seed) >/dev/null 2>&1 || \
            echo "[dify-seed] WARN — openwebui-seed re-run failed; operator can run manually."
    fi
    touch "$APPS_FLAG"
fi

if [ "$APPS_MISSING" -gt 0 ]; then
    cat <<EOF

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  NEXT STEP — Dify example apps                                          │
  │                                                                         │
  │  ${APPS_MISSING}/${#TARGET_APPS[@]} target apps are not yet created in Dify. To enable the   │
  │  Dify pipe for these slots in OpenWebUI:                                │
  │                                                                         │
  │    1. Open https://dify.${MAIN_DOMAIN}                       │
  │    2. Create the missing apps using these EXACT names:                  │
EOF
    for entry in "${TARGET_APPS[@]}"; do
        rest="${entry#*:}"
        app_name="${rest%%:*}"
        pipe_type="${rest##*:}"
        printf "  │       • %-25s (mode: %s)%*s│\n" "$app_name" "$pipe_type" $((36 - ${#app_name} - ${#pipe_type})) ""
    done
    cat <<EOF
  │    3. Reference the seeded knowledge base "razzfazz.ai Stack Docs"      │
  │       in the Allgemein-Chat app's retriever node.                       │
  │    4. Re-run: rzfz post-install --preset standard              │
  │       (this script picks them up, mints API keys, updates .env)         │
  │                                                                         │
  └─────────────────────────────────────────────────────────────────────────┘

EOF
fi

echo "[dify-seed] Done."
exit 0
