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
    *,
    handlers=None,
    deadlines=None,
    default_deadline=DEFAULT_JOB_DEADLINE,
    heartbeat_interval=None,
    sleep=None,
) -> tuple[JobRunner, InMemoryJobsDatabase, FakeClock]:
    db = InMemoryJobsDatabase()
    clock = FakeClock()
    service = JobService(clock=clock, rng=random.Random(3))

    @asynccontextmanager
    async def open_store(
        scope: IndustryScope | None,
    ) -> AsyncIterator[InMemoryJobsStore]:
        yield InMemoryJobsStore(db)

    kwargs: dict = {
        "handlers": handlers,
        "deadlines": deadlines,
        "default_deadline": default_deadline,
        "clock": clock,
    }
    if heartbeat_interval is not None:
        kwargs["heartbeat_interval"] = heartbeat_interval
    if sleep is not None:
        kwargs["sleep"] = sleep
    runner = JobRunner(service, open_store, **kwargs)
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


async def test_runner_skips_kinds_without_handlers() -> None:
    """A fetch-worker must not claim a discover job (composition-root roles)."""
    seen: list[str] = []

    async def handler(ctx) -> None:
        seen.append(ctx.job.kind)
        await ctx.boundary()
        async with ctx.open_store(
            IndustryScope(ctx.job.owner_id, ctx.job.industry_id)
        ) as store:
            await ctx.service.finish(store, ctx.job, state="succeeded")

    runner, db, _clock = make_runner(handlers={"fetch": handler})
    service = JobService(clock=FakeClock(), rng=random.Random(3))
    await seed_job(db, service, kind="discover")
    assert await runner.run_once() is None
    assert seen == []
    await seed_job(db, service, kind="fetch")
    job = await runner.run_once()
    assert seen == ["fetch"]
    assert job is not None and job.kind == "fetch"


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


# --- Fix round 1: the runner owns the lease lifecycle -----------------------


def _fake_sleep(clock: FakeClock):
    """Sleep that advances the injected clock and yields to the loop, so
    the heartbeat task interleaves with a "long" handler deterministically."""

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return sleep


async def test_long_running_handler_keeps_lease_via_heartbeats() -> None:
    """07 §2: a handler spanning well past the 90s lease must keep it —
    the runner heartbeats every 30s while the handler runs, so the reaper
    never requeues a live worker's job."""
    clock = FakeClock()
    sleep = _fake_sleep(clock)

    async def handler(ctx) -> None:
        for _ in range(8):  # 8 × 15s = 120s of work, > one lease window
            await sleep(15)
        await ctx.boundary()
        async with ctx.open_store(ctx.scope) as store:
            await ctx.service.finish(store, ctx.job, state="succeeded")

    db = InMemoryJobsDatabase()
    service = JobService(clock=clock, rng=random.Random(3))

    @asynccontextmanager
    async def open_store(scope):
        yield InMemoryJobsStore(db)

    runner = JobRunner(
        service,
        open_store,
        handlers={"discover": handler},
        heartbeat_interval=30,
        sleep=sleep,
        clock=clock,
    )
    await seed_job(db, service, kind="discover")
    # Spy on the service heartbeat: finish() would also bump heartbeat_at,
    # so only observed heartbeat calls prove the runner-side task ran.
    heartbeats: list[bool] = []
    original_heartbeat = service.heartbeat

    async def counting_heartbeat(store, job_id, token, **kwargs):
        alive = await original_heartbeat(store, job_id, token, **kwargs)
        heartbeats.append(alive)
        return alive

    service.heartbeat = counting_heartbeat  # type: ignore[method-assign]

    job = await runner.run_once()
    assert job.state == "succeeded"

    assert len(heartbeats) >= 2, "the heartbeat task must fire during a long run"
    assert all(heartbeats), "the lease must have been alive at each heartbeat"
    # The claim writes heartbeat_at = T0 exactly; a late timestamp on the
    # terminal row is corroborating evidence (lease_until is released by
    # the terminal finish, so it cannot be asserted here).
    row = db.jobs[job.id]
    assert row["heartbeat_at"] > T0 + timedelta(seconds=60)
    assert row["lease_until"] is None


async def test_heartbeat_loss_cancels_handler_and_leaves_row_alone() -> None:
    """When the heartbeat fence fails (lease taken over), the handler is
    cancelled and the row is left to its new owner — the run does not
    write through the dead lease."""
    clock = FakeClock()
    sleep = _fake_sleep(clock)

    async def handler(ctx) -> None:
        for _ in range(20):
            await sleep(30)

    db = InMemoryJobsDatabase()
    service = JobService(clock=clock, rng=random.Random(3))

    @asynccontextmanager
    async def open_store(scope):
        yield InMemoryJobsStore(db)

    runner = JobRunner(
        service,
        open_store,
        handlers={"discover": handler},
        heartbeat_interval=30,
        sleep=_fake_sleep(clock),
        clock=clock,
    )
    await seed_job(db, service, kind="discover")
    run = asyncio.ensure_future(runner.run_once())
    (job_id,) = list(db.jobs)
    for _ in range(1000):  # wait until the run has claimed the job
        if db.jobs[job_id]["state"] == "running" and db.jobs[job_id]["lease_token"]:
            break
        await asyncio.sleep(0)
    else:  # pragma: no cover - claim never happened
        raise AssertionError("run_once did not claim the job")
    # Simulate a take-over: another worker re-claimed while we ran.
    db.jobs[job_id]["lease_token"] = "other-worker-token"

    await run  # must settle without raising
    assert db.jobs[job_id]["lease_token"] == "other-worker-token"
    assert db.jobs[job_id]["state"] == "running", "new owner's run untouched"


async def test_zombie_finalize_does_not_fail_new_owners_run() -> None:
    """Finding 1b: a run that lost its lease and then hit its deadline
    must NOT fail the job the new owner is running — the finalize guard
    only writes when the claim's token still matches."""

    async def handler(ctx) -> None:
        await asyncio.sleep(10)  # real sleep; killed by the tiny deadline

    runner, db, clock = make_runner(
        handlers={"discover": handler},
        deadlines={"discover": timedelta(milliseconds=30)},
    )
    service = JobService(clock=clock, rng=random.Random(3))
    await seed_job(db, service, kind="discover")
    run = asyncio.ensure_future(runner.run_once())
    await asyncio.sleep(0.005)  # A claimed; handler sleeping

    # A's lease expires → reaper requeues → worker B re-claims.
    clock.advance(120)
    await service.reap_expired(InMemoryJobsStore(db))
    clock.advance(61)
    await service.requeue_due(InMemoryJobsStore(db))
    second = await service.claim(InMemoryJobsStore(db))
    assert second is not None
    assert second.lease_token is not None

    result = await run  # A's deadline fires here — must be a no-op
    assert db.jobs[second.id]["state"] == "running", "B's run must survive"
    assert db.jobs[second.id]["lease_token"] == second.lease_token
    # The only error on the row is the reaper's lease_expired marker —
    # A's deadline_exceeded must never land on B's run.
    assert db.jobs[second.id]["error"]["code"] == "lease_expired"
    assert result.state == "running"


async def test_finalize_against_retry_wait_does_not_raise() -> None:
    """Finding 1b: finalizing a run whose job the reaper already moved to
    retry_wait (lease cleared) is a no-op — not an InvalidStateTransition
    escaping run_once."""

    async def handler(ctx) -> None:
        await asyncio.sleep(10)

    runner, db, clock = make_runner(
        handlers={"discover": handler},
        deadlines={"discover": timedelta(milliseconds=30)},
    )
    service = JobService(clock=clock, rng=random.Random(3))
    await seed_job(db, service, kind="discover")
    run = asyncio.ensure_future(runner.run_once())
    await asyncio.sleep(0.005)

    clock.advance(120)
    await service.reap_expired(InMemoryJobsStore(db))  # → retry_wait
    assert db.jobs[next(iter(db.jobs))]["state"] == "retry_wait"

    result = await run  # deadline fires against the reaped row
    assert result.state == "retry_wait"
    assert db.jobs[result.id]["state"] == "retry_wait"


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
