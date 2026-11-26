#!/usr/bin/env bash
# ==============================================================================
# Dify example-app + KB seeder (M019 S03)
# ==============================================================================
# Invoked from razzfazz-post-install.sh after dify_ensure_admin succeeds.
# Idempotent via a flag file at /var/lib/razzfazz/dify-seeded.
#
# What this script DOES (automatable, well-defined Dify console API):
#   1. Provisions a knowledge base "razzfazz.ai Stack Docs" populated from
#      the Help UI's docs (docs/community, plus the box's Enterprise overlay
#      when synced) so the example chatflow can demonstrate the
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
# #2185 + #2445: the flag lives on the HOST, outside dify_db. It is evidence
# only when the dataset it names still exists AND holds documents, and "seeded"
# is only ever printed after Dify reports every document indexing_status=completed.
KB_FAILED=0

# Prints the dataset's document_count; fails when Dify does not know the dataset.
_kb_doc_count() {
    dify_api GET "/console/api/datasets/$1" 2>/dev/null | python3 -c "import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(d, dict) or str(d.get('id') or '') != sys.argv[1]:
    sys.exit(1)
print(int(d.get('document_count') or 0))" "$1" 2>/dev/null
}

# #2445: the console adds documents by UPLOADED FILE id; create-by-text is a
# service-API-only route and answers 404 to the console session. The session
# cookie travels in the environment of the docker-exec (names only in argv),
# never in argv or a log line.
_seed_upload_md() {
    local f="$1" name
    name="$(basename "$f")"
    # #2682: the cookie and CSRF headers go through rz_ctr_curl: in the exec
    # environment by name, and in a 0600 file inside the container for curl.
    RZ_CURL_HEADERS=$'Cookie: access_token='"${DIFY_ACCESS}; csrf_token=${DIFY_CSRF}"$'\nX-CSRF-Token: '"${DIFY_CSRF}" \
    rz_ctr_curl dify-api -s -X POST -F source=datasets \
        -F "file=@-;type=text/markdown;filename=${name}" \
        http://localhost:5001/console/api/files/upload < "$f" 2>/dev/null
}

# Reads {"data":[{id,indexing_status,error}...]} from the given console path and
# prints DONE n/n | ERR <ids: error> | WAIT done/total.
_kb_index_verdict() {
    dify_api GET "$1" 2>/dev/null | python3 -c "import sys, json
try:
    rows = json.load(sys.stdin).get('data') or []
except Exception:
    rows = []
done = sum(1 for r in rows if r.get('indexing_status') == 'completed')
bad = [str(r.get('id')) + ': ' + str(r.get('error')) for r in rows if r.get('indexing_status') == 'error']
if bad:
    print('ERR ' + '; '.join(bad))
elif rows and done == len(rows):
    print('DONE %d/%d' % (done, len(rows)))
else:
    print('WAIT %d/%d' % (done, len(rows)))" 2>/dev/null || echo "WAIT 0/0"
}

# Waits for the path to report every document completed (#2445). A large seed
# on a slow box legitimately takes longer than any fixed window (89 documents
# took 330 s on an offline CPU box, and a fixed 300 s reported a false FAIL), so
# the wait follows PROGRESS: it gives up only when the completed count has not
# moved for RZFZ_DIFY_SEED_INDEX_WAIT_SECONDS (default 300) - indexing is stuck -
# or when RZFZ_DIFY_SEED_INDEX_MAX_SECONDS (default 1800) have passed while it
# is still moving, which is reported as running, not as failed.
# Returns 0 on DONE and on "still progressing"; 1 on an error or a stall.
_kb_await_indexed() {
    local path="$1" limit="${RZFZ_DIFY_SEED_INDEX_WAIT_SECONDS:-300}" max="${RZFZ_DIFY_SEED_INDEX_MAX_SECONDS:-1800}"
    local waited=0 still=0 verdict="" done_now="" done_last=""
    while :; do
        verdict=$(_kb_index_verdict "$path")
        case "$verdict" in DONE*|ERR*) break ;; esac
        done_now="${verdict#WAIT }"; done_now="${done_now%%/*}"
        if [ -n "$done_last" ] && [ "$done_now" != "$done_last" ]; then still=0; fi
        done_last="$done_now"
        [ "$still" -lt "$limit" ] || break
        [ "$waited" -lt "$max" ] || break
        sleep 10; waited=$((waited + 10)); still=$((still + 10))
    done
    case "$verdict" in
        DONE*) echo "[dify-seed] KB seeded and indexed ${verdict#DONE } documents."; return 0 ;;
        ERR*)  echo "[dify-seed] FAIL — KB documents not indexed: ${verdict#ERR } (#2445). Check the Dify default embedding model, delete the 'razzfazz.ai Stack Docs' dataset in Dify and re-run 'rzfz post-install --refresh'." ;;
        *)
            if [ "$still" -lt "$limit" ]; then
                echo "[dify-seed] KB indexing still running: ${verdict#WAIT } documents completed after ${waited}s and still progressing — it finishes in the background (Dify → Knowledge → 'razzfazz.ai Stack Docs' shows the progress)."
                return 0
            fi
            echo "[dify-seed] FAIL — KB not indexed: still ${verdict#WAIT } documents completed, no progress for ${limit}s (#2445). Check the Dify worker's log, then re-run 'rzfz post-install --refresh' to check again." ;;
    esac
    return 1
}

KB_ID=""; KB_DOCS=""
if [ -f "$KB_FLAG" ]; then
    KB_ID=$(head -n1 "$KB_FLAG" 2>/dev/null | tr -d '[:space:]')
    [ -z "$KB_ID" ] || KB_DOCS=$(_kb_doc_count "$KB_ID" || true)
    if [ -z "$KB_DOCS" ]; then
        echo "[dify-seed] KB flag $KB_FLAG names dataset '$(head -n1 "$KB_FLAG" 2>/dev/null)' which Dify does not have (a leftover from a previous install, #2185) — reseeding."
        rm -f "$KB_FLAG"; KB_ID=""
    fi
fi
if [ -n "$KB_ID" ] && [ "${KB_DOCS:-0}" -gt 0 ]; then
    echo "[dify-seed] KB already seeded (flag: $KB_FLAG, dataset ${KB_ID} present, ${KB_DOCS} documents); skipping."
    # The flag is not proof of indexing (#2445): confirm it, or say so.
    _kb_await_indexed "/console/api/datasets/${KB_ID}/documents?page=1&limit=100" || KB_FAILED=1
else
    if [ -z "$KB_ID" ]; then
        echo "[dify-seed] Provisioning knowledge base 'razzfazz.ai Stack Docs'..."
        KB_PAYLOAD='{"name":"razzfazz.ai Stack Docs","description":"Self-stack documentation seeded from the Help UI docs","permission":"only_me","provider":"vendor","indexing_technique":"high_quality"}'
        KB_RESP=$(dify_api POST "/console/api/datasets" "$KB_PAYLOAD" || true)
        KB_ID=$(printf '%s' "$KB_RESP" | python3 -c "import sys,json
try:
    d = json.load(sys.stdin); print(d.get('id') or d.get('dataset', {}).get('id') or '')
except Exception: pass" 2>/dev/null)
    else
        echo "[dify-seed] KB ${KB_ID} exists but holds no documents (#2445) — ingesting into it."
    fi
    if [ -z "$KB_ID" ]; then
        echo "[dify-seed] FAIL — KB creation response had no id; the KB is not seeded (#2445)."
        echo "[dify-seed] Response: $(printf '%s' "${KB_RESP:-}" | head -c 300)"
        KB_FAILED=1
    else
        echo "[dify-seed] KB id: $KB_ID"
        # From here the dataset exists; remember it so a re-run fills it rather than duplicating.
        echo "$KB_ID" > "$KB_FLAG"
        # The Help UI's docs: the community base every box has (core/help
        # bakes docs/community), plus the Enterprise overlay where it is
        # synced. core/help/own_docs was retired in 2026.07, which left the
        # KB empty on every box since (#2445).
        file_ids=()
        while IFS= read -r -d '' md; do
            echo "[dify-seed]   uploading $(basename "$md")..."
            fid=$(_seed_upload_md "$md" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('id') or '')
except Exception: pass" 2>/dev/null || true)
            if [ -n "$fid" ]; then file_ids+=("$fid"); else echo "[dify-seed]   upload of $(basename "$md") was refused"; fi
        done < <(for d in "${SCRIPT_DIR:-.}/docs/community" "${SCRIPT_DIR:-.}/overlay/enterprise/docs"; do
                     [ -d "$d" ] && find "$d" -type f -name '*.md' -print0
                 done | sort -z)
        if [ "${#file_ids[@]}" -eq 0 ]; then
            echo "[dify-seed] FAIL — no document could be uploaded; the KB is empty and not indexed (#2445)."
            KB_FAILED=1
        else
            ids_json=$(printf '"%s",' "${file_ids[@]}"); ids_json="[${ids_json%,}]"
            DOC_RESP=$(dify_api POST "/console/api/datasets/${KB_ID}/documents" \
                "{\"data_source\":{\"info_list\":{\"data_source_type\":\"upload_file\",\"file_info_list\":{\"file_ids\":${ids_json}}}},\"indexing_technique\":\"high_quality\",\"process_rule\":{\"mode\":\"automatic\",\"rules\":{}},\"doc_form\":\"text_model\",\"doc_language\":\"English\"}" || true)
            BATCH=$(printf '%s' "$DOC_RESP" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('batch') or '')
except Exception: pass" 2>/dev/null || true)
            if [ -z "$BATCH" ]; then
                echo "[dify-seed] FAIL — Dify did not accept the documents; the KB is not indexed (#2445): $(printf '%s' "$DOC_RESP" | head -c 300)"
                KB_FAILED=1
            else
                _kb_await_indexed "/console/api/datasets/${KB_ID}/batch/${BATCH}/indexing-status" || KB_FAILED=1
            fi
        fi
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

if [ "${KB_FAILED:-0}" -ne 0 ]; then
    echo "[dify-seed] Done, but the example knowledge base is NOT indexed (see FAIL above, #2445)."
    exit 1
fi
echo "[dify-seed] Done."
exit 0
