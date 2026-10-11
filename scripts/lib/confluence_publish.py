#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Render docs/enterprise markdown to Confluence storage format + publish.

One-way, idempotent, target-pluggable (docs.rzfz.ai / #162). The source of truth
stays in git; this module NEVER reads back or authors in the target. The render
core (`render_markdown_to_storage`, `page_title`, `page_tree`, `rewrite_links`)
is target-agnostic and network-free — everything Confluence-specific lives in the
`_cf_*` adapter and is only reached without `--dry-run` on `--target confluence`.

Design: .gsd/reports/2026.08-docs-rzfz-ai-portal-design.md (Option C).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import markdown as _md  # tests/requirements.txt: markdown==3.*


# --- render core (target-agnostic, no network) ------------------------------

# Fenced code (incl. ```mermaid) is extracted BEFORE python-markdown runs so the
# body is preserved verbatim inside a Confluence `code` macro (CDATA). Language
# tag is restricted to [A-Za-z0-9_+#.-] so it can never inject XML.
_CODE_FENCE = re.compile(r"```([A-Za-z0-9_+#.-]*)[ \t]*\n(.*?)```", re.DOTALL)
# Collision-safe placeholder: an all-letter sentinel + index. python-markdown's
# smart_emphasis leaves the intraword text alone and never rewrites it, and the
# index is scoped to the sentinel on restore — so arbitrary document numbers
# (ports, versions) are NEVER mistaken for a code-block index.
_PLACEHOLDER = "RZFZCODEBLOCKSENTINEL{}ENDSENTINEL"
_PLACEHOLDER_RE = re.compile(
    r"(?:<p>\s*)?RZFZCODEBLOCKSENTINEL(\d+)ENDSENTINEL(?:\s*</p>)?"
)


def _cdata(text: str) -> str:
    """Wrap text in CDATA, splitting any literal ']]>' so it stays well-formed."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _unescape_xml(s: str) -> str:
    """Reverse one level of XHTML entity-escaping for text bound for a CDATA body.

    python-markdown escapes anchor text (`&`->`&amp;`, `<`->`&lt;`); but CDATA is
    literal, so an escaped `&amp;` inside `<ac:plain-text-link-body>` would display
    verbatim as `&amp;`. Undo a single level; `&amp;` LAST so it doesn't re-trigger.
    """
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))


def _code_macro(lang: str, body: str) -> str:
    lang = lang or "text"
    body = body.rstrip("\n")
    return ('<ac:structured-macro ac:name="code">'
            f'<ac:parameter ac:name="language">{lang}</ac:parameter>'
            f'<ac:plain-text-body>{_cdata(body)}</ac:plain-text-body>'
            '</ac:structured-macro>')


_IMG_TAG = re.compile(r'<img\b[^>]*>')
_MERMAID_PCONFIG = '{"args":["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage"]}'


def _att_name(src: str) -> str:
    """Deterministic per-page attachment filename for a local image src.

    Flatten the path below `images/` so two different dirs with the same basename
    (`identity/foo.png` vs `first-boot/foo.png`) never collide on one page.
    """
    s = src.split("images/", 1)[-1] if "images/" in src else os.path.basename(src)
    return s.replace("/", "__")


def _render_mermaid(src: str):
    """Render a mermaid diagram to PNG bytes via mermaid-cli; None on ANY failure.

    Each render spawns a headless Chromium, so this is slow and memory-heavy. Two
    knobs make it resilient + fast (env, both optional):
      * `RZFZ_MERMAID_CACHE` — a dir of `<sha1(src)>.png`. A cached diagram returns
        instantly and NEVER launches Chromium, so a pre-render pass can populate the
        cache (resumable if interrupted) and the actual publish stays Chromium-free.
      * `RZFZ_MMDC` — path to an installed `mmdc` binary (skips per-call `npx`
        resolution). Falls back to `npx -y @mermaid-js/mermaid-cli`.
    On any error the caller falls back to the readable `code` macro — a diagram is
    never dropped.
    """
    key = hashlib.sha1(src.encode("utf-8")).hexdigest()
    cache = os.environ.get("RZFZ_MERMAID_CACHE", "").strip()
    if cache:
        cpath = os.path.join(cache, key + ".png")
        if os.path.isfile(cpath):
            with open(cpath, "rb") as f:
                return f.read()
    mmdc = os.environ.get("RZFZ_MMDC", "").strip()
    cmd = [mmdc] if mmdc else ["npx", "-y", "@mermaid-js/mermaid-cli"]
    png = None
    try:
        with tempfile.TemporaryDirectory() as td:
            i = os.path.join(td, "d.mmd")
            o = os.path.join(td, "d.png")
            p = os.path.join(td, "p.json")
            with open(i, "w", encoding="utf-8") as f:
                f.write(src)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_MERMAID_PCONFIG)
            r = subprocess.run(cmd + ["-i", i, "-o", o, "-b", "white", "-p", p],
                               capture_output=True, timeout=180)
            if r.returncode == 0 and os.path.isfile(o):
                with open(o, "rb") as f:
                    png = f.read()
    except (OSError, subprocess.SubprocessError):
        return None
    if cache and png:
        try:
            os.makedirs(cache, exist_ok=True)
            with open(os.path.join(cache, key + ".png"), "wb") as f:
                f.write(png)
        except OSError:
            pass
    return png


def _rewrite_images(html: str, base_dir: str, attachments: list) -> str:
    """Rewrite `<img>` to Confluence `<ac:image>`: local files -> uploaded
    attachment (registered in `attachments`); http(s) srcs -> external `ri:url`.
    An unresolved local file is left as `<img>` (a visible gap, never silent)."""
    def _sub(m):
        tag = m.group(0)
        sm = re.search(r'src="([^"]+)"', tag)
        if not sm:
            return tag
        src = sm.group(1)
        if src.startswith("http://") or src.startswith("https://"):
            return '<ac:image><ri:url ri:value="%s"/></ac:image>' % src
        local = os.path.normpath(os.path.join(base_dir, src))
        if not os.path.isfile(local):
            return tag
        fn = _att_name(src)
        if not any(a["filename"] == fn for a in attachments):
            attachments.append({"filename": fn, "path": local})
        return '<ac:image><ri:attachment ri:filename="%s"/></ac:image>' % fn
    return _IMG_TAG.sub(_sub, html)


def render_markdown_to_storage(md_text, *, base_dir=None, attachments=None,
                               render_mermaid=False):
    """Markdown -> Confluence storage-format XHTML.

    Default (single-arg) behaviour is unchanged: headings, paragraphs, inline
    emphasis, tables (native), and fenced code / ```mermaid -> a Confluence `code`
    macro. When the publisher passes `base_dir` + `attachments` (a list it fills)
    it ALSO:
      * rewrites local `<img src=../images/..>` to an uploaded `<ac:image>`
        attachment (external http(s) srcs -> `<ac:image><ri:url>`); and
      * when `render_mermaid` and mermaid-cli succeeds, renders each ```mermaid
        block to a PNG attachment + `<ac:image>` — falling back to the `code`
        macro on any render failure so a diagram is never lost.
    Appended attachments are {"filename", "data": bytes} (mermaid, in-memory) or
    {"filename", "path": str} (local image file).
    """
    if attachments is None:
        attachments = []
    # 1. Extract fenced code (incl. mermaid) FIRST -> macros/images, placeholder-swap.
    blocks = []

    def _stash(m):
        lang, body = m.group(1), m.group(2)
        if lang == "mermaid" and render_mermaid:
            png = _render_mermaid(body)
            if png is not None:
                fn = "mermaid-" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:10] + ".png"
                if not any(a["filename"] == fn for a in attachments):
                    attachments.append({"filename": fn, "data": png})
                blocks.append('<ac:image><ri:attachment ri:filename="%s"/></ac:image>' % fn)
                return _PLACEHOLDER.format(len(blocks) - 1)
        blocks.append(_code_macro(lang, body))
        return _PLACEHOLDER.format(len(blocks) - 1)

    staged = _CODE_FENCE.sub(_stash, md_text)
    # 2. Convert the rest with python-markdown (tables + sane lists). No `toc`:
    #    we do not want id-slug attributes on headings (Confluence anchors its own).
    html = _md.markdown(staged, extensions=["tables", "sane_lists"])

    # 3. Restore code macros / mermaid images (index scoped to the sentinel).
    def _unstash(m):
        return blocks[int(m.group(1))]

    html = _PLACEHOLDER_RE.sub(_unstash, html)

    # 4. Publisher-only: turn <img> into attachment/url <ac:image> (needs the file dir).
    if base_dir is not None:
        html = _rewrite_images(html, base_dir, attachments)
    return html


def page_title(rel_path: str) -> str:
    """Derive a Confluence page title from a source-relative markdown path.

    `index.md` -> "Home" (the landing/parent page for its dir); every other
    `<stem>.md` -> Title Case with hyphens/underscores as spaces. Mirrors the
    publish-community-wiki.sh / publish-wiki.sh title convention.
    """
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    if stem == "index":
        return "Home"
    return stem.replace("-", " ").replace("_", " ").title()


_INLINE_MD = [
    (re.compile(r"`([^`]+)`"), r"\1"),            # inline code
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),      # bold
    (re.compile(r"\*([^*]+)\*"), r"\1"),          # italic
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # link -> text
]


def _strip_inline_md(s: str) -> str:
    """Reduce inline markdown to plain text (Confluence page titles are plain)."""
    for rx, rep in _INLINE_MD:
        s = rx.sub(rep, s)
    return s.strip()


def _h1_title(md_text: str):
    """The doc's H1 as a plain-text title, or None.

    The first column-0 `# ` line OUTSIDE any fenced code block — so a leading HTML
    comment / front-matter (several docs open with `<!-- audience: … -->`) is skipped
    to reach the real H1, while a `# ` comment inside a code block is never mistaken
    for the title. Emoji/unicode preserved; inline markdown (code/emphasis/links)
    flattened (Confluence titles are plain text).
    """
    in_fence = False
    for line in md_text.splitlines():
        st = line.lstrip()
        if st.startswith("```") or st.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("# "):
            return _strip_inline_md(line[2:].strip())
    return None


# --- page tree (dirs -> parent pages) + relative-link rewrite ----------------

def _titleize(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").title()


def _title_for(rel: str) -> str:
    """Title of the page for a source-relative markdown path.

    The ROOT `index.md` is the single "Home" page; a SECTION `index.md` takes its
    DIRECTORY name (`how-to/index.md` -> "How To") so titles stay unique in the
    Confluence space (page titles are a flat namespace). Every other file uses
    `page_title`.
    """
    rel = rel.replace(os.sep, "/")
    if rel == "index.md":
        return "Home"
    if os.path.basename(rel) == "index.md":
        return _titleize(os.path.basename(os.path.dirname(rel)))
    return page_title(rel)


def _parent_title(rel: str):
    """Parent-page title for a path, or None for the root.

    A leaf's parent is its directory's `index.md` page; a SECTION `index.md`'s
    parent is the ENCLOSING directory's page (top-level sections hang off "Home").
    """
    rel = rel.replace(os.sep, "/")
    if rel == "index.md":
        return None
    d = os.path.dirname(rel)
    if os.path.basename(rel) == "index.md":
        d = os.path.dirname(d)            # section index -> enclosing dir
    if d == "":
        return "Home"                     # top-level page/section -> site root
    return _title_for(d + "/index.md")


def _section_stub(title: str) -> str:
    """Storage body for a synthesized section landing page (children auto-list)."""
    return (f'<p>Documentation section: <strong>{title}</strong>.</p>'
            '<ac:structured-macro ac:name="children"/>')


def page_tree(source_dir, *, render_mermaid=False):
    """Walk `source_dir` -> ordered list of pages (parents strictly before children).

    Each entry: {"rel_path", "title", "parent_title", "storage", "synthesized"}.
    A landing page is SYNTHESIZED for any directory that has markdown but no
    `index.md` of its own (docs/enterprise sections have none), so every leaf nests
    under a real parent instead of scattering flat at the space root. Ordered by
    depth, then dir-index pages before their siblings, so an upsert can attach a
    child to an already-created parent id.
    """
    entries = []
    for root, _dirs, files in os.walk(source_dir):
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, source_dir).replace(os.sep, "/")
            atts = []
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            storage = render_markdown_to_storage(
                text, base_dir=os.path.dirname(full),
                attachments=atts, render_mermaid=render_mermaid)
            title = _title_for(rel)
            if os.path.basename(rel) != "index.md":     # leaf page -> prefer its verbose H1
                title = _h1_title(text) or title
            entries.append({"rel_path": rel, "title": title,
                            "parent_title": _parent_title(rel), "storage": storage,
                            "synthesized": False, "attachments": atts})

    # Synthesize a landing page for every ancestor directory that lacks an index.md.
    have_index = {os.path.dirname(e["rel_path"]) for e in entries
                  if os.path.basename(e["rel_path"]) == "index.md"}
    needed = set()
    for e in entries:
        d = os.path.dirname(e["rel_path"])
        while d:
            needed.add(d)
            d = os.path.dirname(d)
    for d in sorted(needed):
        if d in have_index:
            continue
        rel = d + "/index.md"
        title = _title_for(rel)
        entries.append({"rel_path": rel, "title": title,
                        "parent_title": _parent_title(rel),
                        "storage": _section_stub(title), "synthesized": True,
                        "attachments": []})

    entries.sort(key=lambda e: (e["rel_path"].count("/"),
                                0 if os.path.basename(e["rel_path"]) == "index.md" else 1,
                                e["rel_path"]))
    return entries


def rewrite_links(storage: str, rel_path: str, title_by_relpath: dict) -> str:
    """Rewrite intra-doc relative `.md` links to Confluence `<ac:link>` page refs.

    External (`://`, `mailto:`) and pure-anchor (`#…`) links pass through unchanged;
    a `.md#fragment` link resolves to the target page (deep anchor dropped in v1).
    """
    base = os.path.dirname(rel_path.replace(os.sep, "/"))

    def _sub(m):
        href, text = m.group(1), m.group(2)
        if "://" in href or href.startswith("#") or href.startswith("mailto:"):
            return m.group(0)
        path = href.split("#", 1)[0]
        if not path.endswith(".md"):
            return m.group(0)
        target = os.path.normpath(os.path.join(base, path)).replace(os.sep, "/")
        title = title_by_relpath.get(target)
        if not title:
            return m.group(0)
        if "<" in text:      # link text carries markup (e.g. <code>) -> RICH body
            body = "<ac:link-body>%s</ac:link-body>" % text       # keep XHTML escaping
        else:                # plain text -> CDATA body (entities un-escaped for literal)
            body = ("<ac:plain-text-link-body>%s</ac:plain-text-link-body>"
                    % _cdata(_unescape_xml(text)))
        return '<ac:link><ri:page ri:content-title="%s"/>%s</ac:link>' % (title, body)

    return re.sub(r'<a href="([^"]+)">(.*?)</a>', _sub, storage)


# --- Confluence Cloud REST v2 adapter (the ONLY target-specific code) --------
# Reached only for `--target confluence` WITHOUT `--dry-run`. All calls funnel
# through _cf_req so the adapter is unit-tested with a mocked REST client
# (tests/scripts/test_confluence_upsert.py); the live smoke is an operator step.

def _cf_base() -> str:
    """Confluence REST v2 base URL.

    Two token types, two URL shapes:
    - **Scoped API tokens** (service-account / `ATSTT…` tokens — the recommended
      publisher identity): do NOT authenticate against the plain
      `https://<site>/wiki/...` URL (it returns 401); they MUST go through the
      `api.atlassian.com` gateway addressed by cloudId. Set `ATLASSIAN_CLOUD_ID`.
    - **Classic user tokens** (`ATATT…`): work against the site URL. Leave
      `ATLASSIAN_CLOUD_ID` unset.
    """
    cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID", "").strip()
    if cloud_id:
        return f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2"
    site = os.environ["ATLASSIAN_SITE"]           # e.g. rzfz.atlassian.net
    return f"https://{site}/wiki/api/v2"


def _cf_auth_header() -> dict:
    email = os.environ["ATLASSIAN_EMAIL"]
    token = os.environ["ATLASSIAN_API_TOKEN"]     # env only — never hardcoded
    raw = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}", "Content-Type": "application/json"}


def _cf_auth_header_v1() -> dict:
    """Auth for the v1 attachment endpoints. v1 authorises on CLASSIC scopes, but a
    token that mixes classic + granular scopes FAILS the v2 page WRITE. So the
    attachment (v1) calls use a separate CLASSIC-scoped `ATLASSIAN_FILE_TOKEN` when
    set; the granular `ATLASSIAN_API_TOKEN` stays clean for v2 pages. Falls back to
    the API token when no file token is configured (single classic token, all-v1)."""
    email = os.environ["ATLASSIAN_EMAIL"]
    token = os.environ.get("ATLASSIAN_FILE_TOKEN") or os.environ["ATLASSIAN_API_TOKEN"]
    raw = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}", "Content-Type": "application/json"}


def _cf_req(method: str, path: str, body=None):
    req = urllib.request.Request(
        _cf_base() + path, method=method,
        data=(json.dumps(body).encode() if body is not None else None),
        headers=_cf_auth_header())
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:                     # surface the API error
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Confluence {method} {path} -> HTTP {e.code}: {detail}") from e


def _cf_space_id(space_key: str) -> str:
    q = urllib.parse.urlencode({"keys": space_key})
    data = _cf_req("GET", f"/spaces?{q}")
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"Confluence space not found: {space_key}")
    return str(results[0]["id"])


def _cf_find_page_id(space_id: str, title: str):
    """Return the page id for `title` in the space, or None (find-by-title)."""
    q = urllib.parse.urlencode({"space-id": space_id, "title": title})
    data = _cf_req("GET", f"/pages?{q}")
    for p in (data.get("results") or []):
        if p.get("title") == title:
            return str(p["id"])
    return None


def _cf_upsert(space_id: str, title: str, parent_id, storage: str, existing_id):
    """Create (POST) or update (PUT, version+1) a page; skip when unchanged.

    Returns one of "created" | "updated" | "unchanged" so the run stays idempotent:
    re-publishing an unmodified page writes NOTHING (no version bump).
    """
    body = {"representation": "storage", "value": storage}
    if existing_id is None:
        payload = {"spaceId": str(space_id), "status": "current",
                   "title": title, "body": body}
        if parent_id is not None:
            payload["parentId"] = str(parent_id)
        res = _cf_req("POST", "/pages", payload)
        return str(res["id"]), "created"

    # Existing page: read current version + stored body; only PUT on a real diff.
    cur = _cf_req("GET", f"/pages/{existing_id}?body-format=storage")
    cur_val = (((cur.get("body") or {}).get("storage") or {}).get("value")) or ""
    cur_parent = str(cur.get("parentId")) if cur.get("parentId") is not None else None
    cur_title = cur.get("title")
    want_parent = str(parent_id) if parent_id is not None else None
    if cur_val == storage and cur_parent == want_parent and cur_title == title:
        return str(existing_id), "unchanged"     # title compared too -> a rename updates in place
    cur_ver = int(((cur.get("version") or {}).get("number")) or 0)
    payload = {"id": str(existing_id), "status": "current", "title": title,
               "body": body, "version": {"number": cur_ver + 1}}
    if parent_id is not None:
        payload["parentId"] = str(parent_id)
    _cf_req("PUT", f"/pages/{existing_id}", payload)
    return str(existing_id), "updated"


# --- attachments: v1 REST (v2 has no upload endpoint) via the same gateway -----
# Attachment CREATE/UPDATE is v1-only in Confluence Cloud; v1 authorises on the
# CLASSIC scopes (write:confluence-file), while v2 pages use GRANULAR scopes — the
# service-account token must carry both families (each endpoint uses its own).

def _cf_base_v1() -> str:
    cloud_id = os.environ.get("ATLASSIAN_CLOUD_ID", "").strip()
    if cloud_id:
        return f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api"
    return f"https://{os.environ['ATLASSIAN_SITE']}/wiki/rest/api"


_MP_BOUNDARY = "----rzfzDocsBotBoundary8f2a1c4e"


def _cf_list_attachments(page_id: str) -> dict:
    """{filename: {"id", "size"}} for a page — v2 (granular read:attachment)."""
    out = {}
    data = _cf_req("GET", "/pages/%s/attachments?limit=250" % page_id)
    for a in (data.get("results") or []):
        out[a.get("title")] = {"id": a.get("id"), "size": a.get("fileSize")}
    return out


def _cf_delete_attachment(att_id: str) -> None:
    """Delete an attachment — v2 (granular delete:attachment)."""
    _cf_req("DELETE", "/attachments/%s" % att_id)


def _cf_upload_attachment(page_id, filename, data, existing,
                          content_type="image/png") -> str:
    """Attach a file to a page. Skips a same-size existing attachment; a CHANGED
    one is deleted (v2) then recreated — avoiding the v1 update-by-id endpoint and
    its id-format quirks. Reads/deletes are v2 (granular scopes); only the
    unavoidable CREATE is v1 (classic write:confluence-file), reached via the same
    gateway. `existing` is the page's {filename: {id,size}} map from
    _cf_list_attachments (fetched once per page)."""
    ex = existing.get(filename)
    if ex and ex.get("size") == len(data):
        return "att-unchanged"
    if ex and ex.get("id"):
        _cf_delete_attachment(ex["id"])

    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n' % (_MP_BOUNDARY, filename, content_type))
    tail = ('\r\n--%s\r\nContent-Disposition: form-data; name="minorEdit"\r\n\r\ntrue'
            '\r\n--%s--\r\n' % (_MP_BOUNDARY, _MP_BOUNDARY))
    payload = head.encode("utf-8") + data + tail.encode("utf-8")
    headers = {
        "Authorization": _cf_auth_header_v1()["Authorization"],
        "X-Atlassian-Token": "no-check",
        "Content-Type": "multipart/form-data; boundary=%s" % _MP_BOUNDARY,
    }
    req = urllib.request.Request(
        _cf_base_v1() + "/content/%s/child/attachment" % page_id,
        data=payload, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("attachment %s -> HTTP %s: %s"
                           % (filename, e.code, detail)) from e
    return "att-updated" if ex else "att-created"


def _cf_reset_space(space_id: str) -> int:
    """Delete every current page in the space; return the count. Confluence is a
    disposable render target (git is the source of truth), so `--reset` gives a clean
    re-sync — needed when page TITLES change (e.g. filename -> H1), which would
    otherwise leave the old-titled pages behind as duplicates. Up to 3 passes cover
    parent/child ordering; a page that can't be deleted (e.g. the space homepage) is
    left in place."""
    deleted = 0
    for _ in range(3):
        data = _cf_req("GET", "/spaces/%s/pages?limit=250" % space_id)
        pages = data.get("results") or []
        if not pages:
            break
        stuck = True
        for p in pages:
            try:
                _cf_req("DELETE", "/pages/%s" % p["id"])
                deleted += 1
                stuck = False
            except RuntimeError:
                pass          # cascaded child / protected homepage — retry next pass
        if stuck:
            break
    return deleted


# --- stable page identity: match by source path, not title --------------------
# Title-matching alone re-creates a page (NEW id) when its H1/title changes, which
# breaks any help-center topic / bookmark that referenced the old id. So each page
# carries a `rzfzRelpath` content property = its source rel_path; the publisher
# matches on that and RENAMES in place, keeping the id stable across title edits.

_RELPATH_PROP = "rzfzRelpath"


def _next_cursor(data):
    nxt = ((data.get("_links") or {}).get("next")) or ""
    if not nxt:
        return None
    q = urllib.parse.urlparse(nxt).query
    return urllib.parse.parse_qs(q).get("cursor", [None])[0]


def _cf_page_property_get(page_id, key):
    """(prop_id, version, value) for a page content property, or None."""
    data = _cf_req("GET", "/pages/%s/properties?key=%s"
                   % (page_id, urllib.parse.quote(key)))
    for r in (data.get("results") or []):
        if r.get("key") == key:
            return (str(r["id"]),
                    int(((r.get("version") or {}).get("number")) or 0),
                    r.get("value"))
    return None


def _cf_page_property_set(page_id, key, value):
    """Idempotently set a page content property (skip when already equal)."""
    ex = _cf_page_property_get(page_id, key)
    if ex and ex[2] == value:
        return
    if ex:
        _cf_req("PUT", "/pages/%s/properties/%s" % (page_id, ex[0]),
                {"key": key, "value": value, "version": {"number": ex[1] + 1}})
    else:
        _cf_req("POST", "/pages/%s/properties" % page_id,
                {"key": key, "value": value})


def _cf_build_relpath_index(space_id: str) -> dict:
    """{source rel_path -> pageId} from the `rzfzRelpath` property on each page."""
    idx = {}
    pages, cursor = [], None
    while True:
        q = "/spaces/%s/pages?limit=250" % space_id
        if cursor:
            q += "&cursor=%s" % urllib.parse.quote(cursor)
        data = _cf_req("GET", q)
        pages.extend(data.get("results") or [])
        cursor = _next_cursor(data)
        if not cursor:
            break
    for p in pages:
        prop = _cf_page_property_get(p["id"], _RELPATH_PROP)
        if prop and prop[2]:
            idx[prop[2]] = str(p["id"])
    return idx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="publish-enterprise-docs")
    ap.add_argument("--target", default="confluence")
    ap.add_argument("--source", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="delete all pages in the space before publishing "
                         "(title migration / clean re-sync)")
    a = ap.parse_args(argv)

    if a.target != "confluence":
        # Documented reversibility exit path (P3): --target static -> self-hosted
        # branded render + Cognee, from the SAME git source. Not built in 2026.08.
        print(f"target '{a.target}' not implemented yet — documented exit path / P3 stub",
              file=sys.stderr)
        return 3

    tree = page_tree(a.source, render_mermaid=not a.dry_run)
    title_by_rel = {e["rel_path"]: e["title"] for e in tree}
    for e in tree:
        e["storage"] = rewrite_links(e["storage"], e["rel_path"], title_by_rel)

    if a.dry_run:
        space = os.environ.get("CONFLUENCE_SPACE_KEY", "<CONFLUENCE_SPACE_KEY unset>")
        print(f"DRY-RUN — {len(tree)} page(s) planned for space {space} (parent -> title):")
        for e in tree:
            tag = "  (synthesized section page)" if e["synthesized"] else ""
            print(f"  {e['parent_title'] or '(space root)'} -> {e['title']}"
                  f"  [{e['rel_path']}]{tag}")
        print("DRY-RUN — no network calls made.")
        return 0

    space_key = os.environ["CONFLUENCE_SPACE_KEY"]
    space_id = _cf_space_id(space_key)
    if a.reset:
        n = _cf_reset_space(space_id)
        print(f"reset: deleted {n} existing page(s) in {space_key}")
    relpath_idx = _cf_build_relpath_index(space_id)   # stable identity: rel_path -> id
    ids = {}
    tally = {"created": 0, "updated": 0, "unchanged": 0}
    atally = {"att-created": 0, "att-updated": 0, "att-unchanged": 0}
    for e in tree:
        # match by the stable source-path property first; fall back to title (new /
        # not-yet-stamped pages) so ids stay put across future title edits.
        existing = relpath_idx.get(e["rel_path"]) or _cf_find_page_id(space_id, e["title"])
        parent_id = ids.get(e["parent_title"])
        if parent_id is None and e["parent_title"] is not None:
            parent_id = ids.get("Home")     # never orphan a page at the space root
        new_id, status = _cf_upsert(space_id, e["title"], parent_id, e["storage"], existing)
        ids[e["title"]] = new_id
        tally[status] += 1
        _cf_page_property_set(new_id, _RELPATH_PROP, e["rel_path"])   # stamp/refresh identity
        page_atts = e.get("attachments") or []
        if page_atts:                                # screenshots + rendered mermaid PNGs
            existing_att = _cf_list_attachments(new_id)
            for att in page_atts:
                data = att.get("data")
                if data is None:
                    with open(att["path"], "rb") as fh:
                        data = fh.read()
                act = _cf_upload_attachment(new_id, att["filename"], data, existing_att)
                atally[act] = atally.get(act, 0) + 1
    print(f"published {len(tree)} page(s) to space {space_key} "
          f"(created {tally['created']}, updated {tally['updated']}, "
          f"unchanged {tally['unchanged']})")
    n_att = sum(atally.values())
    if n_att:
        print("  attachments: %d (created %d, updated %d, unchanged %d)"
              % (n_att, atally["att-created"], atally["att-updated"],
                 atally["att-unchanged"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
