# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Pure relay frame codec for worker↔master WS inference relay (#262).

JSON text frames with type validation and size limits.
No I/O, no network, stdlib only.
"""

import json
import base64
from typing import Any, Dict, Optional


class RelayProtocolError(Exception):
    """Raised on malformed/oversize/unknown frame types."""

    pass


# Maximum frame size (~32 MiB to match a reasonable streaming chunk cap)
MAX_FRAME_SIZE = 32 * 1024 * 1024

# #929 (M-oversize): the byte budget for ONE `chunk` frame's PAYLOAD, i.e. how
# much raw response body a producer may put into a single frame.
#
# An oversize frame is not a per-request failure — the WS peer closes the whole
# connection with 1009, and that connection is MULTIPLEXED, so every OTHER
# in-flight request on the same worker dies with it. The producer therefore must
# never build a frame it cannot send; splitting the payload across frames is
# lossless (the consumer concatenates them) and costs nothing.
#
# MAX_FRAME_SIZE // 8 is deliberately conservative rather than tight: the
# serialized frame is bigger than its payload by whichever expansion the codec
# applies — base64 (4/3) for a non-utf-8 chunk, JSON string escaping (up to 6x
# for a run of control characters, `\u00XX` each) for a text one — plus the
# frame's own keys. One eighth keeps even the worst case inside the cap without
# the producer having to model the encoding.
MAX_CHUNK_PAYLOAD_BYTES = MAX_FRAME_SIZE // 8

# Known frame types
KNOWN_TYPES = {"req", "cancel", "head", "chunk", "end", "err", "ping", "pong"}


def encode(frame: Dict[str, Any]) -> str:
    """Encode a frame dict to JSON text with compact separators.

    Args:
        frame: Dict with at minimum {"t": <type>, ...}

    Returns:
        JSON string with separators=(",", ":")
    """
    return json.dumps(frame, separators=(",", ":"))


def exceeds_max_frame(text: str) -> bool:
    """#929: would this ALREADY-ENCODED frame be rejected as oversize?

    The mirror image of ``decode``'s first check, for a PRODUCER: the sending
    side asks before it writes, so an oversize frame can be turned into a
    failure of ONE request instead of a 1009 close that takes down every other
    request multiplexed onto the same connection. Takes the encoded text (not
    the dict) so a caller that is about to send does not pay for a second
    ``encode`` just to measure it.
    """
    return len(text.encode("utf-8")) > MAX_FRAME_SIZE


def decode(text: str) -> Dict[str, Any]:
    """Decode JSON text to a frame dict with validation.

    Raises RelayProtocolError if:
    - text is not valid JSON
    - frame lacks "t" key
    - "t" value is not in KNOWN_TYPES
    - total size exceeds MAX_FRAME_SIZE

    Args:
        text: JSON string

    Returns:
        Decoded frame dict
    """
    # Check size before parsing
    if len(text.encode("utf-8")) > MAX_FRAME_SIZE:
        raise RelayProtocolError(f"Frame exceeds max size {MAX_FRAME_SIZE}")

    # Parse JSON
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as e:
        raise RelayProtocolError(f"Invalid JSON: {e}")

    # Validate it's a dict
    if not isinstance(frame, dict):
        raise RelayProtocolError("Frame must be a JSON object")

    # Validate "t" field exists
    if "t" not in frame:
        raise RelayProtocolError("Frame missing required 't' (type) field")

    # Validate type is known
    frame_type = frame["t"]
    if frame_type not in KNOWN_TYPES:
        raise RelayProtocolError(f"Unknown frame type '{frame_type}'")

    return frame


# Builders (return plain dicts)


def req(
    id: str, method: str, path: str, headers: Dict[str, str], body: str,
    b64: bool = False,
) -> Dict[str, Any]:
    """Build a request frame.

    Args:
        id: Request ID
        method: HTTP method
        path: Request path (e.g. "/v1/chat/completions")
        headers: Request headers
        body: Request body (utf-8 text, or the raw text to base64 if b64=True)
        b64: If True, `body` is base64-encoded before serializing and the
             frame carries a `b64: true` flag — the lossless path for a
             request body that isn't valid utf-8 (binary/multipart). Mirrors
             `chunk(b64=...)` exactly (surrogateescape + base64), so the two
             directions round-trip the same way.

    Returns:
        Frame dict with body (and b64 flag if True)
    """
    frame = {"t": "req", "id": id, "method": method, "path": path, "headers": headers}
    if b64:
        # Encode to base64 string (same surrogateescape handling as chunk()).
        frame["body"] = base64.b64encode(body.encode("utf-8", errors="surrogateescape")).decode("ascii")
        frame["b64"] = True
    else:
        frame["body"] = body
    return frame


def head(id: str, status: int, headers: Dict[str, str]) -> Dict[str, Any]:
    """Build a response head frame."""
    return {"t": "head", "id": id, "status": status, "headers": headers}


def chunk(id: str, data: str, b64: bool = False) -> Dict[str, Any]:
    """Build a data chunk frame.

    Args:
        id: Request ID
        data: Text chunk (SSE bytes decoded utf-8, or base64 if b64=True)
        b64: If True, encode data as base64 before serializing

    Returns:
        Frame dict with data (and b64 flag if True)
    """
    frame = {"t": "chunk", "id": id}
    if b64:
        # Encode to base64 string
        encoded = base64.b64encode(data.encode("utf-8", errors="surrogateescape")).decode("ascii")
        frame["data"] = encoded
        frame["b64"] = True
    else:
        frame["data"] = data
    return frame


def end(id: str) -> Dict[str, Any]:
    """Build an end frame (no more chunks)."""
    return {"t": "end", "id": id}


def err(id: str, error: str) -> Dict[str, Any]:
    """Build an error frame."""
    return {"t": "err", "id": id, "error": error}


def cancel(id: str) -> Dict[str, Any]:
    """Build a cancel frame (client gone)."""
    return {"t": "cancel", "id": id}


def ping() -> Dict[str, Any]:
    """Build a keepalive ping frame."""
    return {"t": "ping"}


def pong() -> Dict[str, Any]:
    """Build a keepalive pong frame."""
    return {"t": "pong"}
