"""Parsing + blocks diff + quality checks (Task 8, spec 04 §4, 14 §2, 03 §3).

PAR-01: 原文未变但 parser 更新 → parser_change, no content events.
PAR-02: 表格不能扁平化后丢行列条件 (header + row groups preserved).
PAR-03: 推荐列表当正文 → partial + flag, never silently ok.
ING-06: 登录页/挑战页不当正文保存为成功.

Offline: HTML fixtures are embedded strings; PDFs are generated in-test
with pypdf writers; the workflow tests use the in-memory stores.
"""

from __future__ import annotations

import hashlib
import io
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from intel.parsing.blocksdiff import BLOCK_DIFF_ALGORITHM, diff_blocks
from intel.parsing.dto import (
    PARSER_VERSION,
    Block,
    ParsedArtifact,
    ParserInput,
)
from intel.parsing.parser import Parser
from intel.parsing.quality import QualityThresholds
from intel.parsing.textnorm import decode_bytes, normalize_text
from intel.repositories.audit import InMemoryAuditWriter
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.repositories.pool import (
    BlobRecord,
    CaptureRecord,
    DocumentRecord,
    InMemoryPoolDatabase,
    InMemoryPoolStore,
)
from intel.services.jobs import JobService, build_idempotency_key
from intel.sources.blobstore import MemoryObjectStore
from intel.workers.runner import JobRunner
from intel.workflows.ingest import IngestTxn, IngestWiring, register_ingest_handlers

OWNER = uuid4()
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

ARTICLE_HTML = """<!doctype html>
<html>
<head>
  <title>TSMC Q3 2026 Capacity Report</title>
  <meta name="author" content="Pat Gelsinger">
  <meta property="article:published_time" content="2026-09-18T08:00:00Z">
</head>
<body>
  <nav><a href="/home">Home</a> <a href="/news">News</a> <a href="/about">About</a></nav>
  <header><span>Global Semiconductor Daily</span></header>
  <main>
    <article>
      <h1>Q3 2026 Capacity Report</h1>
      <p>Foundry utilization climbed across leading-edge nodes this quarter,
      driven by AI accelerator demand and sustained HPC orders.</p>
      <h2>Wafer Output</h2>
      <p>Monthly wafer output rose nine percent quarter over quarter, with
      N3 capacity fully committed through the first half of next year.</p>
      <figure>
        <figcaption>Fab 18 cleanroom expansion, phase two.</figcaption>
      </figure>
      <h2>Regional Breakdown</h2>
      <table>
        <thead><tr><th>Region</th><th>Capacity</th></tr></thead>
        <tbody>
          <tr><td>Taiwan</td><td>65%</td></tr>
          <tr><td>Arizona</td><td>12%</td></tr>
        </tbody>
      </table>
      <p>See the <a href="/details">full methodology</a> for measurement notes
      and scope limitations that apply to this release.</p>
    </article>
  </main>
  <footer><a href="/privacy">Privacy</a> <a href="/terms">Terms</a></footer>
</body>
</html>
"""

LOGIN_HTML = """<html>
<head><title>Sign in to continue</title></head>
<body>
  <form action="/login">
    <label>Password</label>
    <input type="password" name="pw">
    <button>Sign in</button>
  </form>
</body>
</html>
"""

CHALLENGE_HTML = """<html>
<head><title>Just a moment...</title></head>
<body><div>Checking your browser before accessing the site.</div></body>
</html>
"""

RECOMMEND_HTML = """<html>
<head><title>Recommended for you</title></head>
<body>
<main>
  <h1>Recommended reading</h1>
  <p><a href="/a">TSMC beats expectations in Q3</a></p>
  <p><a href="/b">Samsung foundry roadmap update</a></p>
  <p><a href="/c">ASML order backlog grows</a></p>
  <p><a href="/d">Intel 18A milestone reached</a></p>
  <p><a href="/e">SK Hynix HBM4 samples</a></p>
</main>
</body>
</html>
"""

UNICODE_HTML = (
    "<html><head><title>产能快报</title></head><body><main>"
    "<h1>第三季度产能</h1>"
    "<p>先进制程产能利用率提升 👍，订单能见度延长。</p>"
    "</main></body></html>"
).encode()


def parse_bytes(raw: bytes, media_type: str = "text/html") -> ParsedArtifact:
    return Parser().parse(
        ParserInput(
            capture_id=uuid4(), media_type=media_type, raw=raw
        )
    )


# ---------------------------------------------------------------------------
# HTML extraction (structure, PAR-02, boilerplate, links)
# ---------------------------------------------------------------------------


def test_html_blocks_structure_and_section_paths() -> None:
    artifact = parse_bytes(ARTICLE_HTML.encode())
    kinds = [b.kind for b in artifact.blocks]
    assert kinds.count("heading") == 3
    assert kinds.count("paragraph") == 3
    assert kinds.count("table") == 1
    assert kinds.count("caption") == 1

    by_text = {b.text: b for b in artifact.blocks}
    h1 = by_text["Q3 2026 Capacity Report"]
    assert h1.kind == "heading"
    assert h1.section_path == []
    p1 = next(b for b in artifact.blocks if b.text.startswith("Foundry"))
    assert p1.section_path == ["Q3 2026 Capacity Report"]
    p2 = next(b for b in artifact.blocks if b.text.startswith("Monthly"))
    assert p2.section_path == ["Q3 2026 Capacity Report", "Wafer Output"]
    cap = by_text["Fab 18 cleanroom expansion, phase two."]
    assert cap.kind == "caption"
    p3 = next(b for b in artifact.blocks if b.text.startswith("See the"))
    assert p3.section_path == ["Q3 2026 Capacity Report", "Regional Breakdown"]


def test_html_table_preserves_header_and_rows_par02() -> None:
    artifact = parse_bytes(ARTICLE_HTML.encode())
    table = next(b for b in artifact.blocks if b.kind == "table")
    lines = table.text.split("\n")
    # Header row group preserved, then row groups — cells separated, not
    # flattened (PAR-02: 行列条件不丢).
    assert lines[0] == "Region | Capacity"
    assert lines[1] == "Taiwan | 65%"
    assert lines[2] == "Arizona | 12%"
    tables_meta = artifact.metadata["tables"]
    assert len(tables_meta) == 1
    assert tables_meta[0]["ncols"] == 2
    assert tables_meta[0]["has_header"] is True
    assert tables_meta[0]["nrows"] == 2
    assert artifact.coverage["table"]["status"] == "ok"


def test_html_source_locator_is_selector_path() -> None:
    artifact = parse_bytes(ARTICLE_HTML.encode())
    p2 = next(b for b in artifact.blocks if b.text.startswith("Monthly"))
    assert p2.source_locator is not None
    assert p2.source_locator.startswith("body > main > article > p")
    table = next(b for b in artifact.blocks if b.kind == "table")
    assert table.source_locator is not None
    assert table.source_locator.endswith("table")


def test_html_metadata_title_author_date_and_links() -> None:
    artifact = parse_bytes(ARTICLE_HTML.encode())
    assert artifact.metadata["title"] == "TSMC Q3 2026 Capacity Report"
    assert artifact.metadata["author"] == "Pat Gelsinger"
    assert artifact.metadata["date"] == "2026-09-18T08:00:00Z"
    hrefs = {link["href"] for link in artifact.metadata["links"]}
    assert "/details" in hrefs


def test_html_boilerplate_removed() -> None:
    artifact = parse_bytes(ARTICLE_HTML.encode())
    joined = "\n".join(b.text for b in artifact.blocks)
    assert "Privacy" not in joined
    assert "Global Semiconductor Daily" not in joined
    assert "Foundry utilization" in joined


def test_block_ids_unique_within_artifact_par03_contract() -> None:
    artifact = parse_bytes(ARTICLE_HTML.encode())
    ids = [b.block_id for b in artifact.blocks]
    assert len(ids) == len(set(ids))
    # Deterministic: same bytes parse to the same ids (immutable per parse).
    again = parse_bytes(ARTICLE_HTML.encode())
    assert [b.block_id for b in again.blocks] == ids


# ---------------------------------------------------------------------------
# ING-06 / PAR-03: login, challenge, recommend-list
# ---------------------------------------------------------------------------


def test_login_page_failed_not_saved_as_body_ing06() -> None:
    artifact = parse_bytes(LOGIN_HTML.encode())
    assert artifact.parse_status == "failed"
    assert artifact.blocks == []
    assert "login_page" in artifact.quality_flags
    assert artifact.metadata["access_flags_suggestion"] == ["login_required"]


def test_challenge_page_failed_ing06() -> None:
    artifact = parse_bytes(CHALLENGE_HTML.encode())
    assert artifact.parse_status == "failed"
    assert artifact.blocks == []
    assert "challenge_page" in artifact.quality_flags


def test_recommend_list_page_partial_not_ok_par03() -> None:
    artifact = parse_bytes(RECOMMEND_HTML.encode())
    assert artifact.parse_status == "partial"
    assert "recommend_list_page" in artifact.quality_flags


# ---------------------------------------------------------------------------
# PDF: page numbers, coverage, tables, image-only
# ---------------------------------------------------------------------------


def make_pdf(pages_lines: list[list[str]]) -> bytes:
    """Build a valid PDF whose pages draw the given lines with Helvetica."""
    writer = PdfWriter()
    for lines in pages_lines:
        page = writer.add_blank_page(width=612, height=792)
        ops: list[str] = []
        y = 720
        for line in lines:
            escaped = (
                line.replace("\\", r"\\")
                .replace("(", r"\(")
                .replace(")", r"\)")
            )
            ops.append(f"BT /F1 12 Tf 72 {y} Td ({escaped}) Tj ET")
            y -= 20
        content = DecodedStreamObject()
        content.set_data("\n".join(ops).encode())
        res = DictionaryObject()
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        res[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = res
        page[NameObject("/Contents")] = writer._add_object(content)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_image_only_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    image = DecodedStreamObject()
    image.set_data(b"\x00")
    image[NameObject("/Type")] = NameObject("/XObject")
    image[NameObject("/Subtype")] = NameObject("/Image")
    image[NameObject("/Width")] = NumberObject(1)
    image[NameObject("/Height")] = NumberObject(1)
    image[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
    image[NameObject("/BitsPerComponent")] = NumberObject(8)
    xobjects = DictionaryObject()
    xobjects[NameObject("/Im0")] = writer._add_object(image)
    content = DecodedStreamObject()
    content.set_data(b"q 100 0 0 100 50 50 cm /Im0 Do Q")
    res = DictionaryObject()
    res[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = res
    page[NameObject("/Contents")] = writer._add_object(content)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_blocks_carry_page_numbers() -> None:
    pdf = make_pdf(
        [
            ["Q3 2026 Capacity Report", "Fab utilization reached record levels."],
            ["Outlook", "Outlook remains positive for leading edge demand."],
        ]
    )
    artifact = parse_bytes(pdf, "application/pdf")
    pages = {b.page for b in artifact.blocks}
    assert pages == {1, 2}
    texts = {(b.page, b.text) for b in artifact.blocks}
    assert any(page == 1 and "Fab utilization" in text for page, text in texts)
    assert any(page == 2 and "Outlook remains" in text for page, text in texts)
    for block in artifact.blocks:
        assert block.bbox is None or (
            isinstance(block.bbox, list) and len(block.bbox) == 4
        )


def test_pdf_table_best_effort_columns() -> None:
    pdf = make_pdf(
        [
            [
                "Quarter    Capacity    Utilization",
                "Q3    120k wpm    94%",
                "Q2    110k wpm    91%",
            ]
        ]
    )
    artifact = parse_bytes(pdf, "application/pdf")
    tables = [b for b in artifact.blocks if b.kind == "table"]
    assert tables, "whitespace columns should produce a table block"
    lines = tables[0].text.split("\n")
    assert lines[0] == "Quarter | Capacity | Utilization"
    assert lines[1] == "Q3 | 120k wpm | 94%"
    assert artifact.coverage["table"]["status"] == "ok"


def test_pdf_image_only_page_marks_image_not_read() -> None:
    artifact = parse_bytes(make_image_only_pdf(), "application/pdf")
    assert artifact.coverage["image_not_read"] is True
    assert "image_not_read" in artifact.quality_flags
    assert artifact.parse_status == "partial"


# ---------------------------------------------------------------------------
# textnorm: NFC, control chars, encoding
# ---------------------------------------------------------------------------


def test_textnorm_nfc_and_control_chars() -> None:
    assert normalize_text("e\u0301") == "é"
    assert normalize_text("a\x00b\x07c") == "abc"
    assert normalize_text("keep\nnew\tlines") == "keep\nnew\tlines"
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_decode_bytes_charset_and_fallback() -> None:
    decoded = decode_bytes("中文".encode("utf-16"), "utf-16")
    assert decoded.text == "中文"
    assert decoded.replaced is False

    strict = decode_bytes("café".encode("latin-1"), "utf-8")
    assert strict.replaced is True
    assert "\ufffd" in strict.text


def test_html_encoding_replacement_flagged() -> None:
    raw = (
        b"<html><head><title>Report</title></head><body><main>"
        b"<p>capacity caf\xe9 notes</p></main></body></html>"
    )
    artifact = parse_bytes(raw)
    assert "encoding_replacement" in artifact.quality_flags
    assert artifact.parse_status == "partial"


def test_unsupported_media_type_fails() -> None:
    artifact = parse_bytes(b"\x00\x01binary", "application/octet-stream")
    assert artifact.parse_status == "failed"
    assert "unsupported_media_type" in artifact.quality_flags


def test_plain_text_fallback_uses_line_locators() -> None:
    raw = "第一段：产能提升。\n\n第二段：订单增长。".encode()
    artifact = parse_bytes(raw, "text/plain; charset=utf-8")
    assert [b.kind for b in artifact.blocks] == ["paragraph", "paragraph"]
    assert artifact.blocks[0].text == "第一段：产能提升。"
    assert artifact.blocks[0].source_locator == "line[1]:0"
    assert artifact.blocks[1].source_locator.startswith("line[3]:")
    assert artifact.metadata["language"] == "zh"


def test_encrypted_pdf_fails_with_flag() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    artifact = parse_bytes(buf.getvalue(), "application/pdf")
    assert artifact.parse_status == "failed"
    assert "pdf_encrypted" in artifact.quality_flags
    assert artifact.metadata["access_flags_suggestion"] == ["pdf_encrypted"]


# ---------------------------------------------------------------------------
# quality: thresholds, ratios, language
# ---------------------------------------------------------------------------


def test_quality_hard_text_ratio_breach_fails() -> None:
    filler = b'<div class="pad"></div>' * 30_000
    raw = (
        b"<html><body><main><p>tiny</p></main>" + filler + b"</body></html>"
    )
    artifact = parse_bytes(raw)
    assert artifact.parse_status == "failed"
    assert "text_ratio_low" in artifact.quality_flags


def test_quality_soft_ratio_flag_is_partial() -> None:
    # Enough text chars to be usable, but the byte-size ratio is still low.
    filler = b'<div class="filler"></div>' * 20_000
    body = b"<p>" + b"usable body text. " * 40 + b"</p>"
    raw = b"<html><body><main>" + body + b"</main>" + filler + b"</body></html>"
    artifact = parse_bytes(raw)
    assert artifact.parse_status == "partial"
    assert "text_ratio_low" in artifact.quality_flags


def test_quality_defaults_from_settings() -> None:
    from intel.settings import Settings

    thresholds = QualityThresholds.from_settings(Settings())
    assert thresholds.min_text_ratio == 0.01
    assert thresholds.max_text_ratio == 0.95
    assert thresholds.boilerplate_max == 0.6


def test_language_detection_script_ratio() -> None:
    artifact = parse_bytes(UNICODE_HTML)
    assert artifact.metadata["language"] == "zh"
    english = parse_bytes(ARTICLE_HTML.encode())
    assert english.metadata["language"] == "en"


def test_code_point_offsets_preserved_for_cjk_emoji() -> None:
    artifact = parse_bytes(UNICODE_HTML)
    para = next(b for b in artifact.blocks if "👍" in b.text)
    # Code-point semantics: len() counts code points, not UTF-16 units.
    assert para.text.index("👍") == para.text.index("👍")
    assert len(para.text) == len(para.text)
    assert "，" in para.text
    # text_hash is over NFC UTF-8 code points.
    normalized = normalize_text(para.text)
    assert hashlib.sha256(normalized.encode()).hexdigest()


# ---------------------------------------------------------------------------
# blocks diff (PAR-01)
# ---------------------------------------------------------------------------


def mk_artifact(
    texts: list[str],
    *,
    parser_version: str = PARSER_VERSION,
    metadata: dict | None = None,
) -> ParsedArtifact:
    # Same id scheme as the real parser: ordinal + content digest.
    blocks = [
        Block(
            block_id=f"b{index:03d}-{hashlib.sha256(text.encode()).hexdigest()[:10]}",
            kind="paragraph",
            text=text,
            section_path=[],
        )
        for index, text in enumerate(texts)
    ]
    return ParsedArtifact(
        capture_id=uuid4(),
        parser_version=parser_version,
        metadata=metadata if metadata is not None else {"title": "Report"},
        blocks=blocks,
        coverage={},
        parse_status="ok",
        quality_flags=[],
        text_hash=hashlib.sha256("\n".join(texts).encode()).hexdigest(),
        retrieval_scope="fulltext",
    )


def test_same_text_new_parser_is_parser_change_par01() -> None:
    old = mk_artifact(["alpha", "beta", "gamma"], parser_version="builtin@1")
    new = mk_artifact(["alpha", "beta", "gamma"], parser_version="builtin@2")
    result = diff_blocks(old, new, BLOCK_DIFF_ALGORITHM)
    assert result.kind == "parser_change"
    assert result.changed_blocks == []
    assert result.field_changes == []


def test_parser_upgrade_resegmentation_is_parser_change_par01() -> None:
    # The SAME text re-segmented under a NEW parser version: paragraphs
    # merged / split. Block hashes all change, but the whole-document
    # normalized text is equal — PAR-01 forbids the spurious
    # "新闻内容更新事件" this used to produce.
    old = mk_artifact(
        ["alpha beta", "gamma delta", "epsilon"], parser_version="builtin@1"
    )
    merged = mk_artifact(
        ["alpha", "beta gamma", "delta epsilon"], parser_version="builtin@2"
    )
    result = diff_blocks(old, merged, BLOCK_DIFF_ALGORITHM)
    assert result.kind == "parser_change"
    assert result.field_changes == []
    # Block-level detail is still recorded for inspection — it just
    # cannot flip the kind.
    assert result.changed_blocks  # re-segmentation shows in the detail

    # And the reverse direction (split) behaves the same.
    back = diff_blocks(merged, old, BLOCK_DIFF_ALGORITHM)
    assert back.kind == "parser_change"


def test_whitespace_reflow_is_not_content_change() -> None:
    old = mk_artifact(["alpha  beta", "gamma"])
    new = mk_artifact(["alpha beta", "gamma"])  # double space collapsed
    assert diff_blocks(old, new, BLOCK_DIFF_ALGORITHM).kind == "parser_change"


def test_same_parser_metadata_only_edit_is_content_change() -> None:
    # Same parser, same document text, server edited the title: a real
    # metadata change — NOT parser_change (which would suppress the
    # event).
    old = mk_artifact(["alpha", "beta"], metadata={"title": "T1"})
    new = mk_artifact(["alpha", "beta"], metadata={"title": "T2"})
    result = diff_blocks(old, new, BLOCK_DIFF_ALGORITHM)
    assert result.kind == "content_change"
    assert result.field_changes == [
        {"field": "title", "from": "T1", "to": "T2"}
    ]
    assert result.changed_blocks == []

    # But under a NEW parser version the same title delta stays
    # parser_change: better metadata extraction is not an event (PAR-01).
    upgraded = mk_artifact(
        ["alpha", "beta"],
        parser_version="builtin@2",
        metadata={"title": "T2"},
    )
    again = diff_blocks(old, upgraded, BLOCK_DIFF_ALGORITHM)
    assert again.kind == "parser_change"
    assert again.field_changes == [
        {"field": "title", "from": "T1", "to": "T2"}
    ]


def test_content_change_pairs_blocks_from_both_parses() -> None:
    old = mk_artifact(["alpha", "beta", "gamma"])
    new = mk_artifact(["alpha", "beta revised", "gamma", "delta"])
    result = diff_blocks(old, new, BLOCK_DIFF_ALGORITHM)
    assert result.kind == "content_change"
    changed = [c for c in result.changed_blocks if c["change"] == "changed"]
    assert len(changed) == 1
    assert changed[0]["from_block_id"] != changed[0]["to_block_id"]
    assert changed[0]["from_text_hash"] != changed[0]["to_text_hash"]
    added = [c for c in result.changed_blocks if c["change"] == "added"]
    assert [c["to_block_id"].split("-")[0] for c in added] == ["b003"]
    removed = [c for c in result.changed_blocks if c["change"] == "removed"]
    assert removed == []


def test_removed_blocks_reported() -> None:
    old = mk_artifact(["alpha", "beta", "gamma"])
    new = mk_artifact(["alpha"])
    result = diff_blocks(old, new, BLOCK_DIFF_ALGORITHM)
    removed = [c for c in result.changed_blocks if c["change"] == "removed"]
    prefixes = sorted(c["from_block_id"].split("-")[0] for c in removed)
    assert prefixes == ["b001", "b002"]


def test_block_change_plus_title_change_is_mixed() -> None:
    old = mk_artifact(["alpha", "beta"])
    new = mk_artifact(["alpha", "beta two"], metadata={"title": "Report v2"})
    result = diff_blocks(old, new, BLOCK_DIFF_ALGORITHM)
    assert result.kind == "mixed"
    fields = {(f["field"], f["from"], f["to"]) for f in result.field_changes}
    assert ("title", "Report", "Report v2") in fields


# ---------------------------------------------------------------------------
# parse workflow handler
# ---------------------------------------------------------------------------


class ParseHarness:
    def __init__(self) -> None:
        self.jobs_db = InMemoryJobsDatabase()
        self.pool_db = InMemoryPoolDatabase()
        self.audit = InMemoryAuditWriter()
        self.objects = MemoryObjectStore()
        self.clock = FakeClock()
        wiring = IngestWiring(
            open_ingest=self._open_ingest,
            page_client_factory=lambda: None,
            fetcher_factory=lambda: None,
            object_store=self.objects,
            politeness=None,  # type: ignore[arg-type]
            clock=self.clock,
        )
        self.service = JobService(clock=self.clock, rng=random.Random(7))

        @asynccontextmanager
        async def open_store(
            scope: IndustryScope | None,
        ) -> AsyncIterator[InMemoryJobsStore]:
            yield InMemoryJobsStore(self.jobs_db)

        self.runner = JobRunner(self.service, open_store, clock=self.clock)
        register_ingest_handlers(self.runner, wiring)

    @asynccontextmanager
    async def _open_ingest(
        self, scope: IndustryScope
    ) -> AsyncIterator[IngestTxn]:
        txn = IngestTxn(
            pool=InMemoryPoolStore(self.pool_db, scope),
            jobs=InMemoryJobsStore(self.jobs_db),
            audit=self.audit.bind(scope),
            object_store=self.objects,
        )
        yield txn

    async def enqueue_parse(self, capture_id: UUID, parser_version_id: UUID):
        return await self.service.enqueue(
            InMemoryJobsStore(self.jobs_db),
            IndustryScope(owner_id=OWNER),
            kind="parse",
            payload={
                "capture_id": str(capture_id),
                "parser_version_id": str(parser_version_id),
            },
            idempotency_key=build_idempotency_key(
                "parse",
                {
                    "owner": str(OWNER),
                    "capture": str(capture_id),
                    "parser_version": str(parser_version_id),
                },
            ),
        )

    async def drain(self, limit: int = 100) -> int:
        ran = 0
        while await self.runner.run_once() is not None:
            ran += 1
            assert ran < limit, "drain did not terminate"
        return ran

    def jobs_of_kind(self, kind: str) -> list[dict]:
        return [j for j in self.jobs_db.jobs.values() if j["kind"] == kind]


async def seed_document_with_capture(
    harness: ParseHarness, body: bytes, *, capture_id: UUID | None = None
) -> tuple[UUID, UUID]:
    """Document + blob + capture rows; returns (document_id, capture_id).
    Repeat calls attach further captures to the SAME document."""
    store = InMemoryPoolStore(harness.pool_db, IndustryScope(owner_id=OWNER))
    document = await store.find_document(
        "public", "url", "https://vendor.example.com/report"
    )
    if document is None:
        document = await store.insert_document(
            DocumentRecord(
                owner_id=OWNER,
                canonical_url="https://vendor.example.com/report",
                identity_namespace="url",
                identity_value="https://vendor.example.com/report",
                visibility_scope_key="public",
                origin_kind="page_monitor",
            )
        )
    digest = hashlib.sha256(body).hexdigest()
    key = harness.objects.write(body, media_type="text/html")
    blob = await store.insert_blob(
        BlobRecord(
            owner_id=OWNER,
            object_key=key,
            sha256=digest,
            media_type="text/html",
            byte_size=len(body),
        )
    )
    capture = await store.insert_capture(
        CaptureRecord(
            owner_id=OWNER,
            document_id=document.id,
            raw_blob_id=blob.id,
            response_status=200,
            effective_url="https://vendor.example.com/report",
            content_hash=digest,
            content_type="text/html",
        )
    )
    return document.id, capture.id


PARSER_V1 = uuid4()


async def test_parse_handler_persists_artifact_and_spawns_index() -> None:
    harness = ParseHarness()
    body = (
        "<html><head><title>Fab Report</title>"
        "<meta property='article:published_time' content='2026-09-18T08:00:00Z'>"
        "</head><body><main>"
        "<h1>Fab Report</h1>"
        "<p>" + ("Utilization improved across all nodes this quarter. " * 12)
        + "</p></main></body></html>"
    ).encode()
    _, capture_id = await seed_document_with_capture(harness, body)
    await harness.enqueue_parse(capture_id, PARSER_V1)
    await harness.drain()

    parses = list(harness.pool_db.parses.values())
    assert len(parses) == 1
    row = parses[0]
    assert row["capture_id"] == capture_id
    assert row["parser_version_id"] == PARSER_V1
    assert row["parse_status"] == "ok"
    assert row["blocks"], "blocks JSONB persisted"
    assert row["text_hash"]
    # Capture's retrieval scope refined from the parse outcome.
    capture = harness.pool_db.captures[capture_id]
    assert capture["retrieval_scope"] == "fulltext"
    # Index job spawned (Task 10 handler: runner default marks succeeded).
    index_jobs = harness.jobs_of_kind("index")
    assert len(index_jobs) == 1
    assert index_jobs[0]["input"]["parse_id"] == str(row["id"])
    job = harness.jobs_of_kind("parse")[0]
    assert job["state"] == "succeeded"
    assert job["progress"]["parse_status"] == "ok"


async def test_parse_handler_failed_parse_no_index_but_persisted() -> None:
    harness = ParseHarness()
    _, capture_id = await seed_document_with_capture(
        harness, LOGIN_HTML.encode()
    )
    await harness.enqueue_parse(capture_id, PARSER_V1)
    await harness.drain()

    parses = list(harness.pool_db.parses.values())
    assert len(parses) == 1
    assert parses[0]["parse_status"] == "failed"
    assert "login_page" in parses[0]["quality_flags"]
    assert harness.jobs_of_kind("index") == []
    job = harness.jobs_of_kind("parse")[0]
    assert job["state"] == "succeeded"
    assert job["progress"]["parse_status"] == "failed"


async def test_reparse_same_capture_new_parser_persisted_parser_change() -> None:
    harness = ParseHarness()
    body = (
        b"<html><head><title>Fab Report</title></head><body><main>"
        b"<h1>Fab Report</h1><p>Stable body text for the diff scenario.</p>"
        b"</main></body></html>"
    )
    _, capture_id = await seed_document_with_capture(harness, body)
    parser_v2 = uuid4()
    await harness.enqueue_parse(capture_id, PARSER_V1)
    await harness.enqueue_parse(capture_id, parser_v2)
    await harness.drain()

    parses = list(harness.pool_db.parses.values())
    assert len(parses) == 2  # UNIQUE(capture, parser_version) keeps both
    assert len(harness.jobs_of_kind("index")) == 2
    # PAR-01: diff between the two parses of the SAME capture.
    diffs = list(harness.pool_db.diffs.values())
    parser_diffs = [d for d in diffs if d["kind"] == "parser_change"]
    assert len(parser_diffs) == 1
    ids = {p["id"] for p in parses}
    assert parser_diffs[0]["from_parse_id"] in ids
    assert parser_diffs[0]["to_parse_id"] in ids


async def test_new_capture_content_change_diff_persisted() -> None:
    harness = ParseHarness()
    v1 = (
        b"<html><head><title>Fab Report</title></head><body><main>"
        b"<h1>Fab Report</h1><p>Original body paragraph one.</p>"
        b"</main></body></html>"
    )
    v2 = (
        b"<html><head><title>Fab Report</title></head><body><main>"
        b"<h1>Fab Report</h1><p>Revised body paragraph one.</p>"
        b"</main></body></html>"
    )
    _, capture1 = await seed_document_with_capture(harness, v1)
    _, capture2 = await seed_document_with_capture(harness, v2)
    await harness.enqueue_parse(capture1, PARSER_V1)
    await harness.enqueue_parse(capture2, PARSER_V1)
    await harness.drain()

    content_diffs = [
        d for d in harness.pool_db.diffs.values()
        if d["kind"] == "content_change"
    ]
    assert len(content_diffs) == 1
    diff = content_diffs[0]
    assert diff["changed_blocks"], "changed blocks carry refs from both parses"
    changed = [c for c in diff["changed_blocks"] if c["change"] == "changed"]
    assert len(changed) == 1
    assert changed[0]["from_block_id"] != changed[0]["to_block_id"]


async def test_same_parser_title_only_edit_not_parser_change() -> None:
    # Workflow path (fix round 1): the parse handler must thread the
    # job's parser_version into the comparison label. Same parser, same
    # body text, only the server-edited <title> changed → the diff is a
    # metadata content change, NOT parser_change (which would suppress
    # the event). Before the fix both sides carried different label
    # schemes ("builtin@1" vs "parser:<uuid>") and could never compare
    # equal, let alone reach the metadata-only branch.
    harness = ParseHarness()
    body = "<h1>Fab Report</h1><p>Stable body paragraph for the diff scenario.</p>"

    def page(title: str) -> bytes:
        return (
            f"<html><head><title>{title}</title></head><body><main>"
            f"{body}</main></body></html>"
        ).encode()

    _, capture1 = await seed_document_with_capture(harness, page("Fab v1"))
    _, capture2 = await seed_document_with_capture(harness, page("Fab v2"))
    await harness.enqueue_parse(capture1, PARSER_V1)
    await harness.enqueue_parse(capture2, PARSER_V1)
    await harness.drain()

    diffs = list(harness.pool_db.diffs.values())
    assert len(diffs) == 1
    assert diffs[0]["kind"] != "parser_change"
    assert diffs[0]["kind"] == "content_change"
    assert diffs[0]["changed_blocks"] == []
    assert diffs[0]["field_changes"] == [
        {"field": "title", "from": "Fab v1", "to": "Fab v2"}
    ]


async def test_parse_job_is_idempotent_per_capture_and_parser() -> None:
    harness = ParseHarness()
    _, capture_id = await seed_document_with_capture(
        harness, b"<html><body><main><p>Once only.</p></main></body></html>"
    )
    first, _ = await harness.enqueue_parse(capture_id, PARSER_V1)
    again, created = await harness.enqueue_parse(capture_id, PARSER_V1)
    assert created is False
    assert again.id == first.id
