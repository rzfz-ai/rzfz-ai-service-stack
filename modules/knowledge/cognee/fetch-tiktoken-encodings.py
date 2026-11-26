#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2871 - seed TIKTOKEN_CACHE_DIR at image BUILD time.

tiktoken downloads an encoding from openaipublic.blob.core.windows.net unless the
file is already in TIKTOKEN_CACHE_DIR, named sha1(<url>) (tiktoken.load.read_file_cached).
Each file's sha256 is checked against the value tiktoken itself publishes
(tiktoken_ext/openai_public.py); a mismatch fails the build.

Usage: fetch-tiktoken-encodings.py <cache-dir>
"""
import hashlib
import os
import sys
import urllib.request

BASE = "https://openaipublic.blob.core.windows.net/encodings/"
ENCODINGS = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}


def cache_name(url: str) -> str:
    """The file name tiktoken looks for: the sha1 of the URL."""
    return hashlib.sha1(url.encode()).hexdigest()


def main(dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    for name, want in ENCODINGS.items():
        url = BASE + name + ".tiktoken"
        data = urllib.request.urlopen(url, timeout=60).read()
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            sys.exit("#2871: %s sha256 %s, expected %s" % (name, got, want))
        with open(os.path.join(dest, cache_name(url)), "wb") as fh:
            fh.write(data)
        print("tiktoken cache:", name, cache_name(url))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
