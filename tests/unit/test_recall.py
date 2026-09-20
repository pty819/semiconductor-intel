"""Unit tests: RecallService four-channel orchestration (spec 04 §6 B).

Offline: a fake RecallStore feeds each channel's RawHits; the tests pin the
pure orchestration semantics:

- RRF k=60 math and multi-channel aggregation;
- union deduped BY DOCUMENT, one hit keeps every channel provenance;
- RRF ordering only — no threshold drops (REC-03: top-k 不删除，只定顺序);
- REC-01: a document whose single relevant paragraph matches one channel
  still lands in the page;
- REC-03: >1 page of candidates continues by cursor without duplicates;
- semantic channel skips cleanly when the query has no embedding (coverage
  gap, not an error — missing embedding never blocks, REC-04 shape);
- recall_hits rows recorded per channel provenance;
- RecallPage DTO shape (14 §2): hits/next_cursor/coverage/query_manifest.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from intel.retrieval.recall import (
    CHANNEL_ALIAS,
    CHANNEL_BM25,
    CHANNEL_PROXIMITY,
    CHANNEL_SEMANTIC,
    RECALL_BATCH_SIZE,
    RRF_K,
    RawHit,
    RecallCursor,
    RecallHitRecord,
    RecallQuery,
    RecallService,
    rrf_contrib,
)


def _hit(
    channel: str,
    doc: UUID,
    parse: UUID,
    *,
    chunk: UUID | None = None,
    score: float = 1.0,
    rank_word: str = "",
) -> RawHit:
    return RawHit(
        channel=channel,
        industry_document_id=doc,
        parse_id=parse,
        chunk_id=chunk if chunk is not None else uuid4(),
        block_ids=("b001",),
        score=score,
        reason=f"{channel} {rank_word}".strip(),
    )


class FakeRecallStore:
    def __init__(
        self,
        channels: dict[str, list[RawHit]] | None = None,
        *,
        embedding_pending: int = 0,
    ) -> None:
        self.channels = channels or {}
        self.embedding_pending = embedding_pending
        self.recorded: list[RecallHitRecord] = []
        self.asked: list[str] = []

    async def channel_hits(self, query: RecallQuery, channel: str) -> list[RawHit]:
        self.asked.append(channel)
        return list(self.channels.get(channel, ()))

    async def embedding_pending_documents(self, query: RecallQuery) -> int:
        return self.embedding_pending

    async def record_recall_hits(self, hits: list[RecallHitRecord]) -> None:
        self.recorded.extend(hits)


def _query(**kwargs) -> RecallQuery:
    defaults: dict = {
        "recall_job_id": uuid4(),
        "topic_id": uuid4(),
        "terms": ("刻蚀", "EUV"),
        "aliases": ("RTX 4090",),
        "query_embedding": None,
        "per_channel_k": 100,
    }
    defaults.update(kwargs)
    return RecallQuery(**defaults)


class TestRrfMath:
    def test_contribution_formula(self) -> None:
        assert rrf_contrib(0) == 1 / (RRF_K + 0)
        assert rrf_contrib(1) == 1 / (RRF_K + 1)
        assert RRF_K == 60

    def test_multi_channel_hits_rank_above_single_channel(self) -> None:
        d1, d2, d3 = uuid4(), uuid4(), uuid4()
        store = FakeRecallStore(
            {
                CHANNEL_BM25: [
                    _hit(CHANNEL_BM25, d1, uuid4(), score=9.9),
                    _hit(CHANNEL_BM25, d2, uuid4(), score=8.8),
                ],
                CHANNEL_ALIAS: [
                    _hit(CHANNEL_ALIAS, d2, uuid4(), score=0.5),
                    _hit(CHANNEL_ALIAS, d3, uuid4(), score=0.4),
                ],
            }
        )
        page = _run(store, _query())
        assert [h.industry_document_id for h in page.hits] == [d2, d1, d3]
        # d2 aggregates both channels: 1/60 + 1/61.
        expected = 1 / 60 + 1 / 61
        assert abs(page.hits[0].rrf_score - expected) < 1e-12


def _run(
    store: FakeRecallStore, query: RecallQuery, cursor: RecallCursor | None = None
):
    import asyncio

    service = RecallService()
    return asyncio.run(service.recall_page(store, query, cursor or RecallCursor()))


class TestUnionAndDedup:
    def test_dedup_by_document_keeps_channel_provenance(self) -> None:
        doc, parse = uuid4(), uuid4()
        store = FakeRecallStore(
            {
                CHANNEL_BM25: [
                    _hit(CHANNEL_BM25, doc, parse, chunk=uuid4()),
                    _hit(CHANNEL_BM25, doc, parse, chunk=uuid4()),
                ],
                CHANNEL_PROXIMITY: [_hit(CHANNEL_PROXIMITY, doc, parse)],
            }
        )
        page = _run(store, _query())
        assert len(page.hits) == 1
        hit = page.hits[0]
        assert hit.industry_document_id == doc
        assert {p.channel for p in hit.channels} == {CHANNEL_BM25, CHANNEL_PROXIMITY}
        assert len(hit.channels) == 3  # two chunks + one proximity row

    def test_rrf_never_drops_documents(self) -> None:
        # Scores only decide ORDER; every union'd document survives (REC-03:
        # 相似度只是候选信号 — no threshold deletion).
        docs = [uuid4() for _ in range(5)]
        hits = [_hit(CHANNEL_BM25, docs[i], uuid4(), score=0.001) for i in range(5)]
        store = FakeRecallStore({CHANNEL_BM25: hits})
        page = _run(store, _query())
        assert {h.industry_document_id for h in page.hits} == set(docs)


class TestRec01SingleRelevantParagraph:
    def test_document_with_one_relevant_block_is_hit(self) -> None:
        """REC-01: a long document where only one paragraph matches the
        topic terms still surfaces — the chunk-level channel matches the
        single relevant chunk and document dedup carries it into the page."""
        target_doc, target_parse, target_chunk = uuid4(), uuid4(), uuid4()
        others = [_hit(CHANNEL_BM25, uuid4(), uuid4()) for _ in range(3)]
        store = FakeRecallStore(
            {
                CHANNEL_BM25: [
                    *others,
                    _hit(
                        CHANNEL_BM25,
                        target_doc,
                        target_parse,
                        chunk=target_chunk,
                        score=-7.5,
                    ),
                ]
            }
        )
        page = _run(store, _query())
        assert target_doc in {h.industry_document_id for h in page.hits}
        hit = next(h for h in page.hits if h.industry_document_id == target_doc)
        provenance = next(p for p in hit.channels if p.channel == CHANNEL_BM25)
        assert provenance.parse_id == target_parse
        assert provenance.chunk_id == target_chunk
        assert provenance.block_ids == ("b001",)


class TestBatchingRec03:
    def _store_many(self, n: int) -> FakeRecallStore:
        return FakeRecallStore(
            {CHANNEL_BM25: [_hit(CHANNEL_BM25, uuid4(), uuid4()) for _ in range(n)]}
        )

    def test_pages_continue_by_cursor_without_duplicates(self) -> None:
        import asyncio

        store = self._store_many(250)
        service = RecallService()
        query = _query()
        assert service.batch_size == RECALL_BATCH_SIZE == 100

        page1 = asyncio.run(service.recall_page(store, query))
        assert len(page1.hits) == 100
        assert page1.next_cursor is not None

        page2 = asyncio.run(service.recall_page(store, query, page1.next_cursor))
        assert len(page2.hits) == 100
        assert page2.next_cursor is not None

        page3 = asyncio.run(service.recall_page(store, query, page2.next_cursor))
        assert len(page3.hits) == 50
        assert page3.next_cursor is None

        seen_docs = [
            h.industry_document_id for p in (page1, page2, page3) for h in p.hits
        ]
        assert len(seen_docs) == 250
        assert len(set(seen_docs)) == 250  # no document delivered twice

    def test_seen_documents_are_skipped_by_cursor(self) -> None:
        import asyncio

        store = self._store_many(10)
        service = RecallService()
        query = _query()
        page1 = asyncio.run(service.recall_page(store, query))
        # Mark the first page's docs as processed; a fresh cursor at offset 0
        # must skip them and return only the remainder.
        cursor = RecallCursor(
            offset=0, seen=tuple(h.industry_document_id for h in page1.hits)
        )
        page2 = asyncio.run(service.recall_page(store, query, cursor))
        assert len(page2.hits) == 0
        assert page2.next_cursor is None


class TestSemanticChannelGap:
    def test_missing_embedding_skips_channel_and_records_gap(self) -> None:
        store = FakeRecallStore(
            {CHANNEL_BM25: [_hit(CHANNEL_BM25, uuid4(), uuid4())]},
            embedding_pending=7,
        )
        page = _run(store, _query(query_embedding=None))
        assert CHANNEL_SEMANTIC not in store.asked
        assert page.coverage["semantic"] == "skipped_no_embedding"
        assert page.coverage["embedding_pending_documents"] == 7
        assert len(page.hits) == 1  # not an error, the doc pool is usable

    def test_embedding_present_runs_semantic_channel(self) -> None:
        store = FakeRecallStore(
            {CHANNEL_SEMANTIC: [_hit(CHANNEL_SEMANTIC, uuid4(), uuid4(), score=0.87)]}
        )
        page = _run(store, _query(query_embedding=(0.1, 0.2, 0.3)))
        assert CHANNEL_SEMANTIC in store.asked
        assert page.coverage["channels"][CHANNEL_SEMANTIC] == 1


class TestProvenanceRecording:
    def test_recall_hits_recorded_per_channel(self) -> None:
        doc, parse, chunk = uuid4(), uuid4(), uuid4()
        store = FakeRecallStore(
            {
                CHANNEL_BM25: [_hit(CHANNEL_BM25, doc, parse, chunk=chunk)],
                CHANNEL_ALIAS: [_hit(CHANNEL_ALIAS, doc, parse, chunk=chunk)],
            }
        )
        query = _query()
        page = _run(store, query)
        assert len(page.hits) == 1
        assert len(store.recorded) == 2
        for record in store.recorded:
            assert record.recall_job_id == query.recall_job_id
            assert record.parse_id == parse
            assert record.chunk_id == chunk
            assert record.channel in (CHANNEL_BM25, CHANNEL_ALIAS)
            assert record.rank is not None and record.rank >= 1


class TestRecallPageShape:
    def test_page_carries_coverage_and_query_manifest(self) -> None:
        store = FakeRecallStore(
            {
                CHANNEL_BM25: [_hit(CHANNEL_BM25, uuid4(), uuid4())],
                CHANNEL_ALIAS: [_hit(CHANNEL_ALIAS, uuid4(), uuid4())],
                CHANNEL_PROXIMITY: [_hit(CHANNEL_PROXIMITY, uuid4(), uuid4())],
            }
        )
        page = _run(store, _query())
        assert page.next_cursor is None
        assert page.coverage["union_documents"] == 3
        assert page.coverage["page_size"] == 3
        assert page.coverage["remaining"] == 0
        manifest = page.query_manifest
        assert manifest["terms"] == ["刻蚀", "EUV"]
        assert manifest["bm25_query"] == "刻蚀 EUV"
        assert manifest["aliases"] == ["RTX 4090"]
        assert manifest["rrf_k"] == RRF_K
        assert manifest["per_channel_k"] == 100
        assert set(manifest["channels"]) == {
            CHANNEL_BM25,
            CHANNEL_ALIAS,
            CHANNEL_PROXIMITY,
        }
