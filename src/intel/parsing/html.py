"""HTML extraction with BeautifulSoup/lxml (Task 8; spec 04 §4, 14 §2).

Preserves document structure as ordered blocks:

- heading hierarchy builds ``section_path`` chains for following blocks;
- ``<table>`` becomes one ``kind=table`` block whose text keeps the header
  row group and row groups as `` | ``-separated lines — never flattened
  (PAR-02); column counts land in the artifact's table metadata;
- boilerplate (nav/header/footer/aside/script/style/...) is removed with
  conservative tag rules and measured as a ratio for quality;
- login/challenge pages are detected and produce NO body blocks (ING-06);
- ``source_locator`` is a CSS-ish selector path from ``body``.
"""

from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup, Tag

from intel.parsing.dto import ExtractedDocument
from intel.parsing.textnorm import clean_block_text, decode_bytes

#: Removed before extraction (conservative: no content-bearing tags).
BOILERPLATE_TAGS = (
    "nav", "header", "footer", "aside", "script", "style", "noscript",
    "template", "iframe", "svg",
)

_HEADING_TAGS = {f"h{level}" for level in range(1, 7)}
_MAX_LINKS = 200
_CELL_SEP = " | "

_LOGIN_TEXT_MARKERS = (
    "sign in", "signin", "log in", "login", "password", "登录", "登陆",
)
_CHALLENGE_TEXT_MARKERS = (
    "just a moment", "checking your browser", "verify you are human",
    "captcha", "cf-challenge", "请完成安全验证",
)


def parse_html(raw: bytes, charset: str | None) -> ExtractedDocument:
    decoded = decode_bytes(raw, charset)
    soup = BeautifulSoup(decoded.text, "lxml")

    boilerplate_chars, total_chars = _strip_boilerplate(soup)
    doc = ExtractedDocument(encoding_replaced=decoded.replaced)
    doc.boilerplate_ratio = (
        boilerplate_chars / total_chars if total_chars else 0.0
    )

    if _detect_login_or_challenge(soup, doc):
        # ING-06: 登录页/挑战页不当正文 — no body blocks, metadata only.
        doc.metadata.update(_head_metadata(soup))
        doc.coverage = _empty_coverage()
        return doc

    _extract_content(soup, doc)
    doc.metadata.update(_head_metadata(soup))
    doc.metadata["links"] = _collect_links(soup)
    doc.metadata["tables"] = doc.table_meta
    doc.coverage = _coverage_from(doc)
    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    kept_chars = sum(len(block["text"]) for block in doc.blocks)
    stray = len(body_text) - kept_chars
    doc.structure_hit_rate = (
        kept_chars / (kept_chars + stray) if kept_chars + stray > 0 else 1.0
    )
    doc.text_chars = kept_chars
    doc.link_text_chars = sum(
        len(anchor.get_text(" ", strip=True))
        for anchor in soup.select("a[href]")
    )
    doc.full_text = body_text
    return doc


def _strip_boilerplate(soup: BeautifulSoup) -> tuple[int, int]:
    """Remove boilerplate tags; return (removed_chars, chars_before)."""
    total = len(soup.get_text(" ", strip=True)) if soup else 0
    removed = 0
    for tag in soup.find_all(list(BOILERPLATE_TAGS)):
        removed += len(tag.get_text(" ", strip=True))
        tag.decompose()
    return removed, max(total, 1)


def _detect_login_or_challenge(
    soup: BeautifulSoup, doc: ExtractedDocument
) -> bool:
    if soup.find("input", attrs={"type": "password"}) is not None:
        doc.login_detected = True
        doc.metadata["access_flags_suggestion"] = ["login_required"]
        return True
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    body_text = soup.body.get_text(" ", strip=True).lower() if soup.body else ""
    haystack = f"{title} {body_text[:1000]}"
    if any(marker in haystack for marker in _CHALLENGE_TEXT_MARKERS):
        doc.challenge_detected = True
        doc.metadata["access_flags_suggestion"] = ["challenge_page"]
        return True
    hits = sum(1 for marker in _LOGIN_TEXT_MARKERS if marker in haystack)
    if hits >= 2 and len(body_text) < 2000:
        doc.login_detected = True
        doc.metadata["access_flags_suggestion"] = ["login_required"]
        return True
    return False


def _extract_content(soup: BeautifulSoup, doc: ExtractedDocument) -> None:
    root = soup.body or soup
    heading_stack: list[tuple[int, str]] = []
    for element in root.descendants:
        if not isinstance(element, Tag):
            continue
        name = element.name
        if name in _HEADING_TAGS:
            if _inside_table(element):
                continue
            text = clean_block_text(
                element.get_text(" ", strip=True), kind="heading"
            )
            if not text:
                continue
            level = int(name[1])
            _append_block(
                doc, "heading", text,
                [title for _, title in heading_stack], element,
            )
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
        elif name == "p" and not _inside_table(element):
            text = clean_block_text(
                element.get_text(" ", strip=True), kind="paragraph"
            )
            if text:
                _append_block(
                    doc, "paragraph", text,
                    [title for _, title in heading_stack], element,
                )
        elif name == "figcaption" and not _inside_table(element):
            text = clean_block_text(
                element.get_text(" ", strip=True), kind="caption"
            )
            if text:
                _append_block(
                    doc, "caption", text,
                    [title for _, title in heading_stack], element,
                )
        elif name == "table" and not _inside_table(element):
            _extract_table(element, doc, [t for _, t in heading_stack])


def _inside_table(element: Tag) -> bool:
    return element.find_parent("table") is not None


def _extract_table(
    table: Tag, doc: ExtractedDocument, section_path: list[str]
) -> None:
    """One table = one block: header row group + row groups, cells joined
    with ``|`` separators per line (PAR-02: 行列条件不丢)."""
    lines: list[str] = []
    header_lines = 0
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue  # belongs to a nested table, extracted separately
        cells = [
            clean_block_text(cell.get_text(" ", strip=True), kind="paragraph")
            for cell in row.find_all(["th", "td"])
        ]
        if not any(cells):
            continue
        is_header = (
            row.find_parent("thead") is not None
            or row.find("th") is not None
        )
        lines.append(_CELL_SEP.join(cells))
        if is_header:
            header_lines += 1
    if not lines:
        return
    ncols = len(lines[0].split(_CELL_SEP.strip()))
    text = clean_block_text("\n".join(lines), kind="table")
    block = _append_block(doc, "table", text, section_path, table)
    doc.table_meta.append(
        {
            "block_id": block["block_id"],
            "ncols": ncols,
            "nrows": len(lines) - header_lines,
            "has_header": header_lines > 0,
        }
    )


def _append_block(
    doc: ExtractedDocument,
    kind: str,
    text: str,
    section_path: list[str],
    element: Tag | None,
    page: int | None = None,
    bbox: list[float] | None = None,
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
        "section_path": list(section_path),
        "bbox": bbox,
        "source_locator": _selector_path(element) if element else None,
    }
    doc.blocks.append(block)
    return block


def _selector_path(element: Tag) -> str:
    """CSS-ish selector path rooted at ``body`` (14 §2 HTML
    source_locator)."""
    parts: list[str] = []
    node: Tag | None = element
    while node is not None and node.name != "body":
        selector = node.name or ""
        if node.get("id"):
            selector += f"#{node['id']}"
        elif node.get("class"):
            selector += f".{node['class'][0]}"
        index = _sibling_index(node)
        if index > 1:
            selector += f":nth-of-type({index})"
        parts.append(selector)
        node = node.parent if isinstance(node.parent, Tag) else None
    if not parts:
        return "body"
    return "body > " + " > ".join(reversed(parts))


def _sibling_index(node: Tag) -> int:
    position = 1
    sibling = node.previous_sibling
    while sibling is not None:
        if isinstance(sibling, Tag) and sibling.name == node.name:
            position += 1
        sibling = sibling.previous_sibling
    return position


def _head_metadata(soup: BeautifulSoup) -> dict:
    metadata: dict = {"title": None, "author": None, "date": None}
    if soup.title and soup.title.get_text(strip=True):
        metadata["title"] = soup.title.get_text(" ", strip=True)
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if metadata["title"] is None and og_title and og_title.get("content"):
        metadata["title"] = og_title["content"]
    author = soup.find("meta", attrs={"name": "author"})
    if author and author.get("content"):
        metadata["author"] = author["content"]
    date = soup.find(
        "meta", attrs={"property": "article:published_time"}
    ) or soup.find("meta", attrs={"name": "date"})
    if date and date.get("content"):
        metadata["date"] = date["content"]
    else:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag is not None:
            metadata["date"] = time_tag["datetime"]
    return metadata


def _collect_links(soup: BeautifulSoup) -> list[dict]:
    links: list[dict] = []
    for anchor in soup.select("a[href]")[:_MAX_LINKS]:
        text = anchor.get_text(" ", strip=True)
        if text:
            links.append({"text": text, "href": anchor["href"]})
    return links


def _coverage_from(doc: ExtractedDocument) -> dict:
    tables = [b for b in doc.blocks if b["kind"] == "table"]
    sections = {
        " > ".join(block["section_path"])
        for block in doc.blocks
        if block["section_path"]
    }
    return {
        "table": {
            "found": len(tables),
            "extracted": len(tables),
            "status": "ok" if tables else "none",
        },
        "image_not_read": False,
        "sections": sorted(sections),
    }


def _empty_coverage() -> dict:
    return {
        "table": {"found": 0, "extracted": 0, "status": "none"},
        "image_not_read": False,
        "sections": [],
    }
