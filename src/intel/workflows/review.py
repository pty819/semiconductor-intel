"""Apply-review workflow handler (kind=apply_review).

Wraps :class:`ReviewService.decide` / ``undo``. ``expected_versions`` come
from the job payload (string keys ``{object_type}:{uuid}`` → int). Version
conflicts fail the job with ``version_conflict`` (no retry). Re-delivery of
an already-decided review is a no-op (idempotent apply_review).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID

from intel.repositories.base import IndustryScope
from intel.services.reviews import (
    ReviewDecisionKind,
    ReviewService,
    ReviewStateError,
    ReviewStore,
    ReviewVersionConflict,
)
from intel.workers.runner import JobFailure, JobHandler, RunContext

OpenReviewTxn = Callable[[IndustryScope], AbstractAsyncContextManager[ReviewStore]]


@dataclass(slots=True)
class ReviewWiring:
    open_store: OpenReviewTxn


def _parse_expected_versions(
    raw: dict | None,
) -> dict[tuple[str, UUID], int]:
    parsed: dict[tuple[str, UUID], int] = {}
    for key, version in (raw or {}).items():
        text = str(key)
        if ":" in text:
            kind, ident = text.split(":", 1)
        else:
            kind, ident = "object", text
        parsed[(kind, UUID(ident))] = int(version)
    return parsed


def make_review_handler(wiring: ReviewWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        review_id = UUID(str(payload["review_id"]))
        actor = UUID(str(payload["actor_id"]))
        action = str(payload.get("action") or "approve")
        expected = _parse_expected_versions(payload.get("expected_versions"))

        async with wiring.open_store(ctx.scope) as store:
            service = ReviewService(store)
            try:
                if action == "undo":
                    outcome = await service.undo(review_id=review_id, actor=actor)
                else:
                    decision = (
                        ReviewDecisionKind.APPROVE
                        if action == "approve"
                        else ReviewDecisionKind.REJECT
                    )
                    outcome = await service.decide(
                        review_id=review_id,
                        actor=actor,
                        decision=decision,
                        expected_versions=expected,
                    )
            except ReviewVersionConflict as exc:
                raise JobFailure("version_conflict", str(exc)) from exc
            except ReviewStateError as exc:
                raise JobFailure("invalid_state_transition", str(exc)) from exc

        await ctx.boundary()
        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "review_id": str(outcome.review_id),
                    "status": outcome.status,
                    "applied": outcome.applied,
                    "decision": (
                        outcome.decision.value if outcome.decision is not None else None
                    ),
                    "result": dict(outcome.result),
                },
            )

    return handler
