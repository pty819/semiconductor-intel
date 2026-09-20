"""Job list / detail / cancel / retry / SSE (spec 08 §2, 07 §7)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    CurrentPrincipal,
    Enqueuer,
    cursor_signing_key,
    get_job_event_log,
    get_jobs_repo,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.api.sse import EventCursorExpired, sse_stream
from intel.contracts import JobAccepted, JobCommand, JobView
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import CANCELLABLE_STATES, JobRecord
from intel.services.errors import (
    EventCursorExpiredError,
    InvalidStateTransition,
    NotFound,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

Repo = Annotated[object, Depends(get_jobs_repo)]
Log = Annotated[object, Depends(get_job_event_log)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]
_RETRYABLE = frozenset({"failed", "cancelled", "partial"})


async def request_job_cancel(
    repo, job: JobRecord, *, now: datetime | None = None
) -> None:
    """Cancel through the real JobsStore signature: ``request_cancel(*, at=)``."""
    if job.state not in CANCELLABLE_STATES:
        raise InvalidStateTransition(
            "job cannot be cancelled from its current state",
            action="cancel",
            current=job.state,
        )
    await repo.request_cancel(job.id, at=now or datetime.now(UTC))


def _view(job: JobRecord) -> JobView:
    progress = job.progress or {}
    error = job.error or {}
    return JobView(
        id=job.id,
        industry_id=job.industry_id,
        kind=job.kind,
        state=job.state,  # type: ignore[arg-type]
        stage=progress.get("stage") or progress.get("status"),
        done=int(progress.get("done") or 0),
        total=progress.get("total"),
        error_code=error.get("code"),
        result_url=f"/api/v1/jobs/{job.id}",
    )


@router.get("")
async def list_jobs(
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    industry_id: UUID | None = None,
    kind: str | None = None,
    state: str | None = None,
) -> Page[JobView]:
    jobs = await repo.list_jobs(industry_id=industry_id, kind=kind, state=state)
    return paginate(
        [_view(job) for job in jobs], params, key=cursor_signing_key(request)
    )


@router.get("/{job_id}")
async def get_job(job_id: UUID, repo: Repo) -> JobView:
    job = await repo.get_job(job_id)
    if job is None:
        raise NotFound("job not found")
    return _view(job)


@router.get("/{job_id}/events")
async def job_events(
    job_id: UUID,
    request: Request,
    repo: Repo,
    log: Log,
):
    job = await repo.get_job(job_id)
    if job is None:
        raise NotFound("job not found")
    try:
        return await sse_stream(request, job_id=job_id, log=log)
    except EventCursorExpired as exc:
        raise EventCursorExpiredError(str(exc)) from exc


@router.post("/{job_id}/cancel", status_code=202)
async def cancel_job(
    job_id: UUID,
    body: JobCommand,
    guard: Guard,
    repo: Repo,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    job = await repo.get_job(job_id)
    if job is None:
        raise NotFound("job not found")
    await request_job_cancel(repo, job)
    accepted = JobAccepted(
        job_id=job.id,
        state=job.state,
        events_url=f"/api/v1/jobs/{job.id}/events",
        result_url=f"/api/v1/jobs/{job.id}",
    )
    payload = accepted.model_dump(mode="json")
    await guard.store(202, payload)
    return JSONResponse(payload, status_code=202)


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(
    job_id: UUID,
    body: JobCommand,
    guard: Guard,
    repo: Repo,
    enqueuer: Enqueuer,
    principal: CurrentPrincipal,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    job = await repo.get_job(job_id)
    if job is None:
        raise NotFound("job not found")
    if job.state not in _RETRYABLE:
        raise InvalidStateTransition(
            "job cannot be retried from its current state",
            action="retry",
            current=job.state,
        )
    payload = {**dict(job.input), "retry_of": str(job.id), "reason": body.reason}
    # Keep original payload ids as strings; never insert UUID objects.
    for key, value in list(payload.items()):
        if hasattr(value, "hex"):
            payload[key] = str(value)
    accepted = await enqueuer.enqueue(
        IndustryScope(owner_id=principal.user_id, industry_id=job.industry_id),
        kind=job.kind,
        payload=payload,
        idempotency_key=guard.key,
    )
    body_out = accepted.model_dump(mode="json")
    await guard.store(202, body_out)
    return JSONResponse(body_out, status_code=202)
