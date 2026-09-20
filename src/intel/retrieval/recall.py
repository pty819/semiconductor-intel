"""RecallService: the four-channel topic recall (spec 04 §6 B, 14 §2).

Channels (04 §6 B):

1. ``semantic`` — document/paragraph vectors, exact KNN first version
   (:mod:`intel.retrieval.vector`);
2. ``bm25`` — professional-term full-text via pg_textsearch ``<@>``
   (:mod:`intel.retrieval.bm25`); the query string is built app-side from
   the topic's terms (jiebaqry 全模式 shape);
3. ``alias`` — exact model numbers / entity aliases via pg_trgm similarity
   on ``chunks.normalized_terms`` (短语与精确表述匹配不依赖 BM25 索引,
   由 trigram/ILIKE 复查通道承担);
4. ``proximity`` — documents historically linked to the topic's positive
   examples (document_topics join). v1 documents the simplification: the
   channel returns the positive documents themselves; neighbor-block
   expansion inside them is a later refinement.

Union rules (04 §6): multi-channel union deduped BY DOCUMENT (one
:class:`DocumentHit` keeps every channel provenance); RRF (initial k=60)
decides PROCESSING ORDER ONLY — no threshold drops (REC-03: top-k 不删除,
只定顺序; 相似度只是候选信号). Batches of ``RECALL_BATCH_SIZE`` documents
with a persisted cursor (offset + seen set live in the recall job's
input/progress — the caller persists what :func:`RecallService.recall_page`
returns). Every page records its per-channel provenance into ``recall_hits``
(UNIQUE(job, parse, chunk, channel)).

``RecallPage`` is the 14 §2 DTO: hits[], next_cursor?, coverage,
query_manifest (hit 带 parse/block/channel/score; scores are candidate
signals, never confidence).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import Select, bindparam, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from intel.db.models.jobs import RecallHit
from intel.db.models.knowledge import DocumentTopic, IndustryDocument
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import ScopedRepository
from intel.retrieval.bm25 import bm25_search_stmt, build_bm25_query, positive_bm25_score
from intel.retrieval.vector import embedding_gap_stmt, knn_search_stmt

#: Channels, in evaluation order.
CHANNEL_SEMANTIC = "semantic"
CHANNEL_BM25 = "bm25"
CHANNEL_ALIAS = "alias"
CHANNEL_PROXIMITY = "proximity"
CHANNELS = (CHANNEL_SEMANTIC, CHANNEL_BM25, CHANNEL_ALIAS, CHANNEL_PROXIMITY)

#: RRF k (04 §6: 初始 k=60) — ordering-only, never a retention threshold.
RRF_K = 60
#: Recall batch size (04 §6: 每次 batch_size=100).
RECALL_BATCH_SIZE = 100


def rrf_contrib(rank: int, k: int = RRF_K) -> float:
    """1 / (k + rank) for a 0-based channel rank."""
    return 1.0 / (k + rank)


# --------------------------------------------------------------------------
# DTOs (14 §2)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawHit:
    """One channel's raw row (chunk- or document-level)."""

    channel: str
    industry_document_id: UUID
    parse_id: UUID
    chunk_id: UUID | None
    block_ids: tuple[str, ...]
    score: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ChannelProvenance:
    """Per-channel evidence for one document hit (recall_hits row)."""

    channel: str
    parse_id: UUID
    chunk_id: UUID | None
    block_ids: tuple[str, ...]
    score: float
    reason: str
    rank: int  # 1-based position within the channel's raw ordering


@dataclass(frozen=True, slots=True)
class DocumentHit:
    """The union entry: one document with aggregated RRF + provenances."""

    industry_document_id: UUID
    rrf_score: float
    channels: tuple[ChannelProvenance, ...]


@dataclass(frozen=True, slots=True)
class RecallCursor:
    """Persisted pagination state (recall job input/progress).

    ``offset`` is the position in the FULL RRF ordering where the next
    page starts; ``seen`` are documents earlier pages already delivered —
    the duplicate guard for when the ordering shifts between pages (new
    candidates landing mid-pagination). Applying both as skip filters
    would double-skip the pool, so ``seen`` never advances the start
    position (候选未处理完应继续任务 — a job resumes from this cursor
    after a restart).
    """

    offset: int = 0
    seen: tuple[UUID, ...] = ()

    def to_progress(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "seen": [str(doc) for doc in self.seen],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> RecallCursor:
        if not payload:
            return cls()
        return cls(
            offset=int(payload.get("offset", 0)),
            seen=tuple(UUID(str(item)) for item in payload.get("seen", ())),
        )


@dataclass(frozen=True, slots=True)
class RecallPage:
    """14 §2 RecallPage: hits[], next_cursor?, coverage, query_manifest."""

    hits: tuple[DocumentHit, ...]
    next_cursor: RecallCursor | None
    coverage: dict[str, Any]
    query_manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """What one recall run searches for (topic revision projection)."""

    recall_job_id: UUID
    topic_id: UUID
    terms: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    query_embedding: tuple[float, ...] | None = None
    per_channel_k: int = 100


@dataclass(slots=True)
class RecallHitRecord:
    """recall_hits row payload (03 §7: UNIQUE(job, parse, chunk, channel))."""

    recall_job_id: UUID
    parse_id: UUID
    chunk_id: UUID | None
    channel: str
    rank: int | None
    score: float | None
    reason: str


# --------------------------------------------------------------------------
# pure orchestration math
# --------------------------------------------------------------------------


def rank_hits(hits: Sequence[RawHit], *, rrf_k: int = RRF_K) -> list[DocumentHit]:
    """Union by document, aggregate RRF over channel ranks, order desc.

    No document is ever dropped here — RRF decides order only (REC-03).
    Channel rank is each hit's 0-based position within its channel's input
    order (SQL channels arrive pre-ordered best-first).
    """
    scores: dict[UUID, float] = {}
    provenance: dict[UUID, list[ChannelProvenance]] = {}
    channel_rank: dict[str, int] = {}
    for hit in hits:
        rank = channel_rank.get(hit.channel, 0)
        channel_rank[hit.channel] = rank + 1
        scores[hit.industry_document_id] = scores.get(
            hit.industry_document_id, 0.0
        ) + rrf_contrib(rank, rrf_k)
        provenance.setdefault(hit.industry_document_id, []).append(
            ChannelProvenance(
                channel=hit.channel,
                parse_id=hit.parse_id,
                chunk_id=hit.chunk_id,
                block_ids=hit.block_ids,
                score=hit.score,
                reason=hit.reason,
                rank=rank + 1,
            )
        )
    # Deterministic tie-break on document id so pagination is stable.
    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        DocumentHit(
            industry_document_id=doc_id,
            rrf_score=score,
            channels=tuple(provenance[doc_id]),
        )
        for doc_id, score in ordered
    ]


# --------------------------------------------------------------------------
# alias + proximity statement builders (SQL shape pinned in unit tests)
# --------------------------------------------------------------------------


def alias_search_stmt() -> Select:
    """Trigram similarity on ``normalized_terms`` for ONE alias string.

    Binds: ``owner_id``, ``industry_id``, ``alias``, ``limit``. Score is
    ``similarity(normalized_terms, :alias)`` (pg_trgm, ≥ the session's
    similarity threshold via the ``%`` operator). The executor runs this
    once per alias and keeps the best score per chunk.
    """
    from intel.db.models.pool import Capture, Chunk, ParsedArtifact

    chunks = Chunk.__table__
    pa = ParsedArtifact.__table__
    captures = Capture.__table__
    idoc = IndustryDocument.__table__
    match = chunks.c.normalized_terms.op("%")(bindparam("alias"))
    score = func.similarity(chunks.c.normalized_terms, bindparam("alias"))
    return (
        select(
            chunks.c.id.label("chunk_id"),
            chunks.c.parsed_artifact_id.label("parse_id"),
            chunks.c.ordinal.label("ordinal"),
            chunks.c.block_ids.label("block_ids"),
            score.label("alias_score"),
            idoc.c.id.label("industry_document_id"),
        )
        .select_from(chunks)
        .join(
            pa,
            (pa.c.owner_id == chunks.c.owner_id)
            & (pa.c.id == chunks.c.parsed_artifact_id),
        )
        .join(
            captures,
            (captures.c.owner_id == pa.c.owner_id) & (captures.c.id == pa.c.capture_id),
        )
        .join(
            idoc,
            (idoc.c.owner_id == captures.c.owner_id)
            & (idoc.c.document_id == captures.c.document_id)
            & (idoc.c.industry_id == bindparam("industry_id")),
        )
        .where(
            chunks.c.owner_id == bindparam("owner_id"),
            idoc.c.active.is_(True),
            match,
        )
        .order_by(desc(score))
        .limit(bindparam("limit"))
    )


def proximity_search_stmt() -> Select:
    """Documents linked to the topic's historical positives (document_topics).

    Binds: ``owner_id``, ``industry_id``, ``topic_id``, ``limit``. Ranked by
    association recency (updated_at DESC); v1 scores every linked document
    equally (1.0) — the channel is a provenance signal, not a ranking one.
    """
    dt = DocumentTopic.__table__
    idoc = IndustryDocument.__table__
    return (
        select(
            dt.c.industry_document_id.label("industry_document_id"),
            IndustryDocument.current_parse_id.label("parse_id"),
            func.count(dt.c.id).label("positive_links"),
        )
        .select_from(dt)
        .join(
            idoc,
            (idoc.c.owner_id == dt.c.owner_id)
            & (idoc.c.industry_id == dt.c.industry_id)
            & (idoc.c.id == dt.c.industry_document_id),
        )
        .where(
            dt.c.owner_id == bindparam("owner_id"),
            dt.c.industry_id == bindparam("industry_id"),
            dt.c.topic_id == bindparam("topic_id"),
            idoc.c.active.is_(True),
        )
        .group_by(dt.c.industry_document_id, IndustryDocument.current_parse_id)
        .order_by(desc(func.max(dt.c.updated_at)))
        .limit(bindparam("limit"))
    )


def recall_hits_insert_stmt() -> Any:
    """Idempotent recall_hits insert (conflict = already recorded).

    NULL chunk_id rows (document-level hits) never conflict under the
    UNIQUE (NULLs are distinct) and dedupe at the service layer, per the
    model's documented contract.
    """
    return (
        pg_insert(RecallHit)
        .values(
            id=bindparam("id"),
            owner_id=bindparam("owner_id"),
            industry_id=bindparam("industry_id"),
            recall_job_id=bindparam("recall_job_id"),
            parse_id=bindparam("parse_id"),
            chunk_id=bindparam("chunk_id"),
            channel=bindparam("channel"),
            rank=bindparam("rank"),
            score=bindparam("score"),
            reason=bindparam("reason"),
        )
        .on_conflict_do_nothing(constraint="uq_recall_hits_job_parse_chunk_channel")
    )


# --------------------------------------------------------------------------
# store protocol + service
# --------------------------------------------------------------------------


@runtime_checkable
class RecallStore(Protocol):
    """What the recall service needs from storage (04 §6)."""

    async def channel_hits(self, query: RecallQuery, channel: str) -> list[RawHit]: ...

    async def embedding_pending_documents(self, query: RecallQuery) -> int: ...

    async def record_recall_hits(self, hits: list[RecallHitRecord]) -> None: ...


@dataclass(slots=True)
class RecallService:
    """Four-channel recall with RRF ordering and batch cursors."""

    rrf_k: int = RRF_K
    batch_size: int = RECALL_BATCH_SIZE

    async def recall_page(
        self,
        store: RecallStore,
        query: RecallQuery,
        cursor: RecallCursor | None = None,
    ) -> RecallPage:
        cursor = cursor or RecallCursor()
        channels_run: list[str] = []
        raw: list[RawHit] = []
        skipped: dict[str, str] = {}

        if query.query_embedding is not None:
            channels_run.append(CHANNEL_SEMANTIC)
            raw.extend(await store.channel_hits(query, CHANNEL_SEMANTIC))
        else:
            skipped[CHANNEL_SEMANTIC] = "skipped_no_embedding"
        for channel in (CHANNEL_BM25, CHANNEL_ALIAS, CHANNEL_PROXIMITY):
            channels_run.append(channel)
            raw.extend(await store.channel_hits(query, channel))

        ordered = rank_hits(raw, rrf_k=self.rrf_k)
        seen = set(cursor.seen)
        # offset addresses the FULL ordering; `seen` only guards duplicates.
        page: list[DocumentHit] = []
        last_index = cursor.offset - 1
        for index, hit in enumerate(ordered):
            if index < cursor.offset or hit.industry_document_id in seen:
                continue
            if len(page) >= self.batch_size:
                break
            page.append(hit)
            last_index = index
        next_offset = last_index + 1
        remaining = sum(
            1 for hit in ordered[next_offset:] if hit.industry_document_id not in seen
        )

        records: list[RecallHitRecord] = []
        for hit in page:
            for provenance in hit.channels:
                records.append(
                    RecallHitRecord(
                        recall_job_id=query.recall_job_id,
                        parse_id=provenance.parse_id,
                        chunk_id=provenance.chunk_id,
                        channel=provenance.channel,
                        rank=provenance.rank,
                        score=provenance.score,
                        reason=provenance.reason,
                    )
                )
        if records:
            await store.record_recall_hits(records)

        next_cursor = (
            RecallCursor(
                offset=next_offset,
                seen=tuple(
                    sorted(seen | {h.industry_document_id for h in page}, key=str)
                ),
            )
            if remaining > 0
            else None
        )

        coverage: dict[str, Any] = {
            "channels": {
                channel: sum(1 for hit in raw if hit.channel == channel)
                for channel in channels_run
            },
            **skipped,
            "embedding_pending_documents": (
                await store.embedding_pending_documents(query)
            ),
            "union_documents": len(ordered),
            "page_offset": cursor.offset,
            "page_size": len(page),
            "remaining": remaining,
        }
        manifest: dict[str, Any] = {
            "terms": list(query.terms),
            "bm25_query": build_bm25_query(query.terms),
            "aliases": list(query.aliases),
            "channels": channels_run,
            "rrf_k": self.rrf_k,
            "per_channel_k": query.per_channel_k,
            "embedding": {
                "available": query.query_embedding is not None,
                "dims": len(query.query_embedding)
                if query.query_embedding is not None
                else 0,
            },
        }
        return RecallPage(
            hits=tuple(page),
            next_cursor=next_cursor,
            coverage=coverage,
            query_manifest=manifest,
        )


# --------------------------------------------------------------------------
# SqlAlchemy adapter (production)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SqlAlchemyRecallStore(ScopedRepository):
    """RecallStore over one AsyncConnection, RLS-bound to owner+industry.

    Runs the four channel statements and records provenance. All channel
    rows resolve through the parse→capture→industry_documents chain, so
    the industry binding (not just RLS) scopes every candidate (ISO-03).
    """

    async def _bind_owner_industry(self) -> None:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.scope.owner_id, industry_id)

        await require_owner_guc(self.conn)

    async def channel_hits(self, query: RecallQuery, channel: str) -> list[RawHit]:
        await self._bind_owner_industry()
        industry_id = self.scope.require_industry_id()
        base: dict[str, Any] = {
            "owner_id": self.owner_id,
            "industry_id": industry_id,
            "limit": query.per_channel_k,
        }
        if channel == CHANNEL_BM25:
            bm25_query = build_bm25_query(query.terms)
            if not bm25_query:
                return []
            rows = (
                await self.conn.execute(
                    bm25_search_stmt(), {**base, "bm25_q": bm25_query}
                )
            ).mappings()
            return [
                RawHit(
                    channel=channel,
                    industry_document_id=row["industry_document_id"],
                    parse_id=row["parse_id"],
                    chunk_id=row["chunk_id"],
                    block_ids=tuple(row["block_ids"] or ()),
                    score=positive_bm25_score(row["bm25_score"]),
                    reason=f"bm25:{bm25_query}",
                )
                for row in rows
            ]
        if channel == CHANNEL_SEMANTIC:
            if query.query_embedding is None:
                return []
            vector_literal = "[" + ",".join(str(v) for v in query.query_embedding) + "]"
            rows = (
                await self.conn.execute(
                    knn_search_stmt(), {**base, "qvec": vector_literal}
                )
            ).mappings()
            return [
                RawHit(
                    channel=channel,
                    industry_document_id=row["industry_document_id"],
                    parse_id=row["parse_id"],
                    chunk_id=row["chunk_id"],
                    block_ids=tuple(row["block_ids"] or ()),
                    # Cosine similarity (1 - distance) as the display score.
                    score=1.0 - float(row["distance"]),
                    reason="semantic:exact_knn",
                )
                for row in rows
            ]
        if channel == CHANNEL_ALIAS:
            if not query.aliases:
                return []
            hits: list[RawHit] = []
            for alias in query.aliases:
                rows = (
                    await self.conn.execute(
                        alias_search_stmt(),
                        {**base, "alias": alias},
                    )
                ).mappings()
                for row in rows:
                    hits.append(
                        RawHit(
                            channel=channel,
                            industry_document_id=row["industry_document_id"],
                            parse_id=row["parse_id"],
                            chunk_id=row["chunk_id"],
                            block_ids=tuple(row["block_ids"] or ()),
                            score=float(row["alias_score"]),
                            reason=f"alias:{alias}",
                        )
                    )
            return hits
        if channel == CHANNEL_PROXIMITY:
            rows = (
                await self.conn.execute(
                    proximity_search_stmt(),
                    {**base, "topic_id": query.topic_id},
                )
            ).mappings()
            return [
                RawHit(
                    channel=channel,
                    industry_document_id=row["industry_document_id"],
                    parse_id=row["parse_id"],
                    chunk_id=None,
                    block_ids=(),
                    score=1.0,
                    reason=(f"proximity:topic_positive_links={row['positive_links']}"),
                )
                for row in rows
            ]
        raise ValueError(f"unknown recall channel {channel!r}")

    async def embedding_pending_documents(self, query: RecallQuery) -> int:
        await self._bind_owner_industry()
        industry_id = self.scope.require_industry_id()
        result = await self.conn.execute(
            embedding_gap_stmt(),
            {
                "owner_id": self.owner_id,
                "industry_id": industry_id,
            },
        )
        row = result.one_or_none()
        return int(row[0]) if row is not None else 0

    async def record_recall_hits(self, hits: list[RecallHitRecord]) -> None:
        if not hits:
            return
        await self._bind_owner_industry()
        industry_id = self.scope.require_industry_id()
        # Document-level rows (NULL chunk) dedupe here: NULLs never
        # conflict under the UNIQUE, so intra-page duplicates are filtered
        # and re-recording a page stays idempotent.
        seen_now: set[tuple[UUID, UUID | None, str]] = set()
        for hit in hits:
            key = (hit.recall_job_id, hit.parse_id, hit.chunk_id)
            if key in seen_now:
                continue
            seen_now.add(key)
            await self.conn.execute(
                recall_hits_insert_stmt(),
                {
                    "id": uuid4(),
                    "owner_id": self.owner_id,
                    "industry_id": industry_id,
                    "recall_job_id": hit.recall_job_id,
                    "parse_id": hit.parse_id,
                    "chunk_id": hit.chunk_id,
                    "channel": hit.channel,
                    "rank": hit.rank,
                    "score": hit.score,
                    "reason": hit.reason,
                },
            )
