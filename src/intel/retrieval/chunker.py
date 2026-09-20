"""Chunker: parsed blocks → retrieval chunks (spec 04 §5).

Pure function, no DB access — the index handler (``intel.retrieval.indexer``)
feeds it the parse's stored blocks and persists the output.

Rules implemented (04 §5):

- 按标题层级和段落分块: heading blocks open a new chunk; paragraphs
  accumulate toward the target size;
- ~800 model tokens target / ~1200 max, with ~100 token context overlap
  between consecutive chunks of the same flow. Token counts are a
  char-based estimate (chars/4) — the documented v1 proxy; actual values
  are retuned in M3 and recorded via ``CHUNKER_VERSION``;
- 表格按行组保留 header: a table block too large for one chunk is split
  by row groups and every group chunk repeats the header lines. v1
  simplification (documented): the header is the table block's leading
  line(s) — the HTML parser (PAR-02) emits header rows first;
- 每个 chunk 保留 block IDs: references bind to immutable parse block IDs,
  never to positions or vector chunk ids (引用不绑定易变的向量 chunk ID);
- ``normalized_terms``: casefolded, punctuation-stripped token string that
  keeps token adjacency — the trigram alias/phrase channel (03 §8) matches
  multi-word model numbers against this column, so tokens are NOT deduped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from intel.parsing.dto import Block

#: Release label of this chunker's parameters (04 §5 记 chunker_version).
#: Bump when any parameter below changes so re-indexes mint new index jobs.
CHUNKER_VERSION = "chunker@1"

#: The parameter set ``CHUNKER_VERSION`` pins (mirrored in job manifests).
CHUNKER_PARAMS: dict[str, object] = {
    "target_tokens": 800,
    "max_tokens": 1200,
    "overlap_tokens": 100,
    "token_estimate": "chars/4",
}

TARGET_TOKENS = int(CHUNKER_PARAMS["target_tokens"])  # type: ignore[arg-type]
MAX_TOKENS = int(CHUNKER_PARAMS["max_tokens"])  # type: ignore[arg-type]
OVERLAP_TOKENS = int(CHUNKER_PARAMS["overlap_tokens"])  # type: ignore[arg-type]

#: Chars per estimated token (documented v1 proxy for model tokens).
TOKEN_CHARS = 4

_CJK_RANGES = (
    ("\u4e00", "\u9fff"),  # CJK unified ideographs
    ("\u3400", "\u4dbf"),  # extension A
    ("\uf900", "\ufaff"),  # compatibility ideographs
)
#: Term-like token: alnum/CJK lead char, then alnum/CJK/+-.:/ — keeps
#: model numbers ("RTX-4090", "3nm", "AD102") and CJK runs, drops punctuation.
_TERM_TOKEN_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z\u4e00-\u9fff+\-.:/]*")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrieval chunk; ``block_ids`` reference the parse's blocks."""

    ordinal: int
    block_ids: tuple[str, ...]
    text: str
    language: str
    normalized_terms: str


def estimate_tokens(text: str) -> int:
    """Char-based token estimate (chars/4); 0 for empty, never 0 otherwise."""
    if not text:
        return 0
    return max(1, len(text) // TOKEN_CHARS)


def normalize_terms(text: str) -> str:
    """Casefold + strip punctuation, keep token ORDER and adjacency.

    Adjacency matters: the trigram channel matches multi-word aliases
    ("rtx 4090") as substrings, so tokens are joined by single spaces
    without dedup.
    """
    return " ".join(
        match.group(0).casefold() for match in _TERM_TOKEN_RE.finditer(text)
    )


def detect_language(text: str) -> str:
    """'zh' when CJK ideographs dominate the letters, else 'en'."""
    cjk = 0
    letters = 0
    for char in text:
        if char.isalpha() or "\u4e00" <= char <= "\u9fff":
            letters += 1
            if any(lo <= char <= hi for lo, hi in _CJK_RANGES):
                cjk += 1
    if letters and cjk / letters > 0.15:
        return "zh"
    return "en"


def _as_block(block: Block | dict) -> Block:
    if isinstance(block, Block):
        return block
    return Block(**block)


# --------------------------------------------------------------------------
# unit model: the atomic text pieces the assembler works with
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Unit:
    block_ids: tuple[str, ...]
    text: str
    starts_section: bool = False
    is_table: bool = False


def _split_tables(block: Block) -> list[_Unit]:
    """One table block → row-group units, each repeating the header lines.

    The HTML parser keeps header rows first (PAR-02); v1 treats the leading
    line as the header row group. Groups pack as many rows as fit the
    target (sized by the MOST expensive row, so a group chunk stays under
    the max even with ragged rows). A single row that alone exceeds the max
    is still kept whole — splitting a row would lose cells (PAR-02: 行列
    条件不丢), and the max is approximate by design.
    """
    lines = [line for line in block.text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0]
    body = lines[1:] or lines  # header-only table: the header is the body
    header_cost = estimate_tokens(header) + 1  # + newline
    row_cost = max(estimate_tokens(row) for row in body)
    group_rows = max(1, (TARGET_TOKENS - header_cost) // max(1, row_cost))
    units: list[_Unit] = []
    for start in range(0, len(body), group_rows):
        group = body[start : start + group_rows]
        units.append(
            _Unit(
                block_ids=(block.block_id,),
                text="\n".join([header, *group]),
                is_table=True,
            )
        )
    return units


# --------------------------------------------------------------------------
# assembler
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Accumulator:
    """A chunk under construction: per-unit parts for overlap attribution."""

    parts: list[tuple[tuple[str, ...], str]]  # (block_ids, text)
    #: Whether any non-heading unit landed here — a heading alone never
    #: flushes (section titles stay attached to their first content).
    saw_content: bool = False

    def text(self) -> str:
        return "\n".join(text for _, text in self.parts)

    def tokens(self) -> int:
        return estimate_tokens(self.text())

    def block_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for block_ids, _ in self.parts:
            for block_id in block_ids:
                if block_id not in ids:
                    ids.append(block_id)
        return tuple(ids)


def _overlap_tail(acc: _Accumulator) -> tuple[str, tuple[str, ...]]:
    """Trailing ~OVERLAP_TOKENS of the accumulator (words, block-attributed)."""
    words_with_ids: list[tuple[str, tuple[str, ...]]] = []
    for block_ids, text in acc.parts:
        for word in text.split():
            words_with_ids.append((word, block_ids))
    tail: list[str] = []
    tail_ids: list[str] = []
    budget = OVERLAP_TOKENS * TOKEN_CHARS
    used = 0
    for word, block_ids in reversed(words_with_ids):
        if used >= budget:
            break
        tail.append(word)
        used += len(word) + 1
        for block_id in block_ids:
            if block_id not in tail_ids:
                tail_ids.append(block_id)
    return " ".join(reversed(tail)), tuple(reversed(tail_ids))


def _split_oversized(unit: _Unit) -> list[_Unit]:
    """Hard-split one oversized unit into ≤max windows with overlap.

    Windows are cut at word boundaries around the target size; consecutive
    windows share the ~100-token tail/head (04 §5 必要时上下文重叠).
    """
    words = unit.text.split()
    if estimate_tokens(unit.text) <= MAX_TOKENS:
        return [unit]
    units: list[_Unit] = []
    start = 0
    window_words = max(1, (TARGET_TOKENS * TOKEN_CHARS))
    while start < len(words):
        piece: list[str] = []
        used = 0
        for word in words[start:]:
            if used and used + len(word) + 1 > window_words:
                break
            piece.append(word)
            used += len(word) + 1
        if not piece:  # pragma: no cover - window_words >= 1 word always
            break
        units.append(
            _Unit(
                block_ids=unit.block_ids,
                text=" ".join(piece),
                starts_section=unit.starts_section and start == 0,
            )
        )
        if estimate_tokens(units[-1].text) > MAX_TOKENS:  # pragma: no cover
            # A single word longer than the window budget: keep it whole —
            # estimates are heuristic and the max is approximate anyway.
            pass
        # Next window starts ~overlap before the end of this one.
        consumed = len(piece)
        step_back = 0
        back_chars = 0
        while (start + consumed - step_back - 1) > start and (
            back_chars < OVERLAP_TOKENS * TOKEN_CHARS
        ):
            step_back += 1
            back_chars += len(words[start + consumed - step_back]) + 1
        start = start + consumed - step_back
    return units


def chunk_blocks(blocks: Sequence[Block | dict[str, object]]) -> list[Chunk]:
    """Blocks (Block DTOs or stored block dicts) → ordered chunks."""
    units: list[_Unit] = []
    for block in blocks:
        parsed = _as_block(block)  # type: ignore[arg-type]
        if not parsed.text.strip():
            continue
        if parsed.kind == "table":
            units.extend(_split_tables(parsed))
        else:
            units.extend(
                _split_oversized(
                    _Unit(
                        block_ids=(parsed.block_id,),
                        text=parsed.text.strip(),
                        starts_section=parsed.kind == "heading",
                    )
                )
            )

    chunks: list[Chunk] = []
    acc = _Accumulator(parts=[])
    pending_overlap: tuple[str, tuple[str, ...]] | None = None
    carry_overlap = False

    def flush(*, continue_flow: bool) -> None:
        nonlocal acc, pending_overlap, carry_overlap
        if not acc.parts:
            return
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                block_ids=acc.block_ids(),
                text=acc.text(),
                language=detect_language(acc.text()),
                normalized_terms=normalize_terms(acc.text()),
            )
        )
        carry_overlap = continue_flow
        pending_overlap = _overlap_tail(acc) if continue_flow else None
        acc = _Accumulator(parts=[])

    for unit in units:
        if unit.is_table:
            # Tables never merge with prose; each row-group chunk is
            # standalone (the repeated header provides the row context).
            flush(continue_flow=False)
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    block_ids=unit.block_ids,
                    text=unit.text,
                    language=detect_language(unit.text),
                    normalized_terms=normalize_terms(unit.text),
                )
            )
            pending_overlap = None
            carry_overlap = False
            continue

        if unit.starts_section:
            # 标题层级: new section starts a fresh chunk, overlap resets.
            flush(continue_flow=False)

        projected = acc.tokens() + estimate_tokens(unit.text) + 1
        if acc.parts and acc.saw_content and projected > TARGET_TOKENS:
            flush(continue_flow=True)

        if pending_overlap is not None and carry_overlap and not acc.parts:
            overlap_text, overlap_ids = pending_overlap
            acc.parts.append((overlap_ids, overlap_text))
        acc.parts.append((unit.block_ids, unit.text))
        if not unit.starts_section:
            acc.saw_content = True
        if acc.tokens() > MAX_TOKENS:  # pragma: no cover - assembler guard
            flush(continue_flow=True)

    flush(continue_flow=False)
    return chunks


def block_ids_of(chunks: Iterable[Chunk]) -> set[str]:
    """Every block id referenced by ``chunks`` (coverage assertions)."""
    return {block_id for chunk in chunks for block_id in chunk.block_ids}
