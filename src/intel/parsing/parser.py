"""Parser entrypoint: media-type dispatch + quality assessment (Task 8).

``Parser.parse(ParserInput) -> ParsedArtifact`` is pure: bytes in, data
out — no DB, no network (14 §2: parser 无 DB 权限). The workflow handler
reads the capture blob, calls this, and persists what comes back.

Dispatch:

- ``text/html`` (and XHTML) → :mod:`intel.parsing.html`
- ``application/pdf`` → :mod:`intel.parsing.pdf`
- other ``text/*`` → :mod:`intel.parsing.textnorm` plain-text path
- anything else → failed artifact (``unsupported_media_type``), never a
  silent success.
"""

from __future__ import annotations

import hashlib

from intel.parsing.dto import (
    Block,
    ExtractedDocument,
    ParsedArtifact,
    ParserInput,
)
from intel.parsing.html import parse_html
from intel.parsing.pdf import parse_pdf
from intel.parsing.quality import (
    Assessment,
    QualitySignals,
    QualityThresholds,
    assess_quality,
    detect_language,
    is_language_mixed,
)
from intel.parsing.textnorm import decode_bytes, normalize_text

_HTML_TYPES = {"text/html", "application/xhtml+xml"}
#: media types where ~1.0 chars/byte is legitimate (no markup to strip).
_PLAIN_TYPES = {"text/plain", "text/csv", "text/markdown"}


class Parser:
    """The built-in parser behind parser_versions row ``builtin@1``."""

    def __init__(
        self, thresholds: QualityThresholds | None = None
    ) -> None:
        self.thresholds = thresholds or QualityThresholds()

    def parse(self, capture: ParserInput) -> ParsedArtifact:
        media = (capture.media_type or "").split(";")[0].strip().lower()
        charset = _charset_of(capture.media_type)

        if media in _HTML_TYPES:
            extracted = parse_html(capture.raw, charset)
        elif media == "application/pdf":
            extracted = parse_pdf(capture.raw)
        elif media.startswith("text/") or not media:
            extracted = _parse_plain(capture.raw, charset)
        else:
            extracted = _unsupported(capture.raw)

        assessment = self._assess(capture, extracted, media)
        return ParsedArtifact(
            capture_id=capture.capture_id,
            parser_version=capture.parser_version,
            metadata=extracted.metadata,
            blocks=[Block(**block) for block in extracted.blocks],
            coverage=extracted.coverage,
            parse_status=assessment.parse_status,
            quality_flags=assessment.quality_flags,
            text_hash=_text_hash(extracted.blocks),
            retrieval_scope=assessment.retrieval_scope,
        )

    def _assess(
        self,
        capture: ParserInput,
        extracted,
        media: str,
    ) -> Assessment:
        text = extracted.full_text or ""
        language = detect_language(text)
        extracted.metadata["language"] = language
        extracted.metadata["parser"] = capture.parser_version
        signals = QualitySignals(
            media_type=media,
            byte_size=len(capture.raw),
            text_chars=extracted.text_chars,
            block_count=len(extracted.blocks),
            boilerplate_ratio=extracted.boilerplate_ratio,
            structure_hit_rate=extracted.structure_hit_rate,
            link_text_chars=extracted.link_text_chars,
            has_title=bool(extracted.metadata.get("title")),
            has_date=bool(extracted.metadata.get("date")),
            has_author=bool(extracted.metadata.get("author")),
            table_status=extracted.coverage.get("table", {}).get(
                "status", "none"
            ),
            image_not_read=bool(extracted.coverage.get("image_not_read")),
            text_gap_explained=(
                bool(extracted.coverage.get("image_not_read"))
                and not extracted.blocks
            ),
            encoding_replaced=extracted.encoding_replaced,
            language_mixed=is_language_mixed(text),
            title=extracted.metadata.get("title"),
            login_detected=extracted.login_detected,
            challenge_detected=extracted.challenge_detected,
        )
        note = extracted.metadata.get("parse_note")
        if note == "unsupported_media_type":
            signals.flags.append("unsupported_media_type")
        elif note == "pdf_encrypted":
            signals.flags.append("pdf_encrypted")
        elif isinstance(note, str) and note.startswith("pdf_unreadable"):
            signals.flags.append("parser_exception")
        return assess_quality(signals, self.thresholds)


# --------------------------------------------------------------------------
# plain-text path (spec 14 §2: 文本 line/offset)
# --------------------------------------------------------------------------


def _parse_plain(raw: bytes, charset: str | None) -> ExtractedDocument:
    decoded = decode_bytes(raw, charset)
    doc = ExtractedDocument(encoding_replaced=decoded.replaced)
    normalized_lines = [
        normalize_text(line) for line in decoded.text.split("\n")
    ]
    blocks: list[dict] = []
    offset = 0
    for line_number, line in enumerate(normalized_lines, start=1):
        text = line.strip()
        if not text:
            offset += len(line) + 1
            continue
        block_id = (
            f"b{len(blocks):03d}-"
            f"{hashlib.sha256(text.encode()).hexdigest()[:10]}"
        )
        blocks.append(
            {
                "block_id": block_id,
                "kind": "paragraph",
                "text": " ".join(text.split()),
                "page": 1,
                "section_path": [],
                "bbox": None,
                "source_locator": f"line[{line_number}]:{offset}",
            }
        )
        offset += len(line) + 1
    doc.blocks = blocks
    doc.text_chars = sum(len(block["text"]) for block in blocks)
    doc.full_text = "\n".join(block["text"] for block in blocks)
    doc.coverage = {
        "table": {"found": 0, "extracted": 0, "status": "none"},
        "image_not_read": False,
        "sections": [],
    }
    return doc


def _unsupported(raw: bytes) -> ExtractedDocument:
    doc = ExtractedDocument()
    doc.metadata["parse_note"] = "unsupported_media_type"
    doc.coverage = {
        "table": {"found": 0, "extracted": 0, "status": "none"},
        "image_not_read": False,
        "sections": [],
    }
    return doc


def _charset_of(media_type: str | None) -> str | None:
    if not media_type or ";" not in media_type:
        return None
    for part in media_type.split(";", 1)[1].split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip()
    return None


def _text_hash(blocks: list[dict]) -> str:
    joined = "".join(normalize_text(block["text"]) for block in blocks)
    return hashlib.sha256(joined.encode()).hexdigest()
