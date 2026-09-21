# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz-shortcuts domain logic (2026.08 — issue #185).

Pure functions + psycopg2 DB access, mirroring the idioms in
``core/start-portal/app.py`` (``_db_conn`` / ``_ensure_prefs_table`` /
``_set_pinned``). No Flask routing lives here — ``app.py`` wires the routes.

A *shortcut* is an admin-defined, DB-backed tile in the start portal. P1 ships
the **redirect** kinds only (``owui_persona``, ``dify_chat``, ``cloud_link``):
the portal builds a pre-configured target URL server-side and the tile opens it.
In-portal execution (``dify_workflow``/``owui_prompt``/...), stored secrets, and
the ``cloud_api``/``agent_message`` kinds are P2/P3 and deliberately absent.

Variable injection is a **fixed whitelist**, never a template engine — user data
is never eval'd. In URL context every substituted value is URL-encoded so
``{{input}}`` cannot break out of the query string.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import requests

logger = logging.getLogger(__name__)

# ── Variable whitelist ───────────────────────────────────────────────────────
# The ONLY placeholders that resolve. Anything else is left literal. `ctx` is
# built server-side from the forward-auth identity + the user's one input box.
_WHITELIST = {
    "{{user.name}}":     lambda c: c.get("name", "") or "",
    "{{user.email}}":    lambda c: c.get("email", "") or "",
    "{{user.username}}": lambda c: c.get("username", "") or "",
    "{{user.groups}}":   lambda c: ",".join(c.get("groups", []) or []),
    "{{date}}":          lambda c: c.get("date", "") or "",
    "{{domain}}":        lambda c: c.get("domain", "") or "",
    "{{input}}":         lambda c: c.get("input", "") or "",
}


def render_vars(template: str, ctx: dict, mode: str = "text") -> str:
    """Substitute whitelisted ``{{placeholders}}`` in ``template`` from ``ctx``.

    ``mode="url"`` URL-encodes each substituted value (``safe=""`` — encode
    everything, including ``/`` and ``&``) so it is safe inside a query string.
    ``mode="text"`` substitutes raw. Unknown placeholders are left literal.
    """
    out = template or ""
    for token, getter in _WHITELIST.items():
        if token in out:
            val = getter(ctx)
            out = out.replace(token, quote(val, safe="") if mode == "url" else val)
    return out


# ── Redirect-URL builders (P1 kinds) ─────────────────────────────────────────
# Best-effort prompt-prefill bases for the cloud deep-link kind. If a provider
# drops its prefill param, the shortcut still opens the chat (degrade, not fail).
_CLOUD_PREFILL = {
    "claude":  "https://claude.ai/new?q=",
    "chatgpt": "https://chatgpt.com/?q=",
    "gemini":  "https://gemini.google.com/app?q=",
}

# The set of kinds P1 ships. Used both here and by the tile/route layer so the
# "redirect only in P1" gate lives in one place.
_REDIRECT_KINDS = frozenset({"owui_persona", "dify_chat", "cloud_link",
                             "owui_app"})


def build_redirect_url(kind: str, config: dict, ctx: dict) -> str:
    """Build the target URL for a redirect shortcut. ``ctx["domain"]`` is the box.

    Raises ``ValueError`` for a non-redirect kind or an unknown cloud provider,
    ``KeyError`` when a required config field is missing.
    """
    dom = ctx["domain"]
    if kind == "owui_persona":
        return f"https://chat.{dom}/?models={quote(config['model_id'], safe='')}"
    if kind == "owui_app":
        # #248 P1: a pre-baked OWUI scenario. `model_id` references an OWUI
        # WORKSPACE model (0.11: Workspace -> Models) — that object carries
        # the system prompt + knowledge collections, so this kind gets
        # prompt+KB pre-baking WITHOUT the portal storing either. Optional
        # `prompt_tmpl` prefills the first message via ?q= (whitelist-
        # rendered, whole-value encoded like cloud_link). Degrade, not
        # fail: if OWUI drops ?q= the chat still opens on the model.
        # #1192: the ?q= deep link is now the FALLBACK only. OWUI's ?q=
        # auto-submit races the forward-auth redirect chain (double SPA
        # mount → the answer lands in a second chat + ghost chats), so a
        # prompt-bearing owui_app tile routes through the portal
        # (`tile_url` → /shortcuts/<id>/open), which creates the chat
        # server-side (`start_owui_app_chat`) and redirects to /c/<chat_id>.
        # This URL is what the user gets when that path is unavailable.
        url = f"https://chat.{dom}/?models={quote(config['model_id'], safe='')}"
        tmpl = config.get("prompt_tmpl") or ""
        if tmpl:
            rendered = render_vars(tmpl, ctx, mode="text")
            url += "&q=" + quote(rendered, safe="")
        return url
    if kind == "dify_chat":
        path = (config.get("app_path") or "").lstrip("/")
        return f"https://dify.{dom}/{path}"
    if kind == "cloud_link":
        base = _CLOUD_PREFILL.get(config.get("provider"))
        if not base:
            raise ValueError(f"unknown cloud provider: {config.get('provider')}")
        # The rendered prompt is a SINGLE query-param value, so encode it whole:
        # substitute the whitelist raw (text mode), then percent-encode the
        # entire result. (render_vars' own mode="url" only encodes substituted
        # values — correct when the template itself carries URL syntax, wrong
        # here where the literal prompt text is also part of the value.)
        rendered = render_vars(config.get("prompt_tmpl", ""), ctx, mode="text")
        return base + quote(rendered, safe="")
    raise ValueError(f"not a redirect kind: {kind}")


def owui_app_uses_server_chat(config: dict) -> bool:
    """#1192 — an ``owui_app`` with a non-blank ``prompt_tmpl`` auto-submits a
    first message, which is exactly the case that must NOT go through OWUI's
    ``?q=`` link. A prompt-less app opens the model without submitting, so it
    has no race and keeps the plain deep link."""
    return bool(((config or {}).get("prompt_tmpl") or "").strip())


def portal_open_path(sid) -> str:
    """Portal-relative href for a tile that must be resolved server-side at
    click time (``app.py::shortcut_open``)."""
    return f"/shortcuts/{quote(str(sid), safe='')}/open"


def tile_url(row: dict, ctx: dict) -> str:
    """The href a shortcut TILE carries.

    Always builds the redirect URL first — that keeps ``build_redirect_url``'s
    validation (``KeyError``/``ValueError`` on a broken config) so
    ``shortcuts_as_tiles`` still skips bad rows — and then swaps in the portal's
    ``/shortcuts/<id>/open`` path for the one kind whose target must be created
    at click time (#1192). Every other kind keeps its direct URL unchanged.
    """
    kind = row.get("kind")
    config = row.get("config") or {}
    url = build_redirect_url(kind, config, ctx)
    if kind == "owui_app" and owui_app_uses_server_chat(config):
        return portal_open_path(row["id"])
    return url


# ── #248 App Builder — icon storage + render + validation ────────────────────
# A shortcut's `icon` column (TEXT) carries one of THREE shapes, chosen in the
# admin editor's icon picker (admin-shortcuts.js):
#   1. a plain emoji/short glyph — the pre-#248 default, unchanged.
#   2. `curated:<name>` — a reference into the EXISTING /static/icons/ PNG set
#      already used by the non-shortcut portal tiles (razzfazz-ai_<name>_icon.png).
#      Restricted to a fixed allowlist so an admin-authored value can never
#      reach the filesystem/URL as an unvalidated path fragment.
#   3. a `data:image/...;base64,...` URI — a custom upload, downscaled to a
#      ~128px square client-side (mirrors start-portal-profile.js's avatar
#      downscale) and validated server-side here exactly like #299's avatar
#      path (`app.py::_validate_avatar_data_uri`) — magic-byte sniffed, never
#      the client-claimed mime, capped far smaller than an avatar since an
#      icon renders at a fraction of the size.
CURATED_ICONS = ("pdf", "chat", "documents", "workflow", "search",
                  "rag", "mcp", "coding")

MAX_ICON_BYTES = 128 * 1024   # 128 KB raw (pre-base64) — generous for a
                              # client-downscaled ~128px icon; backstop only.

_ICON_DATA_URI_RE = re.compile(r'^data:([^;,]+);base64,(.+)$', re.S)

# Same sniff table as app.py's _validate_avatar_data_uri (#299) — duplicated
# rather than imported so shortcuts.py's icon validation has no dependency on
# app.py's private helpers (scope: shortcuts.py owns icon/render logic).
_ICON_IMAGE_MAGIC = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def _sniff_icon_mime(raw: bytes):
    for magic, mime in _ICON_IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if len(raw) >= 12 and raw[0:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return None


def validate_icon_data_uri(data_uri: str, max_bytes: int = MAX_ICON_BYTES):
    """Validate + normalize an uploaded icon `data:` URI.

    Returns the normalized data URI (mime rebuilt from the SNIFFED magic
    bytes, never the client-claimed one) on success, or ``None`` on any
    failure (malformed, oversize, or not a recognised image format).
    """
    m = _ICON_DATA_URI_RE.match((data_uri or '').strip())
    if not m:
        return None
    _claimed_mime, b64 = m.group(1), m.group(2)
    # CFG-13: check the ENCODED length before decoding. Decoding first made
    # `max_bytes` a cap on what is STORED rather than on what is PROCESSED —
    # a crafted POST could make the portal materialise hundreds of MB. base64
    # expands 4/3 (+ padding), so anything longer cannot decode within the cap.
    if len(b64) > (max_bytes * 4) // 3 + 8:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > max_bytes:
        return None
    sniffed = _sniff_icon_mime(raw)
    if not sniffed:
        return None
    return f'data:{sniffed};base64,{b64}'


def validate_icon(value):
    """Validate a shortcut's proposed `icon` value at save time (called from
    the create/update routes in app.py). Returns ``(ok, normalized, error)``:
    ``normalized`` is the value to actually persist on success; ``error`` is
    a human-readable reason on failure. An empty/missing value normalizes to
    the pre-#248 default emoji, matching the DB column's own default.
    """
    value = (value or '').strip()
    if not value:
        return True, '✨', None
    if value.startswith('data:'):
        normalized = validate_icon_data_uri(value)
        if not normalized:
            return False, None, (
                'invalid icon upload — must be a PNG/JPEG/GIF/WEBP image no '
                f'larger than {MAX_ICON_BYTES // 1024} KB')
        return True, normalized, None
    if value.startswith('curated:'):
        name = value.split(':', 1)[1]
        if name not in CURATED_ICONS:
            return False, None, f'unknown curated icon: {name!r}'
        return True, value, None
    # Plain emoji/glyph — the pre-#248 shape. Generous length cap: a single
    # emoji is 1-2 codepoints, but ZWJ-sequence emoji (e.g. family/flag
    # combos) can run longer; anything past this is not a glyph any more.
    if len(value) > 32:
        return False, None, ('icon must be a short emoji/glyph, a '
                              "'curated:<name>' reference, or an uploaded image")
    return True, value, None


def icon_tile_fields(icon):
    """Translate a stored shortcut `icon` value into the tile-dict field(s)
    `_tile.html` renders. Three mutually-exclusive shapes, mirroring
    `validate_icon` above:

      * `data:image/...` → `{"icon_data_uri": <value>}` — _tile.html's new
        (additive) branch renders it as an `<img>`.
      * `curated:<name>` → `{"icon": <name>}` — falls into _tile.html's
        PRE-EXISTING PNG-lookup branch unchanged (same one non-shortcut
        tiles use), since `name` is drawn from the same CURATED_ICONS
        allowlist as the /static/icons/razzfazz-ai_<name>_icon.png files.
        An unrecognised name (e.g. a pre-validation legacy row) falls back
        to 'home' rather than emitting a dead image link.
      * anything else (emoji/glyph, the pre-#248 default) →
        `{"icon_emoji": <value>}`, byte-for-byte the pre-#248 behaviour.
    """
    icon = icon or '✨'
    if icon.startswith('data:image/'):
        return {'icon_data_uri': icon}
    if icon.startswith('curated:'):
        name = icon.split(':', 1)[1]
        return {'icon': name if name in CURATED_ICONS else 'home'}
    return {'icon_emoji': icon}


# ── Access resolution ────────────────────────────────────────────────────────

def user_can_use(shortcut: dict, user: dict) -> bool:
    """May ``user`` run ``shortcut``? Admin bypass, ``any`` group, or overlap.

    ``user`` is the ``_get_user()`` dict (``is_admin`` bool + ``groups`` list);
    ``shortcut["allowed_groups"]`` is the Authentik group-name list. This is the
    SAME rule the portal's ``_filter_tile`` uses for tile visibility, so a tile a
    user can see is exactly a shortcut they can ``/run``.
    """
    if user.get("is_admin"):
        return True
    # #248 S1: visibility classes. Backwards-compatible: every pre-#248 row
    # defaults to 'company' and keeps the exact old rule below.
    vis = shortcut.get("visibility") or "company"
    if vis == "private":
        return (shortcut.get("owner_username") or "") == (user.get("username") or "")
    allowed = set(shortcut.get("allowed_groups") or [])
    if vis == "group":
        # group visibility REQUIRES an overlap — no 'any' escape hatch
        return bool(allowed & set(user.get("groups") or []))
    if vis == "company" and not allowed:
        # CFG-15: an UNRESTRICTED company row means COMPANY-WIDE. The editor
        # labels this visibility "Company-wide (every signed-in user)" and
        # the publish-governance gate (#248 P2) exists precisely to protect
        # that broadcast — but the resolver fell through to the pre-#248 rule
        # (`'any' in allowed_groups`, else group overlap), so an admin who
        # picked Company and ticked no group produced a shortcut visible to
        # admins ONLY: the broadcast never happened.
        #
        # Deliberately scoped to rows with NO allowed_groups. Every pre-#248
        # row was migrated with `visibility DEFAULT 'company'` while KEEPING
        # its group list, so returning True for those too would silently
        # widen existing group-restricted shortcuts to the whole box on
        # upgrade. A company row that still names groups therefore keeps the
        # old rule below (and `any` still opens it to everyone).
        return True
    if "any" in allowed:
        return True
    return bool(allowed & set(user.get("groups") or []))


# ── #248 P2 — company-wide-publish governance ────────────────────────────────
# A COMPANY-visibility shortcut broadcasts to every user in the box; the write
# side needs a narrower gate than plain admin so a curated set of trusted
# authors can publish without a full Authentik-admin grant. This predicate is
# consulted by app.py ONLY when the effective end-state visibility is
# "company" — private/group are untouched (they keep whatever route access
# they already have; see the routes in app.py for that gate).
#
# `SHORTCUT_AUTHORS_GROUP` names the Authentik group (env-configurable,
# matching the ADMIN_GROUP / SUPER_ADMIN_GROUP convention in app.py); admin
# is ALWAYS allowed regardless of the env var or its value. Read per-call
# (not frozen at import), mirroring `fetch_owui_models()`'s OWUI_API_KEY
# lookup, so tests can monkeypatch it without a fresh module import.
_DEFAULT_SHORTCUT_AUTHORS_GROUP = "razzfazz.ai Shortcut Authors"


def user_can_publish_company_wide(user: dict) -> bool:
    """May ``user`` create or keep a shortcut at ``visibility="company"``?

    True for an admin (``user['is_admin']``), or for a user whose Authentik
    groups include the configured ``SHORTCUT_AUTHORS_GROUP`` (default:
    ``"razzfazz.ai Shortcut Authors"``). False otherwise.
    """
    if user.get("is_admin"):
        return True
    authors_group = (os.environ.get("SHORTCUT_AUTHORS_GROUP")
                      or _DEFAULT_SHORTCUT_AUTHORS_GROUP)
    return authors_group in (user.get("groups") or [])


# ── #248 P2 — no-embedded-secret rule for SHARED shortcut config ─────────────
# A shared shortcut (visibility company/group) is read by EVERY viewer it's
# visible to, so its `config` must be portable: no value that only makes
# sense for the author — a resumed terminal/tmux session, a raw credential —
# may be baked in. `private` rows are exempt from this rule (app.py only
# calls it for company/group); a private shortcut is per-user by
# construction (see `user_can_use` above), so user-specific state there is
# not a portability problem.
#
# P1 kinds (owui_persona/owui_app/dify_chat/cloud_link) only ever carry
# model_id/app_path/prompt_tmpl/provider/description/category-shaped fields —
# none of which match — so today this is a forward guard for the P2/P3
# in-portal kinds (dify_workflow/owui_prompt/cloud_api/agent_message) that
# read the same `config` column once they land. Deliberately an ENUMERATED
# list of forbidden field-NAME substrings, not a blanket "no strings" rule —
# over-blocking legitimate portable config (a `description`, a `category`)
# would make the validator useless.
#
# #923 — `authorization` is on this list, not in the segment set below, so it
# also catches `Proxy-Authorization` and `authorizationHeader`. It is the one
# shape from the #923 report that CFG-2's camelCase folding did NOT already
# cover: `Authorization` normalises to `authorization`, which contains no other
# entry here and is not a forbidden SEGMENT (`auth` is deliberately absent —
# see below). An HTTP auth header is never portable shared config, so blocking
# the whole family is fail-closed in the direction the gate wants.
_FORBIDDEN_CONFIG_KEY_SUBSTRINGS = (
    "resume_session", "tmux_session", "session_id", "session_token",
    "api_key", "apikey", "access_token", "refresh_token", "auth_token",
    "authorization",
    "secret", "password", "passwd", "credential", "cookie", "private_key",
)

# CFG-2 — short field names that are only meaningful as WHOLE normalised
# segments. `pat` (personal access token) as a substring would block `path`,
# `pattern` and `compatible`; `pw` would block `pwd_hint`… but also `spwn`.
# Matching them segment-wise keeps them usable without over-blocking, which
# is the failure mode the enumerated-list comment above warns about.
#
# Deliberately NOT here: `auth` and `key`. Both appear as legitimate
# *container* names (`auth: {...}`, `key: "sort"`), and blocking them would
# report the wrapper instead of the actual offending leaf — the reason string
# is the whole point of this rule. Their real cases are already covered:
# `auth_token`/`api_key` by the substring list, and a raw credential under any
# name by the value scan below.
_FORBIDDEN_KEY_SEGMENTS = frozenset({
    "token", "bearer", "pat", "pwd", "pw", "jwt", "creds", "cred", "otp",
})

# CFG-2 — vendor token shapes. A secret pasted as a VALUE is invisible to any
# key-name rule, and the most common way one gets into a shared shortcut is
# pasted into a free-text field (`prompt_tmpl`, `description`) where no key
# name could ever hint at it.
_SECRET_VALUE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("openai/anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}")),
    ("google api key", re.compile(r"\bAQ\.[A-Za-z0-9_-]{10,}")),
    ("google api key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("pem private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

# Runs of secret-ish characters. `/` and `:` are deliberately EXCLUDED so a
# URL or a path (`chat/abc`, `https://example.com/x/y`) breaks into short
# harmless pieces instead of arriving as one long high-entropy blob.
_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9+=_.-]{28,}")
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_HEX_RE = re.compile(r"\A[0-9a-fA-F]{32,}\Z")

_MIN_SECRET_ENTROPY_BITS = 3.6


def _normalise_config_key(key) -> str:
    """Fold a field name to snake_case so one spelling can't dodge the rule.

    ``accessToken`` / ``access-token`` / ``Access Token`` / ``ACCESS_TOKEN``
    all become ``access_token``. Before CFG-2 the rule lowercased and nothing
    else, so ONLY the snake_case spelling was ever caught — every camelCase
    and kebab-case variant walked straight through a list that was written
    as if it covered them.
    """
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).lower()
    return s.strip("_")


def _shannon_entropy_bits(s: str) -> float:
    """Shannon entropy per character, in bits."""
    if not s:
        return 0.0
    from collections import Counter
    from math import log2
    n = len(s)
    return -sum((c / n) * log2(c / n) for c in Counter(s).values())


def _looks_like_high_entropy_secret(run: str) -> bool:
    """Is this character run a random-looking credential rather than text?

    Conservative on purpose — a false positive here blocks a legitimate
    shared shortcut, so every gate has to agree: long enough, random enough,
    and NOT one of the long-but-legitimate shapes (a UUID is an app/object
    id, not a secret).
    """
    if _UUID_RE.match(run):
        return False
    if _HEX_RE.match(run):
        # A 32+ char hex blob is a digest or a raw key, never prose.
        return True
    has_lower = any(c.islower() for c in run)
    has_upper = any(c.isupper() for c in run)
    has_digit = any(c.isdigit() for c in run)
    if not (has_digit and has_lower and has_upper):
        return False
    return _shannon_entropy_bits(run) >= _MIN_SECRET_ENTROPY_BITS


def _scan_value_for_secret(value: str) -> str | None:
    """Return a human label for the first secret shape found, else None."""
    for label, pat in _SECRET_VALUE_PATTERNS:
        if pat.search(value):
            return label
    for run in _TOKEN_RUN_RE.findall(value):
        if _looks_like_high_entropy_secret(run):
            return "high-entropy string"
    return None


def shared_config_is_portable(kind: str, config: dict) -> tuple[bool, str | None]:
    """Is ``config`` safe to share at visibility company/group?

    Recursively scans a JSON-shaped dict/list/scalar tree. Returns
    ``(True, None)`` when clean, ``(False, reason)`` on the FIRST hit — the
    reason names the offending field (dotted/bracketed path) so the 422 the
    route layer returns is actionable, not a guess. ``kind`` is accepted for
    a future kind-specific rule but unused today (every P1 kind shares the
    same rule set).

    Two independent checks (CFG-2 — before it, only the first existed, and
    only in its weakest form):

    1. **Field NAME**, after `_normalise_config_key` folds camelCase and
       kebab-case into snake_case. The pre-CFG-2 rule lowercased the key and
       compared it to a snake_case-only list, so `accessToken`,
       `refreshToken`, `authToken`, `privateKey`, `sessionId` and `api-key`
       all passed a check written specifically to stop them. Folding alone
       was still not enough for `Authorization` (#923): it normalises to a
       word no other entry contains, so it needed its own list entry.
    2. **Field VALUE** — vendor token shapes (`sk-`, `ghp_`, `AQ.`, `eyJ…`,
       PEM blocks, …) and high-entropy blobs. The name check alone can only
       stop a secret whose field was *labelled* as one; it can do nothing
       about a key pasted into `prompt_tmpl` or `description`, which is the
       likeliest way one actually ends up in a shared shortcut.
    """
    def _scan(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                field = f"{path}.{k}" if path else str(k)
                key_norm = _normalise_config_key(k)
                for pat in _FORBIDDEN_CONFIG_KEY_SUBSTRINGS:
                    if pat in key_norm:
                        return field, f"field name matched '{pat}'"
                if set(key_norm.split("_")) & _FORBIDDEN_KEY_SEGMENTS:
                    seg = sorted(set(key_norm.split("_")) & _FORBIDDEN_KEY_SEGMENTS)[0]
                    return field, f"field name matched '{seg}'"
                hit = _scan(v, field)
                if hit:
                    return hit
        elif isinstance(node, list):
            for i, item in enumerate(node):
                hit = _scan(item, f"{path}[{i}]")
                if hit:
                    return hit
        elif isinstance(node, str):
            label = _scan_value_for_secret(node)
            if label:
                return (path or "<root>"), f"value looks like a {label}"
        return None

    hit = _scan(config or {}, "")
    if hit:
        field, why = hit
        return False, (
            f"shared shortcut config field '{field}' looks like a secret or "
            f"per-user session value ({why}) — not portable across "
            "users; remove it or make this shortcut private")
    return True, None


# ── Table + CRUD (psycopg2, mirrors app.py's _db_conn idioms) ────────────────
# Columns SELECTed back (secret_enc / secret_ref exist in the schema for P2 but
# are never read into the tile/admin surface in P1 — no secrets stored yet).
_COLS = ["id", "title", "description", "icon", "category", "sort_order",
         "allowed_groups", "kind", "config", "enabled", "created_by",
         "created_at", "updated_at", "visibility", "owner_username"]

# Fields an update() may set. `kind` is included so the admin editor can retype
# a shortcut; the route layer still rejects non-P1 kinds before calling update.
_MUTABLE = {"title", "description", "icon", "category", "sort_order",
            "allowed_groups", "kind", "config", "enabled",
            "visibility", "owner_username"}


def ensure_shortcuts_table(conn):
    """Auto-migrate the one shortcuts table at boot — same mechanism as
    ``_ensure_prefs_table`` (``CREATE TABLE IF NOT EXISTS``; no init-db.sh
    change). ``id`` is a Python ``uuid4`` string (TEXT), so no pgcrypto dep.
    ``secret_enc``/``secret_ref`` are declared now (unused in P1) so the P2
    secret store needs no migration."""
    with conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS start_portal_shortcuts (
                id             TEXT PRIMARY KEY,
                title          TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
                icon           TEXT NOT NULL DEFAULT '✨',
                category       TEXT NOT NULL DEFAULT '1 Workspace',
                sort_order     INTEGER NOT NULL DEFAULT 100,
                allowed_groups TEXT[] NOT NULL DEFAULT '{}',
                kind           TEXT NOT NULL,
                config         JSONB NOT NULL DEFAULT '{}',
                secret_enc     BYTEA,
                secret_ref     TEXT,
                enabled        BOOLEAN NOT NULL DEFAULT TRUE,
                created_by     TEXT NOT NULL,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # #248 S1: CREATE IF NOT EXISTS never alters an EXISTING table —
        # the visibility columns need their own idempotent migration.
        cur.execute("""
            ALTER TABLE start_portal_shortcuts
                ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'company',
                ADD COLUMN IF NOT EXISTS owner_username TEXT NOT NULL DEFAULT ''
        """)
        # #1158 rev-B: v5 taxonomy stored-state migration. CREATE IF NOT EXISTS
        # never touches an existing table, so pre-v5 rows keep the retired
        # 'Shortcuts' home and render as a ghost section; the column default
        # only applies to NEW rows and needs its own SET DEFAULT. Idempotent —
        # both statements are no-ops once applied.
        cur.execute("""
            UPDATE start_portal_shortcuts
               SET category = '1 Workspace'
             WHERE category = 'Shortcuts'
        """)
        cur.execute("""
            ALTER TABLE start_portal_shortcuts
                ALTER COLUMN category SET DEFAULT '1 Workspace'
        """)


def _row(r):
    d = dict(r)
    # psycopg2 decodes JSONB to a dict already; be defensive if a driver hands
    # back the raw string.
    if isinstance(d.get("config"), str):
        d["config"] = json.loads(d["config"])
    # text[] -> list; psycopg2 already does this, but normalise None -> [].
    if d.get("allowed_groups") is None:
        d["allowed_groups"] = []
    return d


def list_all(conn):
    """Every shortcut, ordered by ``sort_order``. Defensive: returns [] if the
    table doesn't exist yet (mirrors the prefs reader's fail-soft behaviour)."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"SELECT {', '.join(_COLS)} FROM start_portal_shortcuts "
                f"ORDER BY sort_order, title")
            return [_row(r) for r in cur.fetchall()]
    except Exception:
        conn.rollback()
        return []


def get(conn, sid):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM start_portal_shortcuts WHERE id = %s",
            (sid,))
        r = cur.fetchone()
        return _row(r) if r else None


def create(conn, *, title, kind, created_by, description="", icon="✨",
           category="1 Workspace", sort_order=100, allowed_groups=None,
           config=None, visibility="company", owner_username=""):
    if visibility not in ("company", "group", "private"):
        raise ValueError(f"invalid visibility: {visibility}")
    if visibility == "private" and not owner_username:
        # an owner-less private shortcut would be visible to NOBODY —
        # refuse loudly instead of storing a dead row
        raise ValueError("private visibility requires owner_username")
    sid = str(uuid.uuid4())
    with conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO start_portal_shortcuts
              (id, title, description, icon, category, sort_order,
               allowed_groups, kind, config, created_by, visibility,
               owner_username)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (sid, title, description, icon, category, sort_order,
              allowed_groups or [], kind, psycopg2.extras.Json(config or {}),
              created_by, visibility, owner_username))
    return get(conn, sid)


def update(conn, sid, **fields):
    """Partial update of the mutable columns. Unknown fields are ignored.

    #679 review: the same validation create() enforces — an update could
    otherwise store a typo visibility (silently falling through to the
    company rule) or strip the owner off a private row (visible to nobody).
    Validated against the EFFECTIVE end state (existing row + patch).
    """
    if "visibility" in fields or "owner_username" in fields:
        current = get(conn, sid) or {}
        vis = fields.get("visibility", current.get("visibility") or "company")
        if vis not in ("company", "group", "private"):
            raise ValueError(f"invalid visibility: {vis}")
        owner = fields.get("owner_username",
                           current.get("owner_username") or "")
        if vis == "private" and not owner:
            raise ValueError("private visibility requires owner_username")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _MUTABLE:
            continue
        sets.append(f"{k} = %s")
        vals.append(psycopg2.extras.Json(v) if k == "config" else v)
    if not sets:
        return get(conn, sid)
    sets.append("updated_at = NOW()")
    vals.append(sid)
    with conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE start_portal_shortcuts SET {', '.join(sets)} WHERE id = %s",
            vals)
    return get(conn, sid)


def delete(conn, sid):
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM start_portal_shortcuts WHERE id = %s", (sid,))
        return cur.rowcount > 0


# ── Shortcut rows -> portal tiles ────────────────────────────────────────────

def shortcuts_as_tiles(rows, user, ctx):
    """Convert shortcut rows into portal tile dicts, filtered to what ``user``
    may see. Drops disabled rows, non-redirect (P2/P3) kinds, and rows the user
    can't use; any row whose URL can't be built (bad config) is skipped rather
    than blowing up the whole page.

    Emitted tiles carry whichever icon field ``icon_tile_fields()`` picks —
    ``icon_data_uri`` (uploaded image), ``icon`` (curated PNG name) or
    ``icon_emoji`` (the pre-#248 emoji/glyph default); the three-way split
    landed with #248 and replaced the unconditional ``icon_emoji`` this
    docstring used to promise (CFG-36). Tiles also carry
    ``required_group="any"`` /
    ``profile="core"`` — the access check already happened here, so the tiles
    pass the portal's ``_filter_tile`` gate unchanged and flow into the normal
    favourites/sections split.
    """
    tiles = []
    for r in rows:
        if not r.get("enabled"):
            continue
        if r.get("kind") not in _REDIRECT_KINDS:      # P1: redirect only
            continue
        if not user_can_use(r, user):
            continue
        try:
            url = tile_url(r, ctx)          # #1192: prompt-bearing owui_app → portal path
        except (ValueError, KeyError):
            continue
        tile = {
            "id": f"shortcut-{r['id']}",
            "name": r["title"],
            "category": r.get("category", "1 Workspace"),
            "url": url,
            "description": r.get("description", ""),
            "required_group": "any",
            "profile": "core",
            "order": r.get("sort_order", 100),
            "default_pinned": False,
            # #299 tile-reskin-to-prototype — admin-curated shortcuts render
            # "Run →" in the tile footer (they invoke a redirect/action)
            # instead of "Open →" (which every other tile type gets).
            "is_shortcut": True,
        }
        # #248 App Builder: icon may now be an emoji, a curated PNG
        # reference, or an uploaded data-URI — see icon_tile_fields().
        tile.update(icon_tile_fields(r.get("icon")))
        tiles.append(tile)
    return tiles


# ── Authentik groups + member resolution (admin UI) ──────────────────────────
# Same base + bootstrap token the password broker already uses server-side
# (password_broker.py): RZFZ_AUTHENTIK_BASE + AUTHENTIK_BOOTSTRAP_TOKEN, both
# injected into the portal container via env_file: ../.env.
_AK_BASE = os.environ.get("RZFZ_AUTHENTIK_BASE", "http://authentik-server:9000").rstrip("/")


def _authentik_get(path: str) -> dict:
    """GET the Authentik REST API with the bootstrap token. Raises on error."""
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "")
    r = requests.get(f"{_AK_BASE}{path}",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json"},
                     timeout=10)
    r.raise_for_status()
    return r.json() or {}


def fetch_groups() -> list[str]:
    """Every Authentik group name, for the admin's access multi-select."""
    data = _authentik_get("/api/v3/core/groups/?include_users=false")
    return [g["name"] for g in data.get("results", [])]


def resolve_members(group_names) -> list[str]:
    """Resolve group names to their effective member usernames — so the admin
    *sees which users* a shortcut's access grants (the operator's requirement).
    """
    wanted = set(group_names or [])
    if not wanted:
        return []
    data = _authentik_get("/api/v3/core/groups/?include_users=true")
    out = []
    for g in data.get("results", []):
        if g.get("name") in wanted:
            out += [u.get("username") for u in g.get("users_obj", []) if u.get("username")]
    return sorted(set(out))


def member_initials(usernames, limit=4):
    """Up to `limit` short initials for the App Builder's Step 4 "who can
    use this" avatar row — presentation only, never used for access control
    (the actual gate is `user_can_use`). Splits each username on the usual
    separators (`.`, `_`, `-`, whitespace) and takes the first letter of the
    first two parts (e.g. "anna.schmidt" -> "AS"), or the first two letters
    of a single-token username.
    """
    out = []
    for u in (usernames or [])[:limit]:
        parts = [p for p in re.split(r'[._\-\s]+', u) if p]
        if len(parts) >= 2:
            out.append((parts[0][:1] + parts[1][:1]).upper())
        elif parts:
            out.append(parts[0][:2].upper())
    return out


# ── OWUI model list (#227 point 6) ───────────────────────────────────────────
# Backs the shortcuts editor's model picker for the `owui_persona`/`owui_app`
# kinds — today the admin types an OWUI model id by hand. `OWUI_API_KEY` is a
# static, operator-configured token (empty by default; #299 — the
# Start-Portal redesign — replaces it with live per-user token resolution and
# adds the actual <select> widget). Neither the widget nor the live token is
# in scope here: this is the endpoint + its degrade logic only.
_OWUI_BASE = os.environ.get("OWUI_INTERNAL_URL", "http://openwebui:8080").rstrip("/")


def fetch_owui_models() -> list[dict]:
    """Normalized ``[{id, name}, ...]`` from OWUI's ``/api/models``.

    Raises on ANY failure — no token configured, OWUI unreachable, or a
    non-200 response — so the route layer can degrade the editor to a
    free-text fallback instead of ever 500ing (#227 point 6). Never hits the
    network with an empty token: an unset ``OWUI_API_KEY`` is treated as "not
    configured", not as an anonymous request.
    """
    token = os.environ.get("OWUI_API_KEY", "")
    if not token:
        raise RuntimeError("OWUI_API_KEY not configured")
    r = requests.get(f"{_OWUI_BASE}/api/models",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "application/json"},
                     timeout=5)
    r.raise_for_status()
    data = r.json() or {}
    # OWUI's /api/models wraps the list as {"data": [...]}, mirroring the
    # OpenAI-compatible shape; be defensive and also accept a bare list.
    items = data.get("data") if isinstance(data, dict) else data
    out = []
    for m in items or []:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id")
        if not model_id:
            continue
        out.append({"id": model_id, "name": m.get("name") or model_id})
    return out


# ── #1192 — owui_app: server-side chat creation ──────────────────────────────
# WHY: OWUI's `?q=` deep link auto-submits on SPA mount. Behind the Caddy
# forward_auth redirect chain (302 → Authentik → back) the SPA mounts twice /
# the auto-submit races the `/c/<id>` navigation, so submission A creates chat
# A (which receives the answer) while the visible view lands in chat B (never
# answered), plus "Initial Greeting" ghost chats per click (operator repro
# 2026-09-02 on 0.91). Nothing client-side makes `?q=` idempotent from
# outside OWUI, so the portal creates the chat itself and redirects to it.
#
# HOW (verified against the open-webui v0.11.1 source, backend/open_webui/
# main.py::chat_completion): `POST /api/chat/completions` with `parent_id:
# null` and NO `chat_id` is the backend-managed new-chat path — OWUI mints the
# chat id, inserts the chat with the user message + assistant placeholder,
# and — given a `session_id` — runs the completion as a background task keyed
# on that chat, returning `{"status": true, "task_ids": [...], "chat_id": ...}`
# immediately. The event emitter broadcasts to the `user:<id>` room and
# persists to the DB, so the browser that opens `/c/<chat_id>` a moment later
# picks the stream up (Chat.svelte::loadChat → getTaskIdsByChatId + `events`).
#
# AUTH: OWUI verifies bearer JWTs with `jwt.decode(token, WEBUI_SECRET_KEY,
# algorithms=['HS256'])` and resolves `payload["id"]` → user. The portal
# already holds WEBUI_SECRET_KEY (env_file: ../.env — no new secret) and maps
# the forward-auth email to the OWUI user id with ONE read-only SELECT on
# OWUI's own DB (same role/DSN chain modules/chat/compose.yml uses — no new
# credential either). The token lives 60 s, is used once, server-to-server,
# and is never returned to the client. A user who has never opened OWUI has
# no OWUI account yet (OIDC provisions on first login) — that, and every
# other failure, degrades to the legacy `?q=` URL (`start_owui_app_chat` →
# None), never a 500.
OWUI_TOKEN_TTL_S = 60
OWUI_HTTP_TIMEOUT_S = 10          # OWUI answers before the LLM runs (background task)
OWUI_DB_CONNECT_TIMEOUT_S = 3

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_owui_token(user_id: str, secret: str, ttl_s: int = OWUI_TOKEN_TTL_S,
                    now: float | None = None) -> str:
    """A short-lived HS256 JWT OWUI accepts for ``user_id`` (no PyJWT needed —
    header/payload/HMAC-SHA256 is ~10 lines). Carries ``iat``/``jti`` like
    OWUI's own ``create_token`` so its Redis revocation checks (per-jti, and
    per-user ``revoked_at`` which rejects tokens WITHOUT ``iat``) behave."""
    if not user_id:
        raise ValueError("owui user id required")
    if not secret:
        raise ValueError("WEBUI_SECRET_KEY required")
    ts = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"id": str(user_id), "iat": ts, "exp": ts + int(ttl_s),
               "jti": uuid.uuid4().hex}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"),
                   hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def owui_prompt_variables(ctx: dict, now: datetime | None = None) -> dict:
    """Mirror of the OWUI frontend's ``getPromptVariables()`` (src/lib/utils/
    index.ts) — the ``{{USER_NAME}}``/``{{CURRENT_DATE}}``… placeholders a
    workspace model's system prompt may carry. The browser would fill these
    from its own clock/locale; here the box TZ and the Accept-Language the
    portal saw stand in."""
    ctx = ctx or {}
    tz_name = (ctx.get("tz") or os.environ.get("TZ") or "UTC").strip() or "UTC"
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001 — no tzdata in the alpine image → UTC
            tz_name = "UTC"
            now = datetime.now(timezone.utc)
    return {
        "{{USER_NAME}}": ctx.get("name") or ctx.get("username") or "",
        "{{USER_EMAIL}}": ctx.get("email") or "Unknown",
        "{{USER_LOCATION}}": "Unknown",
        "{{CURRENT_DATETIME}}": now.strftime("%Y-%m-%d %H:%M:%S"),
        "{{CURRENT_DATE}}": now.strftime("%Y-%m-%d"),
        "{{CURRENT_TIME}}": now.strftime("%H:%M:%S"),
        "{{CURRENT_WEEKDAY}}": _WEEKDAYS[now.weekday()],
        "{{CURRENT_TIMEZONE}}": tz_name,
        "{{USER_LANGUAGE}}": ctx.get("language") or "en-US",
    }


def build_owui_new_chat_payload(model_id: str, prompt: str, ctx: dict) -> dict:
    """The ``/api/chat/completions`` body for OWUI 0.11's backend-managed
    new-chat path — the same fields Chat.svelte sends for a first message,
    minus browser-only extras (tool servers, files, model_item).

    ``parent_id: None`` + no ``chat_id`` = "create a chat"; ``session_id``
    (any non-empty string — it only names the socket room OWUI would call
    back for client-side tools) selects the background fan-out so the call
    returns with the chat id instead of blocking for the whole generation.
    Only the title is generated server-side; tags/follow-ups are extra LLM
    calls the tile click does not need.
    """
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "features": {},
        "variables": owui_prompt_variables(ctx),
        "session_id": f"start-portal-{uuid.uuid4().hex}",
        "id": assistant_message_id,
        "parent_id": None,
        "user_message": {
            "id": user_message_id,
            "parentId": None,
            "childrenIds": [],
            "role": "user",
            "content": prompt,
            "timestamp": int(time.time()),
            "models": [model_id],
        },
        "background_tasks": {"title_generation": True},
    }


def owui_chat_url(domain: str, chat_id: str) -> str:
    return f"https://chat.{domain}/c/{quote(str(chat_id), safe='')}"


class OwuiClient:
    """The two OWUI touchpoints the server-side path needs. Tests substitute a
    duck-typed fake (``resolve_user_id`` / ``start_chat``)."""

    def __init__(self, base_url: str, secret: str, db_dsn: str):
        self.base_url = (base_url or "").rstrip("/")
        self.secret = secret
        self.db_dsn = db_dsn

    def resolve_user_id(self, email: str):
        """OWUI user id for ``email`` (case-insensitive, like OWUI's own
        ``get_user_by_email``) or ``None`` when no OWUI account exists yet."""
        conn = psycopg2.connect(self.db_dsn, connect_timeout=OWUI_DB_CONNECT_TIMEOUT_S)
        try:
            with conn, conn.cursor() as cur:
                cur.execute('SELECT id FROM "user" WHERE lower(email) = %s ORDER BY email ASC LIMIT 1',
                            ((email or "").strip().lower(),))
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None

    def start_chat(self, user_id: str, payload: dict) -> str:
        """Create the chat + kick off the completion as ``user_id``; returns
        the new chat id. Raises ``RuntimeError`` on anything but a clean
        ``{"status": true, "chat_id": ...}`` so the caller can fall back."""
        token = mint_owui_token(user_id, self.secret)
        r = requests.post(f"{self.base_url}/api/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json",
                                   "Accept": "application/json"},
                          timeout=OWUI_HTTP_TIMEOUT_S)
        if r.status_code != 200:
            raise RuntimeError(f"owui completions HTTP {r.status_code}: "
                               f"{(r.text or '')[:200]}")
        data = r.json() or {}
        chat_id = data.get("chat_id") if isinstance(data, dict) else None
        if not (isinstance(data, dict) and data.get("status") and chat_id):
            raise RuntimeError("owui completions returned no chat_id")
        return str(chat_id)


def get_owui_client():
    """Env-driven ``OwuiClient`` (read at call time, like ``fetch_owui_models``)
    or ``None`` when the box has no shared ``WEBUI_SECRET_KEY`` — the caller
    then keeps the legacy deep link. The DSN mirrors modules/chat/compose.yml's
    ``DATABASE_URL`` fallback chain exactly; ``OWUI_DATABASE_URL`` overrides it."""
    secret = os.environ.get("WEBUI_SECRET_KEY", "")
    if not secret:
        return None
    base = os.environ.get("OWUI_INTERNAL_URL", "http://openwebui:8080")
    dsn = os.environ.get("OWUI_DATABASE_URL", "")
    if not dsn:
        db_user = os.environ.get("OPENWEBUI_DB_USER") or os.environ.get("POSTGRES_USER") or "docker"
        db_pw = os.environ.get("OPENWEBUI_DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD") or ""
        db_name = os.environ.get("OPENWEBUI_DB") or "openwebui_db"
        dsn = (f"postgresql://{quote(db_user, safe='')}:{quote(db_pw, safe='')}"
               f"@postgres:5432/{db_name}")
    return OwuiClient(base, secret, dsn)


def start_owui_app_chat(config: dict, ctx: dict, client):
    """Create the ``owui_app`` chat server-side for the ctx user and return the
    ``/c/<chat_id>`` URL — or ``None`` (logged) when that is not possible, so
    the caller falls back to ``build_redirect_url``'s ``?q=`` deep link.
    Never raises for OWUI/DB trouble; a broken shortcut config is the caller's
    ``build_redirect_url`` problem (it raises the same ``KeyError`` there)."""
    if client is None:
        return None
    config = config or {}
    tmpl = config.get("prompt_tmpl") or ""
    model_id = config.get("model_id")
    if not tmpl.strip() or not model_id:
        return None
    email = (ctx.get("email") or "").strip()
    if not email:
        return None
    prompt = render_vars(tmpl, ctx, mode="text")
    if not prompt.strip():
        return None
    try:
        user_id = client.resolve_user_id(email)
    except Exception as e:  # noqa: BLE001 — degrade to the deep link
        logger.warning(f"owui_app: OWUI user lookup failed: {e}")
        return None
    if not user_id:
        logger.info("owui_app: no OWUI account for this user yet — deep-link fallback")
        return None
    payload = build_owui_new_chat_payload(model_id, prompt, ctx)
    try:
        chat_id = client.start_chat(user_id, payload)
    except Exception as e:  # noqa: BLE001 — degrade to the deep link
        logger.warning(f"owui_app: server-side chat creation failed: {e}")
        return None
    if not chat_id:
        return None
    return owui_chat_url(ctx["domain"], chat_id)
