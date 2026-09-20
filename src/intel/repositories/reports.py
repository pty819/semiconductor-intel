"""Report repository (08 §2 reports; 05 §8 snapshots)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.contracts import AnswerBlock, Citation
from intel.db.models.conversation import Report, ReportRevision
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.services.reports import ReportComposition


def _citations_from_json(raw: object) -> list[dict]:
    citations: list[dict] = []
    if not isinstance(raw, list):
        return citations
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            citations.append(Citation.model_validate(item).model_dump(mode="json"))
        except (TypeError, ValueError):
            continue
    return citations


def _blocks_from_content(content: object) -> list[dict]:
    blocks: list[dict] = []
    if not isinstance(content, list):
        return blocks
    for item in content:
        if not isinstance(item, dict):
            continue
        statements = item.get("statements")
        if isinstance(statements, list):
            for statement in statements:
                if isinstance(statement, dict):
                    blocks.append(
                        AnswerBlock(
                            text=str(statement.get("text") or ""),
                            kind="fact",
                            citation_ids=[
                                str(cid)
                                for cid in (
                                    statement.get("citations")
                                    or statement.get("citation_ids")
                                    or []
                                )
                            ],
                        ).model_dump(mode="json")
                    )
                elif isinstance(statement, str) and statement:
                    blocks.append(
                        AnswerBlock(text=statement, kind="fact").model_dump(mode="json")
                    )
            continue
        if item.get("text"):
            kind = item.get("kind")
            if kind not in ("fact", "inference", "unknown"):
                kind = "fact"
            blocks.append(
                AnswerBlock(
                    text=str(item.get("text") or ""),
                    kind=kind,  # type: ignore[arg-type]
                    citation_ids=[
                        str(cid)
                        for cid in (
                            item.get("citation_ids") or item.get("citations") or []
                        )
                    ],
                ).model_dump(mode="json")
            )
    return blocks


def report_revision_to_view(
    report: Report | object, revision: ReportRevision | object | None
) -> dict[str, Any]:
    """Map report + revision content onto the ReportView projection."""
    coverage = (
        getattr(revision, "coverage", None) if revision is not None else None
    ) or {
        "status": "pending",
        "processed": 0,
        "failed": 0,
        "gaps": [],
    }
    manifest = (
        getattr(revision, "input_manifest", None) if revision is not None else None
    ) or {}
    content = getattr(revision, "content", None) if revision is not None else None
    citations = getattr(revision, "citations", None) if revision is not None else None
    return {
        "id": report.id,
        "revision_id": revision.id if revision is not None else report.id,
        "title": report.title,
        "as_of": getattr(revision, "as_of", None)
        if revision is not None
        else datetime.now(UTC),
        "blocks": _blocks_from_content(content),
        "citations": _citations_from_json(citations),
        "coverage": coverage,
        "stale": bool(manifest.get("stale", False)),
        "type": report.type,
    }


class ReportRepository:
    """Protocol surface the report routes consume."""


class SqlAlchemyReportRepository(ScopedRepository, ReportRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def list_reports(self) -> list[dict[str, Any]]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                select(Report, ReportRevision)
                .join(
                    ReportRevision,
                    Report.current_revision_id == ReportRevision.id,
                    isouter=True,
                )
                .where(
                    Report.owner_id == self.owner_id,
                    Report.industry_id == industry_id,
                )
            )
        ).all()
        return [self._view(report, revision) for report, revision in rows]

    async def get_report(self, report_id: UUID) -> dict[str, Any] | None:
        industry_id = await self._bind()
        row = (
            await self.conn.execute(
                select(Report, ReportRevision)
                .join(
                    ReportRevision,
                    Report.current_revision_id == ReportRevision.id,
                    isouter=True,
                )
                .where(
                    Report.id == report_id,
                    Report.owner_id == self.owner_id,
                    Report.industry_id == industry_id,
                )
            )
        ).first()
        if row is None:
            return None
        return self._view(row[0], row[1])

    async def create_placeholder(self, *, report_type: str, title: str) -> UUID:
        industry_id = await self._bind()
        report_id = uuid4()
        await self.conn.execute(
            pg_insert(Report).values(
                id=report_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                type=report_type,
                title=title,
                status="building",
            )
        )
        return report_id

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
        industry_id = await self._bind()
        report_id = uuid4()
        revision_id = composition.revision_id
        status = "ready" if composition.publishable else "partial"
        coverage = {
            "status": "complete" if composition.publishable else "partial",
            "processed": 1,
            "failed": 0,
            "gaps": list(composition.unsupported_statements),
        }
        await self.conn.execute(
            pg_insert(Report).values(
                id=report_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                type=report_type,
                title=title,
                status=status,
                current_revision_id=None,
            )
        )
        await self.conn.execute(
            pg_insert(ReportRevision).values(
                id=revision_id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                report_id=report_id,
                version=1,
                content=composition.sections,
                citations=[],
                as_of=composition.created_at,
                coverage=coverage,
                input_manifest={
                    **input_manifest,
                    "period": period,
                    "stale": composition.stale,
                    "stale_reason": composition.stale_reason,
                    "unsupported_statements": list(composition.unsupported_statements),
                },
            )
        )
        await self.conn.execute(
            update(Report)
            .where(Report.id == report_id)
            .values(current_revision_id=revision_id, title=title, status=status)
        )
        return report_id

    def _view(self, report: Report, revision: ReportRevision | None) -> dict[str, Any]:
        return report_revision_to_view(report, revision)
