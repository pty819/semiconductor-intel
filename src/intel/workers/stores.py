"""SQLAlchemy adapters the composition root injects into kind handlers.

Each opener is one short transaction (07 §2): SET LOCAL ROLE intel_app,
bind RLS GUCs, yield the store, commit. LLM/network stay outside.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from intel.contracts.models import ConversationContext, SourceBlock
from intel.db.models.conversation import Conversation, Message
from intel.db.models.knowledge import ClaimRevision, Event, EventRevision, Evidence
from intel.db.models.pool import Capture, ParsedArtifact, ProcessingDecision
from intel.db.models.workspace import Industry, IndustryRevision
from intel.db.rls import require_owner_guc, set_app_role, set_scope
from intel.repositories.base import IndustryScope
from intel.repositories.conversations import (
    assistant_message_status,
    message_to_view,
)
from intel.repositories.jobs import SqlAlchemyJobsStore
from intel.repositories.knowledge import SqlAlchemyKnowledgeStore
from intel.repositories.reports import SqlAlchemyReportRepository
from intel.repositories.reviews import SqlAlchemyReviewStore
from intel.services.reports import ReportComposition


async def bind_app(conn: AsyncConnection, scope: IndustryScope) -> None:
    """App role + RLS GUCs for one worker-role transaction."""
    await set_app_role(conn)
    await set_scope(conn, scope.owner_id, scope.industry_id)
    await require_owner_guc(conn)


def _block_kind(raw: object) -> str:
    if raw in ("paragraph", "heading", "table", "caption"):
        return str(raw)
    return "paragraph"


def _source_blocks(raw: list[dict[str, Any]] | None) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("block_id") or item.get("id") or "")
        text = str(item.get("text") or "")
        if not block_id or not text:
            continue
        page = item.get("page")
        blocks.append(
            SourceBlock(
                block_id=block_id,
                text=text,
                kind=_block_kind(item.get("kind")),  # type: ignore[arg-type]
                page=int(page) if isinstance(page, int) and page >= 1 else None,
            )
        )
    return blocks


class SqlAlchemyRouteStore:
    """RouteStore over one worker-role connection."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        self.conn = conn
        self.scope = scope
        self.jobs = SqlAlchemyJobsStore(conn, scope)

    async def get_blocks(self, parse_id: UUID) -> list[str] | None:
        await bind_app(self.conn, self.scope)
        row = (
            await self.conn.execute(
                select(ParsedArtifact).where(
                    ParsedArtifact.owner_id == self.scope.owner_id,
                    ParsedArtifact.id == parse_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return [block.text for block in _source_blocks(list(row.blocks or []))]

    async def active_industries(self, owner_id: UUID) -> Sequence[dict]:
        await bind_app(self.conn, IndustryScope(owner_id))
        rows = (
            await self.conn.execute(
                select(Industry, IndustryRevision)
                .outerjoin(
                    IndustryRevision,
                    Industry.current_revision_id == IndustryRevision.id,
                )
                .where(
                    Industry.owner_id == owner_id,
                    Industry.status == "active",
                    Industry.deleted_at.is_(None),
                )
            )
        ).all()
        out: list[dict] = []
        for industry, revision in rows:
            description = revision.description if revision is not None else ""
            profile = revision.profile if revision is not None else None
            out.append(
                {
                    "industry_id": str(industry.id),
                    "profile": (
                        f"{industry.id} {industry.name} {description} {profile or ''}"
                    ),
                    "current_revision_id": (
                        str(industry.current_revision_id)
                        if industry.current_revision_id
                        else None
                    ),
                }
            )
        return out

    async def insert_processing_decision(
        self,
        scope: IndustryScope,
        *,
        parse_id: UUID,
        outcome: str,
        reasons: list[str],
        block_references: list[dict],
        input_manifest: dict,
    ) -> UUID:
        await bind_app(self.conn, scope)
        industry_id = scope.require_industry_id()
        revision_id = (
            await self.conn.execute(
                select(Industry.current_revision_id).where(
                    Industry.owner_id == scope.owner_id,
                    Industry.id == industry_id,
                )
            )
        ).scalar_one_or_none()
        if revision_id is None:
            revision_id = (
                await self.conn.execute(
                    select(IndustryRevision.id)
                    .where(
                        IndustryRevision.owner_id == scope.owner_id,
                        IndustryRevision.industry_id == industry_id,
                    )
                    .order_by(IndustryRevision.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if revision_id is None:
            raise ValueError(
                f"industry {industry_id} has no revision to decide against"
            )
        decision_id = uuid4()
        allowed = {"direct", "background", "uncertain", "unrelated"}
        stored_outcome = outcome if outcome in allowed else "uncertain"
        await self.conn.execute(
            pg_insert(ProcessingDecision).values(
                id=decision_id,
                owner_id=scope.owner_id,
                parse_id=parse_id,
                industry_id=industry_id,
                industry_revision_id=revision_id,
                outcome=stored_outcome,
                reasons=list(reasons),
                candidate_claims={
                    "block_references": block_references,
                    "input_manifest": input_manifest,
                    "raw_outcome": outcome,
                },
            )
        )
        return decision_id


class SqlAlchemyExtractStore:
    """ExtractStore: parse blocks + KnowledgeStore on one transaction."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        self.conn = conn
        self.scope = scope
        self.knowledge = SqlAlchemyKnowledgeStore(conn, scope)
        # Same-transaction job queue handle (the RouteStore pattern): the
        # event_build spawn rides the extraction commit itself.
        self.jobs = SqlAlchemyJobsStore(conn, scope)

    async def get_source_blocks(self, parse_id: UUID) -> list[SourceBlock] | None:
        await bind_app(self.conn, self.scope)
        row = (
            await self.conn.execute(
                select(ParsedArtifact).where(
                    ParsedArtifact.owner_id == self.scope.owner_id,
                    ParsedArtifact.id == parse_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _source_blocks(list(row.blocks or []))

    async def get_retrieval_scope(self, parse_id: UUID) -> str:
        await bind_app(self.conn, self.scope)
        row = (
            await self.conn.execute(
                select(Capture.retrieval_scope)
                .select_from(ParsedArtifact)
                .join(Capture, Capture.id == ParsedArtifact.capture_id)
                .where(
                    ParsedArtifact.owner_id == self.scope.owner_id,
                    ParsedArtifact.id == parse_id,
                )
            )
        ).scalar_one_or_none()
        return str(row or "fulltext")

    async def get_origin_ref(self, parse_id: UUID) -> str | None:
        """The parse's document canonical URL — the origin source
        families dedupe on (EVT-01): reposts of one announcement share
        the family regardless of which industry extracted them."""
        await bind_app(self.conn, self.scope)
        from intel.db.models.pool import Document

        row = (
            await self.conn.execute(
                select(Document.canonical_url)
                .select_from(ParsedArtifact)
                .join(Capture, Capture.id == ParsedArtifact.capture_id)
                .join(Document, Document.id == Capture.document_id)
                .where(
                    ParsedArtifact.owner_id == self.scope.owner_id,
                    ParsedArtifact.id == parse_id,
                )
            )
        ).scalar_one_or_none()
        return row or None


class SqlAlchemyEventBuildStore:
    """EventBuildStore: claims for one extraction run + KnowledgeStore."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        self.conn = conn
        self.scope = scope
        self.knowledge = SqlAlchemyKnowledgeStore(conn, scope)

    async def get_extraction_claims(
        self, extraction_run_id: UUID
    ) -> Sequence[dict] | None:
        await bind_app(self.conn, self.scope)
        industry_id = self.scope.require_industry_id()
        rows = (
            await self.conn.execute(
                select(ClaimRevision)
                .join(
                    Evidence,
                    Evidence.claim_revision_id == ClaimRevision.id,
                )
                .where(
                    Evidence.owner_id == self.scope.owner_id,
                    Evidence.industry_id == industry_id,
                    Evidence.extraction_run_id == extraction_run_id,
                )
                .distinct()
            )
        ).scalars()
        claims = [
            {
                "id": str(row.id),
                "claim_id": str(row.claim_id),
                "text": row.text,
                "kind": row.kind,
                "predicate": row.predicate,
                "object": row.object_value,
            }
            for row in rows
        ]
        return claims or None


class SqlAlchemyAnswerStore:
    """AnswerStore: persist one assistant message for archive/investigate."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        self.conn = conn
        self.scope = scope

    async def insert_answer_message(
        self,
        scope: IndustryScope,
        *,
        conversation_id: UUID,
        question: str,
        blocks: list[dict[str, Any]],
        status: str,
        referenced_ids: list[str] | None = None,
    ) -> UUID:
        await bind_app(self.conn, scope)
        industry_id = scope.require_industry_id()
        conversation = (
            await self.conn.execute(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.owner_id == scope.owner_id,
                    Conversation.industry_id == industry_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
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
        text = (
            "\n".join(
                str(block.get("text") or "") for block in blocks if block.get("text")
            )
            or question
        )
        # The in-flight user turn settles in THIS transaction (16 §6):
        # pending → ready, otherwise the serial-turn guard in begin_turn
        # would reject every later turn of the conversation forever.
        await self.conn.execute(
            update(Message)
            .where(
                Message.owner_id == scope.owner_id,
                Message.industry_id == industry_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
                Message.status == "pending",
            )
            .values(status="ready")
        )
        await self.conn.execute(
            pg_insert(Message).values(
                id=message_id,
                owner_id=scope.owner_id,
                industry_id=industry_id,
                conversation_id=conversation_id,
                role="assistant",
                content=text,
                # Workflow outcome → the MessageView status literal happens
                # in exactly one place (assistant_message_status); unknown
                # outcomes raise instead of persisting an unreadable row.
                status=assistant_message_status(status),
                parent_message_id=(
                    conversation.last_committed_message_id
                    if conversation is not None
                    else None
                ),
                turn_index=turn_index,
                citation_manifest={
                    "blocks": blocks,
                    "question": question,
                    "referenced_ids": list(referenced_ids or []),
                },
            )
        )
        if conversation is not None:
            await self.conn.execute(
                Conversation.__table__.update()
                .where(Conversation.id == conversation_id)
                .values(
                    last_committed_message_id=message_id,
                    state_version=Conversation.state_version + 1,
                )
            )
        return message_id


class SqlAlchemyReportStore(SqlAlchemyReportRepository):
    """ReportStore protocol: persist_report already lives on the repository."""

    async def persist_report(
        self,
        scope: IndustryScope,
        *,
        report_type: str,
        title: str,
        period: str,
        composition: ReportComposition,
        input_manifest: dict,
    ) -> UUID:
        return await super().persist_report(
            scope,
            report_type=report_type,
            title=title,
            period=period,
            composition=composition,
            input_manifest=input_manifest,
        )


def sql_jobs_opener(engine: AsyncEngine):
    """Dispatcher (scope=None) vs worker-role (scoped + intel_app) jobs store."""

    @asynccontextmanager
    async def open_store(scope: IndustryScope | None):
        async with engine.connect() as conn, conn.begin():
            if scope is not None:
                await set_app_role(conn)
            yield SqlAlchemyJobsStore(conn, scope)

    return open_store


def _worker_opener(engine: AsyncEngine, store_cls):
    """Worker-role txn: SET LOCAL ROLE intel_app before any DML.

    Connecting as postgres (superuser) would otherwise bypass FORCE RLS.
    Store methods still bind GUCs; the role switch must happen at open.
    """

    @asynccontextmanager
    async def open_store(scope: IndustryScope):
        async with engine.connect() as conn, conn.begin():
            await set_app_role(conn)
            yield store_cls(conn, scope)

    return open_store


def sql_route_opener(engine: AsyncEngine):
    return _worker_opener(engine, SqlAlchemyRouteStore)


def sql_extract_opener(engine: AsyncEngine):
    return _worker_opener(engine, SqlAlchemyExtractStore)


def sql_event_build_opener(engine: AsyncEngine):
    return _worker_opener(engine, SqlAlchemyEventBuildStore)


def sql_answer_opener(engine: AsyncEngine):
    return _worker_opener(engine, SqlAlchemyAnswerStore)


def sql_report_opener(engine: AsyncEngine):
    return _worker_opener(engine, SqlAlchemyReportStore)


def sql_review_opener(engine: AsyncEngine):
    return _worker_opener(engine, SqlAlchemyReviewStore)


async def archive_packet_source(
    engine: AsyncEngine, scope: IndustryScope, question: str
) -> dict[str, Any]:
    """Industry-scoped evidence materials for archive_answer / report_build."""
    del question  # retrieval is industry-wide in v1; question filters later
    async with engine.connect() as conn, conn.begin():
        await bind_app(conn, scope)
        industry_id = scope.require_industry_id()
        evidence_rows = (
            await conn.execute(
                select(Evidence)
                .where(
                    Evidence.owner_id == scope.owner_id,
                    Evidence.industry_id == industry_id,
                )
                .order_by(Evidence.created_at.desc())
                .limit(50)
            )
        ).scalars()
        evidence = [
            {
                "id": str(row.id),
                "exact_quote": row.exact_quote,
                "block_id": row.block_id,
                "context": "",
            }
            for row in evidence_rows
        ]
        claim_rows = (
            await conn.execute(
                select(ClaimRevision)
                .where(
                    ClaimRevision.owner_id == scope.owner_id,
                    ClaimRevision.industry_id == industry_id,
                )
                .order_by(ClaimRevision.recorded_at.desc())
                .limit(50)
            )
        ).scalars()
        claims = [
            {"id": str(row.id), "text": row.text, "kind": row.kind}
            for row in claim_rows
        ]
        event_rows = (
            await conn.execute(
                select(Event, EventRevision)
                .join(EventRevision, Event.current_revision_id == EventRevision.id)
                .where(
                    Event.owner_id == scope.owner_id,
                    Event.industry_id == industry_id,
                )
                .limit(50)
            )
        ).all()
        events = [
            {
                "id": str(event.id),
                "title": revision.title,
                "summary": revision.summary,
            }
            for event, revision in event_rows
        ]
        return {
            "claims": claims,
            "events": events,
            "evidence": evidence,
            "coverage": {"processed": len(evidence), "failed": 0, "gaps": []},
            "read_blocks": [item["block_id"] for item in evidence],
        }


async def conversation_context_source(
    engine: AsyncEngine,
    scope: IndustryScope,
    conversation_id: UUID,
    *,
    exclude_message_id: UUID | None = None,
    recent_messages: int = 12,
) -> ConversationContext | None:
    """Planner context for one conversation: the recent committed turns.

    One short transaction; the QueryPlanner's LLM call happens after this
    returns, outside any transaction (07 §2). ``exclude_message_id``
    drops the turn being answered — its text IS the question handed to
    the planner. Assistant turns surface their cited ids through the
    blocks' citation_ids (message_to_view), which is what a follow-up
    resolves pronouns against.
    """
    async with engine.connect() as conn, conn.begin():
        await bind_app(conn, scope)
        industry_id = scope.require_industry_id()
        conversation = (
            await conn.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.owner_id == scope.owner_id,
                    Conversation.industry_id == industry_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return None
        rows = (
            (
                await conn.execute(
                    select(Message)
                    .where(
                        Message.owner_id == scope.owner_id,
                        Message.industry_id == industry_id,
                        Message.conversation_id == conversation_id,
                    )
                    .order_by(Message.turn_index.desc(), Message.created_at.desc())
                    .limit(recent_messages)
                )
            )
            .scalars()
            .all()
        )
        views = [
            message_to_view(row)
            for row in reversed(rows)
            if row.id != exclude_message_id
        ]
        return ConversationContext(
            conversation_id=conversation_id,
            state_version=max(int(conversation.state_version or 1), 1),
            resolved_question="",
            recent_messages=views,
            constraints=[],
            ordered_references=[],
            open_questions=[],
        )
