"""Cursor codec: opaque urlsafe-base64 JSON position markers (04 §3).

Each adapter owns its payload shape; on decode the blob is untrusted
input — malformed cursors yield an empty state and enumeration restarts
from the seed rather than trusting a corrupt position.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any


def encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), separators=(",", ":"), default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        state = json.loads(raw)
    except (ValueError, UnicodeError):
        return {}
    return state if isinstance(state, dict) else {}
