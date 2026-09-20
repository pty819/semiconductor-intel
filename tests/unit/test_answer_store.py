"""Unit tests: answer message persistence seam (16 §6/§7; final review).

Critical pair of behaviors pinned offline (statement-shape + lifecycle,
the test_knowledge_store_sql harness pattern — no real database):

- ``SqlAlchemyAnswerStore.insert_answer_message`` settles the in-flight
  user turn (pending → ready) in the SAME transaction, and maps the
  workflow status onto the MessageView status literal through the one
  shared mapping — a row like status="ok" would 500 every later GET of
  the conversation;
- begin_turn → insert_answer_message → a SECOND begin_turn succeeds (no
  ParentMismatch): the serial-turn guard no longer sees a pending turn;
- ``message_to_view`` on the persisted rows validates against
  MessageView (and rejects the unmapped workflow strings).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from intel.db.models.conversation import Conversation, Message
from intel.repositories.base import IndustryScope
from intel.repositories.conversations import (
    ASSISTANT_STATUS_TO_MESSAGE,
    assistant_message_status,
    message_to_view,
)
from intel.repositories.conversations import (
    SqlAlchemyConversationRepository as Repo,
)
from intel.workers.stores import SqlAlchemyAnswerStore

OWNER = uuid4()
INDUSTRY = uuid4()
SCOPE = IndustryScope(owner_id=OWNER, industry_id=INDUSTRY)


class _Scalars:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def first(self):
        return self._scalar

    def all(self):
        return [] if self._scalar is None else [self._scalar]


class FakeResult:
    def __init__(self, *, scalar=None, row=None) -> None:
        self._scalar = scalar
        self._row = row
        self.rowcount = 1

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row

    def scalars(self):
        return _Scalars(self._scalar)

    def all(self):
        return []

    def first(self):
        return None


class FakeAnswerConn:
    """Interprets the statements of the turn lifecycle over in-memory rows.

    RLS statements answer like test_knowledge_store_sql's FakeConn; the
    conversation/message DML is applied to real ORM instances kept in
    dicts, so the guard queries observe the writes.
    """

    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        self.messages: list[Message] = []
        self.statements: list[str] = []
        self.executed: list[tuple[str, dict]] = []

    def _sql(self, stmt) -> str:
        return str(stmt.compile(dialect=postgresql.dialect()))

    async def execute(self, stmt, params=None):
        compiled = stmt.compile(dialect=postgresql.dialect())
        text = str(compiled)
        bound = dict(compiled.params)
        self.statements.append(text)
        self.executed.append((text, bound))
        if text.startswith("SET LOCAL ROLE"):
            return FakeResult()
        if text.startswith("SELECT set_config"):
            return FakeResult()
        if text.startswith("SELECT current_setting"):
            return FakeResult(row=(str(OWNER),))
        if text.startswith("UPDATE messages"):
            # The settle write: pending user turn of this conversation.
            for row in self.messages:
                if (
                    row.conversation_id == self.conversation.id
                    and row.role == "user"
                    and row.status == "pending"
                ):
                    row.status = bound["status"]
            return FakeResult()
        if text.startswith("UPDATE conversations"):
            if "last_committed_message_id" in bound:
                self.conversation.last_committed_message_id = bound[
                    "last_committed_message_id"
                ]
            if "state_version" in bound:
                self.conversation.state_version = bound["state_version"]
            return FakeResult()
        if text.startswith("INSERT INTO messages"):
            values = bound
            self.messages.append(
                Message(
                    id=values["id"],
                    owner_id=values["owner_id"],
                    industry_id=values["industry_id"],
                    conversation_id=values["conversation_id"],
                    role=values["role"],
                    content=values["content"],
                    status=values["status"],
                    parent_message_id=values.get("parent_message_id"),
                    turn_index=values["turn_index"],
                    citation_manifest=values["citation_manifest"],
                )
            )
            return FakeResult()
        if "FROM conversations" in text:
            return FakeResult(scalar=self.conversation)
        if "count(*)" in text and "FROM messages" in text:
            # begin_turn's serial-slot guard: pending messages of this
            # conversation (status arrives as a bound parameter).
            pending = [
                m
                for m in self.messages
                if m.conversation_id == self.conversation.id and m.status == "pending"
            ]
            return FakeResult(scalar=len(pending))
        if "max(messages.turn_index)" in text:
            turns = [m.turn_index for m in self.messages]
            return FakeResult(scalar=max(turns) if turns else 0)
        raise AssertionError(f"unexpected statement: {text[:120]}")


def _conversation() -> Conversation:
    return Conversation(
        id=uuid4(),
        owner_id=OWNER,
        industry_id=INDUSTRY,
        title="t",
        state_version=1,
        last_committed_message_id=None,
    )


class TestStatusMapping:
    def test_mapping_covers_both_workflow_outcomes(self) -> None:
        assert ASSISTANT_STATUS_TO_MESSAGE == {
            "ok": "ready",
            "insufficient_evidence": "partial",
        }

    def test_unknown_status_raises_instead_of_persisting(self) -> None:
        with pytest.raises(ValueError, match="unknown answer workflow status"):
            assistant_message_status("ok-ish")


class TestInsertAnswerMessageSql:
    async def test_settles_pending_and_persists_mapped_literal(self) -> None:
        conn = FakeAnswerConn(_conversation())
        store = SqlAlchemyAnswerStore(conn, SCOPE)
        message_id = await store.insert_answer_message(
            SCOPE,
            conversation_id=conn.conversation.id,
            question="q",
            blocks=[{"text": "答", "citations": ["ev-1"]}],
            status="ok",
            referenced_ids=["ev-1"],
        )
        assert isinstance(message_id, UUID)
        joined = "\n".join(conn.statements)
        # The in-flight user turn settles (same transaction): pending →
        # ready, scoped to this conversation's user rows only.
        settles = [
            params
            for text, params in conn.executed
            if text.startswith("UPDATE messages")
        ]
        assert len(settles) == 1
        assert settles[0]["status"] == "ready"  # the SET value
        where_values = set(settles[0].values())
        assert "pending" in where_values  # WHERE status = pending
        assert "user" in where_values  # WHERE role = user
        assert "UPDATE messages" in joined
        assert "messages.role" in joined and "messages.status" in joined
        assistant = [m for m in conn.messages if m.role == "assistant"]
        assert len(assistant) == 1
        # The assistant row carries the MAPPED literal, never "ok".
        assert assistant[0].status == "ready"
        assert assistant[0].citation_manifest["referenced_ids"] == ["ev-1"]
        assert assistant[0].citation_manifest["question"] == "q"

    async def test_insufficient_evidence_maps_to_partial(self) -> None:
        conn = FakeAnswerConn(_conversation())
        store = SqlAlchemyAnswerStore(conn, SCOPE)
        await store.insert_answer_message(
            SCOPE,
            conversation_id=conn.conversation.id,
            question="q",
            blocks=[],
            status="insufficient_evidence",
        )
        assistant = [m for m in conn.messages if m.role == "assistant"]
        assert assistant[0].status == "partial"

    async def test_non_literal_status_is_rejected_before_any_write(self) -> None:
        conn = FakeAnswerConn(_conversation())
        store = SqlAlchemyAnswerStore(conn, SCOPE)
        with pytest.raises(ValueError, match="unknown answer workflow status"):
            await store.insert_answer_message(
                SCOPE,
                conversation_id=conn.conversation.id,
                question="q",
                blocks=[],
                status="definitely-not-a-status",
            )
        assert conn.messages == []  # nothing persisted
        assert "INSERT INTO messages" not in "\n".join(conn.statements)


class TestTurnLifecycle:
    async def test_second_begin_turn_succeeds_after_answer_persisted(self) -> None:
        conn = FakeAnswerConn(_conversation())
        repo = Repo(conn, SCOPE)
        store = SqlAlchemyAnswerStore(conn, SCOPE)
        conversation_id = conn.conversation.id

        first_user = await repo.begin_turn(
            conversation_id,
            text="第一问",
            parent_message_id=None,
            mode="archive",
            as_of=None,
            topic_ids=[],
        )
        assert any(m.id == first_user and m.status == "pending" for m in conn.messages)
        # While the turn is in flight, a second begin_turn is rejected.
        with pytest.raises(Exception, match="serial commit slot"):
            await repo.begin_turn(
                conversation_id,
                text="抢跑",
                parent_message_id=None,
                mode="archive",
                as_of=None,
                topic_ids=[],
            )

        assistant_id = await store.insert_answer_message(
            SCOPE,
            conversation_id=conversation_id,
            question="第一问",
            blocks=[{"text": "答一", "citations": []}],
            status="ok",
        )
        assert conn.conversation.last_committed_message_id == assistant_id
        assert any(m.id == first_user and m.status == "ready" for m in conn.messages)

        # The fix: the settled turn frees the serial slot, and the new
        # parent matches the committed assistant message.
        second_user = await repo.begin_turn(
            conversation_id,
            text="第二问",
            parent_message_id=assistant_id,
            mode="archive",
            as_of=None,
            topic_ids=[],
        )
        assert second_user != first_user

        # message_to_view on the persisted rows validates against the
        # MessageView contract (literal statuses only). The first user
        # turn settled to ready; the second is (correctly) pending.
        views = [message_to_view(row) for row in conn.messages]
        users = [view for view in views if view.role == "user"]
        assistants = [view for view in views if view.role == "assistant"]
        assert [view.status for view in users] == ["ready", "pending"]
        assert [view.status for view in assistants] == ["ready"]

    def test_message_to_view_rejects_non_literal_status(self) -> None:
        # Documents the failure mode the mapping exists to prevent: a
        # row persisted with the raw workflow status cannot even be
        # projected onto MessageView (GET messages would 500).
        from pydantic import ValidationError

        row = Message(
            id=uuid4(),
            owner_id=OWNER,
            industry_id=INDUSTRY,
            conversation_id=uuid4(),
            role="assistant",
            content="x",
            status="ok",
            turn_index=1,
        )
        with pytest.raises(ValidationError):
            message_to_view(row)
