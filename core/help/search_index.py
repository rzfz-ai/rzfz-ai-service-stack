"""In-memory full-text search index for the rzfz.ai Help Center.

Pure stdlib (re, html) — no third-party search engine, no network, no CDN.
Builds a small inverted index over the local doc corpus (own docs, module
docs, cached mirrors) so `/api/search` can answer queries without ever
touching the network. Safe for air-gapped deployments.
"""

import html
import os
import re

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_SNIPPET_RADIUS = 60
_MAX_TEXT_PER_DOC = 200_000  # ~200 KB cap, see collect_corpus()

# HLP-3: query cost is bounded HERE as well as at the route.
#
# `search()` walks the postings once per term and `_make_snippet()` builds one
# alternation regex over the unique terms; both are linear in the term count,
# and the snippet regex used to be rebuilt per returned hit. A 100 KB `q` (the
# route caps the raw string at MAX_QUERY_CHARS, but a caller inside the process
# has no such cap) yields ~10k unique terms, i.e. a 10k-branch alternation
# compiled up to `limit` times per request — a CPU pin from a single request.
# So: truncate the term list, and compile the <mark> pattern ONCE per search.
MAX_QUERY_CHARS = 512   # rejected with 400 at /api/search
MAX_QUERY_TERMS = 32    # terms beyond this are dropped, not an error


def _tokenize(text):
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


class Index:
    """A built search index. Construct via build_index()."""

    def __init__(self, docs, postings):
        # docs: dict[id -> {"title", "section", "text"}]
        # postings: dict[term -> dict[id -> tf]]
        self._docs = docs
        self._postings = postings

    def search(self, query, allowed_ids=None, limit=20):
        # HLP-3: cap the term count before ANY per-term work happens.
        terms = _tokenize(query)[:MAX_QUERY_TERMS]
        if not terms:
            return []

        scores = {}
        for term in terms:
            hits = self._postings.get(term)
            if not hits:
                continue
            for doc_id, tf in hits.items():
                scores[doc_id] = scores.get(doc_id, 0) + tf

        if allowed_ids is not None:
            scores = {doc_id: s for doc_id, s in scores.items() if doc_id in allowed_ids}

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

        # HLP-3: compile the <mark> alternation ONCE per query, not once per
        # returned hit — it depends only on the terms, which do not change
        # across the result loop.
        mark_re = _mark_pattern(terms)

        results = []
        for doc_id, score in ranked[:limit]:
            doc = self._docs.get(doc_id, {})
            results.append({
                "id": doc_id,
                "title": doc.get("title", ""),
                "section": doc.get("section", ""),
                "snippet": _make_snippet(doc.get("text", ""), terms, mark_re),
                "score": score,
            })
        return results


def _mark_pattern(terms):
    """Word-boundary, case-insensitive alternation over the unique terms."""
    unique_terms = sorted(set(terms), key=len, reverse=True)
    if not unique_terms:
        return None
    return re.compile(
        r"\b(" + "|".join(re.escape(t) for t in unique_terms) + r")\b",
        re.IGNORECASE,
    )


def _make_snippet(text, terms, mark_re=None):
    """Build a ±60-char snippet around the first match, HTML-escaped,
    with matched terms wrapped in <mark>...</mark>.

    `mark_re` is the pre-compiled alternation from `_mark_pattern(terms)`;
    `search()` passes it so the pattern is compiled once per query instead of
    once per hit (HLP-3). It is optional so a direct caller still works.
    """
    if not text:
        return ""

    lower = text.lower()
    match_start = None
    for term in terms:
        pos = lower.find(term)
        if pos != -1 and (match_start is None or pos < match_start):
            match_start = pos
    if match_start is None:
        match_start = 0

    start = max(0, match_start - _SNIPPET_RADIUS)
    end = min(len(text), match_start + _SNIPPET_RADIUS)
    window = text[start:end]

    escaped = html.escape(window)

    # Wrap matched terms (word-boundary, case-insensitive) in <mark>.
    pattern = mark_re if mark_re is not None else _mark_pattern(terms)
    if pattern is not None:
        escaped = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)

    return escaped


def build_index(docs):
    """docs: list[{"id", "title", "section", "text"}] -> Index

    HLP-17: doc ids are UNIQUE in the index. A duplicate id used to be
    half-merged — `doc_map[doc_id] = {...}` overwrote (last wins) while the
    postings ACCUMULATED across both entries, so the surviving doc carried the
    other one's ranking weight and matched terms that do not occur in the text
    the user is then shown in the snippet. Last-wins on both halves instead:
    a later entry fully replaces an earlier one with the same id.
    """
    doc_map = {}
    doc_tf = {}

    for doc in docs:
        doc_id = doc["id"]
        doc_map[doc_id] = {
            "title": doc.get("title", ""),
            "section": doc.get("section", ""),
            "text": doc.get("text", ""),
        }
        tokens = _tokenize(doc.get("text", "")) + _tokenize(doc.get("title", ""))
        tf = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        doc_tf[doc_id] = tf   # replaces, never merges — see docstring

    postings = {}
    for doc_id, tf in doc_tf.items():
        for term, count in tf.items():
            postings.setdefault(term, {})[doc_id] = count

    return Index(doc_map, postings)


def _strip_tags(html_text):
    """Very small tag-stripper for cached mirror HTML -> plain text."""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def collect_corpus(cm):
    """Pull text for own docs, box-local module docs, and cached external
    mirrors from the real `cache_manager.CacheManager` API.

    Filtering by visibility happens at search time via `Index.search`'s
    `allowed_ids` — this builds the corpus once for everyone, so `cm` is the
    only argument (no `allowed` param: there is nothing to filter here).
    """
    docs = []

    # Own docs (docs/enterprise/**, auto-discovered) — rendered HTML stripped
    # back to text. render_own_doc() can return None for a resolution failure
    # (traversal guard, missing file); skip those rather than erroring.
    for d in cm.discover_own_docs():
        rendered = cm.render_own_doc(d["id"])
        if not rendered:
            continue
        docs.append({
            "id": d["id"],
            "title": d.get("name") or d["id"],
            "section": d.get("section_label") or d.get("section") or "",
            "text": _strip_tags(rendered)[:_MAX_TEXT_PER_DOC],
        })

    # Box-local module docs (core/help/module_docs/*.md, `local_doc` entries)
    # and cached external mirrors (wget/monolith trees under the cache dir),
    # both declared in mirror_config.json's "apps" list.
    for entry in cm.get_config().get("apps", []):
        app_id = entry.get("id")
        if not app_id:
            continue

        title = entry.get("name") or app_id
        section = entry.get("section_label") or entry.get("section") or ""
        text = ""

        # #1196: a markdown-tree capture (llms-txt / git-markdown) is the
        # real upstream documentation — index its pages, not the curated
        # fallback. getattr-guarded so a minimal stub cm still works.
        _tree_text = getattr(cm, "tree_corpus_text", None)
        _has_tree = getattr(cm, "has_valid_markdown_tree", None)
        if _tree_text is not None and _has_tree is not None and _has_tree(app_id):
            text = _tree_text(app_id)
        elif entry.get("local_doc"):
            rendered = cm.render_local_doc(app_id)
            if rendered:
                text = _strip_tags(rendered)
        else:
            cache_path = cm.get_cache_path(app_id) or ""
            index_path = os.path.join(cache_path, "index.html") if cache_path else ""
            if index_path and os.path.isfile(index_path):
                try:
                    with open(index_path, "r", encoding="utf-8", errors="replace") as fh:
                        raw = fh.read(_MAX_TEXT_PER_DOC)
                    text = _strip_tags(raw)
                except OSError:
                    text = ""

        if not text:
            continue

        docs.append({
            "id": app_id,
            "title": title,
            "section": section,
            "text": text[:_MAX_TEXT_PER_DOC],
        })

    return _dedupe_by_id(docs)


def _dedupe_by_id(docs):
    """One entry per doc id (HLP-17).

    An own-doc slug (`_slug_from_relpath`) can collide with a `mirror_config`
    app id — nothing prevents it, and nothing reported it. The collision has a
    single right answer: the index must describe the doc `/docs/<id>/` actually
    serves, and `CacheManager.get_app_entry()` resolves an id against
    `mirror_config.json` FIRST and only then against the discovered own docs.
    `collect_corpus` appends own docs first and mirror/local entries second, so
    LAST-WINS here reproduces exactly that precedence.
    """
    by_id = {}
    for doc in docs:
        by_id[doc["id"]] = doc
    return list(by_id.values())
