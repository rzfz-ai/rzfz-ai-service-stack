# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#3299: a model's licence and its model-card link, for the deploy dialog.

The operator has to see the licence BEFORE deploying: some models carry terms the
customer is responsible for. Everything here is what Hugging Face STATES, never
an inference from a model name:

- the licence id from the card metadata (``cardData.license``), else from the
  ``license:<id>`` tag the Hub derives from it;
- its name and link where the card gives them (``license_name`` /
  ``license_link``, used for ``other`` licences);
- a link to the licence text, in this order: the card's ``license_link``, a
  ``LICENSE`` file at the repo root, the SPDX text for a standard id.

Three states, kept apart on purpose:
  ``stated``      the repo was read and names a licence;
  ``not_stated``  the repo was read and names none ("check the model card");
  ``unknown``     the repo could not be read (offline, lookup failed or ran out
                  of time) — saying "not stated" there would be a false claim.

The model-card link is built from a well-formed repo id alone, so it is offered
in every state, offline included (it is a link, nothing is fetched).
"""
from __future__ import annotations

import posixpath
import re
from typing import Optional

_HF_HOST = "https://huggingface.co"
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# third-party strings: bounded before they reach the console
_MAX_TEXT = 100
# A licence file at the repo root, as repos name it.
_LICENSE_FILE_RE = re.compile(r"(?i)^licen[cs]e(?:\.(?:md|txt))?$")
# Hub licence ids that are SPDX identifiers with one unambiguous text. Ids the
# Hub uses for families (gpl-3.0 only/or-later, llama*, gemma, openrail*) are
# deliberately absent: those link only through the card or a repo file.
_SPDX = {
    "apache-2.0": "Apache-2.0",
    "mit": "MIT",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "mpl-2.0": "MPL-2.0",
    "cc0-1.0": "CC0-1.0",
    "cc-by-4.0": "CC-BY-4.0",
    "cc-by-sa-4.0": "CC-BY-SA-4.0",
    "cc-by-nc-4.0": "CC-BY-NC-4.0",
    "cc-by-nc-sa-4.0": "CC-BY-NC-SA-4.0",
    "cc-by-nc-nd-4.0": "CC-BY-NC-ND-4.0",
    "unlicense": "Unlicense",
}


def _valid_repo_id(repo_id: Optional[str]) -> bool:
    if not repo_id or not _REPO_ID_RE.match(repo_id):
        return False
    return all(seg not in (".", "..") for seg in repo_id.split("/"))


def model_card_url(repo_id: Optional[str]) -> Optional[str]:
    """``https://huggingface.co/<owner>/<repo>`` for a well-formed id, else None."""
    return f"{_HF_HOST}/{repo_id}" if _valid_repo_id(repo_id) else None


def _text(v) -> Optional[str]:
    if isinstance(v, (list, tuple)):
        parts = [str(x).strip() for x in v if isinstance(x, str) and x.strip()]
        v = ", ".join(parts)
    if not isinstance(v, str) or not v.strip():
        return None
    return v.strip()[:_MAX_TEXT]


def _repo_file_url(repo_id: str, path: str) -> Optional[str]:
    """A link to ``path`` inside the repo; None if it would leave the repo."""
    path = path.strip()
    norm = posixpath.normpath(path) if path else ""
    if not norm or norm == "." or norm.startswith(("..", "/")):
        return None
    if not all(re.match(r"^[A-Za-z0-9._-]+$", seg) for seg in norm.split("/")):
        return None
    return f"{_HF_HOST}/{repo_id}/blob/main/{norm}"


def _link_url(repo_id: str, link) -> Optional[str]:
    """The card's ``license_link``: an https URL as given, or a repo-relative path.
    Anything else (``javascript:``, plain http, protocol-relative, ``..``) is dropped."""
    if not isinstance(link, str) or not link.strip():
        return None
    link = link.strip()
    if link.startswith("https://"):
        return link if not re.search(r"\s", link) else None
    if ":" in link or link.startswith("/"):
        return None
    return _repo_file_url(repo_id, link)


def _license_file(repo_id: str, files) -> Optional[str]:
    names = []
    for f in files or []:
        p = (f.get("path") or f.get("rfilename") or "") if isinstance(f, dict) else ""
        if p and "/" not in p and _LICENSE_FILE_RE.match(p):
            names.append(p)
    # LICENSE before LICENSE.md before LICENSE.txt: the shortest name first
    for p in sorted(names, key=lambda n: (len(n), n)):
        return _repo_file_url(repo_id, p)
    return None


def _empty(state: str, reason: Optional[str], card: Optional[str]) -> dict:
    return {"state": state, "reason": reason, "id": None, "name": None,
            "source": None, "url": None, "url_source": None, "card_url": card}


def license_facts(info: Optional[dict], files: Optional[list], repo_id: Optional[str], *,
                  offline: bool = False) -> dict:
    """The licence block for one repo.

    ``info``: the Hub's ``/api/models/<repo>`` answer (``{}``/None = not read).
    ``files``: the repo tree (paths). ``offline``: the box may not reach the Hub.
    """
    card = model_card_url(repo_id)
    if offline:
        return _empty("unknown", "offline", card)
    if not isinstance(info, dict) or not info or not card:
        return _empty("unknown", "not retrieved", card)

    meta = info.get("cardData") if isinstance(info.get("cardData"), dict) else {}
    lic, source = _text(meta.get("license")), "card"
    if lic is None:
        tags = [t for t in (info.get("tags") or []) if isinstance(t, str) and t.startswith("license:")]
        lic, source = _text([t.split(":", 1)[1] for t in tags]), "tag"
    file_url = _license_file(repo_id, files)

    if lic is None:
        out = _empty("not_stated", None, card)
        if file_url:   # a fact about the repo, not a guess at the licence
            out.update(url=file_url, url_source="repo_file")
        return out

    url, url_source = _link_url(repo_id, meta.get("license_link")), "license_link"
    if url is None and file_url:
        url, url_source = file_url, "repo_file"
    if url is None and lic.lower() in _SPDX:
        url, url_source = f"https://spdx.org/licenses/{_SPDX[lic.lower()]}.html", "spdx"
    if url is None:
        url_source = None
    return {"state": "stated", "reason": None, "id": lic, "name": _text(meta.get("license_name")),
            "source": source, "url": url, "url_source": url_source, "card_url": card}
