# Per-user cognee proxy with a HOST-INDEPENDENT per-instance bearer gate (#36
# BLOCKER-1). Wraps the stock cognee-mcp image: cognee-mcp runs on :8001 (loopback-bind requested; verified at
# runtime by shim.py, see #94)
# :8001, a tiny bearer-checking shim listens on :8000 (the only externally
# addressed port). A request without the exact PROXY_AUTH_TOKEN bearer gets 401
# even when reached by container-name with a spoofed Host header.
#
# Pinned to the SAME tag as the shared cognee-mcp sidecar (bump in lockstep).
FROM cognee/cognee-mcp:1.5.3.dev1
# #2336: security floor over the base image. (1) every OS package of the base to
# today's security level; (2) the Python pins in requirements-security-pins.txt
# over the base's own virtualenv. The base's final stage is python:3.12-slim-bookworm with pip, and
# its venv has none, so the system pip installs into it (`--python`). Wheels are
# preferred; `unstructured` 0.24 depends on `langdetect`, which publishes no wheel
# at all, but the base already carries it (satisfied, so never rebuilt). The image
# has no compiler: a pin that needed a compiled source build fails the build. The
# check afterwards fails the build if a pin did not take (pip keeps an
# already-satisfied requirement silently).
USER root
# #3343 (2026.11-ga.1): the interpreter the base ships carries 2 Critical CVEs, fixed in
# Python 3.12.16. The base's runtime is python:3.12-slim-bookworm, so the same image at the
# fixed patch release is laid over /usr/local (same ABI, same Debian; the venv links to
# /usr/local/bin/python3 and keeps its packages). The check fails the build if it did not take.
COPY --from=python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df /usr/local/ /usr/local/
RUN /app/.venv/bin/python -c "import sys; sys.exit('python %s, want 3.12.16+' % sys.version.split()[0] if sys.version_info[:3] < (3, 12, 16) else 0)"
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY requirements-security-pins.txt /tmp/requirements-security-pins.txt
RUN /usr/local/bin/python -m pip --python /app/.venv/bin/python install \
        --no-cache-dir --disable-pip-version-check --prefer-binary -r /tmp/requirements-security-pins.txt \
    && /app/.venv/bin/python -c "import importlib.metadata as m, sys; bad=[l for l in open('/tmp/requirements-security-pins.txt').read().splitlines() if l and not l.startswith('#') and m.version(l.split('==')[0])!=l.split('==')[1]]; sys.exit('pins did not take: %s' % bad if bad else 0)" \
    && rm -f /tmp/requirements-security-pins.txt

# #2336: unstructured 0.24 downloads the spaCy model en_core_web_sm from GitHub on
# the first partition (nlp/tokenize.py). Under `--network none` that raises and
# nothing can be ingested; on an online box it is an unplanned egress. The model is
# baked here instead: unstructured's own pinned wheel (the same version and sha256 as
# its _SPACY_MODEL_VERSION/_SPACY_MODEL_SHA256), fetched over TLS, checked with
# sha256sum, installed from that file only (--no-index --no-deps) into the venv.
# spaCy models are GitHub release assets, not on PyPI, so there is no index to pin
# against. The image has no curl; python does the fetch. The load check below fails
# the build if the model is not importable and loadable, and if spaCy left the 3.8
# line the model was trained for. unstructured 0.24 has no NLTK download path (it
# does not import nltk), so there is nothing else to bake.
ARG SPACY_MODEL_VERSION=3.8.0
ARG SPACY_MODEL_SHA256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85
RUN set -eu; \
    whl="en_core_web_sm-${SPACY_MODEL_VERSION}-py3-none-any.whl"; \
    mtmp="$(mktemp -d)"; \
    /usr/local/bin/python -c "import sys, urllib.request as u; u.urlretrieve(sys.argv[1], sys.argv[2])" \
        "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-${SPACY_MODEL_VERSION}/$whl" "$mtmp/$whl"; \
    echo "${SPACY_MODEL_SHA256}  $mtmp/$whl" | sha256sum -c -; \
    /usr/local/bin/python -m pip --python /app/.venv/bin/python install \
        --no-cache-dir --disable-pip-version-check --no-index --no-deps "$mtmp/$whl"; \
    rm -rf "$mtmp"; \
    /app/.venv/bin/python -c "import en_core_web_sm; en_core_web_sm.load()"; \
    /app/.venv/bin/python -c "import spacy, sys; sys.exit('spaCy %s is outside the 3.8 line en_core_web_sm 3.8.0 needs' % spacy.__version__ if not spacy.__version__.startswith('3.8.') else 0)"

# A user's recall reaches every dataset they may read (shared ones and the company brain
# included), and a failed tool call is an MCP error (isError: true), not text that reads as
# an answer. cognee-mcp sends dataset NAMES, which cognee resolves among the caller's OWN
# datasets only, so one shared dataset 404ed the whole recall. rzfz_mcp_recall (loaded by its
# .pth at interpreter start) sends dataset ids from the user's own dataset list and raises
# cognee-mcp's "Error: ..." answers as tool errors. The check runs the real cognee-mcp client
# and registry through a real FastMCP round trip and fails the build when a seam moved.
COPY rzfz_mcp_recall.py rzfz_mcp_recall.pth /tmp/rzfz-mcp-recall/
RUN SP="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && cp /tmp/rzfz-mcp-recall/rzfz_mcp_recall.py /tmp/rzfz-mcp-recall/rzfz_mcp_recall.pth "$SP/" \
    && rm -rf /tmp/rzfz-mcp-recall \
    && cd /app && /app/.venv/bin/python -c "import rzfz_mcp_recall as R; R.self_check(); print('rzfz_mcp_recall OK')"
USER cognee

# The upstream cognee-mcp image runs as a non-root user, so a build-time chmod
# is not permitted — invoke the scripts via their interpreter instead (no exec
# bit needed).
COPY shim.py /app/shim.py
COPY entrypoint.sh /app/authshim-entrypoint.sh

EXPOSE 8000
# The shim answers /shim-health locally without a bearer (creds never exposed).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/shim-health',timeout=4)" || exit 1

ENTRYPOINT ["bash", "/app/authshim-entrypoint.sh"]
