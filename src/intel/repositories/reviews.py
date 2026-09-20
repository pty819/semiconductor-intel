"""Review-task repository (08 §2 reviews + ReviewStore for apply_review)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.conversation import AuditLog, ReviewTask
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository
from intel.services.reviews import ReviewDecisionKind, ReviewRecord

_TO_SERVICE = {
    "pending": "pending",
    "accepted": "approved",
    "applied": "approved",
    "rejected": "rejected",
    "obsolete": "undone",
    "failed": "pending",
}
_FROM_SERVICE = {
    "pending": "pending",
    "approved": "accepted",
    "rejected": "rejected",
    "undone": "obsolete",
}


class ReviewRepository:
    """Protocol surface the review routes consume."""


class SqlAlchemyReviewRepository(ScopedRepository, ReviewRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def list_reviews(self, status: str | None = None) -> list[dict[str, Any]]:
        industry_id = await self._bind()
        stmt = select(ReviewTask).where(
            ReviewTask.owner_id == self.owner_id,
            ReviewTask.industry_id == industry_id,
        )
        if status:
            stmt = stmt.where(ReviewTask.status == status)
        rows = (await self.conn.execute(stmt)).scalars()
        return [self._view(row) for row in rows]

    async def get_review(self, review_id: UUID) -> dict[str, Any] | None:
        industry_id = await self._bind()
        row = (
            (
                await self.conn.execute(
                    select(ReviewTask).where(
                        ReviewTask.id == review_id,
                        ReviewTask.owner_id == self.owner_id,
                        ReviewTask.industry_id == industry_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        return None if row is None else self._view(row)

    def _view(self, row: ReviewTask) -> dict[str, Any]:
        return {
            "id": row.id,
            "row_version": row.row_version,
            "type": row.type,
            "status": row.status,
            "proposal": row.proposal,
            "expected_versions": row.expected_versions,
        }

    # -- ReviewStore (workflow) ------------------------------------------------

    async def get_review_record(self, review_id: UUID) -> ReviewRecord | None:
        industry_id = await self._bind()
        row = (
            (
                await self.conn.execute(
                    select(ReviewTask).where(
                        ReviewTask.id == review_id,
                        ReviewTask.owner_id == self.owner_id,
                        ReviewTask.industry_id == industry_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        decision = None
        if row.decision in ("approve", "reject"):
            decision = ReviewDecisionKind(row.decision)
        return ReviewRecord(
            id=row.id,
            object_type=row.type,
            payload=dict(row.proposal or {}),
            status=_TO_SERVICE.get(row.status, row.status),
            decided_by=None,
            decided_at=row.decided_at,
            decision=decision,
            applied_at=row.decided_at if row.status == "applied" else None,
            compensated=row.status == "obsolete",
        )


class SqlAlchemyReviewStore(SqlAlchemyReviewRepository):
    """ReviewStore protocol adapter used by apply_review."""

    async def get_review(self, review_id: UUID) -> ReviewRecord | None:
        return await self.get_review_record(review_id)

    async def row_versions(
        self, object_refs: list[tuple[str, UUID]]
    ) -> dict[tuple[str, UUID], int]:
        from intel.db.models.knowledge import Event

        industry_id = await self._bind()
        actual: dict[tuple[str, UUID], int] = {}
        for kind, ident in object_refs:
            if kind in ("events", "event"):
                row = (
                    await self.conn.execute(
                        select(Event.row_version).where(
                            Event.id == ident,
                            Event.owner_id == self.owner_id,
                            Event.industry_id == industry_id,
                        )
                    )
                ).first()
                actual[(kind, ident)] = int(row[0]) if row is not None else 0
            else:
                actual[(kind, ident)] = 1
        return actual

    async def save_review(self, review: ReviewRecord) -> None:
        industry_id = await self._bind()
        status = _FROM_SERVICE.get(review.status, review.status)
        if review.status == "approved" and review.applied_at is not None:
            status = "applied"
        existing = (
            await self.conn.execute(
                select(ReviewTask.id).where(ReviewTask.id == review.id)
            )
        ).first()
        values = {
            "type": review.object_type,
            "status": status,
            "proposal": dict(review.payload),
            "expected_versions": {},
            "decision": (
                review.decision.value if review.decision is not None else None
            ),
            "decided_at": review.decided_at,
        }
        if existing is None:
            await self.conn.execute(
                pg_insert(ReviewTask).values(
                    id=review.id,
                    owner_id=self.owner_id,
                    industry_id=industry_id,
                    **values,
                )
            )
            return
        await self.conn.execute(
            update(ReviewTask)
            .where(ReviewTask.id == review.id, ReviewTask.owner_id == self.owner_id)
            .values(**values, row_version=ReviewTask.row_version + 1)
        )

    async def append_audit(self, entry: dict[str, Any]) -> None:
        await self._bind()
        actor_id = UUID(str(entry["actor"])) if entry.get("actor") else self.owner_id
        target_id = UUID(str(entry["review_id"])) if entry.get("review_id") else uuid4()
        await self.conn.execute(
            pg_insert(AuditLog).values(
                id=uuid4(),
                owner_id=self.owner_id,
                industry_id=self.scope.industry_id,
                action=str(entry.get("action") or "review"),
                actor_type="user",
                actor_id=actor_id,
                target_type="review",
                target_id=target_id,
                details=entry,
            )
        )

    async def apply_decision(self, review: ReviewRecord) -> dict[str, Any]:
        return {"applied": str(review.id)}

    async def compensate(self, review: ReviewRecord) -> None:
        return None
