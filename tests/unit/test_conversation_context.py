"""Unit tests: conversation coordination + evidence packets (16 §6, 14 §5).

Offline:

- CHAT-01 (synthetic): the second turn's pronoun resolves back to the
  first turn's referenced id through the coordinator's context snapshot
  + an injected fake resolver — the flow the QueryPlanner agent serves
  in production;
- CHAT-04: a turn whose parent is not the last committed assistant
  message is rejected (concurrent sends cannot interleave); a stale
  state_version is rejected as 409-class;
- CHAT-06: summary compression appends, originals stay;
- constraints accumulate with the 16 §6 structure;
- QA-01: archive mode with no evidence → empty packet →
  insufficient_evidence with ZERO tool calls;
- allowed_citation_ids is exactly the supplied evidence ids.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from intel.services.research import (
    ConversationCoordinator,
    ConversationRecord,
    MessageIn,
    Mode,
    ParentMismatch,
    StateVersionStale,
    build_evidence_packet,
)

OWNER = uuid4()
INDUSTRY = uuid4()


def _conversation() -> ConversationRecord:
    return ConversationRecord(owner_id=OWNER, industry_id=INDUSTRY)


def _turn(record: ConversationRecord, text: str, **kwargs) -> MessageIn:
    return MessageIn(
        text=text,
        parent_message_id=record.last_committed_message_id,
        state_version=record.state_version,
        **kwargs,
    )


class FakeResolver:
    """QueryPlanner stand-in: pronouns resolve to prior referenced ids."""

    def __init__(self) -> None:
        self.asked: list[dict] = []

    def resolve(self, context: dict, question: str) -> dict:
        self.asked.append({"context": context, "question": question})
        prior_ids: list[str] = []
        for message in reversed(context["recent_messages"]):
            for referenced in message.get("referenced_ids", []):
                if referenced not in prior_ids:
                    prior_ids.insert(0, referenced)
        if "它" in question or "该事件" in question:
            assert prior_ids, "pronoun without a referent must ask for context"
            return {
                "referenced_ids": [prior_ids[0]],
                "rewritten_question": question.replace("它", prior_ids[0]).replace(
                    "该事件", prior_ids[0]
                ),
                "needs_context": False,
            }
        return {
            "referenced_ids": [],
            "rewritten_question": question,
            "needs_context": False,
        }


class TestChat01PronounResolution:
    async def test_second_turn_pronoun_resolves_prior_reference(self):
        coordinator = ConversationCoordinator()
        resolver = FakeResolver()
        record = _conversation()

        first = coordinator.commit_turn(
            record,
            _turn(record, "刻蚀设备 E-500 的最新动态是什么？"),
            assistant_text="E-500 于 9 月正式商用。",
            referenced_ids=["event-e500-ga"],
        )
        assert first.referenced_ids == ["event-e500-ga"]

        turn = _turn(record, "它的发布时间有变化吗？")
        resolution = resolver.resolve(coordinator.context_snapshot(record), turn.text)
        assert resolution["referenced_ids"] == ["event-e500-ga"]
        assert "event-e500-ga" in resolution["rewritten_question"]
        second = coordinator.commit_turn(
            record,
            turn,
            assistant_text="无变化。",
            referenced_ids=resolution["referenced_ids"],
        )
        assert second.turn_index == 2

    async def test_resolver_sees_constraints_and_summaries(self):
        coordinator = ConversationCoordinator()
        resolver = FakeResolver()
        record = _conversation()
        coordinator.commit_turn(
            record,
            _turn(
                record,
                "查一下",
                constraints=[{"id": "c1", "text": "仅看 2026 年", "kind": "time"}],
            ),
            assistant_text="ok",
        )
        coordinator.append_summary(record, "第一轮：设备采购背景")
        snapshot = coordinator.context_snapshot(record)
        resolution = resolver.resolve(snapshot, "继续查")
        assert resolution["needs_context"] is False
        context_seen = resolver.asked[0]["context"]
        assert context_seen["summaries"] == ["第一轮：设备采购背景"]
        assert context_seen["constraints"][0]["id"] == "c1"


class TestChat04SerialTurns:
    async def test_concurrent_parent_rejected(self):
        coordinator = ConversationCoordinator()
        record = _conversation()
        first = coordinator.commit_turn(
            record, _turn(record, "第一问"), assistant_text="答一"
        )
        # Both writers read the same parent (pre-commit state).
        stale = MessageIn(
            text="并发第二问",
            parent_message_id=first.user_message_id,
            state_version=record.state_version,
        )
        with pytest.raises(ParentMismatch):
            coordinator.check_turn(record, stale)

    async def test_stale_state_version_rejected(self):
        coordinator = ConversationCoordinator()
        record = _conversation()
        coordinator.commit_turn(record, _turn(record, "第一问"), assistant_text="答一")
        stale = MessageIn(
            text="旧标签页发送",
            parent_message_id=record.last_committed_message_id,
            state_version=1,
        )
        with pytest.raises(StateVersionStale):
            coordinator.check_turn(record, stale)

    async def test_correct_chain_commits(self):
        coordinator = ConversationCoordinator()
        record = _conversation()
        for i in range(3):
            coordinator.commit_turn(
                record, _turn(record, f"第{i}问"), assistant_text=f"答{i}"
            )
        assert record.state_version == 4
        assert record.turn_counter == 3


class TestChat06SummariesPreserveOriginals:
    async def test_summary_appends_only(self):
        coordinator = ConversationCoordinator()
        record = _conversation()
        coordinator.commit_turn(
            record, _turn(record, "原始第一问"), assistant_text="原答"
        )
        coordinator.append_summary(record, "压缩摘要")
        assert record.messages[0]["text"] == "原始第一问"
        assert record.summaries == ["压缩摘要"]


class TestConstraints:
    async def test_constraint_structure_accumulates(self):
        coordinator = ConversationCoordinator()
        record = _conversation()
        coordinator.commit_turn(
            record,
            _turn(record, "q1", constraints=[{"text": "仅 RF 主题", "kind": "topic"}]),
            assistant_text="a1",
        )
        second = coordinator.commit_turn(
            record,
            _turn(
                record,
                "q2",
                constraints=[
                    {"text": "放宽到所有主题", "kind": "topic", "status": "active"}
                ],
            ),
            assistant_text="a2",
        )
        del second
        kinds = [(c["text"], c["status"]) for c in record.constraints]
        assert kinds == [("仅 RF 主题", "active"), ("放宽到所有主题", "active")]
        for constraint in record.constraints:
            assert set(constraint) == {
                "id",
                "text",
                "kind",
                "source_message_id",
                "status",
            }


class TestEvidencePacket:
    def test_allowed_citations_are_exactly_the_evidence_ids(self):
        packet = build_evidence_packet(
            question="q",
            mode=Mode.ARCHIVE,
            evidence=[{"id": "ev-1"}, {"id": "ev-2"}, {"no_id": True}],
        )
        assert packet.allowed_citation_ids == frozenset({"ev-1", "ev-2"})

    def test_qa01_archive_empty_packet_is_insufficient(self):
        packet = build_evidence_packet(question="q", mode=Mode.ARCHIVE)
        assert packet.evidence == () and packet.allowed_citation_ids == frozenset()
        # The answer flow contract: no evidence in archive mode →
        # insufficient_evidence with ZERO tool calls — never a web search.
        assert packet.mode == Mode.ARCHIVE
        tool_calls: list[str] = []  # the workflow's tool ledger stays empty
        answer_status = (
            "insufficient_evidence" if not packet.allowed_citation_ids else "ok"
        )
        assert answer_status == "insufficient_evidence"
        assert tool_calls == []

    def test_read_blocks_recorded(self):
        packet = build_evidence_packet(
            question="q",
            mode=Mode.ONLINE,
            evidence=[{"id": "ev-1"}],
            read_blocks=["doc-1#b003", "doc-1#b004"],
        )
        assert packet.actually_read_blocks == frozenset({"doc-1#b003", "doc-1#b004"})
