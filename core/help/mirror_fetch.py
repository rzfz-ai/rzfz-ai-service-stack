# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Bounded HTTP fetcher for the non-wget capture paths (#1196).

`wget --mirror` is the right tool for a whole static site; the llms-txt
capture and the wget asset-completion pass instead need a handful of
single, size-capped GETs with a polite inter-request delay. This is that —
and nothing more: no retries beyond `requests`' own, no redirects across
schemes we do not want, never an exception to the caller (a failed fetch is
a status of 0 with the reason in `error`).

Tests inject a stand-in with the same two methods (`get`, `text`), so the
capture drivers never touch the network in the unit tier.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

USER_AGENT = 'Mozilla/5.0 (compatible; razzfazz-help-cache/1.0)'
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_DELAY = 0.1


@dataclass
class FetchResult:
    status: int
    content_type: str
    body: bytes
    url: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


class HttpFetcher:
    def __init__(self, *, user_agent: str = USER_AGENT, timeout: int = DEFAULT_TIMEOUT,
                 max_bytes: int = DEFAULT_MAX_BYTES, delay: float = DEFAULT_DELAY,
                 session=None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.delay = delay
        self._session = session or requests.Session()
        self._session.headers.update({'User-Agent': user_agent})
        self._last = 0.0
        self.count = 0
        self.bytes = 0

    def _pace(self) -> None:
        if self.delay <= 0:
            return
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str) -> FetchResult:
        """GET `url`, capped at max_bytes (an over-size body is a status-0
        failure, never a truncated success)."""
        self._pace()
        self.count += 1
        try:
            with self._session.get(url, timeout=self.timeout, stream=True,
                                   allow_redirects=True) as resp:
                ctype = resp.headers.get('Content-Type', '') or ''
                clen = resp.headers.get('Content-Length')
                if clen and clen.isdigit() and int(clen) > self.max_bytes:
                    return FetchResult(0, ctype, b'', url,
                                       f'body larger than {self.max_bytes} bytes')
                chunks, total = [], 0
                for chunk in resp.iter_content(65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_bytes:
                        return FetchResult(0, ctype, b'', url,
                                           f'body larger than {self.max_bytes} bytes')
                    chunks.append(chunk)
                body = b''.join(chunks)
                self.bytes += len(body)
                return FetchResult(resp.status_code, ctype, body, resp.url or url)
        except requests.RequestException as e:
            return FetchResult(0, '', b'', url, f'{e.__class__.__name__}: {e}')

    def text(self, url: str) -> tuple[int, str]:
        r = self.get(url)
        return r.status, r.body.decode('utf-8', errors='replace')
