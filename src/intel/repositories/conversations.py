"""Conversation + message repository (16 §7, 08 §2)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.contracts import AnswerBlock, Citation, ConversationView, MessageView
from intel.db.models.conversation import Conversation, Message
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.services.research import ParentMismatch


def _block_kind(raw: object, *, role: str) -> str:
    if raw in ("fact", "inference", "unknown"):
        return str(raw)
    return "unknown" if role == "user" else "fact"


def _citations_from_manifest(manifest: dict) -> list[Citation]:
    citations: list[Citation] = []
    for item in manifest.get("citations") or []:
        if not isinstance(item, dict):
            continue
        try:
            citations.append(Citation.model_validate(item))
        except (TypeError, ValueError):
            continue
    return citations


#: Workflow outcome → messages.status literal (doc 16 §7): ONE mapping,
#: shared by every writer (SQL store and test doubles alike) so no string
#: outside the MessageView literal ever reaches the messages table — a row
#: like status="ok" would 500 every later GET of the conversation.
ASSISTANT_STATUS_TO_MESSAGE: dict[str, str] = {
    "ok": "ready",
    "insufficient_evidence": "partial",
}


def assistant_message_status(workflow_status: str) -> str:
    """Map an answer workflow outcome onto the MessageView status literal."""
    try:
        return ASSISTANT_STATUS_TO_MESSAGE[workflow_status]
    except KeyError:
        raise ValueError(
            f"unknown answer workflow status {workflow_status!r}; expected one"
            f" of {sorted(ASSISTANT_STATUS_TO_MESSAGE)}"
        ) from None


def message_to_view(row) -> MessageView:
    """Map a messages row (content + citation_manifest) onto MessageView."""
    manifest = dict(getattr(row, "citation_manifest", None) or {})
    raw_blocks = manifest.get("blocks")
    blocks: list[AnswerBlock] = []
    if isinstance(raw_blocks, list):
        for item in raw_blocks:
            if not isinstance(item, dict):
                continue
            citations = item.get("citation_ids") or item.get("citations") or []
            blocks.append(
                AnswerBlock(
                    text=str(item.get("text") or ""),
                    kind=_block_kind(item.get("kind"), role=str(row.role)),  # type: ignore[arg-type]
                    citation_ids=[str(cid) for cid in citations],
                )
            )
    if not blocks and getattr(row, "content", None):
        blocks = [
            AnswerBlock(
                text=str(row.content),
                kind=_block_kind(None, role=str(row.role)),  # type: ignore[arg-type]
            )
        ]
    return MessageView(
        id=row.id,
        parent_message_id=row.parent_message_id,
        turn_index=row.turn_index,
        role=row.role,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        blocks=blocks,
        citations=_citations_from_manifest(manifest),
        as_of=row.as_of,
        job_id=row.job_id,
    )


class ConversationRepository:
    """Protocol surface the conversation routes consume."""


class SqlAlchemyConversationRepository(ScopedRepository, ConversationRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def list_conversations(self) -> list[ConversationView]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                select(Conversation)
                .where(
                    Conversation.owner_id == self.owner_id,
                    Conversation.industry_id == industry_id,
                )
                .order_by(Conversation.created_at.desc())
            )
        ).scalars()
        return [self._view(row) for row in rows]

    async def create_conversation(self, title: str) -> ConversationView:
        industry_id = await self._bind()
        conversation_id = uuid4()
        await self.conn.execute(
            pg_insert(Conversation).values(
                id=conversation_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                title=title,
                state_version=1,
            )
        )
        return ConversationView(
            id=conversation_id,
            title=title,
            industry_id=industry_id,
            state_version=1,
            last_committed_message_id=None,
        )

    async def get_conversation(self, conversation_id: UUID) -> ConversationView | None:
        industry_id = await self._bind()
        row = (
            (
                await self.conn.execute(
                    select(Conversation).where(
                        Conversation.id == conversation_id,
                        Conversation.owner_id == self.owner_id,
                        Conversation.industry_id == industry_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        return None if row is None else self._view(row)

    async def list_messages(self, conversation_id: UUID) -> list[MessageView]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                select(Message)
                .where(
                    Message.owner_id == self.owner_id,
                    Message.industry_id == industry_id,
                    Message.conversation_id == conversation_id,
                )
                .order_by(Message.turn_index.asc(), Message.created_at.asc())
            )
        ).scalars()
        return [self._message_view(row) for row in rows]

    async def begin_turn(
        self,
        conversation_id: UUID,
        *,
        text: str,
        parent_message_id: UUID | None,
        mode: str,
        as_of: datetime | None,
        topic_ids: list[UUID],
    ) -> UUID:
        industry_id = await self._bind()
        stmt = (
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.owner_id == self.owner_id,
                Conversation.industry_id == industry_id,
            )
            .with_for_update()
        )
        conversation = (await self.conn.execute(stmt)).scalars().first()
        if conversation is None:
            raise ParentMismatch("conversation not found")
        pending = (
            await self.conn.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.status == "pending",
                )
            )
        ).scalar_one()
        if pending:
            raise ParentMismatch("another turn holds the serial commit slot")
        if parent_message_id != conversation.last_committed_message_id:
            raise ParentMismatch(
                f"parent_message_id {parent_message_id} is not the last"
                " committed assistant message"
                f" ({conversation.last_committed_message_id})"
            )
        turn_index = (
            int(
                (
                    await self.conn.execute(
                        select(func.coalesce(func.max(Message.turn_index), 0)).where(
                            Message.conversation_id == conversation_id
                        )
                    )
                ).scalar_one()
            )
            + 1
        )
        message_id = uuid4()
        await self.conn.execute(
            pg_insert(Message).values(
                id=message_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                conversation_id=conversation_id,
                role="user",
                content=text,
                status="pending",
                parent_message_id=parent_message_id,
                turn_index=turn_index,
                as_of=as_of,
                citation_manifest={
                    "mode": mode,
                    "topic_ids": [str(t) for t in topic_ids],
                },
            )
        )
        return message_id

    def _view(self, row: Conversation) -> ConversationView:
        return ConversationView(
            id=row.id,
            title=row.title,
            industry_id=row.industry_id,
            state_version=max(int(row.state_version or 1), 1),
            last_committed_message_id=row.last_committed_message_id,
        )

    def _message_view(self, row: Message) -> MessageView:
        return message_to_view(row)
