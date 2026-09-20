"""Unit tests: chunker semantics (spec 04 §5).

Offline, no DB — the chunker is a pure function from blocks to chunks:

- sizes: ~800 model tokens target / 1200 max (char-based /4 estimate is the
  documented v1 token proxy);
- ~100-token context overlap between consecutive chunks of the same flow;
- heading blocks open a new chunk (标题层级分块) and reset the overlap;
- table blocks split by row groups with the header line repeated in every
  group chunk (表格行组带 header);
- chunks carry ``block_ids`` — references bind to immutable parse block IDs,
  never to chunk positions or vector chunk ids (引用不绑定易变 chunk ID);
- ``normalized_terms`` is a casefolded, punctuation-stripped token string
  that keeps token adjacency (the trigram alias channel matches multi-word
  model numbers against it);
- ``chunker_version`` records the parameter set (spec 04 §5 记 chunker_version).
"""

from __future__ import annotations

from intel.parsing.dto import Block
from intel.retrieval.chunker import (
    CHUNKER_PARAMS,
    CHUNKER_VERSION,
    MAX_TOKENS,
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    chunk_blocks,
    detect_language,
    estimate_tokens,
    normalize_terms,
)


def _para(idx: int, text: str) -> Block:
    return Block(block_id=f"p{idx:03d}", kind="paragraph", text=text)


def _long_paragraphs(n: int, *, words: int = 400, start: int = 0) -> list[Block]:
    """Paragraphs of ~`words` whitespace words → ~4*words/4 = words tokens."""
    out: list[Block] = []
    for i in range(start, start + n):
        text = " ".join(f"w{i}t{j}" for j in range(words))
        out.append(_para(i, text))
    return out


class TestTokenEstimate:
    def test_chars_divided_by_four(self) -> None:
        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("abc") == 1  # never zero for non-empty text

    def test_empty_text_is_zero(self) -> None:
        assert estimate_tokens("") == 0


class TestNormalizeTerms:
    def test_casefolds_and_strips_punctuation(self) -> None:
        normalized = normalize_terms("The RTX 4090 (AD102) — 24GB GDDR6X!")
        tokens = normalized.split()
        assert "rtx" in tokens
        assert "4090" in tokens
        assert "ad102" in tokens
        assert "24gb" in tokens
        assert "gddr6x" in tokens
        assert all("(" not in t and "—" not in t and "!" not in t for t in tokens)

    def test_keeps_token_adjacency_for_trigram_phrases(self) -> None:
        # The alias channel matches multi-word aliases ("rtx 4090") against
        # this column; deduping tokens would destroy adjacency.
        assert "rtx 4090" in normalize_terms("the RTX 4090 launched")

    def test_cjk_tokens_survive(self) -> None:
        # CJK runs stay whole (one token per run) — the trigram channel
        # matches substrings, so 刻蚀 matches inside 等离子刻蚀工艺.
        assert "等离子刻蚀工艺" in normalize_terms("等离子刻蚀工艺")
        assert "刻蚀" in normalize_terms("等离子刻蚀工艺")


class TestLanguageDetection:
    def test_chinese_text_detected(self) -> None:
        assert detect_language("半导体刻蚀工艺的进展报道") == "zh"

    def test_english_text_detected(self) -> None:
        assert detect_language("plasma etching quarterly update") == "en"


class TestChunkerVersion:
    def test_version_constant_and_params(self) -> None:
        assert CHUNKER_VERSION == "chunker@1"
        assert CHUNKER_PARAMS["target_tokens"] == TARGET_TOKENS == 800
        assert CHUNKER_PARAMS["max_tokens"] == MAX_TOKENS == 1200
        assert CHUNKER_PARAMS["overlap_tokens"] == OVERLAP_TOKENS == 100
        assert CHUNKER_PARAMS["token_estimate"] == "chars/4"


class TestChunkSizes:
    def test_long_document_chunks_within_max(self) -> None:
        blocks = _long_paragraphs(12)  # ~4800 tokens total
        chunks = chunk_blocks(blocks)
        assert len(chunks) >= 3
        for chunk in chunks:
            assert estimate_tokens(chunk.text) <= MAX_TOKENS

    def test_oversized_single_block_is_hard_split(self) -> None:
        huge = _para(0, " ".join(f"big{j}" for j in range(4000)))  # ~4000 tok
        chunks = chunk_blocks([huge])
        assert len(chunks) >= 4
        for chunk in chunks:
            assert estimate_tokens(chunk.text) <= MAX_TOKENS
        # Hard-split pieces of one block share its block id.
        for chunk in chunks:
            assert chunk.block_ids == ("p000",)

    def test_small_document_is_one_chunk(self) -> None:
        blocks = [
            Block(block_id="h000", kind="heading", text="Title"),
            _para(1, "one short paragraph"),
        ]
        chunks = chunk_blocks(blocks)
        assert len(chunks) == 1
        assert "one short paragraph" in chunks[0].text


class TestOverlap:
    def test_consecutive_chunks_share_trailing_context(self) -> None:
        blocks = _long_paragraphs(10)
        chunks = chunk_blocks(blocks)
        assert len(chunks) >= 2
        first, second = chunks[0], chunks[1]
        # The overlap tail of chunk 1 reappears verbatim at the head of
        # chunk 2 (100-token context overlap, spec 04 §5).
        head = " ".join(second.text.split()[:20])
        assert head in first.text

    def test_overlap_is_about_100_tokens(self) -> None:
        blocks = _long_paragraphs(10)
        first, second = chunk_blocks(blocks)[:2]
        head_words = second.text.split()
        # The head of chunk 2 is drawn from the tail of chunk 1...
        assert " ".join(head_words[:40]) in " ".join(first.text.split()[-120:])
        # ...and does not swallow the chunk (well below the target size).
        assert len(head_words) > 150


class TestHeadingBoundaries:
    def test_heading_opens_new_chunk_and_resets_overlap(self) -> None:
        blocks = _long_paragraphs(6)
        blocks.append(Block(block_id="h100", kind="heading", text="Next Section"))
        blocks.extend(_long_paragraphs(6, start=100))

        chunks = chunk_blocks(blocks)
        # Exactly one chunk starts at the heading (per-section chunks).
        heading_chunks = [c for c in chunks if c.block_ids[0] == "h100"]
        assert len(heading_chunks) == 1
        heading_chunk = heading_chunks[0]
        # New section ⇒ no context overlap carried across the boundary.
        prior = chunks[chunks.index(heading_chunk) - 1]
        head = " ".join(heading_chunk.text.split()[:10])
        assert head not in prior.text


class TestTables:
    def _table_block(self, rows: int) -> Block:
        lines = ["型号 | 制程 | 量产时间"]
        filler = "该行记录了本季度各产线的产能利用率与良率细节 "
        for i in range(rows):
            lines.append(f"X99{i:02d} | 3nm | 2027Q{i % 4} | " + filler * 5)
        return Block(block_id="t000", kind="table", text="\n".join(lines))

    def test_table_splits_by_row_groups_with_header_repeated(self) -> None:
        table = self._table_block(rows=60)  # far past the 800-token target
        chunks = chunk_blocks([table])
        assert len(chunks) >= 2
        for chunk in chunks:
            # Every group chunk repeats the header row group (spec 04 §5).
            assert chunk.text.splitlines()[0] == "型号 | 制程 | 量产时间"
            assert estimate_tokens(chunk.text) <= MAX_TOKENS
            assert chunk.block_ids == ("t000",)

    def test_small_table_is_one_chunk(self) -> None:
        chunks = chunk_blocks([self._table_block(rows=3)])
        assert len(chunks) == 1

    def test_table_never_merges_with_adjacent_prose(self) -> None:
        blocks = [_para(0, "intro " + "x " * 50), self._table_block(rows=2)]
        for chunk in chunk_blocks(blocks):
            assert not ("intro" in chunk.text and "型号" in chunk.text)


class TestBlockReferences:
    def test_chunks_carry_block_ids_not_positions(self) -> None:
        blocks = _long_paragraphs(8)
        ids = {b.block_id for b in blocks}
        chunks = chunk_blocks(blocks)
        for chunk in chunks:
            assert chunk.block_ids
            assert set(chunk.block_ids) <= ids
            assert all(isinstance(b, str) for b in chunk.block_ids)

    def test_every_non_empty_block_is_covered(self) -> None:
        blocks = _long_paragraphs(8)
        blocks.append(_para(99, "   "))  # whitespace-only: skipped
        chunks = chunk_blocks(blocks)
        covered = {bid for c in chunks for bid in c.block_ids}
        assert covered == {b.block_id for b in blocks[:8]}

    def test_ordinals_are_sequential_from_zero(self) -> None:
        chunks = chunk_blocks(_long_paragraphs(12))
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))

    def test_accepts_raw_dicts_from_the_db_row(self) -> None:
        raw = [
            {
                "block_id": "p000",
                "kind": "paragraph",
                "text": "a short block",
                "page": None,
                "section_path": [],
                "bbox": None,
                "source_locator": None,
            }
        ]
        chunks = chunk_blocks(raw)  # type: ignore[arg-type]
        assert chunks[0].block_ids == ("p000",)
        assert "a short block" in chunks[0].text

    def test_each_chunk_gets_language_and_terms(self) -> None:
        chunks = chunk_blocks(
            [_para(0, "The RTX 4090 supply chain report " + "filler " * 200)]
        )
        assert chunks[0].language == "en"
        assert "rtx" in chunks[0].normalized_terms.split()
        zh = chunk_blocks([_para(1, "刻蚀设备市场分析 " + "补充 " * 200)])
        assert zh[0].language == "zh"
        assert "刻蚀" in zh[0].normalized_terms
