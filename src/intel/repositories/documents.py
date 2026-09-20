"""Industry-bound document reads (08 §2 documents)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from intel.contracts.models import DocumentTopicCommand
from intel.db.models.knowledge import (
    DocumentAssociationHistory,
    DocumentTopic,
    IndustryDocument,
)
from intel.db.models.pool import Capture, Document, DocumentDiff, ParsedArtifact
from intel.db.models.workspace import Topic
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.services.errors import NotFound, VersionConflict


class DocumentRepository:
    """Protocol surface the document routes consume."""


def list_revisions_stmt(*, owner_id: UUID, industry_id: UUID, document_id: UUID):
    """Parsed artifacts of one industry-bound document (Capture → Document)."""
    return (
        select(ParsedArtifact)
        .join(
            Capture,
            (Capture.id == ParsedArtifact.capture_id)
            & (Capture.owner_id == ParsedArtifact.owner_id),
        )
        .join(
            Document,
            (Document.id == Capture.document_id)
            & (Document.owner_id == Capture.owner_id),
        )
        .join(
            IndustryDocument,
            (IndustryDocument.document_id == Document.id)
            & (IndustryDocument.owner_id == owner_id)
            & (IndustryDocument.industry_id == industry_id),
        )
        .where(
            ParsedArtifact.owner_id == owner_id,
            Document.id == document_id,
        )
        .order_by(ParsedArtifact.parsed_at.asc())
    )


def get_diff_stmt(
    *,
    owner_id: UUID,
    industry_id: UUID,
    document_id: UUID,
    from_parse_id: UUID | None = None,
    to_parse_id: UUID | None = None,
):
    """Diff rows whose from/to parses both belong to the bound document."""
    to_parse = aliased(ParsedArtifact)
    to_capture = aliased(Capture)
    stmt = (
        select(DocumentDiff)
        .join(
            ParsedArtifact,
            (ParsedArtifact.id == DocumentDiff.from_parse_id)
            & (ParsedArtifact.owner_id == DocumentDiff.owner_id),
        )
        .join(
            Capture,
            (Capture.id == ParsedArtifact.capture_id)
            & (Capture.owner_id == ParsedArtifact.owner_id),
        )
        .join(
            to_parse,
            (to_parse.id == DocumentDiff.to_parse_id)
            & (to_parse.owner_id == DocumentDiff.owner_id),
        )
        .join(
            to_capture,
            (to_capture.id == to_parse.capture_id)
            & (to_capture.owner_id == to_parse.owner_id),
        )
        .join(
            IndustryDocument,
            (IndustryDocument.document_id == Capture.document_id)
            & (IndustryDocument.owner_id == owner_id)
            & (IndustryDocument.industry_id == industry_id),
        )
        .where(
            DocumentDiff.owner_id == owner_id,
            Capture.document_id == document_id,
            to_capture.document_id == document_id,
        )
    )
    if from_parse_id is not None:
        stmt = stmt.where(DocumentDiff.from_parse_id == from_parse_id)
    if to_parse_id is not None:
        stmt = stmt.where(DocumentDiff.to_parse_id == to_parse_id)
    return stmt.limit(1)


class SqlAlchemyDocumentRepository(ScopedRepository, DocumentRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def list_documents(self) -> list[dict[str, Any]]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                select(IndustryDocument, Document, ParsedArtifact, Capture)
                .join(Document, Document.id == IndustryDocument.document_id)
                .join(
                    ParsedArtifact,
                    ParsedArtifact.id == IndustryDocument.current_parse_id,
                    isouter=True,
                )
                .join(
                    Capture,
                    Capture.id == Document.current_capture_id,
                    isouter=True,
                )
                .where(
                    IndustryDocument.owner_id == self.owner_id,
                    IndustryDocument.industry_id == industry_id,
                    IndustryDocument.active.is_(True),
                )
            )
        ).all()
        return [
            self._view(binding, document, parse, capture)
            for binding, document, parse, capture in rows
        ]

    async def get_document(self, document_id: UUID) -> dict[str, Any] | None:
        for item in await self.list_documents():
            if item["id"] == document_id:
                return item
        return None

    async def list_revisions(self, document_id: UUID) -> list[dict[str, Any]]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                list_revisions_stmt(
                    owner_id=self.owner_id,
                    industry_id=industry_id,
                    document_id=document_id,
                )
            )
        ).scalars()
        return [
            {
                "id": row.id,
                "document_id": document_id,
                "capture_id": row.capture_id,
                "parser_version_id": row.parser_version_id,
                "parsed_at": row.parsed_at,
                "coverage": row.coverage,
            }
            for row in rows
        ]

    async def get_diff(self, document_id: UUID, **kwargs) -> dict[str, Any] | None:
        industry_id = await self._bind()
        row = (
            (
                await self.conn.execute(
                    get_diff_stmt(
                        owner_id=self.owner_id,
                        industry_id=industry_id,
                        document_id=document_id,
                        from_parse_id=kwargs.get("from_parse_id"),
                        to_parse_id=kwargs.get("to_parse_id"),
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return {
            "from_parse_id": row.from_parse_id,
            "to_parse_id": row.to_parse_id,
            "kind": row.kind,
            "algorithm_version": row.diff_algorithm_version,
            "changed_blocks": row.changed_blocks,
            "field_changes": row.field_changes,
        }

    async def apply_topic_decision(
        self, document_id: UUID, body: DocumentTopicCommand
    ) -> dict[str, Any]:
        """Persist a human topic lock against industry_documents.row_version."""
        industry_id = await self._bind()
        binding = (
            (
                await self.conn.execute(
                    select(IndustryDocument).where(
                        IndustryDocument.owner_id == self.owner_id,
                        IndustryDocument.industry_id == industry_id,
                        IndustryDocument.document_id == document_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if binding is None:
            raise NotFound("document not found")
        if int(binding.row_version) != int(body.expected_version):
            raise VersionConflict(int(binding.row_version))
        topic = (
            (
                await self.conn.execute(
                    select(Topic).where(
                        Topic.id == body.topic_id,
                        Topic.owner_id == self.owner_id,
                        Topic.industry_id == industry_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if topic is None or topic.current_revision_id is None:
            raise NotFound("topic not found")
        result = await self.conn.execute(
            update(IndustryDocument)
            .where(
                IndustryDocument.id == binding.id,
                IndustryDocument.owner_id == self.owner_id,
                IndustryDocument.row_version == body.expected_version,
            )
            .values(
                row_version=IndustryDocument.row_version + 1,
                association_reason=body.rationale,
            )
        )
        if result.rowcount == 0:
            raise VersionConflict(int(binding.row_version))
        await self.conn.execute(
            pg_insert(DocumentTopic)
            .values(
                id=uuid4(),
                owner_id=self.owner_id,
                industry_id=industry_id,
                industry_document_id=binding.id,
                topic_id=body.topic_id,
                topic_revision_id=topic.current_revision_id,
                relevance=body.relevance,
                supporting_block_ids=[],
                decision_origin="user",
                locked_by_user=body.lock,
            )
            .on_conflict_do_update(
                constraint="uq_document_topics_document_topic",
                set_={
                    "topic_revision_id": topic.current_revision_id,
                    "relevance": body.relevance,
                    "decision_origin": "user",
                    "locked_by_user": body.lock,
                    "row_version": DocumentTopic.row_version + 1,
                },
            )
        )
        await self.conn.execute(
            pg_insert(DocumentAssociationHistory).values(
                id=uuid4(),
                owner_id=self.owner_id,
                industry_id=industry_id,
                industry_document_id=binding.id,
                topic_id=body.topic_id,
                topic_revision_id=topic.current_revision_id,
                relevance=body.relevance,
                origin="user",
                locked_by_user=body.lock,
            )
        )
        return {
            "id": document_id,
            "topic_id": body.topic_id,
            "relevance": body.relevance,
            "locked": body.lock,
            "row_version": int(binding.row_version) + 1,
        }

    def _view(
        self,
        binding: IndustryDocument,
        document: Document,
        parse: ParsedArtifact | None,
        capture: Capture | None,
    ) -> dict[str, Any]:
        return {
            "id": document.id,
            "industry_id": binding.industry_id,
            "row_version": binding.row_version,
            "title": document.canonical_url,
            "source_url": document.canonical_url,
            "current_parse_id": binding.current_parse_id,
            "retrieval_scope": capture.retrieval_scope
            if capture is not None
            else "fulltext",
            "parse_status": parse.parse_status if parse is not None else "pending",
            "relevance": binding.relevance,
            "quality_flags": list(parse.quality_flags) if parse is not None else [],
            "first_seen_at": document.first_seen_at,
        }
