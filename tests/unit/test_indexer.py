"""Unit tests: index workflow handler (kind=index) against fakes (04 §5).

The handler re-reads the parse's blocks, chunks them, embeds via the
EmbeddingClient protocol and persists chunk rows idempotently:

- REC-04: NoopEmbeddingClient leaves ``embedding`` NULL and the manifest /
  coverage mark embedding pending — 缺 embedding 不妨碍归档 (the job still
  succeeds; the gap is recorded, never raised);
- chunk rows carry block_ids/normalized_terms/language + UNIQUE(parse,
  ordinal) idempotency (on_conflict_do_nothing);
- a real (fake) embedding client stores vectors + version and reports ok;
- IndexManifest (14 §2) records completed ordinals — 缺任何 chunk 不标完整;
- coverage_batches watermark bump is wired (no batches → no-op);
- ingest.py's spawn constants now point at the real chunker/embedding
  versions (Task 8 left placeholders for this task).
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from intel.parsing.dto import Block
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.repositories.pool import ParsedArtifactRecord
from intel.retrieval.chunker import CHUNKER_VERSION
from intel.retrieval.indexer import (
    NOOP_EMBEDDING_VERSION,
    ChunkRecord,
    IndexManifest,
    IndexStore,
    IndexWiring,
    NoopEmbeddingClient,
    OpenIndexTxn,
    register_index_handlers,
)
from intel.services.jobs import JobService
from intel.workers.runner import JobRunner

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OWNER = uuid4()


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class InMemoryIndexStore:
    """IndexStore double: chunk rows keyed by UNIQUE(parse, ordinal)."""

    def __init__(self, parses: dict[UUID, ParsedArtifactRecord]) -> None:
        self.parses = parses
        self.rows: dict[UUID, dict] = {}
        self.by_parse_ordinal: dict[tuple[UUID, int], UUID] = {}
        self.watermark_phases: list[str] = []
        self.coverage_batches: list[dict] = []

    async def get_parse(self, parse_id: UUID) -> ParsedArtifactRecord | None:
        return self.parses.get(parse_id)

    async def insert_chunks(self, records: list[ChunkRecord]) -> list[tuple[int, bool]]:
        out: list[tuple[int, bool]] = []
        for record in records:
            key = (record.parsed_artifact_id, record.ordinal)
            if key in self.by_parse_ordinal:
                out.append((record.ordinal, False))
                continue
            self.by_parse_ordinal[key] = record.id
            self.rows[record.id] = {
                "id": record.id,
                "owner_id": record.owner_id,
                "parsed_artifact_id": record.parsed_artifact_id,
                "ordinal": record.ordinal,
                "block_ids": list(record.block_ids),
                "text": record.text,
                "language": record.language,
                "normalized_terms": record.normalized_terms,
                "embedding": record.embedding,
                "embedding_model_version": record.embedding_model_version,
            }
            out.append((record.ordinal, True))
        return out

    async def bump_coverage_watermark(self, *, phase: str, now: datetime) -> int:
        self.watermark_phases.append(phase)
        matched = 0
        for batch in self.coverage_batches:
            if (
                batch["phase"] == phase
                and batch["status"] == "running"
                and batch["window_start"] <= now < batch["window_end"]
            ):
                batch["watermark"] = now
                batch["done_count"] += 1
                matched += 1
        return matched


def _parse_record(n_blocks: int = 8) -> ParsedArtifactRecord:
    blocks = [
        Block(
            block_id=f"b{i:03d}",
            kind="heading" if i == 0 else "paragraph",
            text=f"section word{i} " + "filler " * 300,
        ).model_dump()
        for i in range(n_blocks)
    ]
    return ParsedArtifactRecord(
        owner_id=OWNER,
        capture_id=uuid4(),
        parser_version_id=uuid4(),
        normalized_blob_id=uuid4(),
        text_hash="h" * 64,
        blocks=blocks,
        artifact_metadata={},
        parse_status="ok",
        coverage={},
        parsed_at=T0,
    )


def make_runner(
    index_store: InMemoryIndexStore, *, embedder=None
) -> tuple[JobRunner, InMemoryJobsDatabase, JobService]:
    db = InMemoryJobsDatabase()
    clock = FakeClock()
    service = JobService(clock=clock, rng=random.Random(3))

    @asynccontextmanager
    async def open_store(
        scope: IndustryScope | None,
    ) -> AsyncIterator[InMemoryJobsStore]:
        yield InMemoryJobsStore(db)

    wiring = IndexWiring(
        open_store=_open_index(index_store),
        embedder=embedder or NoopEmbeddingClient(),
        clock=clock,
    )
    runner = JobRunner(service, open_store)
    register_index_handlers(runner, wiring)
    return runner, db, service


def _open_index(store: InMemoryIndexStore) -> OpenIndexTxn:
    """Factory of the shape IndexWiring expects: scope → context manager
    (passing an already-invoked @asynccontextmanager object would make
    ``wiring.open_store(scope)`` return CM.__call__'s wrapper function)."""

    @asynccontextmanager
    async def _open(
        scope: IndustryScope | None,
    ) -> AsyncIterator[InMemoryIndexStore]:
        yield store

    return _open


async def _enqueue_index(
    service: JobService,
    db: InMemoryJobsDatabase,
    parse_id: UUID,
    *,
    key_suffix: str = "",
) -> UUID:
    job, created = await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(owner_id=OWNER),
        kind="index",
        payload={
            "parse_id": str(parse_id),
            "chunker_version": CHUNKER_VERSION,
            "embedding_version": NOOP_EMBEDDING_VERSION,
        },
        idempotency_key=f"index:{OWNER}/{parse_id}/{CHUNKER_VERSION}"
        f"/{NOOP_EMBEDDING_VERSION}{key_suffix}",
    )
    assert created
    return job.id


class TestNoopEmbeddingClient:
    async def test_returns_none_vector_for_every_text(self) -> None:
        client = NoopEmbeddingClient()
        assert client.version == NOOP_EMBEDDING_VERSION
        vectors = await client.embed(["a", "b", "c"])
        assert vectors == [None, None, None]


class TestIndexHandler:
    async def test_noop_embedding_leaves_null_and_marks_pending(
        self,
    ) -> None:
        """REC-04: missing embeddings never block archiving — rows persist
        with NULL vectors, the manifest and coverage say pending, job ok."""
        parse = _parse_record()
        store = InMemoryIndexStore({parse.id: parse})
        runner, db, service = make_runner(store)

        await _enqueue_index(service, db, parse.id)
        job = await runner.run_once()

        assert job is not None and job.state == "succeeded"
        assert store.rows
        for row in store.rows.values():
            assert row["embedding"] is None
            assert row["embedding_model_version"] is None
            assert row["block_ids"]
            assert row["normalized_terms"]
        progress = job.progress
        assert progress["embedding_status"] == "pending"
        manifest = progress["manifest"]
        assert manifest["embedding_status"] == "pending"
        assert manifest["chunker_version"] == CHUNKER_VERSION
        assert manifest["embedding_version"] == NOOP_EMBEDDING_VERSION
        assert manifest["parse_id"] == str(parse.id)
        assert manifest["chunk_count"] == len(manifest["completed_ordinals"])
        assert manifest["completed_ordinals"] == sorted(manifest["completed_ordinals"])

    async def test_chunks_carry_block_references_and_terms(self) -> None:
        parse = _parse_record()
        store = InMemoryIndexStore({parse.id: parse})
        runner, db, service = make_runner(store)
        await _enqueue_index(service, db, parse.id)
        job = await runner.run_once()
        assert job is not None and job.state == "succeeded"

        ids = {b["block_id"] for b in parse.blocks}
        for row in store.rows.values():
            assert set(row["block_ids"]) <= ids
            assert row["owner_id"] == OWNER
            assert row["parsed_artifact_id"] == parse.id
        covered = {bid for r in store.rows.values() for bid in r["block_ids"]}
        assert covered == ids

    async def test_rerun_is_idempotent(self) -> None:
        parse = _parse_record()
        store = InMemoryIndexStore({parse.id: parse})
        runner, db, service = make_runner(store)
        await _enqueue_index(service, db, parse.id)
        first = await runner.run_once()
        assert first is not None and first.state == "succeeded"
        rows_after_first = dict(store.rows)

        # A duplicate index job for the same parse/version inserts nothing
        # new (handler-level idempotency on UNIQUE(parse, ordinal)).
        await _enqueue_index(service, db, parse.id, key_suffix=":rerun")
        second = await runner.run_once()
        assert second is not None and second.state == "succeeded"
        assert store.rows == rows_after_first
        assert (
            second.progress["manifest"]["completed_ordinals"]
            == (first.progress["manifest"]["completed_ordinals"])
        )

    async def test_missing_parse_fails_with_clear_class(self) -> None:
        store = InMemoryIndexStore({})
        runner, db, service = make_runner(store)
        ghost = uuid4()
        await _enqueue_index(service, db, ghost)
        job = await runner.run_once()
        assert job is not None and job.state == "failed"
        assert job.error is not None
        assert job.error["code"] == "parse_missing"

    async def test_real_embedder_stores_vectors(self) -> None:
        class FakeEmbedder:
            version = "fake-embed@1"

            async def embed(self, texts):
                return [[0.1, 0.2, 0.3] for _ in texts]

        parse = _parse_record()
        store = InMemoryIndexStore({parse.id: parse})
        runner, db, service = make_runner(store, embedder=FakeEmbedder())
        await _enqueue_index(service, db, parse.id)
        job = await runner.run_once()
        assert job is not None and job.state == "succeeded"
        assert job.progress["embedding_status"] == "ok"
        for row in store.rows.values():
            assert row["embedding"] == [0.1, 0.2, 0.3]
            assert row["embedding_model_version"] == "fake-embed@1"

    async def test_coverage_watermark_bumped(self) -> None:
        parse = _parse_record()
        store = InMemoryIndexStore({parse.id: parse})
        store.coverage_batches.append(
            {
                "phase": "index",
                "status": "running",
                "window_start": T0 - timedelta(hours=1),
                "window_end": T0 + timedelta(hours=1),
                "watermark": None,
                "done_count": 0,
            }
        )
        runner, db, service = make_runner(store)
        await _enqueue_index(service, db, parse.id)
        job = await runner.run_once()
        assert job is not None and job.state == "succeeded"
        assert store.watermark_phases == ["index"]
        assert store.coverage_batches[0]["done_count"] == 1
        assert store.coverage_batches[0]["watermark"] is not None


class TestIndexManifest:
    def test_manifest_is_serializable_progress_payload(self) -> None:
        parse_id = uuid4()
        manifest = IndexManifest(
            parse_id=parse_id,
            chunker_version=CHUNKER_VERSION,
            embedding_version=NOOP_EMBEDDING_VERSION,
            chunk_count=3,
            completed_ordinals=(0, 1, 2),
            embedding_status="pending",
        )
        payload = manifest.to_progress()
        assert payload["parse_id"] == str(parse_id)
        assert payload["completed_ordinals"] == [0, 1, 2]
        assert payload["chunk_count"] == 3
        assert payload["embedding_status"] == "pending"


class TestIngestSpawnConstants:
    def test_placeholders_replaced_with_real_versions(self) -> None:
        from intel.workflows.ingest import (
            INDEX_CHUNKER_VERSION,
            INDEX_EMBEDDING_VERSION,
        )

        assert INDEX_CHUNKER_VERSION == CHUNKER_VERSION
        assert INDEX_EMBEDDING_VERSION == NOOP_EMBEDDING_VERSION


def test_index_store_protocol_shape() -> None:
    # The in-memory double implements the IndexStore protocol the handler
    # consumes (static check that the protocol is structurally satisfiable).
    assert isinstance(InMemoryIndexStore({}), IndexStore)
