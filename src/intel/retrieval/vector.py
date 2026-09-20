"""Vector channel: exact KNN + the diskann switch gate (spec 03 §8, 04 §6 B1).

First version orders EXACTLY (``ORDER BY embedding <=> q``) — no vector
index in the default index generation; filtered recall is guaranteed by
construction. pgvectorscale's diskann index is created only as a separate
index generation (flag-gated DDL in migration 0003), and before that
generation may become the default, filtered recall against a golden set
must reach ≥ 98% of the exact baseline (:data:`DISKANN_RECALL_GATE`,
:func:`passes_diskann_gate` — the REC-08 harness, unit-testable offline on
two result lists). The diskann query parameters (query_search_list_size /
query_rescore, spec 10 §5) are recorded with the generation when it is
evaluated — see Settings.diskann_query_*.

Verified upstream facts (Task 9): ``CREATE INDEX ... USING diskann
(emb vector_cosine_ops)``; KNN ``ORDER BY emb <=> $1``; pgvector exact uses
the same operator without an index.

REC-04 shape: chunks without embeddings are EXCLUDED here (the operator
needs a vector) but never error — the recall service counts them as a
coverage gap via :func:`embedding_gap_stmt` instead (延迟 embedding 列入
补扫，不能跨过缺口后永久漏掉).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Float, Select, bindparam, cast, distinct, func, select

from intel.db.base import VectorType
from intel.db.models.knowledge import IndustryDocument
from intel.db.models.pool import Capture, Chunk, ParsedArtifact

_CHUNKS = Chunk.__table__
_PA = ParsedArtifact.__table__
_CAPTURES = Capture.__table__
_IDOC = IndustryDocument.__table__


def knn_distance_expr() -> Any:
    """``chunks.embedding <=> CAST(:qvec AS vector)`` (cosine distance)."""
    return _CHUNKS.c.embedding.op("<=>", return_type=Float)(
        cast(bindparam("qvec"), VectorType)
    )


def knn_search_stmt() -> Select:
    """Exact-KNN chunk hits scoped to one owner+industry.

    Binds: ``owner_id``, ``industry_id``, ``qvec`` (vector literal string
    like ``'[0.1,0.2]'`` — the cast accepts any driver's text form),
    ``limit``. Rows with NULL embedding are excluded from the channel
    (counted as a coverage gap instead, REC-04).
    """
    distance = knn_distance_expr()
    return (
        select(
            _CHUNKS.c.id.label("chunk_id"),
            _CHUNKS.c.parsed_artifact_id.label("parse_id"),
            _CHUNKS.c.ordinal.label("ordinal"),
            _CHUNKS.c.block_ids.label("block_ids"),
            distance.label("distance"),
            _IDOC.c.id.label("industry_document_id"),
        )
        .select_from(_CHUNKS)
        .join(
            _PA,
            (_PA.c.owner_id == _CHUNKS.c.owner_id)
            & (_PA.c.id == _CHUNKS.c.parsed_artifact_id),
        )
        .join(
            _CAPTURES,
            (_CAPTURES.c.owner_id == _PA.c.owner_id)
            & (_CAPTURES.c.id == _PA.c.capture_id),
        )
        .join(
            _IDOC,
            (_IDOC.c.owner_id == _CAPTURES.c.owner_id)
            & (_IDOC.c.document_id == _CAPTURES.c.document_id)
            & (_IDOC.c.industry_id == bindparam("industry_id")),
        )
        .where(
            _CHUNKS.c.owner_id == bindparam("owner_id"),
            _IDOC.c.active.is_(True),
            _CHUNKS.c.embedding.is_not(None),
        )
        .order_by(distance.asc())
        .limit(bindparam("limit"))
    )


def embedding_gap_stmt() -> Select:
    """Count in-scope DOCUMENTS that have chunks still missing embeddings.

    The coverage signal for the delayed-embedding backfill queue (04 §6:
    延迟 embedding 列入补扫) — a number, never an error path.
    """
    return (
        select(func.count(distinct(_IDOC.c.id)).label("embedding_pending_documents"))
        .select_from(_CHUNKS)
        .join(
            _PA,
            (_PA.c.owner_id == _CHUNKS.c.owner_id)
            & (_PA.c.id == _CHUNKS.c.parsed_artifact_id),
        )
        .join(
            _CAPTURES,
            (_CAPTURES.c.owner_id == _PA.c.owner_id)
            & (_CAPTURES.c.id == _PA.c.capture_id),
        )
        .join(
            _IDOC,
            (_IDOC.c.owner_id == _CAPTURES.c.owner_id)
            & (_IDOC.c.document_id == _CAPTURES.c.document_id)
            & (_IDOC.c.industry_id == bindparam("industry_id")),
        )
        .where(
            _CHUNKS.c.owner_id == bindparam("owner_id"),
            _IDOC.c.active.is_(True),
            _CHUNKS.c.embedding.is_(None),
        )
    )


#: REC-08: a diskann generation may only become the default when its
#: filtered recall reaches ≥ 98% of the exact baseline on a golden set.
DISKANN_RECALL_GATE = 0.98


def recall_at_k(
    exact: Sequence[Any], approximate: Sequence[Any], *, k: int = 100
) -> float:
    """|top-k(approx) ∩ top-k(exact)| / |top-k(exact)| — REC-08 harness.

    Both arguments are result orderings (best first; ids or rows — anything
    hashable). An empty exact top-k trivially recalls fully (nothing was
    required). Duplicate entries are ignored.
    """
    exact_top = list(dict.fromkeys(exact[:k]))
    approx_top = set(dict.fromkeys(approximate[:k]))
    if not exact_top:
        return 1.0
    hit = sum(1 for item in exact_top if item in approx_top)
    return hit / len(exact_top)


def passes_diskann_gate(
    exact: Sequence[Any], approximate: Sequence[Any], *, k: int = 100
) -> bool:
    """Whether the diskann result set clears the 98% switch gate."""
    return recall_at_k(exact, approximate, k=k) >= DISKANN_RECALL_GATE
