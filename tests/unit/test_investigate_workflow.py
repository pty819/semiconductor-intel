"""Unit tests: investigate workflow (kind=investigate; QA-01 archive gate;
CHAT-01 follow-up resolution on the production wiring)."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from intel.contracts import AnswerBlock, ConversationContext, MessageView
from intel.repositories.base import IndustryScope
from intel.repositories.conversations import assistant_message_status
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService
from intel.workers.runner import JobRunner
from intel.workflows.investigate import InvestigateWiring, make_investigate_handler

OWNER = uuid4()
INDUSTRY = uuid4()


@dataclass
class Report:
    findings: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


class RecordingAgent:
    def __init__(self, gateway=None) -> None:
        self.gateway = gateway
        self.calls = 0
        self.questions: list[str] = []
        self.tool_calls: list[str] = []

    def attach_gateway(self, gateway) -> None:
        self.gateway = gateway

    async def investigate(self, question: str) -> Report:
        self.calls += 1
        self.questions.append(question)
        if self.gateway is not None and hasattr(self.gateway, "search_web"):
            try:
                await self.gateway.search_web("probe")
                self.tool_calls.append("search_web")
            except RuntimeError:
                pass
        return Report(findings="ok", evidence_ids=["ev-1"])


@dataclass
class AnswerStoreDouble:
    """In-memory double of the answer persistence seam — maps the workflow
    status through the SAME shared mapping the SQL store uses, so the
    seam stays honest on both sides."""

    messages: list[dict] = field(default_factory=list)

    async def insert_answer_message(
        self,
        scope,
        *,
        conversation_id,
        question,
        blocks,
        status,
        referenced_ids=None,
    ):
        message_id = uuid4()
        self.messages.append(
            {
                "id": message_id,
                "status": assistant_message_status(status),
                "blocks": blocks,
                "question": question,
                "referenced_ids": list(referenced_ids or []),
            }
        )
        return message_id


class FakeGateway:
    def __init__(self, *, online: bool) -> None:
        self.online = online
        self.actions = {"search_archive", "read_evidence", "read_document_blocks"}
        if online:
            self.actions |= {"fetch_public", "search_web"}

    async def search_web(self, query: str, limit: int = 5):
        if "search_web" not in self.actions:
            raise RuntimeError("search_web not granted")
        return [{"url": "https://example.com"}]


def _drive(wiring, *, online: bool, message_id=None):
    db = InMemoryJobsDatabase()
    service = JobService(clock=lambda: datetime.now(UTC), rng=random.Random(1))

    @asynccontextmanager
    async def open_jobs(scope):
        yield InMemoryJobsStore(db)

    runner = JobRunner(service, open_jobs)
    runner.register("investigate", make_investigate_handler(wiring))
    message_id = message_id or uuid4()
    return runner, db, service, message_id


async def _run(wiring, *, online: bool):
    runner, db, service, message_id = _drive(wiring, online=online)
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(OWNER, INDUSTRY),
        kind="investigate",
        payload={
            "industry": str(INDUSTRY),
            "message_id": str(message_id),
            "request_version": "1",
            "conversation_id": str(uuid4()),
            "question": "q",
            "online": online,
        },
        idempotency_key=f"investigate:{INDUSTRY}/{message_id}/1",
    )
    return await runner.run_once()


def _wiring(
    store,
    agent,
    materials,
    *,
    gateway_factory=None,
    query_planner=None,
    conversation_source=None,
):
    async def packet_source(scope, question):
        return materials

    @asynccontextmanager
    async def open(scope):
        yield store

    return InvestigateWiring(
        open_store=open,
        agent=agent,
        packet_source=packet_source,
        gateway_factory=gateway_factory,
        query_planner=query_planner,
        conversation_source=conversation_source,
    )


EMPTY = {"claims": [], "events": [], "evidence": []}
FULL = {
    "claims": [{"id": "cl-1"}],
    "events": [{"id": "ev-1"}],
    "evidence": [{"id": "ev-1", "exact_quote": "引文"}],
    "read_blocks": ["doc-1#b001"],
}


class TestInvestigateArchiveQa01:
    async def test_archive_empty_packet_skips_llm_and_search_web(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        result = await _run(_wiring(store, agent, EMPTY), online=False)
        assert result.state == "succeeded"
        assert result.progress["status"] == "insufficient_evidence"
        assert result.progress["tool_calls"] == []
        assert agent.calls == 0
        assert agent.tool_calls == []
        # The persisted message carries the MAPPED literal, never the
        # workflow status itself (GET messages must not 500).
        assert store.messages[0]["status"] == "partial"

    async def test_payload_ids_are_strings(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        result = await _run(_wiring(store, agent, EMPTY), online=False)
        assert isinstance(result.input["message_id"], str)
        assert isinstance(result.input["industry"], str)
        assert isinstance(result.input["request_version"], str)


class TestInvestigateOnline:
    async def test_online_grants_search_web_and_invokes_agent(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        factories: list[bool] = []

        def factory(*, online: bool, **kwargs):
            factories.append(online)
            return FakeGateway(online=online)

        result = await _run(
            _wiring(store, agent, EMPTY, gateway_factory=factory), online=True
        )
        assert result.state == "succeeded"
        assert agent.calls == 1
        assert factories == [True]
        assert "search_web" in agent.tool_calls
        assert store.messages[0]["status"] == "ready"

    async def test_archive_with_evidence_does_not_grant_search_web(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()

        def factory(*, online: bool, **kwargs):
            return FakeGateway(online=online)

        result = await _run(
            _wiring(store, agent, FULL, gateway_factory=factory), online=False
        )
        assert result.state == "succeeded"
        assert agent.calls == 1
        assert "search_web" not in agent.tool_calls
        assert result.progress["tool_calls"] == []
        assert store.messages[0]["status"] == "ready"


# -- CHAT-01: follow-up resolution on the production wiring ---------------------


@dataclass
class Resolution:
    referenced_ids: list[str] = field(default_factory=list)
    rewritten_question: str = ""


class FakeQueryPlanner:
    """QueryPlanner stand-in: pronouns resolve to the prior turn's id."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def resolve_followup(self, context, question):
        self.questions.append(question)
        prior: list[str] = []
        for message in reversed(context.recent_messages):
            for block in message.blocks:
                for citation in block.citation_ids:
                    if citation not in prior:
                        prior.insert(0, citation)
        if "它" in question and prior:
            return Resolution(
                referenced_ids=[prior[0]],
                rewritten_question=question.replace("它", prior[0]),
            )
        return Resolution(rewritten_question=question)


class TestChat01PronounResolution:
    async def test_archive_turn_feeds_agent_the_restated_question(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        planner = FakeQueryPlanner()
        prior_turn = MessageView(
            id=uuid4(),
            parent_message_id=None,
            turn_index=1,
            role="assistant",
            status="ready",
            blocks=[
                AnswerBlock(
                    text="E-500 于 9 月正式商用。",
                    kind="fact",
                    citation_ids=["event-e500-ga"],
                )
            ],
            citations=[],
        )

        async def conversation_source(scope, conversation_id, pending_message_id=None):
            return ConversationContext(
                conversation_id=conversation_id,
                state_version=3,
                resolved_question="",
                recent_messages=[prior_turn],
                constraints=[],
                ordered_references=[],
                open_questions=[],
            )

        wiring = _wiring(
            store,
            agent,
            FULL,
            query_planner=planner,
            conversation_source=conversation_source,
        )
        # _run posts "q"; drive with a pronoun question instead.
        db = InMemoryJobsDatabase()
        service = JobService(clock=lambda: datetime.now(UTC), rng=random.Random(1))

        @asynccontextmanager
        async def open_jobs(scope):
            yield InMemoryJobsStore(db)

        runner = JobRunner(service, open_jobs)
        runner.register("investigate", make_investigate_handler(wiring))
        message_id = uuid4()
        await service.enqueue(
            InMemoryJobsStore(db),
            IndustryScope(OWNER, INDUSTRY),
            kind="investigate",
            payload={
                "industry": str(INDUSTRY),
                "message_id": str(message_id),
                "request_version": "1",
                "conversation_id": str(uuid4()),
                "question": "它的发布时间有变化吗？",
                "online": False,
            },
            idempotency_key=f"investigate:{INDUSTRY}/{message_id}/1",
        )
        result = await runner.run_once()
        assert result.state == "succeeded"
        restated = "event-e500-ga的发布时间有变化吗？"
        assert agent.questions == [restated]
        assert store.messages[0]["question"] == restated
        assert store.messages[0]["referenced_ids"] == ["event-e500-ga"]
        assert result.progress["referenced_ids"] == ["event-e500-ga"]
