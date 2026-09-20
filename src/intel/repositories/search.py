"""Industry archive search + coverage reads (08 §2; no web)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.jobs import CoverageBatch
from intel.db.models.knowledge import ClaimRevision, Event, EventRevision
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository


class SearchRepository:
    """Protocol surface the search/coverage routes consume."""


class SqlAlchemySearchRepository(ScopedRepository, SearchRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def search(self, query: str, **kwargs) -> list[dict[str, Any]]:
        industry_id = await self._bind()
        pattern = f"%{query}%"
        events = (
            await self.conn.execute(
                select(Event, EventRevision)
                .join(EventRevision, Event.current_revision_id == EventRevision.id)
                .where(
                    Event.owner_id == self.owner_id,
                    Event.industry_id == industry_id,
                    or_(
                        EventRevision.title.ilike(pattern),
                        EventRevision.summary.ilike(pattern),
                    ),
                )
                .limit(50)
            )
        ).all()
        hits: list[dict[str, Any]] = []
        for event, revision in events:
            hits.append(
                {
                    "object_id": event.id,
                    "object_type": "event",
                    "title": revision.title,
                    "excerpt": revision.summary,
                    "channels": ["archive"],
                    "citation_ids": [],
                }
            )
        claims = (
            await self.conn.execute(
                select(ClaimRevision)
                .where(
                    ClaimRevision.owner_id == self.owner_id,
                    ClaimRevision.industry_id == industry_id,
                    ClaimRevision.text.ilike(pattern),
                )
                .limit(50)
            )
        ).scalars()
        for claim in claims:
            hits.append(
                {
                    "object_id": claim.id,
                    "object_type": "claim",
                    "title": claim.text[:120],
                    "excerpt": claim.text,
                    "channels": ["archive"],
                    "citation_ids": [],
                }
            )
        return hits

    async def coverage(self) -> dict[str, Any]:
        industry_id = await self._bind()
        rows = (
            await self.conn.execute(
                select(CoverageBatch).where(
                    CoverageBatch.owner_id == self.owner_id,
                    CoverageBatch.industry_id == industry_id,
                )
            )
        ).scalars()
        batches = list(rows)
        if not batches:
            return {"status": "unknown", "processed": 0, "failed": 0, "gaps": []}
        processed = sum(b.done_count for b in batches)
        failed = sum(b.failed_count for b in batches)
        expected = sum(b.expected_count for b in batches)
        gaps = [b.phase for b in batches if b.status != "complete"]
        if failed:
            status = "partial"
        elif gaps:
            status = "pending"
        else:
            status = "complete"
        return {
            "status": status,
            "expected": expected,
            "processed": processed,
            "failed": failed,
            "gaps": gaps,
        }
