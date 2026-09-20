"""Text normalization primitives (Task 8; spec 03 §3, 14 §2).

All block text passes through here before it enters a block:

- bytes decode with the content-type charset first, falling back to
  UTF-8 ``errors=replace`` (flagged) — garbage never raises out of a parse;
- Unicode NFC (code-point offsets stay 左闭右开 semantics, 03 §3);
- control characters stripped except ``\\n`` and ``\\t``;
- whitespace collapse tuned per block kind (tables keep row lines).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_WHITESPACE_RUN = re.compile(r"[ \t\f\v]+")

_CONTROL_TABLE = str.maketrans(
    "", "", "".join(chr(c) for c in range(32) if c not in (9, 10))
)


@dataclass(frozen=True, slots=True)
class DecodedText:
    """One decode attempt's outcome; ``replaced`` feeds quality flags."""

    text: str
    encoding: str
    replaced: bool


def decode_bytes(data: bytes, charset: str | None) -> DecodedText:
    """Decode ``data`` preferring ``charset``; fall back to UTF-8 and
    finally to UTF-8 with replacement (flagged, never raising)."""
    for encoding in _encoding_candidates(charset):
        try:
            return DecodedText(data.decode(encoding), encoding, replaced=False)
        except (UnicodeDecodeError, LookupError):
            continue
    return DecodedText(
        data.decode("utf-8", errors="replace"), "utf-8+replace", replaced=True
    )


def _encoding_candidates(charset: str | None) -> tuple[str, ...]:
    if charset:
        cleaned = charset.strip().strip('"').lower()
        if cleaned:
            return (cleaned, "utf-8")
    return ("utf-8",)


def normalize_text(text: str) -> str:
    """NFC + CRLF folding + control-char strip (keeps ``\\n``/``\\t``)."""
    folded = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = folded.translate(_CONTROL_TABLE)
    return unicodedata.normalize("NFC", stripped)


def collapse_whitespace(text: str, *, kind: str) -> str:
    """Kind-aware whitespace collapse.

    - ``table`` keeps one text line per row (``\\n`` is the row-group
      separator; collapsing it would flatten rows — PAR-02);
    - other kinds collapse all whitespace runs to single spaces.
    """
    if kind == "table":
        rows = [
            _WHITESPACE_RUN.sub(" ", row).strip() for row in text.split("\n")
        ]
        return "\n".join(row for row in rows if row)
    collapsed = _WHITESPACE_RUN.sub(" ", text.replace("\n", " "))
    return collapsed.strip()


def clean_block_text(text: str, *, kind: str) -> str:
    """Full pipeline for one block's text."""
    return collapse_whitespace(normalize_text(text), kind=kind)
