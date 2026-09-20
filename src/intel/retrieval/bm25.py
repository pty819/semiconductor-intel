"""BM25 channel: pg_textsearch ``<@>`` with jieba tokenization (spec 04 §6 B2).

Verified upstream facts (Task 9, deploy/nas/README.md):

- index DDL: ``CREATE INDEX ... ON chunks USING bm25 (text) WITH
  (text_config='jieba', k1=..., b=...)`` — the ``jieba`` text search
  configuration is the custom D13 mapping (parser ``jieba``, POS → simple
  dictionary), created idempotently by migration 0003 AND the NAS init
  script;
- the score operator ``text <@> query`` returns NEGATIVE BM25 scores for
  ascending index scans — lower (more negative) ranks first, so channel
  ordering is ASCENDING and :func:`positive_bm25_score` negates the raw
  column for display/recording.

Query construction happens app-side (04 §6: 召回优先查询用 jiebaqry 全模式):
the topic's terms/aliases are whitespace-joined into the query string —
plain space-separated terms are pg_textsearch's conjunction-of-terms form,
so no server-side query syntax crosses the boundary (untrusted topic text
never enters the operator string unfiltered beyond term splitting).

Scope (ISO-03 变体): every channel query filters owner AND industry above
RLS — chunks are joined through their parse/capture to the industry
binding (``industry_documents``), so even a mis-bound RLS session cannot
widen the candidate set.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Float, Select, bindparam, select
from sqlalchemy.sql.elements import ColumnElement

from intel.db.models.knowledge import IndustryDocument
from intel.db.models.pool import Capture, Chunk, ParsedArtifact

#: k1/b baked into the DEFAULT index generation (migration 0003). They
#: mirror the ``Settings.bm25_k1`` / ``bm25_b`` defaults — a NEW generation
#: (03 §8: 词典/分词配置变更视为 index_generation 变更) may change them and
#: rebuild. Keep in sync with the ORM index declaration in db/models/pool.py
#: (pinned by tests/unit/test_retrieval_sql.py).
BM25_K1 = 1.5
BM25_B = 0.75

#: Name shared by the ORM index declaration and migration 0003.
BM25_INDEX_NAME = "ix_chunks_text_bm25"


def build_bm25_query(terms: Iterable[str]) -> str:
    """Whitespace-join deduped non-empty terms (app-side jiebaqry shape)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        cleaned = term.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return " ".join(ordered)


def positive_bm25_score(raw: float) -> float:
    """Negate the raw ``<@>`` column for positive display/recording.

    pg_textsearch returns negative BM25 scores (ascending scans rank the
    best match first with the LOWEST value); recall_hits and the UI show
    conventional positive scores.
    """
    return -raw


def bm25_score_expr() -> ColumnElement[float]:
    """``chunks.text <@> :bm25_q`` as a score expression (negative values)."""
    return Chunk.__table__.c.text.op("<@>", return_type=Float)(bindparam("bm25_q"))


def bm25_search_stmt() -> Select:
    """Chunk-level BM25 hits scoped to one owner+industry.

    Binds: ``owner_id``, ``industry_id``, ``bm25_q`` (the whitespace-joined
    query string), ``limit``. Ordered ascending on the negative score —
    the most relevant rows first. Returns per-chunk rows: chunk id, parse
    id, ordinal, block_ids, the raw score (``bm25_score``) and the industry
    binding id.
    """
    score = bm25_score_expr()
    return (
        select(
            Chunk.__table__.c.id.label("chunk_id"),
            Chunk.__table__.c.parsed_artifact_id.label("parse_id"),
            Chunk.__table__.c.ordinal.label("ordinal"),
            Chunk.__table__.c.block_ids.label("block_ids"),
            score.label("bm25_score"),
            IndustryDocument.id.label("industry_document_id"),
        )
        .select_from(Chunk.__table__)
        .join(
            ParsedArtifact.__table__,
            (ParsedArtifact.__table__.c.owner_id == Chunk.__table__.c.owner_id)
            & (ParsedArtifact.__table__.c.id == Chunk.__table__.c.parsed_artifact_id),
        )
        .join(
            Capture.__table__,
            (Capture.__table__.c.owner_id == ParsedArtifact.__table__.c.owner_id)
            & (Capture.__table__.c.id == ParsedArtifact.__table__.c.capture_id),
        )
        .join(
            IndustryDocument.__table__,
            (IndustryDocument.__table__.c.owner_id == Capture.__table__.c.owner_id)
            & (
                IndustryDocument.__table__.c.document_id
                == Capture.__table__.c.document_id
            )
            & (IndustryDocument.__table__.c.industry_id == bindparam("industry_id")),
        )
        .where(
            Chunk.__table__.c.owner_id == bindparam("owner_id"),
            IndustryDocument.active.is_(True),
        )
        .order_by(score.asc())
        .limit(bindparam("limit"))
    )


def bm25_snippet_terms(query: str) -> list[str]:
    """Terms of a built query, for provenance reasons/manifests."""
    return query.split()
