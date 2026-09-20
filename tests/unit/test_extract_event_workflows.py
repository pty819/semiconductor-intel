"""Unit tests: route/extract/event_build workflow handlers (Task 12 glue).

Fake-agent, in-memory-store runs through the real JobRunner — the same
harness pattern as test_indexer.py. The handlers are thin glue over the
already-tested domain/service layers; these tests pin the glue:

- route: direct → extract job enqueued; uncertain → recorded, nothing
  spawned; missing parse → parse_missing failure class;
- extract: mixed proposal keeps good claims and preserves rejection
  reasons (never rewrites quotes); all-rejected still succeeds with
  extraction_failed;
- event_build: same-key repost links into the existing event (EVT-01
  end-to-end through the handler).
"""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from intel.contracts.models import (
    ClaimProposal,
    EvidenceProposal,
    ExtractionInput,
    ExtractionProposal,
    SourceBlock,
)
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService, build_idempotency_key
from intel.services.knowledge import InMemoryKnowledgeStore
from intel.workers.runner import JobRunner
from intel.workflows.event_build import (
    EventBuildWiring,
    make_event_build_handler,
)
from intel.workflows.extract import (
    EVENT_POLICY_VERSION,
    ExtractWiring,
    make_extract_handler,
)
from intel.workflows.route import RouteWiring, make_route_handler

OWNER = uuid4()
INDUSTRY_A = uuid4()
INDUSTRY_B = uuid4()


@dataclass
class Digest:
    doc_kind: str = "announcement"
    summary: str = "采购协议"


@dataclass
class Verdict:
    decision: str
    reasons: list[str] = field(default_factory=list)
    block_references: list = field(default_factory=list)


class FakeRoutingAgent:
    def __init__(self, per_industry: dict[UUID, Verdict]) -> None:
        self.per_industry = per_industry

    async def describe_document(self, blocks: list[str]) -> Digest:
        return Digest()

    async def judge_industry(self, digest: Digest, profile: str) -> Verdict:
        # Industries are distinguished by the profile text the store emits.
        for industry_id, verdict in self.per_industry.items():
            if str(industry_id) in profile:
                return verdict
        return Verdict("unrelated")


@dataclass
class RouteStoreDouble:
    jobs_db: InMemoryJobsDatabase
    blocks: dict[UUID, list[str]]
    industries: list[dict]
    decisions: list[dict] = field(default_factory=list)

    @property
    def jobs(self):
        return InMemoryJobsStore(self.jobs_db)

    async def get_blocks(self, parse_id: UUID) -> list[str] | None:
        return self.blocks.get(parse_id)

    async def active_industries(self, owner_id: UUID) -> list[dict]:
        return self.industries

    async def insert_processing_decision(
        self, scope, *, parse_id, outcome, reasons, block_references, input_manifest
    ) -> UUID:
        decision_id = uuid4()
        self.decisions.append(
            {
                "id": decision_id,
                "scope": scope,
                "parse_id": parse_id,
                "outcome": outcome,
                "reasons": reasons,
            }
        )
        return decision_id


def _runner(kind: str, handler, wiring_open):
    db = InMemoryJobsDatabase()
    service = JobService(
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        rng=random.Random(1),
    )

    @asynccontextmanager
    async def open_jobs(scope):
        yield InMemoryJobsStore(db)

    runner = JobRunner(service, open_jobs)
    runner.register(kind, handler)
    return runner, db, service


class TestRouteHandler:
    def _wiring(self, store: RouteStoreDouble, agent):
        @asynccontextmanager
        async def open(scope):
            yield store

        return RouteWiring(
            open_store=open,
            agent=agent,
            jobs=JobService(
                clock=lambda: __import__("datetime").datetime.now(
                    __import__("datetime").UTC
                )
            ),
        )

    async def test_direct_spawns_uncertain_queues_unrelated_records(self):
        parse_id = uuid4()
        store = RouteStoreDouble(
            jobs_db=InMemoryJobsDatabase(),
            blocks={parse_id: ["块文本"]},
            industries=[
                {"industry_id": INDUSTRY_A, "profile": f"profile {INDUSTRY_A}"},
                {"industry_id": INDUSTRY_B, "profile": f"profile {INDUSTRY_B}"},
            ],
        )
        agent = FakeRoutingAgent(
            {
                INDUSTRY_A: Verdict("direct", reasons=["半导体设备采购"]),
                INDUSTRY_B: Verdict("uncertain", reasons=["疑似相关"]),
            }
        )
        wiring = self._wiring(store, agent)
        runner, db, service = _runner(
            "route", make_route_handler(wiring), wiring.open_store
        )
        _job, _created = await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER),
            kind="route",
            payload={"parse_id": str(parse_id)},
            idempotency_key=f"route:{parse_id}",
        )
        result = await runner.run_once()
        assert result is not None and result.state == "succeeded"
        assert result.progress["industries_judged"] == 2
        assert len(result.progress["extract_jobs"]) == 1
        outcomes = {d["outcome"] for d in store.decisions}
        assert outcomes == {"direct", "uncertain"}
        # uncertain 进待判断入口: recorded, nothing spawned for it.
        spawned = store.jobs_db.jobs[UUID(result.progress["extract_jobs"][0])]
        assert UUID(spawned["input"]["industry_id"]) == INDUSTRY_A

    async def test_missing_parse_fails_with_class(self):
        store = RouteStoreDouble(
            jobs_db=InMemoryJobsDatabase(), blocks={}, industries=[]
        )
        wiring = self._wiring(store, FakeRoutingAgent({}))
        runner, db, service = _runner(
            "route", make_route_handler(wiring), wiring.open_store
        )
        ghost = uuid4()
        await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER),
            kind="route",
            payload={"parse_id": str(ghost)},
            idempotency_key=f"route:{ghost}",
        )
        result = await runner.run_once()
        assert result is not None and result.state == "failed"
        assert result.error["code"] == "parse_missing"


BLOCKS = [
    SourceBlock(
        block_id="b001",
        text="官方公告：公司与台积电签署设备采购协议，价值 5 亿美元。",
    )
]


class FakeExtractAgent:
    def __init__(self, proposal: ExtractionProposal) -> None:
        self.proposal = proposal

    async def extract_claims(self, request: ExtractionInput) -> ExtractionProposal:
        return self.proposal


@dataclass
class ExtractStoreDouble:
    knowledge: InMemoryKnowledgeStore
    blocks: dict[UUID, list[SourceBlock]]
    jobs_db: InMemoryJobsDatabase = field(default_factory=InMemoryJobsDatabase)

    @property
    def jobs(self):
        return InMemoryJobsStore(self.jobs_db)

    async def get_source_blocks(self, parse_id):
        return self.blocks.get(parse_id)

    async def get_retrieval_scope(self, parse_id):
        return "fulltext"


GOOD_CLAIM = ClaimProposal(
    text="公司与台积电签署设备采购协议，价值 5 亿美元",
    kind="source_statement",
    attribution="公司官方",
    evidence=[
        EvidenceProposal(
            block_id="b001",
            exact_quote="公司与台积电签署设备采购协议，价值 5 亿美元",
            relation="supports",
        )
    ],
)
BAD_CLAIM = ClaimProposal(
    text="伪造主张",
    kind="inference",
    evidence=[
        EvidenceProposal(
            block_id="b001", exact_quote="不存在的引文内容", relation="supports"
        )
    ],
)


class TestExtractHandler:
    def _wiring(self, store: ExtractStoreDouble, agent):
        @asynccontextmanager
        async def open(scope):
            yield store

        return ExtractWiring(
            open_store=open,
            agent=agent,
            jobs=JobService(
                clock=lambda: __import__("datetime").datetime.now(
                    __import__("datetime").UTC
                )
            ),
        )

    async def test_mixed_proposal_keeps_good_preserves_rejections(self):
        parse_id = uuid4()
        store = ExtractStoreDouble(
            knowledge=InMemoryKnowledgeStore(), blocks={parse_id: BLOCKS}
        )
        wiring = self._wiring(
            store, FakeExtractAgent(ExtractionProposal(claims=[GOOD_CLAIM, BAD_CLAIM]))
        )
        runner, db, service = _runner(
            "extract", make_extract_handler(wiring), wiring.open_store
        )
        await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER, INDUSTRY_A),
            kind="extract",
            payload={"parse_id": str(parse_id), "industry_id": str(INDUSTRY_A)},
            idempotency_key=f"extract:{parse_id}",
        )
        result = await runner.run_once()
        assert result is not None and result.state == "succeeded"
        assert result.progress["state"] == "ok"
        assert result.progress["accepted"] == 1
        assert result.progress["rejected"] == 1
        assert result.progress["rejections"][0]["code"] == "quote_not_found"
        # The good claim landed with verified offsets.
        assert len(store.knowledge.claim_revisions) == 1
        evidence = next(iter(store.knowledge.evidence.values()))
        assert evidence["start_char"] == 5  # after 官方公告：
        assert evidence["semantic_support_status"] == "pending"
        # Committed claims chain: the extraction run's event_build job was
        # spawned (same commit), keyed from KIND_SPECS.
        run_id = UUID(str(result.progress["extraction_run_id"]))
        spawned = [j for j in store.jobs_db.jobs.values() if j["kind"] == "event_build"]
        assert len(spawned) == 1
        assert spawned[0]["state"] == "queued"
        assert spawned[0]["input"] == {"extraction_run_id": str(run_id)}
        assert spawned[0]["idempotency_key"] == build_idempotency_key(
            "event_build",
            {
                "industry": str(INDUSTRY_A),
                "extraction_commit": str(run_id),
                "event_policy_version": EVENT_POLICY_VERSION,
            },
        )
        assert result.progress["event_build_job"] == str(spawned[0]["id"])

    async def test_all_rejected_still_succeeds_as_data(self):
        parse_id = uuid4()
        store = ExtractStoreDouble(
            knowledge=InMemoryKnowledgeStore(), blocks={parse_id: BLOCKS}
        )
        wiring = self._wiring(
            store, FakeExtractAgent(ExtractionProposal(claims=[BAD_CLAIM]))
        )
        runner, db, service = _runner(
            "extract", make_extract_handler(wiring), wiring.open_store
        )
        await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER, INDUSTRY_A),
            kind="extract",
            payload={"parse_id": str(parse_id)},
            idempotency_key=f"extract:{parse_id}",
        )
        result = await runner.run_once()
        assert result is not None and result.state == "succeeded"
        assert result.progress["state"] == "extraction_failed"
        assert not store.knowledge.claim_revisions
        # Nothing committed → no event_build spawn (it would find no
        # claims and fail extraction_missing).
        assert [
            j for j in store.jobs_db.jobs.values() if j["kind"] == "event_build"
        ] == []
        assert result.progress["event_build_job"] is None


@dataclass
class _EventProposal:
    event_type: str
    title: str
    claim_ids: list[str] = field(default_factory=list)
    identity_fields: dict = field(default_factory=dict)


@dataclass
class _EventProposals:
    proposals: list[_EventProposal]


class FakeEventAgent:
    def __init__(self, proposals: _EventProposals) -> None:
        self.proposals = proposals

    async def propose_events(self, request, claims) -> _EventProposals:
        return self.proposals


@dataclass
class EventBuildStoreDouble:
    knowledge: InMemoryKnowledgeStore
    claims_by_run: dict[UUID, list[dict]]

    async def get_extraction_claims(self, run_id):
        return self.claims_by_run.get(run_id)


class TestEventBuildHandler:
    async def test_same_key_repost_links_existing_event(self):
        knowledge = InMemoryKnowledgeStore()
        run_id = uuid4()
        store = EventBuildStoreDouble(
            knowledge=knowledge, claims_by_run={run_id: [{"id": str(uuid4())}]}
        )

        @asynccontextmanager
        async def open(scope):
            yield store

        identity = {
            "announcement_ref": "https://corp.example.com/pr/1",
            "counterparty": "台积电",
            "action": "设备采购协议",
        }
        wiring = EventBuildWiring(
            open_store=open,
            agent=FakeEventAgent(
                _EventProposals(
                    [
                        _EventProposal(
                            "commercial_announcement",
                            "采购协议",
                            identity_fields=identity,
                        ),
                        _EventProposal(
                            "commercial_announcement",
                            "采购协议（转载）",
                            identity_fields=identity,
                        ),
                    ]
                )
            ),
        )
        runner, db, service = _runner(
            "event_build", make_event_build_handler(wiring), open
        )
        await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER, INDUSTRY_A),
            kind="event_build",
            payload={"extraction_run_id": str(run_id)},
            idempotency_key=f"event_build:{run_id}",
        )
        result = await runner.run_once()
        assert result is not None and result.state == "succeeded"
        assert result.progress["events_created"] == 1
        assert result.progress["events_linked"] == 1
        assert len(knowledge.events) == 1

    async def test_missing_extraction_fails_with_class(self):
        store = EventBuildStoreDouble(
            knowledge=InMemoryKnowledgeStore(), claims_by_run={}
        )

        @asynccontextmanager
        async def open(scope):
            yield store

        wiring = EventBuildWiring(
            open_store=open, agent=FakeEventAgent(_EventProposals([]))
        )
        runner, db, service = _runner(
            "event_build", make_event_build_handler(wiring), open
        )
        ghost = uuid4()
        await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER, INDUSTRY_A),
            kind="event_build",
            payload={"extraction_run_id": str(ghost)},
            idempotency_key=f"event_build:{ghost}",
        )
        result = await runner.run_once()
        assert result is not None and result.state == "failed"
        assert result.error["code"] == "extraction_missing"
