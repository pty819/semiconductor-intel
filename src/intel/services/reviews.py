"""Review lifecycle: approve/reject/undo with expected versions (05 §4/08).

Reviews gate model-proposed mutations (merges, corrections). The service
is pure over a store protocol:

- every decision carries ``expected_versions`` — the client read state;
  a moved row rejects with :class:`ReviewVersionConflict` (409-class)
  BEFORE any write;
- ``apply_review`` is idempotent: re-applying a decided review is a
  no-op returning the recorded outcome (at-least-once delivery safe);
- ``undo`` re-opens a decided review unless its application was already
  compensated downstream — then it rejects rather than double-undo;
- every decision appends an audit entry (审计) with the actor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

__all__ = [
    "ReviewDecisionKind",
    "ReviewRecord",
    "ReviewService",
    "ReviewStateError",
    "ReviewVersionConflict",
]


class ReviewStateError(Exception):
    """Invalid review lifecycle transition (409-class)."""

    code = "review_state_error"


class ReviewVersionConflict(ReviewStateError):
    """expected_versions did not match the locked rows."""

    code = "version_conflict"


class ReviewDecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(slots=True)
class ReviewRecord:
    id: UUID
    object_type: str  # "event_merge" | "claim_correction" | ...
    payload: dict[str, Any]
    status: str = "pending"  # pending | approved | rejected | undone
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    decision: ReviewDecisionKind | None = None
    applied_at: datetime | None = None
    compensated: bool = False
    undo_of: UUID | None = None


@runtime_checkable
class ReviewStore(Protocol):
    async def get_review(self, review_id: UUID) -> ReviewRecord | None: ...

    async def row_versions(
        self, object_refs: list[tuple[str, UUID]]
    ) -> dict[tuple[str, UUID], int]: ...

    async def save_review(self, review: ReviewRecord) -> None: ...

    async def append_audit(self, entry: dict[str, Any]) -> None: ...

    async def apply_decision(self, review: ReviewRecord) -> dict[str, Any]:
        """Apply the decided mutation (merge/correction) in-transaction."""

    async def compensate(self, review: ReviewRecord) -> None: ...


@dataclass(slots=True)
class ReviewOutcome:
    review_id: UUID
    status: str
    decision: ReviewDecisionKind | None
    applied: bool = False
    result: dict[str, Any] = field(default_factory=dict)


class ReviewService:
    def __init__(self, store: ReviewStore) -> None:
        self._store = store

    async def decide(
        self,
        *,
        review_id: UUID,
        actor: UUID,
        decision: ReviewDecisionKind,
        expected_versions: dict[tuple[str, UUID], int],
    ) -> ReviewOutcome:
        """Record the decision, check versions, apply once (idempotent)."""
        review = await self._require(review_id)
        if review.status in ("approved", "rejected") and review.decision == decision:
            # Idempotent re-delivery: the recorded outcome stands.
            return ReviewOutcome(
                review_id=review.id,
                status=review.status,
                decision=review.decision,
                applied=False,
                result={"idempotent": True},
            )
        if review.status != "pending":
            raise ReviewStateError(
                f"review {review_id} is {review.status}, not pending"
            )

        actual = await self._store.row_versions(list(expected_versions))
        for ref, expected in expected_versions.items():
            if actual.get(ref) != expected:
                raise ReviewVersionConflict(
                    f"{ref[0]} {ref[1]} moved: expected {expected},"
                    f" found {actual.get(ref)}"
                )

        review.status = (
            "approved" if decision == ReviewDecisionKind.APPROVE else "rejected"
        )
        review.decision = decision
        review.decided_by = actor
        review.decided_at = datetime.now(UTC)
        await self._store.save_review(review)
        await self._store.append_audit(
            {
                "action": f"review.{decision.value}",
                "review_id": str(review_id),
                "actor": str(actor),
                "at": review.decided_at.isoformat(),
            }
        )
        if decision == ReviewDecisionKind.APPROVE:
            result = await self._store.apply_decision(review)
            review.applied_at = datetime.now(UTC)
            await self._store.save_review(review)
            return ReviewOutcome(
                review_id=review.id,
                status=review.status,
                decision=review.decision,
                applied=True,
                result=result,
            )
        return ReviewOutcome(
            review_id=review.id, status=review.status, decision=review.decision
        )

    async def undo(self, *, review_id: UUID, actor: UUID) -> ReviewOutcome:
        """Re-open a decided review, compensating an applied mutation."""
        review = await self._require(review_id)
        if review.status not in ("approved", "rejected"):
            raise ReviewStateError(f"review {review_id} is {review.status}")
        if review.compensated:
            raise ReviewStateError(
                f"review {review_id} was already compensated; open a new review"
            )
        if review.applied_at is not None:
            await self._store.compensate(review)
            review.compensated = True
        undone = ReviewRecord(
            id=uuid4(),
            object_type=review.object_type,
            payload=dict(review.payload),
            status="pending",
            undo_of=review.id,
        )
        review.status = "undone"
        await self._store.save_review(review)
        await self._store.save_review(undone)
        await self._store.append_audit(
            {
                "action": "review.undo",
                "review_id": str(review_id),
                "new_review_id": str(undone.id),
                "actor": str(actor),
            }
        )
        return ReviewOutcome(review_id=undone.id, status="pending", decision=None)

    async def _require(self, review_id: UUID) -> ReviewRecord:
        review = await self._store.get_review(review_id)
        if review is None:
            raise ReviewStateError(f"review {review_id} not found")
        return review
