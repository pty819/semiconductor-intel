"""PDF extraction with pypdf (Task 8; spec 04 §4, 14 §2).

- blocks carry 1-based ``page`` numbers; ``bbox`` is best-effort from the
  text matrix runs and stays ``None`` when positions are unavailable;
- tables are best-effort whitespace-column detection (no external OCR /
  table service): consecutive lines with 2+ multi-space gaps become one
  ``kind=table`` block with the first row as header; column counts land
  in table metadata (PAR-02);
- an image-only page (no extractable text but image XObjects) sets
  ``coverage.image_not_read`` — 不支持图中数字则标记 image_not_read;
- encrypted/broken PDFs degrade to signals, never exceptions.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any

from intel.parsing.dto import ExtractedDocument
from intel.parsing.textnorm import clean_block_text

_TABLE_GAP = re.compile(r"\s{2,}")
_MIN_TABLE_ROWS = 2


class _RunCollector:
    """visitor_text sink: keeps positioned non-empty text runs per page."""

    def __init__(self) -> None:
        self.items: list[tuple[float, float, str]] = []

    def visit(
        self,
        text: str,
        cm: Any,
        tm: Any,
        font_dict: Any,
        font_size: Any,
    ) -> None:
        if text.strip():
            self.items.append((float(tm[4]), float(tm[5]), text))


def parse_pdf(raw: bytes) -> ExtractedDocument:
    from pypdf import PdfReader

    doc = ExtractedDocument()
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            # Empty user password covers most "view-only" PDFs.
            try:
                result = reader.decrypt("")
            except Exception:  # noqa: BLE001 - pypdf raises assorted types
                result = 0
            if not result:
                doc.metadata["parse_note"] = "pdf_encrypted"
                doc.coverage = _empty_coverage()
                doc.metadata["access_flags_suggestion"] = ["pdf_encrypted"]
                return doc
        for number, page in enumerate(reader.pages, start=1):
            runs = _RunCollector()
            try:
                text = page.extract_text(visitor_text=runs.visit) or ""
            except Exception:  # noqa: BLE001 - one bad page must not kill all
                text = ""
            lines = [line for line in text.split("\n") if line.strip()]
            _emit_page_blocks(doc, number, lines, runs.items, page)
    except Exception as exc:  # noqa: BLE001 - PdfReadError and friends
        doc.metadata["parse_note"] = f"pdf_unreadable: {type(exc).__name__}"
        doc.metadata["access_flags_suggestion"] = ["pdf_unreadable"]
        doc.coverage = _empty_coverage()
        return doc

    doc.coverage = _coverage_from(doc)
    doc.text_chars = sum(len(block["text"]) for block in doc.blocks)
    doc.full_text = "\n".join(block["text"] for block in doc.blocks)
    return doc


def _emit_page_blocks(
    doc: ExtractedDocument,
    page_number: int,
    lines: list[str],
    runs: list[tuple[float, float, str]],
    page: Any,
) -> None:
    table_lines: list[str] = []

    def flush_paragraph(paragraph: list[str]) -> None:
        if not paragraph:
            return
        text = clean_block_text(" ".join(paragraph), kind="paragraph")
        if text:
            _append_block(
                doc, "paragraph", text, page_number,
                _bbox_for(runs, text),
                source_locator=f"page[{page_number}]",
            )

    paragraph: list[str] = []
    for line in lines:
        if _is_table_line(line):
            flush_paragraph(paragraph)
            paragraph = []
            table_lines.append(line)
            continue
        if table_lines:
            _flush_table(doc, page_number, table_lines)
            table_lines = []
        paragraph.append(line)
    flush_paragraph(paragraph)
    if table_lines:
        _flush_table(doc, page_number, table_lines)

    if not lines and _page_has_images(page):
        # Image-only page: 不支持图中数字则标记 image_not_read (04 §4).
        doc.coverage["image_pages"] = doc.coverage.get("image_pages", []) + [
            page_number
        ]


def _is_table_line(line: str) -> bool:
    """Whitespace-column heuristic: ≥2 gaps of 2+ spaces look tabular."""
    return len(_TABLE_GAP.split(line.strip())) >= 3


def _flush_table(
    doc: ExtractedDocument, page_number: int, table_lines: list[str]
) -> None:
    if len(table_lines) < _MIN_TABLE_ROWS:
        # A single spaced line is not a table; keep it as a paragraph.
        text = clean_block_text(" ".join(table_lines), kind="paragraph")
        if text:
            _append_block(
                doc, "paragraph", text, page_number, None,
                source_locator=f"page[{page_number}]",
            )
        return
    rows = [
        [cell.strip() for cell in _TABLE_GAP.split(line.strip())]
        for line in table_lines
    ]
    header, body = rows[0], rows[1:]
    lines = [
        " | ".join(row) for row in ([header] + body) if any(row)
    ]
    text = clean_block_text("\n".join(lines), kind="table")
    block = _append_block(
        doc, "table", text, page_number, None,
        source_locator=f"page[{page_number}]#table{len(doc.table_meta) + 1}",
    )
    doc.table_meta.append(
        {
            "block_id": block["block_id"],
            "ncols": len(header),
            "nrows": len(body),
            "has_header": True,
        }
    )


def _append_block(
    doc: ExtractedDocument,
    kind: str,
    text: str,
    page: int,
    bbox: list[float] | None,
    *,
    source_locator: str | None,
) -> dict:
    block_id = (
        f"b{len(doc.blocks):03d}-"
        f"{hashlib.sha256(text.encode()).hexdigest()[:10]}"
    )
    block = {
        "block_id": block_id,
        "kind": kind,
        "text": text,
        "page": page,
        "section_path": [],
        "bbox": bbox,
        "source_locator": source_locator,
    }
    doc.blocks.append(block)
    return block


def _bbox_for(
    runs: list[tuple[float, float, str]], paragraph_text: str
) -> list[float] | None:
    """Coarse best-effort bbox: union of text-matrix runs whose text
    occurs in the paragraph. None when no positioned runs match."""
    if not runs:
        return None
    matched: list[tuple[float, float, str]] = []
    probe = paragraph_text[:24]
    if not probe:
        return None
    for run in runs:
        piece = run[2].strip()
        if piece and piece[:8] in probe:
            matched.append(run)
    if not matched:
        return None
    xs = [run[0] for run in matched]
    # Approximate run height by font ascent ~ 12pt when unknown.
    ys = [run[1] for run in matched]
    return [min(xs), min(ys), max(xs) + 40.0, max(ys) + 12.0]


def _page_has_images(page: Any) -> bool:
    try:
        resources = page.get("/Resources")
        if resources is None:
            return False
        resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return False
        xobjects = xobjects.get_object()
        for candidate in xobjects.values():
            candidate = candidate.get_object()
            if candidate.get("/Subtype") == "/Image":
                return True
    except Exception:  # noqa: BLE001 - malformed resources
        return False
    return False


def _coverage_from(doc: ExtractedDocument) -> dict:
    tables = [b for b in doc.blocks if b["kind"] == "table"]
    image_pages = doc.coverage.get("image_pages", [])
    return {
        "table": {
            "found": len(tables),
            "extracted": len(tables),
            "status": "ok" if tables else "none",
        },
        "image_not_read": bool(image_pages),
        "image_pages": image_pages,
        "sections": [],
    }


def _empty_coverage() -> dict:
    return {
        "table": {"found": 0, "extracted": 0, "status": "none"},
        "image_not_read": False,
        "image_pages": [],
        "sections": [],
    }
