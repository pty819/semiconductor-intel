"""Report routes (spec 08 §2 / 05 §8)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    Enqueuer,
    IndustryScopeDep,
    cursor_signing_key,
    get_report_repo,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import Coverage, ReportCreate, ReportView
from intel.services.errors import NotFound

router = APIRouter(prefix="/industries/{industry_id}/reports", tags=["reports"])

Repo = Annotated[object, Depends(get_report_repo)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


def _view(row: dict) -> ReportView:
    coverage = row.get("coverage") or {}
    if not isinstance(coverage, Coverage):
        coverage = Coverage(
            status=coverage.get("status") or "pending",
            expected=coverage.get("expected"),
            processed=int(coverage.get("processed") or 0),
            failed=int(coverage.get("failed") or 0),
            gaps=list(coverage.get("gaps") or []),
        )
    return ReportView(
        id=row["id"],
        revision_id=row["revision_id"],
        title=row["title"],
        as_of=row["as_of"],
        blocks=list(row.get("blocks") or []),
        citations=list(row.get("citations") or []),
        coverage=coverage,
        stale=bool(row.get("stale", False)),
    )


@router.get("")
async def list_reports(
    industry_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    _scope: IndustryScopeDep,
) -> Page[ReportView]:
    items = [_view(row) for row in await repo.list_reports()]
    return paginate(items, params, key=cursor_signing_key(request))


@router.get("/{report_id}")
async def get_report(
    industry_id: UUID, report_id: UUID, repo: Repo, _scope: IndustryScopeDep
) -> ReportView:
    row = await repo.get_report(report_id)
    if row is None:
        raise NotFound("report not found")
    return _view(row)


@router.post("", status_code=202)
async def create_report(
    industry_id: UUID,
    body: ReportCreate,
    guard: Guard,
    repo: Repo,
    enqueuer: Enqueuer,
    scope: IndustryScopeDep,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    period = body.from_time.date().isoformat() if body.from_time else "current"
    manifest = {
        "type": body.type,
        "title": body.title,
        "period": period,
        "topic_ids": [str(tid) for tid in body.topic_ids],
    }
    input_manifest_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()[:32]
    payload = {
        "industry": str(industry_id),
        "report_type": body.type,
        "period": period,
        "input_manifest_hash": input_manifest_hash,
        "title": body.title,
    }
    accepted = await enqueuer.enqueue(
        scope, kind="report_build", payload=payload, idempotency_key=guard.key
    )
    body_out = accepted.model_dump(mode="json")
    await guard.store(202, body_out)
    return JSONResponse(body_out, status_code=202)
