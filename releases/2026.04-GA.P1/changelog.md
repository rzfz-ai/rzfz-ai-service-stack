# 🔧 razzfazz.ai — Release 2026.04-GA.P1

**Patch 1 — Setup Wizard Fixes, License Updates & Developer Tooling**

*April 2026 · 16 commits · Post-GA hardening*

---

This first patch for the April GA release addresses issues found in clean installs and post-install configuration: the Cognee application wrongly appeared in the Authentik library even when the module was inactive, the setup wizard rendered raw `${VAR}` placeholders instead of resolved values, and the license container listed outdated model entries. Additionally, the release toolchain is hardened for CalVer versioning, and the post-install provisioning gains a new skill-import script for Open WebUI.

---

## ✨ Highlights

### 🔐 Cognee: Authentik App Hidden When Profile Inactive

On a clean install without the `cognee` profile enabled, the Cognee application was visible in the Authentik application library and linked to a URL that returned HTTP 404 — because the Authentik blueprint `11-cognee.yaml` had its `meta_launch_url` hardcoded to `https://${COGNEE_DOMAIN}/docs` instead of using the conditional `${COGNEE_LAUNCH_URL}` variable.

All other optional modules (LightRAG, Chat, Dify, Monitor, Gitea) already use the `blank://blank` pattern correctly: `init-authentik.sh` sets the launch URL variable to `blank://blank` when the profile is inactive, which causes Authentik to hide the app from the library. Cognee now follows the same pattern consistently.

When the `cognee` profile **is** active, the launch URL correctly resolves to `https://<cognee-domain>/docs` (the API documentation — Cognee has no separate web UI).

### 🧙 Setup Wizard: Resolved `.env` Values Instead of Raw Variables

Fields in the setup wizard's advanced SMTP page (and other sections) displayed literal placeholder text like `razzfazz-ai-box-1@${MAIN_DOMAIN}` instead of the resolved address — because the Python `config_manager.py` read `.env` values verbatim without expanding `${VAR}` references.

Docker Compose and Bash both perform variable substitution automatically when reading `.env` files. The setup app now does the same: after parsing all key/value pairs, a second pass resolves `${VAR}` and `${VAR:-default}` references using other values from the same file, matching the runtime behaviour the user actually sees.

### 📋 License Container: Gemma 4 / Apache-2.0, Llama 3 Removed

Updated the model license registry to reflect the current model portfolio:

- **Gemma 3 (Terms of Use)** → **Gemma 4 (Apache-2.0)** — Gemma 4 ships under the permissive Apache 2.0 license, a significant improvement over the previous custom Terms of Use
- **Llama 3** removed — no longer part of the default model presets

---

## 🆕 What Changed

### Authentik Blueprints (`core/Authentik/blueprints/base/`)
- `11-cognee.yaml`: `meta_launch_url` changed from hardcoded `"https://${COGNEE_DOMAIN}/docs"` to `"${COGNEE_LAUNCH_URL}"` — consistent with all other optional-module blueprints

### Setup App (`core/setup/app/config_manager.py`)
- `_read_env_file()`: Added a second expansion pass after parsing to resolve `${VAR}` and `${VAR:-default}` references within the same file
- Added `import re` (stdlib, no new dependency)

### License Container (`core/licenses/`)
- `app.py`: Replaced `("Gemma 3", "Terms of Use", "Gemma 3")` with `("Gemma 4", "Apache-2.0", "Gemma 4")`; removed `("Llama 3", "Llama Community License", "Llama 3")`
- `download_licenses.py`: Updated URL for Gemma to `https://raw.githubusercontent.com/google-deepmind/gemma/main/LICENSE` (Apache 2.0 text); removed Llama 3 entry

### Post-Install Scripts (`scripts/`)
- `import-skills-to-openwebui.sh`: New 396-line script that imports Claude and Pi SKILL.md files as Open WebUI Skills, using the Open WebUI API with correct authentication
- `generate-getting-started-pdf.sh`, `generate-quickstart-pdf.sh`: Minor updates

### Release Tooling (`scripts/prepare-release.sh`)
- Version sort key fixed: `[int(x) for x in version.split('.')]` replaced with plain `version` string sort — the integer parse crashed on CalVer strings like `2026.04-ga.1` because `04-ga` is not a valid integer

---

## 🐛 Bug Fixes

| Area | Fix |
|------|-----|
| **Authentik / Cognee** | Cognee app shown at HTTP 404 in Authentik library on installs without `cognee` profile — hardcoded launch URL replaced with conditional variable |
| **Setup Wizard** | SMTP "Sender Address" field shows `razzfazz-ai-box-1@${MAIN_DOMAIN}` literally — `config_manager.py` now expands `${VAR}` references after parsing `.env` |
| **License UI** | Gemma 3 listed under restrictive "Terms of Use" — updated to Gemma 4 / Apache-2.0 |
| **License UI** | Llama 3 listed despite not being part of the model portfolio |
| **Release Script** | `prepare-release.sh` crashes with `ValueError: invalid literal for int()` when sorting CalVer versions like `2026.04-ga.1` |

---

## 📊 By the Numbers

| Metric | Value |
|--------|-------|
| Commits since 2026.04-GA | 16 |
| Files changed | 12 |
| Lines added | 791 |
| Lines removed | 74 |
| Bug fixes | 5 |
| New scripts | 1 (`scripts/import-skills-to-openwebui.sh`) |
| New env variables | 0 |

---

## ⬆️ Upgrade Path

```bash
# From 2026.04-GA (online)
./razzfazz-upgrade.sh

# Dry run first
./razzfazz-upgrade.sh --check
```

The upgrade automatically:
1. Rebuilds the `razzfazz-setup` and `razzfazz-licenses` containers (updated Python code)
2. Re-applies the updated Authentik blueprint for Cognee (hides app when profile inactive)
3. Verifies service health

No env changes. No manual steps required.

---

Built with ❤️ by the razzfazz.ai team.

---

*Full diff: `git diff v2026.04-ga..v2026.04-ga.1`*
