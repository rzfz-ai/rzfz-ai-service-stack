# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Personal-MCP catalog loader (#36).

Reads core/mcp/personal-mcp-catalog.yaml (bind-mounted into the container at
/personal-mcp-catalog.yaml, overridable via PERSONAL_MCP_CATALOG) and exposes
the offerable per-user integrations to the UI + provisioner. Mirrors the
read-only / fail-safe-empty pattern of agent-manager's mcp_config.py and
llm_config.py.

The catalog is the SOURCE OF TRUTH for which integrations a user may provision,
their credential model (pat | oauth:<provider>), the proxy image/transport/port,
the credential fields the UI collects, and the env mapping used at launch to
inject the user's decrypted credentials into THEIR proxy container.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(os.environ.get("PERSONAL_MCP_CATALOG", "/personal-mcp-catalog.yaml"))

# `none` = no user-supplied credential; the manager auto-provisions the backing
# identity at launch (e.g. cognee-personal mints a per-user cognee API key).
VALID_CRED_MODELS_SIMPLE = {"pat", "none"}
VALID_TRANSPORTS = {"sse", "streamable-http"}
# #36 follow-up: coding agents (codex, claude) join the consumer set so a proxy
# can be wired into Codex's config.toml and Claude Code's .mcp.json too.
VALID_CONSUMERS = {"hermes", "moltis", "opencode", "codex", "claude", "all"}
# #36 follow-up (MCP-into-coding-agents): tenancy of the provisioned MCP server.
#   per-user : one instance per user, holding only that user's creds (today's
#              behaviour for every shipped entry — the safe default).
#   shared   : one instance serving all users. Only permitted for an image that
#              is genuinely multi-tenant, gated behind an explicit
#              `multi_tenancy_attested: true`. Single-tenant images (raw
#              cognee-mcp, the razzfazz-mcp-proxy that injects one user's creds
#              into env) MUST NOT be shared.
VALID_TENANCIES = {"per-user", "shared"}


def validate_catalog(spec: dict) -> list[str]:
    """Return a list of human-readable validation errors ([] if valid)."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["catalog is not a mapping"]
    if spec.get("version") != 1:
        errors.append(f"unsupported catalog version: {spec.get('version')}")
    seen: set[str] = set()
    for m in spec.get("integrations", []):
        mid = m.get("id")
        if not mid:
            errors.append("integration entry missing 'id'")
            continue
        if mid in seen:
            errors.append(f"{mid}: duplicate id")
        seen.add(mid)

        cred_model = str(m.get("cred_model", ""))
        is_oauth = cred_model.startswith("oauth:")
        if cred_model not in VALID_CRED_MODELS_SIMPLE and not is_oauth:
            errors.append(
                f"{mid}: invalid cred_model {cred_model!r} "
                f"(allowed: 'pat' or 'oauth:<provider>')"
            )
        if is_oauth and not cred_model.split(":", 1)[1]:
            errors.append(f"{mid}: oauth cred_model missing provider")

        if not m.get("image"):
            errors.append(f"{mid}: missing 'image'")
        if m.get("transport") not in VALID_TRANSPORTS:
            errors.append(
                f"{mid}: invalid transport {m.get('transport')!r} "
                f"(allowed: {sorted(VALID_TRANSPORTS)})"
            )
        if not isinstance(m.get("container_port"), int):
            errors.append(f"{mid}: 'container_port' must be an int")

        # #36 follow-up: tenancy gate. Every entry declares per-user | shared;
        # `shared` requires an explicit multi-tenancy attestation so a
        # single-tenant image can never be silently exposed to all users.
        tenancy = m.get("tenancy")
        if tenancy not in VALID_TENANCIES:
            errors.append(
                f"{mid}: invalid tenancy {tenancy!r} "
                f"(allowed: {sorted(VALID_TENANCIES)})"
            )
        elif tenancy == "shared" and m.get("multi_tenancy_attested") is not True:
            errors.append(
                f"{mid}: tenancy 'shared' requires 'multi_tenancy_attested: true' "
                f"(the image must be verified multi-tenant with per-request auth)"
            )

        consumers = m.get("consumers", [])
        if not consumers:
            errors.append(f"{mid}: must declare at least one consumer")
        bad = [c for c in consumers if c not in VALID_CONSUMERS]
        if bad:
            errors.append(f"{mid}: unknown consumer(s) {bad}; valid: {sorted(VALID_CONSUMERS)}")

        # PAT entries must declare the fields the UI collects.
        if cred_model == "pat":
            cred_fields = m.get("cred_fields") or []
            if not cred_fields:
                errors.append(f"{mid}: pat cred_model requires non-empty 'cred_fields'")
            for f in cred_fields:
                if not f.get("key"):
                    errors.append(f"{mid}: a cred_field is missing 'key'")
    return errors


class PersonalMCPCatalog:
    """Read-only view of the personal-MCP catalog. Fail-safe to empty."""

    def __init__(self, path: str | None = None):
        self._path = Path(path) if path else CATALOG_PATH
        self._spec = self._load()

    def _load(self) -> dict:
        p = self._path
        if not p.exists():
            logger.warning(
                "personal-mcp-catalog.yaml not at %s — no integrations offered; "
                "is the bind-mount missing from mcp-manager compose.yml?",
                p,
            )
            return {"version": 1, "integrations": []}
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — personal-MCP catalog unavailable.")
            return {"version": 1, "integrations": []}
        try:
            return yaml.safe_load(p.read_text()) or {"version": 1, "integrations": []}
        except Exception:
            logger.exception("personal-mcp-catalog.yaml unreadable — offering nothing.")
            return {"version": 1, "integrations": []}

    def raw(self) -> dict:
        return self._spec

    def all(self) -> list[dict]:
        return list(self._spec.get("integrations", []))

    def get(self, mcp_id: str) -> dict | None:
        for m in self.all():
            if m.get("id") == mcp_id:
                return m
        return None

    def cred_model(self, mcp_id: str) -> str | None:
        """Return 'pat' for PAT integrations, 'oauth' for any oauth:* model,
        or None if the id is unknown. (The full 'oauth:<provider>' is available
        via get(mcp_id)['cred_model'].)"""
        m = self.get(mcp_id)
        if not m:
            return None
        cm = str(m.get("cred_model", ""))
        return "oauth" if cm.startswith("oauth:") else cm

    def tenancy(self, mcp_id: str) -> str | None:
        """Return 'per-user' | 'shared' for the entry (None if unknown). Missing
        tenancy in the raw data defaults to 'per-user' — the safe default that
        matches today's per-user-instance behaviour."""
        m = self.get(mcp_id)
        if not m:
            return None
        return str(m.get("tenancy") or "per-user")

    def oauth_provider(self, mcp_id: str) -> str | None:
        """Return the OAuth provider id for an oauth:* integration, else None."""
        m = self.get(mcp_id)
        if not m:
            return None
        cm = str(m.get("cred_model", ""))
        return cm.split(":", 1)[1] if cm.startswith("oauth:") else None
