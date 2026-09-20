"""Unit tests: answer workflow (QA-01 archive mode; citation fix loop;
CHAT-01 follow-up resolution on the production wiring)."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from intel.contracts import AnswerBlock, ConversationContext, MessageView
from intel.repositories.base import IndustryScope
from intel.repositories.conversations import assistant_message_status
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService
from intel.workers.runner import JobRunner
from intel.workflows.answer import AnswerWiring, make_answer_handler

OWNER = uuid4()
INDUSTRY = uuid4()


@dataclass
class Draft:
    blocks: list[dict] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)


class RecordingAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.questions: list[str] = []

    async def compose_answer(self, question, evidence_blocks):
        self.calls += 1
        self.questions.append(question)
        return Draft(
            blocks=[
                {"text": "有据回答", "citations": ["ev-1"]},
                {"text": "外引回答", "citations": ["ev-77"]},
                {"text": "无引断言", "citations": []},
            ]
        )


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


def _run(wiring, payload):
    db = InMemoryJobsDatabase()
    service = JobService(clock=lambda: datetime.now(UTC), rng=random.Random(1))

    @asynccontextmanager
    async def open_jobs(scope):
        yield InMemoryJobsStore(db)

    runner = JobRunner(service, open_jobs)
    runner.register("archive_answer", make_answer_handler(wiring))
    return runner, db, service


async def _drive(wiring, question: str = "q"):
    runner, db, service = _run(wiring, None)
    message_id = uuid4()
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(OWNER, INDUSTRY),
        kind="archive_answer",
        payload={
            "question": question,
            "conversation_id": str(uuid4()),
            "message_id": str(message_id),
        },
        idempotency_key=f"archive_answer:{INDUSTRY}/{message_id}",
    )
    return await runner.run_once()


def _wiring(store, agent, materials, *, query_planner=None, conversation_source=None):
    seen: list[str] = []

    async def packet_source(scope, question):
        seen.append(question)
        return materials

    @asynccontextmanager
    async def open(scope):
        yield store

    wiring = AnswerWiring(
        open_store=open,
        agent=agent,
        packet_source=packet_source,
        query_planner=query_planner,
        conversation_source=conversation_source,
    )
    return wiring, seen


EMPTY_MATERIALS: dict = {"claims": [], "events": [], "evidence": []}
FULL_MATERIALS = {
    "claims": [{"id": "cl-1"}],
    "events": [{"id": "ev-1"}],
    "evidence": [{"id": "ev-1", "exact_quote": "引文"}],
    "read_blocks": ["doc-1#b001"],
}


class TestQa01ArchiveMode:
    async def test_empty_packet_skips_llm_entirely(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        wiring, _ = _wiring(store, agent, EMPTY_MATERIALS)
        result = await _drive(wiring)
        assert result.state == "succeeded"
        assert result.progress["status"] == "insufficient_evidence"
        assert result.progress["tool_calls"] == []  # QA-01: 零工具调用
        assert agent.calls == 0  # 且 LLM 根本没被调用
        # The persisted message carries the MAPPED literal, never the
        # workflow status itself (GET messages must not 500).
        assert store.messages[0]["status"] == "partial"

    async def test_citation_mismatch_repaired_by_deletion(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        wiring, _ = _wiring(store, agent, FULL_MATERIALS)
        result = await _drive(wiring)
        assert result.state == "succeeded"
        assert result.progress["status"] == "ok"
        blocks = store.messages[0]["blocks"]
        # In-packet citation survives; out-of-packet and uncited drop.
        assert [b["text"] for b in blocks] == ["有据回答"]
        assert set(result.progress["dropped_citations"]) == {
            "无引断言",
            "外引回答",
        }
        assert agent.calls == 1
        assert store.messages[0]["status"] == "ready"


# -- CHAT-01: follow-up resolution on the production wiring ---------------------


@dataclass
class Resolution:
    referenced_ids: list[str] = field(default_factory=list)
    rewritten_question: str = ""
    needs_context: bool = False


class FakeQueryPlanner:
    """QueryPlanner stand-in: 它/该事件 resolve to the most recent
    citation the prior assistant turn actually made."""

    def __init__(self) -> None:
        self.contexts: list[ConversationContext] = []
        self.questions: list[str] = []

    async def resolve_followup(self, context, question):
        self.contexts.append(context)
        self.questions.append(question)
        prior: list[str] = []
        for message in reversed(context.recent_messages):
            for block in message.blocks:
                for citation in block.citation_ids:
                    if citation not in prior:
                        prior.insert(0, citation)
        if ("它" in question or "该事件" in question) and prior:
            referent = prior[0]
            return Resolution(
                referenced_ids=[referent],
                rewritten_question=question.replace("它", referent).replace(
                    "该事件", referent
                ),
            )
        return Resolution(rewritten_question=question)


def _context_with_prior_reference() -> ConversationContext:
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
    return ConversationContext(
        conversation_id=uuid4(),
        state_version=3,
        resolved_question="",
        recent_messages=[prior_turn],
        constraints=[],
        ordered_references=[],
        open_questions=[],
    )


class TestChat01PronounResolution:
    async def test_second_turn_pronoun_feeds_restated_question(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        planner = FakeQueryPlanner()
        context = _context_with_prior_reference()
        asked: list[UUID | None] = []

        async def conversation_source(scope, conversation_id, pending_message_id=None):
            asked.append(pending_message_id)
            return context

        wiring, seen = _wiring(
            store,
            agent,
            FULL_MATERIALS,
            query_planner=planner,
            conversation_source=conversation_source,
        )
        result = await _drive(wiring, question="它的发布时间有变化吗？")
        assert result.state == "succeeded"
        # The planner saw the pending turn excluded, and the raw question.
        assert asked[0] is not None
        assert planner.questions == ["它的发布时间有变化吗？"]
        assert planner.contexts[0].recent_messages[0].blocks[0].citation_ids == [
            "event-e500-ga"
        ]
        # The packet AND the agent see the RESTATED question.
        restated = "event-e500-ga的发布时间有变化吗？"
        assert seen == [restated]
        assert agent.questions == [restated]
        # The resolution's referenced ids persist with the answer (the
        # NEXT turn's planner resolves against them).
        assert store.messages[0]["referenced_ids"] == ["event-e500-ga"]
        assert store.messages[0]["question"] == restated
        assert result.progress["referenced_ids"] == ["event-e500-ga"]

    async def test_no_history_falls_back_to_raw_question(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        planner = FakeQueryPlanner()

        async def conversation_source(scope, conversation_id, pending_message_id=None):
            return ConversationContext(
                conversation_id=conversation_id,
                state_version=1,
                resolved_question="",
                recent_messages=[],
                constraints=[],
                ordered_references=[],
                open_questions=[],
            )

        wiring, seen = _wiring(
            store,
            agent,
            FULL_MATERIALS,
            query_planner=planner,
            conversation_source=conversation_source,
        )
        result = await _drive(wiring)
        assert result.state == "succeeded"
        assert planner.questions == []  # nothing to resolve against
        assert seen == ["q"]
        assert agent.questions == ["q"]
        assert store.messages[0]["referenced_ids"] == []
