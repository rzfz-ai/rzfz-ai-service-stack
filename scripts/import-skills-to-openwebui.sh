#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# razzfazz.ai — Import Claude/pi SKILL.md files into Open WebUI Skills
# ==============================================================================
#
# Converts Claude Code / pi-style SKILL.md files into Open WebUI Skills via API.
#
# A SKILL.md file has the structure:
#
#   ---
#   name: my-skill
#   description: Use when the user needs X. Triggers on "do X", "help with X".
#   ---
#
#   # My Skill
#   Full markdown body with instructions, examples, code blocks...
#
# Open WebUI Skill fields:
#   id          — slug (lowercase, hyphenated)
#   name        — human label
#   description — short summary (shown in UI)
#   content     — full text injected into model context (the skill body)
#   meta.tags   — list of tags
#
# Usage:
#   ./scripts/import-skills-to-openwebui.sh <SKILL.md>           # single file
#   ./scripts/import-skills-to-openwebui.sh <skills-dir/>        # all SKILL.md under dir
#   ./scripts/import-skills-to-openwebui.sh --list               # list imported skills
#   ./scripts/import-skills-to-openwebui.sh --delete <id>        # delete a skill by id
#   ./scripts/import-skills-to-openwebui.sh --dry-run <path>     # preview without importing
#
# Options:
#   --owui-url URL        Open WebUI base URL (default: http://127.0.0.1:8080)
#   --owui-user EMAIL     Admin email (default: razzfazz-ai-admin@<MAIN_DOMAIN>)
#   --owui-pass PASS      Admin password (default: AUTHENTIK_BOOTSTRAP_PASSWORD from .env)
#   --tags TAG1,TAG2      Extra tags to add to all imported skills
#   --update              Update existing skills instead of skipping (default: skip)
#   --dry-run             Show what would be imported without making changes
#   --list                List all skills currently in Open WebUI
#   --delete ID           Delete a skill by ID
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

print_step()    { echo -e "${BLUE}[STEP] $1${NC}"; }
print_success() { echo -e "${GREEN}  ✓ $1${NC}"; }
print_warning() { echo -e "${YELLOW}  ! $1${NC}"; }
print_error()   { echo -e "${RED}  ✗ $1${NC}"; }
print_info()    { echo -e "${CYAN}  ℹ $1${NC}"; }
print_skip()    { echo -e "  ↷ $1"; }

# ==============================================================================
# Load .env
# ==============================================================================
load_env() {
    local env_file="${STACK_DIR}/.env"
    if [ -f "$env_file" ]; then
        set +e
        eval "$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$env_file" | grep -v '[<>]' | sed 's/^/export /')"
        set -e
    fi
}
load_env

# ==============================================================================
# Defaults
# ==============================================================================
OWUI_URL="${OWUI_URL:-http://127.0.0.1:${OPENWEBUI_PORT:-8080}}"
OWUI_USER="${OWUI_USER:-razzfazz-ai-admin@${MAIN_DOMAIN:-localhost}}"
OWUI_PASS="${OWUI_PASS:-${AUTHENTIK_BOOTSTRAP_PASSWORD:-admin}}"
EXTRA_TAGS=""
UPDATE_EXISTING=false
DRY_RUN=false
MODE="import"  # import | list | delete
DELETE_ID=""
PATHS=()

# ==============================================================================
# Parse Arguments
# ==============================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --owui-url)   OWUI_URL="$2"; shift 2 ;;
        --owui-user)  OWUI_USER="$2"; shift 2 ;;
        --owui-pass)  OWUI_PASS="$2"; shift 2 ;;
        --tags)       EXTRA_TAGS="$2"; shift 2 ;;
        --update)     UPDATE_EXISTING=true; shift ;;
        --dry-run)    DRY_RUN=true; shift ;;
        --list)       MODE="list"; shift ;;
        --delete)     MODE="delete"; DELETE_ID="$2"; shift 2 ;;
        -h|--help)
            head -60 "$0" | grep "^#" | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            print_error "Unknown option: $1"
            exit 1
            ;;
        *)
            PATHS+=("$1")
            shift
            ;;
    esac
done

# ==============================================================================
# Authenticate with Open WebUI
# ==============================================================================
OWUI_TOKEN=""

owui_login() {
    local resp
    resp=$(curl -sf --max-time 10 -X POST "${OWUI_URL}/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${OWUI_USER}\",\"password\":\"${OWUI_PASS}\"}" 2>/dev/null)
    
    OWUI_TOKEN=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
    
    if [ -z "$OWUI_TOKEN" ]; then
        print_error "Failed to authenticate with Open WebUI at ${OWUI_URL}"
        print_info "Check --owui-user and --owui-pass"
        exit 1
    fi
}

owui_api() {
    local method="$1" path="$2" data="$3"
    if [ -n "$data" ]; then
        curl -sf --max-time 15 -X "$method" "${OWUI_URL}${path}" \
            -H "Authorization: Bearer ${OWUI_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "$data" 2>/dev/null
    else
        curl -sf --max-time 15 -X "$method" "${OWUI_URL}${path}" \
            -H "Authorization: Bearer ${OWUI_TOKEN}" 2>/dev/null
    fi
}

# ==============================================================================
# Parse a SKILL.md file
# ==============================================================================
parse_skill() {
    local file="$1"
    
    # Extract YAML frontmatter between --- delimiters
    local fm_name="" fm_desc=""
    if grep -q "^---" "$file"; then
        fm_name=$(awk '/^---/{f++; next} f==1{print}' "$file" | grep "^name:" | head -1 | sed 's/^name:\s*//' | xargs)
        fm_desc=$(awk '/^---/{f++; next} f==1{print}' "$file" | grep "^description:" | head -1 | sed 's/^description:\s*//' | xargs)
    fi
    
    # Fallback: derive name from first H1 heading or filename
    if [ -z "$fm_name" ]; then
        fm_name=$(grep "^# " "$file" | head -1 | sed 's/^# //' | xargs)
    fi
    if [ -z "$fm_name" ]; then
        fm_name=$(basename "$(dirname "$file")")
    fi
    
    # Extract body (everything after the closing ---)
    local body
    if grep -q "^---" "$file"; then
        body=$(awk 'BEGIN{f=0} /^---/{f++; if(f==2){found=1; next}} found{print}' "$file")
    else
        body=$(cat "$file")
    fi
    
    # Fold in any reference markdown files alongside the skill
    local skill_dir
    skill_dir="$(dirname "$file")"
    local refs_dir="${skill_dir}/references"
    if [ -d "$refs_dir" ]; then
        for ref in "${refs_dir}"/*.md; do
            [ -f "$ref" ] || continue
            local ref_name
            ref_name=$(basename "$ref" .md)
            body="${body}

---
## Reference: ${ref_name}

$(cat "$ref")
"
        done
    fi
    
    # Generate ID: lowercase, spaces/underscores to hyphens, strip special chars
    local skill_id
    skill_id=$(echo "$fm_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//')
    
    # Extract tags: from extra tags param + try to infer from description/name
    local tags="[]"
    if [ -n "$EXTRA_TAGS" ]; then
        tags=$(echo "$EXTRA_TAGS" | python3 -c "import sys,json; t=sys.stdin.read().strip().split(','); print(json.dumps([x.strip() for x in t if x.strip()]))")
    fi
    
    # Output as JSON-safe variables
    PARSED_ID="$skill_id"
    PARSED_NAME="$fm_name"
    PARSED_DESC="$fm_desc"
    PARSED_CONTENT="$body"
    PARSED_TAGS="$tags"
}

# ==============================================================================
# Import a single SKILL.md
# ==============================================================================
import_skill() {
    local file="$1"
    
    parse_skill "$file"
    
    if [ -z "$PARSED_ID" ] || [ -z "$PARSED_NAME" ]; then
        print_warning "Skipping $(basename "$file") — could not parse name"
        return
    fi
    
    if [ "$DRY_RUN" = true ]; then
        echo ""
        echo -e "${CYAN}  [DRY RUN] Would import:${NC}"
        echo "    id:          $PARSED_ID"
        echo "    name:        $PARSED_NAME"
        echo "    description: ${PARSED_DESC:0:80}..."
        echo "    content:     $(echo "$PARSED_CONTENT" | wc -c) bytes"
        echo "    tags:        $PARSED_TAGS"
        return
    fi
    
    # Check if skill already exists
    local existing
    existing=$(owui_api GET "/api/v1/skills/id/${PARSED_ID}" 2>/dev/null || echo "")
    
    if echo "$existing" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('id') else 1)" 2>/dev/null; then
        if [ "$UPDATE_EXISTING" = false ]; then
            print_skip "  ↷ Skill '${PARSED_NAME}' (${PARSED_ID}) already exists — skipping (use --update to overwrite)"
            return
        fi
        # Update
        local payload
        payload=$(python3 -c "
import json, sys
content = sys.stdin.read()
print(json.dumps({
    'id': '$PARSED_ID',
    'name': $(echo "$PARSED_NAME" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().rstrip()))"),
    'description': $(echo "$PARSED_DESC" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().rstrip()))"),
    'content': content,
    'meta': {'tags': $PARSED_TAGS},
    'is_active': True
}))
" <<< "$PARSED_CONTENT")
        
        local resp
        resp=$(owui_api POST "/api/v1/skills/id/${PARSED_ID}/update" "$payload" 2>/dev/null)
        if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('id') else 1)" 2>/dev/null; then
            print_success "Updated: ${PARSED_NAME} (${PARSED_ID})"
        else
            print_error "Failed to update: ${PARSED_NAME} — $(echo "$resp" | head -c 200)"
        fi
        return
    fi
    
    # Create new
    local payload
    payload=$(python3 -c "
import json, sys
content = sys.stdin.read()
print(json.dumps({
    'id': '$PARSED_ID',
    'name': $(echo "$PARSED_NAME" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().rstrip()))"),
    'description': $(echo "$PARSED_DESC" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().rstrip()))"),
    'content': content,
    'meta': {'tags': $PARSED_TAGS},
    'is_active': True
}))
" <<< "$PARSED_CONTENT")
    
    local resp
    resp=$(owui_api POST "/api/v1/skills/create" "$payload" 2>/dev/null)
    
    if echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('id') else 1)" 2>/dev/null; then
        print_success "Imported: ${PARSED_NAME} (${PARSED_ID})"
    else
        print_error "Failed to import: ${PARSED_NAME} — $(echo "$resp" | head -c 200)"
    fi
}

# ==============================================================================
# List all skills
# ==============================================================================
list_skills() {
    local resp
    resp=$(owui_api GET "/api/v1/skills/" 2>/dev/null)
    
    python3 -c "
import sys, json
skills = json.load(sys.stdin)
if not skills:
    print('  No skills found.')
    sys.exit(0)
print(f'  {len(skills)} skill(s):')
print()
for s in sorted(skills, key=lambda x: x.get('name','')):
    active = '✓' if s.get('is_active') else '○'
    desc = (s.get('description') or '')[:70]
    if len(s.get('description','')) > 70: desc += '...'
    tags = ', '.join(s.get('meta',{}).get('tags',[]))
    print(f\"  {active}  {s['id']:<35} {s['name']}\")
    if desc:
        print(f\"       {desc}\")
    if tags:
        print(f\"       Tags: {tags}\")
    print()
" <<< "$resp"
}

# ==============================================================================
# Main
# ==============================================================================
case "$MODE" in
    list)
        owui_login
        print_step "Skills in Open WebUI (${OWUI_URL})"
        list_skills
        exit 0
        ;;
    delete)
        owui_login
        print_step "Deleting skill: ${DELETE_ID}"
        resp=$(owui_api DELETE "/api/v1/skills/id/${DELETE_ID}/delete" 2>/dev/null)
        if echo "$resp" | python3 -c "import sys; exit(0 if 'true' in sys.stdin.read().lower() else 1)" 2>/dev/null; then
            print_success "Deleted: ${DELETE_ID}"
        else
            print_error "Failed to delete: ${DELETE_ID} — ${resp}"
            exit 1
        fi
        exit 0
        ;;
esac

# Import mode
if [ ${#PATHS[@]} -eq 0 ]; then
    print_error "Usage: $0 <SKILL.md | skills-dir/> [options]"
    echo ""
    echo "  $0 path/to/SKILL.md"
    echo "  $0 ~/.claude/skills/"
    echo "  $0 --list"
    echo "  $0 --delete my-skill-id"
    exit 1
fi

[ "$DRY_RUN" = false ] && owui_login

print_step "Importing skills to Open WebUI (${OWUI_URL})"
[ "$DRY_RUN" = true ] && print_info "Dry run mode — no changes will be made"

IMPORTED=0
SKIPPED=0
FAILED=0

for path in "${PATHS[@]}"; do
    if [ -f "$path" ] && [[ "$(basename "$path")" == "SKILL.md" ]]; then
        # Single SKILL.md file
        import_skill "$path"
        ((IMPORTED++)) || true
        
    elif [ -f "$path" ] && [[ "$path" == *.md ]]; then
        # Any .md file — treat as a skill
        import_skill "$path"
        ((IMPORTED++)) || true
        
    elif [ -d "$path" ]; then
        # Directory — find all SKILL.md files recursively
        while IFS= read -r skill_file; do
            import_skill "$skill_file"
            ((IMPORTED++)) || true
        done < <(find "$path" -name "SKILL.md" -type f | sort)
        
    else
        print_warning "Not found: $path"
        ((FAILED++)) || true
    fi
done

echo ""
echo -e "${GREEN}Done.${NC} ${IMPORTED} processed, ${SKIPPED} skipped, ${FAILED} failed."
