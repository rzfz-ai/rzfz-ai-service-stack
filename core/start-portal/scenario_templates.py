# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#248 P3 slice 4/4 — curated scenario-template gallery for the shortcuts
admin editor.

Presentation-only starter data (mirrors the CATALOG-constant style used by
e.g. ``modules/mcp-manager/manager/app/services/catalog.py``, but has no
YAML loader — this list is small and curated, so it lives as a plain Python
constant): each entry PRE-FILLS the admin "New shortcut" form (title / icon /
category / kind / a config skeleton) so an operator can start from a
known-good scenario instead of a blank form. The operator still reviews and
explicitly saves — nothing here writes to the DB, calls a resolver, or
touches ``build_redirect_url`` / governance. ``app.py`` reads
``SCENARIO_TEMPLATES`` and hands it to the editor template, which renders it
server-side (so the "gallery renders" guard can assert on plain HTML, no JS
runner needed) and also embeds it as a JSON blob the client-side prefill
picks up (see ``admin-shortcuts.js``::``applyTemplate``).
"""
from __future__ import annotations

from shortcuts import _REDIRECT_KINDS

# The config field(s) each P1 kind's gatherConfig()/build_redirect_url()
# requires — used only to validate that a template's config skeleton is not
# missing the one field its kind actually needs. Deliberately a SUBSET check
# (missing required fields fail; extra/optional fields like `prompt_tmpl`
# are fine either way) so a template can still ship "" placeholders for the
# operator to fill in without failing this guard on shape alone.
_REQUIRED_CONFIG_FIELDS = {
    "owui_persona": {"model_id"},
    "owui_app": {"model_id"},
    "dify_chat": {"app_path"},
    "cloud_link": {"provider"},
}

SCENARIO_TEMPLATES = [
    {
        "id": "pdf-json-extraction",
        "title": "PDF → JSON extraction",
        "description": "Upload a PDF and get structured JSON back from a "
                        "pre-baked OWUI scenario model.",
        "icon": "\U0001F4C4",
        "category": "Document AI",
        "kind": "owui_app",
        "config": {"model_id": "", "prompt_tmpl":
                    "Extract the key fields from this document as JSON."},
    },
    {
        "id": "reklamation-triage",
        "title": "Reklamation triage",
        "description": "Classify and route an incoming complaint using a "
                        "pre-baked OWUI scenario model.",
        "icon": "\U0001F5C2️",
        "category": "Support",
        "kind": "owui_app",
        "config": {"model_id": "", "prompt_tmpl":
                    "Triage this complaint and suggest the right team: {{input}}"},
    },
    {
        "id": "dify-workflow-chat",
        "title": "Dify workflow chat",
        "description": "Open a specific Dify chat app end users can run "
                        "directly, no setup required.",
        "icon": "\U0001F500",
        "category": "Automation",
        "kind": "dify_chat",
        "config": {"app_path": ""},
    },
    # ── #248 App Builder — the 3 remaining prototype gallery cards ──────────
    # (the "Blank app" card in the prototype has no config skeleton of its
    # own to prefill — the App Builder UI treats a click on it as a plain
    # resetForm(), never a template lookup — so only these 3 add new curated
    # entries here; SCENARIO_TEMPLATES stays the single source both the
    # server-rendered gallery and the client-side prefill read from.)
    {
        "id": "chat-persona",
        "title": "Chat persona",
        "description": "A named OWUI persona: model + system prompt + "
                        "knowledge baked in.",
        "icon": "\U0001F4AC",
        "category": "Assistants",
        "kind": "owui_persona",
        "config": {"model_id": ""},
    },
    {
        "id": "cloud-link",
        "title": "Cloud link",
        "description": "A deep link into an external tool with an optional "
                        "prefilled prompt.",
        "icon": "☁️",
        "category": "Cloud Tools",
        "kind": "cloud_link",
        "config": {"provider": "claude", "prompt_tmpl": ""},
    },
]


def validate_templates(templates=None) -> list[str]:
    """Return a list of human-readable validation errors (``[]`` if valid).

    Mirrors ``mcp-manager``'s ``validate_catalog()`` shape: every entry needs
    a unique ``id``, a non-empty ``title``, a kind that is one of the P1
    redirect kinds (``shortcuts._REDIRECT_KINDS``), and a ``config`` dict
    that carries at least that kind's required field(s).
    """
    if templates is None:
        templates = SCENARIO_TEMPLATES
    errors: list[str] = []
    seen: set[str] = set()
    for t in templates:
        tid = t.get("id")
        if not tid:
            errors.append("template missing 'id'")
            continue
        if tid in seen:
            errors.append(f"{tid}: duplicate id")
        seen.add(tid)
        if not t.get("title"):
            errors.append(f"{tid}: missing title")
        kind = t.get("kind")
        if kind not in _REDIRECT_KINDS:
            errors.append(f"{tid}: invalid kind {kind!r}")
            continue
        cfg = t.get("config")
        if not isinstance(cfg, dict):
            errors.append(f"{tid}: config must be a dict")
            continue
        missing = _REQUIRED_CONFIG_FIELDS.get(kind, set()) - set(cfg.keys())
        if missing:
            errors.append(
                f"{tid}: config missing required field(s) {sorted(missing)} "
                f"for kind {kind!r}")
    return errors
