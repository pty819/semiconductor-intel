"""Unit tests: JobRunner skeleton against in-memory stores (spec 07 §1/§2).

Covers: claim → dispatch by kind to a registered async handler; the no-op
default handler marks unknown kinds succeeded with an output note;
cancellation takes effect at step boundaries; the supervisor kills the job
at its total deadline (default 15 minutes).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService, LeaseLost
from intel.workers.runner import (
    DEFAULT_JOB_DEADLINE,
    JobCancelled,
    JobDeadlineExceeded,
    JobFailure,
    JobRunner,
)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OWNER = uuid4()


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_runner(
    *, handlers=None, deadlines=None, default_deadline=DEFAULT_JOB_DEADLINE
) -> tuple[JobRunner, InMemoryJobsDatabase, FakeClock]:
    db = InMemoryJobsDatabase()
    clock = FakeClock()
    service = JobService(clock=clock, rng=random.Random(3))

    @asynccontextmanager
    async def open_store(
        scope: IndustryScope | None,
    ) -> AsyncIterator[InMemoryJobsStore]:
        yield InMemoryJobsStore(db)

    runner = JobRunner(
        service,
        open_store,
        handlers=handlers,
        deadlines=deadlines,
        default_deadline=default_deadline,
        clock=clock,
    )
    return runner, db, clock


async def seed_job(
    db: InMemoryJobsDatabase, service: JobService, *, kind: str = "discover"
) -> None:
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(owner_id=OWNER),
        kind=kind,
        payload={"x": 1},
        idempotency_key=f"k-{uuid4().hex[:8]}",
    )


async def test_run_once_claims_and_runs_default_handler() -> None:
    runner, db, _clock = make_runner()
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service)
    job = await runner.run_once()
    assert job is not None
    assert job.state == "succeeded"
    row = db.jobs[job.id]
    assert row["progress"].get("note") == "no handler registered for kind"
    assert row["lease_token"] is None
    # Empty queue afterwards.
    assert await runner.run_once() is None


async def test_run_once_dispatches_registered_handler_by_kind() -> None:
    seen: list[str] = []

    async def handler(ctx) -> None:
        seen.append(ctx.job.kind)
        await ctx.boundary()
        async with ctx.open_store(
            IndustryScope(ctx.job.owner_id, ctx.job.industry_id)
        ) as store:
            await ctx.service.finish(store, ctx.job, state="succeeded")

    runner, db, _clock = make_runner(handlers={"discover": handler})
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service, kind="discover")
    job = await runner.run_once()
    assert seen == ["discover"]
    assert job is not None and job.state == "succeeded"


async def test_runner_cancels_job_at_step_boundary() -> None:
    async def handler(ctx) -> None:
        async with ctx.open_store(
            IndustryScope(ctx.job.owner_id, ctx.job.industry_id)
        ) as store:
            await ctx.service.request_cancel(store, ctx.job.id)
        await ctx.boundary()  # observes the cancellation, raises JobCancelled
        raise AssertionError("must not run past a cancelled boundary")

    runner, db, _clock = make_runner(handlers={"discover": handler})
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service)
    job = await runner.run_once()
    assert job is not None
    assert job.state == "cancelled"
    assert db.jobs[job.id]["lease_token"] is None


async def test_runner_kills_job_at_deadline() -> None:
    async def handler(ctx) -> None:
        await asyncio.sleep(10)

    runner, db, _clock = make_runner(
        handlers={"discover": handler},
        deadlines={"discover": timedelta(milliseconds=20)},
    )
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service)
    job = await runner.run_once()
    assert job is not None
    assert job.state == "failed"
    assert db.jobs[job.id]["error"]["code"] == "deadline_exceeded"


async def test_runner_maps_job_failure_error_class() -> None:
    async def handler(ctx) -> None:
        raise JobFailure("transient", "upstream 503", retry_after=120)

    runner, db, clock = make_runner(handlers={"discover": handler})
    service = JobService(clock=clock, rng=random.Random(3))
    await seed_job(db, service, kind="discover")
    job = await runner.run_once()
    assert job is not None
    assert job.state == "retry_wait"
    assert db.jobs[job.id]["available_at"] > clock.now


async def test_runner_leaves_job_for_reaper_when_lease_lost() -> None:
    async def handler(ctx) -> None:
        raise LeaseLost("stale lease mid-run")

    runner, db, _clock = make_runner(handlers={"discover": handler})
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service)
    job = await runner.run_once()
    assert job is not None
    # Still running with the (already stale) lease; the reaper owns recovery.
    assert db.jobs[job.id]["state"] == "running"


async def test_runner_fails_job_on_misclassified_failure() -> None:
    """A handler-level error class (schema_output, 07 §6) must not escape
    run_once: the job fails closed as an internal error instead."""

    async def handler(ctx) -> None:
        raise JobFailure("schema_output", "predict mismatch")

    runner, db, _clock = make_runner(handlers={"discover": handler})
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service)
    job = await runner.run_once()
    assert job is not None
    assert job.state == "failed"
    assert db.jobs[job.id]["error"]["code"] == "internal_error"


async def test_default_deadline_is_15_minutes() -> None:
    assert DEFAULT_JOB_DEADLINE == timedelta(minutes=15)


async def test_run_context_boundary_raises_on_deadline() -> None:
    @asynccontextmanager
    async def open_store(scope):
        yield InMemoryJobsStore(InMemoryJobsDatabase())

    service = JobService(clock=FakeClock(), rng=random.Random(3))
    db = InMemoryJobsDatabase()
    job, _ = await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(owner_id=OWNER),
        kind="discover",
        payload={},
        idempotency_key="k-boundary",
    )
    from intel.workers.runner import RunContext

    ctx = RunContext(
        job=job,
        service=service,
        open_store=open_store,
        deadline_at=T0,
        clock=FakeClock(T0 + timedelta(seconds=1)),
    )
    with pytest.raises(JobDeadlineExceeded):
        await ctx.boundary()


_ = (JobCancelled, JobDeadlineExceeded)
