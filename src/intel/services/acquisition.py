"""Acquisition job dispatch port (spec 03 §7, 08 §1 长任务 202).

Task 5 only defines the seam: HTTP routes hand the enqueuer a scope, a kind,
a JSON payload and the request's Idempotency-Key, and get back the 202 body.
The real queue — a ``jobs`` row plus scheduling — is Task 6; until then the
app runs on :class:`InMemoryEnqueuer`, which is also what unit tests use to
observe dispatched work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from intel.contracts import JobAccepted
from intel.repositories.base import IndustryScope


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    """One dispatch observed by the in-memory enqueuer (test surface)."""

    scope: IndustryScope
    kind: str
    payload: dict
    idempotency_key: str
    job_id: UUID
    enqueued_at: datetime = field(default_factory=_utcnow)


def job_accepted(job_id: UUID, state: str = "queued") -> JobAccepted:
    """The uniform 202 body for an accepted long-running job (08 §1)."""
    return JobAccepted(
        job_id=job_id,
        state=state,
        events_url=f"/api/v1/jobs/{job_id}/events",
        result_url=f"/api/v1/jobs/{job_id}",
    )


@runtime_checkable
class JobEnqueuer(Protocol):
    """Enqueue a job scoped to an owner (feed polling) or an industry."""

    async def enqueue(
        self,
        scope: IndustryScope,
        *,
        kind: str,
        payload: dict,
        idempotency_key: str,
    ) -> JobAccepted: ...


class InMemoryEnqueuer:
    """Dev/test stand-in: records dispatches, invents job ids.

    No persistence, no worker. ``records`` is the observation surface for
    tests (kind / payload / scope / idempotency_key of each dispatch). The
    real implementation lands in Task 6 against the ``jobs`` table, whose
    UNIQUE(owner_id, kind, idempotency_key) provides the same replay
    semantics the HTTP layer already has via api_idempotency.
    """

    def __init__(self) -> None:
        self.records: list[EnqueuedJob] = []

    async def enqueue(
        self,
        scope: IndustryScope,
        *,
        kind: str,
        payload: dict,
        idempotency_key: str,
    ) -> JobAccepted:
        job_id = uuid4()
        self.records.append(
            EnqueuedJob(
                scope=scope,
                kind=kind,
                payload=dict(payload),
                idempotency_key=idempotency_key,
                job_id=job_id,
            )
        )
        return job_accepted(job_id)
