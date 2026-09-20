"""Index workflow handler (kind=index): chunks + embeddings + manifest.

04 §5 / 14 §2 IndexManifest. The handler re-reads the parse's stored
blocks, runs the pure chunker, embeds via the :class:`EmbeddingClient`
protocol and persists chunk rows in ONE short transaction per commit —
the embedding call happens OUTSIDE any transaction (07 §2: no network
across transactions; the wired client in Task 11 is a network LLM call).

Embedding seam (Task 11 wires the L1 route client):

- :class:`EmbeddingClient` is the protocol;
- :class:`NoopEmbeddingClient` is the default: every vector is None, rows
  persist with NULL ``embedding``/``embedding_model_version``, the
  manifest and job coverage say ``pending`` — REC-04: 缺 embedding 不妨碍
  归档, the gap is a coverage fact (the recall service surfaces it via the
  embedding-gap count), never an error.

Idempotency: chunk inserts conflict-do-nothing on UNIQUE(parse, ordinal);
the job's idempotency key already carries chunker/embedding versions.
Documented v1 limitation: a NEW chunker_version re-running over an
already-chunked parse conflicts on (parse, ordinal) and keeps the OLD
rows — chunk-row replacement belongs to the index_generation rebuild
flow (03 §8), not to this handler.

Coverage: :meth:`IndexStore.bump_coverage_watermark` advances matching
running ``coverage_batches`` rows (phase='index', window contains now);
no batches → a no-op. Task 12+ owns batch creation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import bindparam, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from intel.db.models.jobs import CoverageBatch
from intel.db.models.pool import Chunk, ParsedArtifact
from intel.db.rls import require_owner_guc, set_app_role, set_scope
from intel.parsing.dto import Block
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.repositories.pool import ParsedArtifactRecord
from intel.retrieval.chunker import CHUNKER_VERSION, chunk_blocks
from intel.workers.runner import JobFailure, JobHandler, RunContext

#: Version recorded for parses indexed while the L1 embedding client is
#: not wired yet (Task 11 replaces the default wiring).
NOOP_EMBEDDING_VERSION = "embedding@noop-1"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# embedding seam
# --------------------------------------------------------------------------


class EmbeddingClient(Protocol):
    """Embedding provider seam (Task 11: L1 route client over unifiedllm)."""

    @property
    def version(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float] | None]:
        """One vector per text, best-effort: None marks a failed item
        (its chunk persists with NULL embedding — REC-04)."""
        ...  # pragma: no cover - protocol


class NoopEmbeddingClient:
    """Leaves every embedding NULL; coverage records the pending gap."""

    version: str = NOOP_EMBEDDING_VERSION

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float] | None]:
        return [None for _ in texts]


# --------------------------------------------------------------------------
# records + manifest
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ChunkRecord:
    """One ``chunks`` row payload (03 §3)."""

    owner_id: UUID
    parsed_artifact_id: UUID
    ordinal: int
    block_ids: list[str]
    text: str
    language: str
    normalized_terms: str
    embedding: Sequence[float] | None = None
    embedding_model_version: str | None = None
    id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class IndexManifest:
    """14 §2 IndexManifest — 缺任意 chunk 不标 index 完整."""

    parse_id: UUID
    chunker_version: str
    embedding_version: str
    chunk_count: int
    completed_ordinals: tuple[int, ...]
    embedding_status: str  # "ok" | "pending"

    def to_progress(self) -> dict[str, object]:
        return {
            "parse_id": str(self.parse_id),
            "chunker_version": self.chunker_version,
            "embedding_version": self.embedding_version,
            "chunk_count": self.chunk_count,
            "completed_ordinals": list(self.completed_ordinals),
            "embedding_status": self.embedding_status,
        }


def chunks_insert_stmt():
    """Idempotent chunk insert; conflict target UNIQUE(parse, ordinal).

    ``embedding`` arrives as the vector literal string ('[0.1,...]') — PG
    coerces the unknown-typed parameter to the column's vector type, so
    the statement works through any driver without the pgvector codec.
    """
    return (
        pg_insert(Chunk)
        .values(
            id=bindparam("id"),
            owner_id=bindparam("owner_id"),
            parsed_artifact_id=bindparam("parsed_artifact_id"),
            ordinal=bindparam("ordinal"),
            block_ids=bindparam("block_ids"),
            chunk_text=bindparam("text"),
            language=bindparam("language"),
            normalized_terms=bindparam("normalized_terms"),
            embedding=bindparam("embedding"),
            embedding_model_version=bindparam("embedding_model_version"),
        )
        .on_conflict_do_nothing(index_elements=["parsed_artifact_id", "ordinal"])
        .returning(Chunk.id)
    )


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


@runtime_checkable
class IndexStore(Protocol):
    """What the index handler needs from storage."""

    async def get_parse(self, parse_id: UUID) -> ParsedArtifactRecord | None: ...

    async def insert_chunks(
        self, records: Sequence[ChunkRecord]
    ) -> list[tuple[int, bool]]:
        """Persist rows; returns (ordinal, inserted?) per record — False
        means the UNIQUE(parse, ordinal) row already existed (idempotent
        re-run), which still counts as complete."""
        ...

    async def bump_coverage_watermark(self, *, phase: str, now: datetime) -> int: ...


OpenIndexTxn = Callable[[IndustryScope], AbstractAsyncContextManager[IndexStore]]


def _parse_record_from_row(row: ParsedArtifact) -> ParsedArtifactRecord:
    return ParsedArtifactRecord(
        id=row.id,
        owner_id=row.owner_id,
        capture_id=row.capture_id,
        parser_version_id=row.parser_version_id,
        normalized_blob_id=row.normalized_blob_id,
        text_hash=row.text_hash,
        blocks=list(row.blocks or []),
        artifact_metadata=dict(row.artifact_metadata or {}),
        parse_status=row.parse_status,
        coverage=dict(row.coverage or {}),
        quality_flags=list(row.quality_flags or []),
        parsed_at=row.parsed_at,
    )


def _vector_literal(embedding: Sequence[float] | None) -> str | None:
    """Vector literal string ('[0.1,0.2]'); None stays NULL (REC-04)."""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


class SqlAlchemyIndexStore(ScopedRepository, IndexStore):
    """IndexStore over one AsyncConnection, owner GUC bound (chunks is O)."""

    async def _bind_owner(self) -> None:
        await set_scope(self.conn, self.scope.owner_id, self.scope.industry_id)
        await require_owner_guc(self.conn)

    async def get_parse(self, parse_id: UUID) -> ParsedArtifactRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(ParsedArtifact).where(
                    ParsedArtifact.owner_id == self.owner_id,
                    ParsedArtifact.id == parse_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _parse_record_from_row(row)

    async def insert_chunks(
        self, records: Sequence[ChunkRecord]
    ) -> list[tuple[int, bool]]:
        await self._bind_owner()
        results: list[tuple[int, bool]] = []
        for record in records:
            inserted = (
                await self.conn.execute(
                    chunks_insert_stmt(),
                    {
                        "id": record.id,
                        "owner_id": record.owner_id,
                        "parsed_artifact_id": record.parsed_artifact_id,
                        "ordinal": record.ordinal,
                        "block_ids": list(record.block_ids),
                        "text": record.text,
                        "language": record.language,
                        "normalized_terms": record.normalized_terms,
                        "embedding": _vector_literal(record.embedding),
                        "embedding_model_version": record.embedding_model_version,
                    },
                )
            ).scalar_one_or_none()
            results.append((record.ordinal, inserted is not None))
        return results

    async def bump_coverage_watermark(self, *, phase: str, now: datetime) -> int:
        await self._bind_owner()
        result = await self.conn.execute(
            update(CoverageBatch)
            .where(
                CoverageBatch.owner_id == self.owner_id,
                CoverageBatch.phase == phase,
                CoverageBatch.status == "running",
                CoverageBatch.window_start <= now,
                CoverageBatch.window_end > now,
            )
            .values(
                watermark=now,
                done_count=CoverageBatch.done_count + 1,
            )
        )
        return result.rowcount


def sql_index_txn_factory(engine) -> OpenIndexTxn:
    """Production wiring: one connection/transaction per open."""

    @asynccontextmanager
    async def open_index(scope: IndustryScope) -> AsyncIterator[IndexStore]:
        async with engine.connect() as conn, conn.begin():
            await set_app_role(conn)
            yield SqlAlchemyIndexStore(conn, scope)

    return open_index


# --------------------------------------------------------------------------
# handler
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IndexWiring:
    """Everything the index handler needs, injected by the composition
    root (Task 11 wires the L1 embedding client over the seam)."""

    open_store: OpenIndexTxn
    embedder: EmbeddingClient = field(default_factory=NoopEmbeddingClient)
    clock: Callable[[], datetime] = field(default_factory=_utcnow)


def register_index_handlers(runner, wiring: IndexWiring) -> None:
    runner.register("index", make_index_handler(wiring))


def make_index_handler(wiring: IndexWiring) -> JobHandler:
    """``index`` kind: blocks → chunks → embeddings → one commit."""

    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        parse_id = UUID(str(payload["parse_id"]))

        async with wiring.open_store(ctx.scope) as store:
            parse = await store.get_parse(parse_id)
        if parse is None:
            raise JobFailure("parse_missing", f"parse {parse_id} gone")

        # Pure CPU: no transaction held across the chunker.
        blocks = [Block(**block) for block in parse.blocks]
        chunks = chunk_blocks(blocks)
        await ctx.boundary()

        # Embedding call outside any transaction (07 §2); failed items are
        # None vectors, never job failures (REC-04).
        vectors = await wiring.embedder.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):  # pragma: no cover - client bug
            raise JobFailure(
                "embedding_count_mismatch",
                f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks",
            )

        records = [
            ChunkRecord(
                owner_id=ctx.job.owner_id,
                parsed_artifact_id=parse_id,
                ordinal=chunk.ordinal,
                block_ids=list(chunk.block_ids),
                text=chunk.text,
                language=chunk.language,
                normalized_terms=chunk.normalized_terms,
                embedding=(vector if vector is not None else None),
                embedding_model_version=(
                    wiring.embedder.version if vector is not None else None
                ),
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        async with wiring.open_store(ctx.scope) as store:
            outcomes = await store.insert_chunks(records)
            await store.bump_coverage_watermark(phase="index", now=wiring.clock())

        embedding_status = (
            "ok"
            if all(record.embedding is not None for record in records)
            else "pending"
        )
        manifest = IndexManifest(
            parse_id=parse_id,
            chunker_version=str(payload.get("chunker_version", CHUNKER_VERSION)),
            embedding_version=wiring.embedder.version,
            chunk_count=len(records),
            completed_ordinals=tuple(ordinal for ordinal, _present in outcomes),
            embedding_status=embedding_status,
        )
        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "manifest": manifest.to_progress(),
                    "chunks": len(records),
                    "inserted": sum(1 for _, created in outcomes if created),
                    "embedding_status": embedding_status,
                },
            )

    return handler
