"""Unit tests: answer workflow (QA-01 archive mode; citation fix loop)."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from intel.repositories.base import IndustryScope
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

    async def compose_answer(self, question, evidence_blocks):
        self.calls += 1
        return Draft(
            blocks=[
                {"text": "有据回答", "citations": ["ev-1"]},
                {"text": "外引回答", "citations": ["ev-77"]},
                {"text": "无引断言", "citations": []},
            ]
        )


@dataclass
class AnswerStoreDouble:
    messages: list[dict] = field(default_factory=list)

    async def insert_answer_message(
        self, scope, *, conversation_id, question, blocks, status
    ):
        message_id = uuid4()
        self.messages.append({"id": message_id, "status": status, "blocks": blocks})
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


async def _drive(wiring):
    runner, db, service = _run(wiring, None)
    message_id = uuid4()
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(OWNER, INDUSTRY),
        kind="archive_answer",
        payload={
            "question": "q",
            "conversation_id": str(uuid4()),
            "message_id": str(message_id),
        },
        idempotency_key=f"archive_answer:{INDUSTRY}/{message_id}",
    )
    return await runner.run_once()


def _wiring(store, agent, materials):
    async def packet_source(scope, question):
        return materials

    @asynccontextmanager
    async def open(scope):
        yield store

    return AnswerWiring(open_store=open, agent=agent, packet_source=packet_source)


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
        result = await _drive(_wiring(store, agent, EMPTY_MATERIALS))
        assert result.state == "succeeded"
        assert result.progress["status"] == "insufficient_evidence"
        assert result.progress["tool_calls"] == []  # QA-01: 零工具调用
        assert agent.calls == 0  # 且 LLM 根本没被调用
        assert store.messages[0]["status"] == "insufficient_evidence"

    async def test_citation_mismatch_repaired_by_deletion(self):
        agent = RecordingAgent()
        store = AnswerStoreDouble()
        result = await _drive(_wiring(store, agent, FULL_MATERIALS))
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
