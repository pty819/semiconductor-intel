"""Review routes (spec 08 §2)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    CurrentPrincipal,
    Enqueuer,
    IndustryScopeDep,
    cursor_signing_key,
    get_review_repo,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import ReviewDecision, ReviewView
from intel.services.errors import NotFound

router = APIRouter(prefix="/industries/{industry_id}/reviews", tags=["reviews"])

Repo = Annotated[object, Depends(get_review_repo)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


def _view(row: dict) -> ReviewView:
    return ReviewView(
        id=row["id"],
        row_version=int(row.get("row_version") or 1),
        type=str(row.get("type") or "unknown"),
        status=row.get("status") or "pending",
        proposal=dict(row.get("proposal") or {}),
        expected_versions={
            str(k): int(v) for k, v in (row.get("expected_versions") or {}).items()
        },
    )


@router.get("")
async def list_reviews(
    industry_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    _scope: IndustryScopeDep,
    status: str | None = None,
) -> Page[ReviewView]:
    items = [_view(row) for row in await repo.list_reviews(status=status)]
    return paginate(items, params, key=cursor_signing_key(request))


@router.get("/{review_id}")
async def get_review(
    industry_id: UUID, review_id: UUID, repo: Repo, _scope: IndustryScopeDep
) -> ReviewView:
    row = await repo.get_review(review_id)
    if row is None:
        raise NotFound("review not found")
    return _view(row)


@router.post("/{review_id}/decisions", status_code=202)
async def decide_review(
    industry_id: UUID,
    review_id: UUID,
    body: ReviewDecision,
    guard: Guard,
    repo: Repo,
    enqueuer: Enqueuer,
    scope: IndustryScopeDep,
    principal: CurrentPrincipal,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    row = await repo.get_review(review_id)
    if row is None:
        raise NotFound("review not found")
    payload = {
        "industry": str(industry_id),
        "review_id": str(review_id),
        "decision_version": str(body.expected_version),
        "action": body.action,
        "reason": body.reason,
        "actor_id": str(principal.user_id),
        "expected_versions": {
            str(k): int(v) for k, v in (row.get("expected_versions") or {}).items()
        },
    }
    accepted = await enqueuer.enqueue(
        scope, kind="apply_review", payload=payload, idempotency_key=guard.key
    )
    body_out = accepted.model_dump(mode="json")
    await guard.store(202, body_out)
    return JSONResponse(body_out, status_code=202)
