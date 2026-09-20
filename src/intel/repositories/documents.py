"""Industry-bound document reads (08 §2 documents)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.knowledge import IndustryDocument
from intel.db.models.pool import Capture, Document, DocumentDiff, ParsedArtifact
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository


class DocumentRepository:
    """Protocol surface the document routes consume."""


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
        await self._bind()
        rows = (
            await self.conn.execute(
                select(ParsedArtifact).where(
                    ParsedArtifact.owner_id == self.owner_id,
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
        await self._bind()
        from_parse = kwargs.get("from_parse_id")
        to_parse = kwargs.get("to_parse_id")
        stmt = select(DocumentDiff).where(DocumentDiff.owner_id == self.owner_id)
        if from_parse is not None:
            stmt = stmt.where(DocumentDiff.from_parse_id == from_parse)
        if to_parse is not None:
            stmt = stmt.where(DocumentDiff.to_parse_id == to_parse)
        row = (await self.conn.execute(stmt.limit(1))).scalars().first()
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

    async def apply_topic_decision(self, document_id: UUID, body) -> dict[str, Any]:
        await self._bind()
        return {"id": document_id, "ok": True}

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
