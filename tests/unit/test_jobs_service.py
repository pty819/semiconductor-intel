"""Unit tests: JobService against an in-memory JobsStore (spec 07 全量语义).

Spec references:
- docs/07-workflows.md §1  states, retry_wait requeue, waiting_review,
  partial 必须给具体缺口, cancellation at step boundaries
- docs/07-workflows.md §2  FOR UPDATE SKIP LOCKED claim, lease_token /
  lease_until fencing, attempt, job_events seq in-transaction
- docs/07-workflows.md §3  idempotency-key composition per kind
- docs/07-workflows.md §6  retry classification table
- docs/07-workflows.md §7  job_events 序号单调

No real database: InMemoryJobsStore simulates row-level locking (SKIP
LOCKED), the lease fencing predicate and the UNIQUE constraints the real
schema carries. SQL rendering itself is covered by tests/unit/
test_jobs_sql.py; JOB-01/02/03 run again against Postgres in
tests/integration/test_jobs.py.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from intel.repositories.base import IndustryScope
from intel.repositories.jobs import (
    InMemoryJobsDatabase,
    InMemoryJobsStore,
    JobEventRecord,
    JobRecord,
    JobsStore,
)
from intel.services.errors import InvalidStateTransition, ValidationFailed
from intel.services.jobs import (
    TERMINAL_STATES,
    JobService,
    LeaseLost,
    build_idempotency_key,
    classify_failure,
)
from intel.workers.runner import JobCancelled, RunContext

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OWNER = uuid4()
INDUSTRY = uuid4()


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make_service(
    clock: FakeClock | None = None,
) -> tuple[JobService, InMemoryJobsDatabase, FakeClock]:
    clock = clock if clock is not None else FakeClock()
    service = JobService(clock=clock, rng=random.Random(42))
    return service, InMemoryJobsDatabase(), clock


def store(db: InMemoryJobsDatabase) -> InMemoryJobsStore:
    return InMemoryJobsStore(db)


async def enqueue_simple(
    service: JobService,
    db: InMemoryJobsDatabase,
    *,
    kind: str = "discover",
    key: str | None = None,
    scope: IndustryScope | None = None,
    max_attempts: int = 5,
) -> JobRecord:
    scope = scope if scope is not None else IndustryScope(owner_id=OWNER)
    job, created = await service.enqueue(
        store(db),
        scope,
        kind=kind,
        payload={"n": 1},
        idempotency_key=key if key is not None else f"k-{uuid4().hex[:8]}",
        max_attempts=max_attempts,
    )
    assert created
    return job


# --- enqueue: idempotency (07 §3) --------------------------------------------


async def test_enqueue_conflict_returns_existing_job() -> None:
    service, db, _clock = make_service()
    first, created = await service.enqueue(
        store(db),
        IndustryScope(owner_id=OWNER),
        kind="discover",
        payload={"a": 1},
        idempotency_key="discover:o1/f1/s1/c1",
    )
    assert created
    again, created2 = await service.enqueue(
        store(db),
        IndustryScope(owner_id=OWNER),
        kind="discover",
        payload={"a": 2},  # different payload: the key wins, not the content
        idempotency_key="discover:o1/f1/s1/c1",
    )
    assert created2 is False
    assert again.id == first.id
    assert again.state == first.state
    # Only one row was ever written.
    assert len(db.jobs) == 1


async def test_enqueue_emits_queued_event() -> None:
    service, db, _clock = make_service()
    job = await enqueue_simple(service, db)
    events = db.events[job.id]
    assert [e.type for e in events] == ["queued"]
    assert events[0].seq == 1


# --- idempotency key composition (07 §3 表) -----------------------------------


def test_idempotency_key_composition_per_kind() -> None:
    key = build_idempotency_key(
        "discover",
        {
            "owner": "o1",
            "feed": "f1",
            "schedule_slot": "17",
            "config_version": "3",
        },
    )
    assert key == "discover:o1/f1/17/3"
    # Order follows the spec table regardless of dict order.
    key2 = build_idempotency_key(
        "discover",
        {
            "config_version": "3",
            "schedule_slot": "17",
            "feed": "f1",
            "owner": "o1",
        },
    )
    assert key2 == key
    assert build_idempotency_key(
        "archive_answer", {"industry": "i1", "message_id": "m1"}
    ) == "archive_answer:i1/m1"


def test_idempotency_key_rejects_wrong_parts() -> None:
    with pytest.raises(ValidationFailed):
        build_idempotency_key("discover", {"owner": "o1"})
    with pytest.raises(ValidationFailed):
        build_idempotency_key(
            "discover",
            {
                "owner": "o1",
                "feed": "f1",
                "schedule_slot": "1",
                "config_version": "1",
                "extra": "x",
            },
        )
    with pytest.raises(ValidationFailed):
        build_idempotency_key("not_a_kind", {"owner": "o1"})


# --- claim: JOB-01 concurrent claims, one winner (07 §2) ----------------------


async def test_job01_two_concurrent_claims_have_one_winner() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db)

    w1 = InMemoryJobsStore(db, session_id="w1")
    w2 = InMemoryJobsStore(db, session_id="w2")
    job1, job2 = await asyncio.gather(service.claim(w1), service.claim(w2))

    winners = [j for j in (job1, job2) if j is not None]
    assert len(winners) == 1, "exactly one worker may hold the lease"
    winner = winners[0]
    assert winner.state == "running"
    assert winner.attempt == 1
    assert winner.lease_token
    assert winner.lease_until == clock.now + timedelta(seconds=90)
    assert (job1 is None) != (job2 is None), "the loser claims nothing"


async def test_claim_orders_by_available_at() -> None:
    service, db, clock = make_service()
    early = await enqueue_simple(service, db, key="early")
    late = await enqueue_simple(service, db, key="late")
    db.jobs[late.id]["available_at"] = clock.now - timedelta(seconds=5)

    claimed = await service.claim(store(db))
    assert claimed is not None
    assert claimed.id == late.id
    assert db.jobs[early.id]["state"] == "queued"


# --- fencing: JOB-02 stale worker cannot write (07 §2) ------------------------


async def test_job02_expired_lease_rejects_step_commit() -> None:
    service, db, clock = make_service()
    job = await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None

    # Time passes beyond the 90s lease without a heartbeat.
    clock.advance(91)

    with pytest.raises(LeaseLost):
        await service.complete_step(
            store(db),
            claimed,
            step_key="enumerate",
            input_hash="h1",
            output_ref="obj://step-1",
        )
    # Nothing was committed by the stale worker.
    fresh = await store(db).get_job(job.id)
    assert fresh is not None
    assert fresh.state == "running"
    assert (job.id, "enumerate", "h1") not in db.steps
    assert [e.type for e in db.events[job.id]] == ["queued"]


async def test_job02_stale_worker_cannot_finish_job() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    clock.advance(120)

    with pytest.raises(LeaseLost):
        await service.finish(store(db), claimed, state="succeeded")
    assert db.jobs[claimed.id]["state"] == "running"


async def test_heartbeat_extends_lease_and_rejects_after_expiry() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None

    clock.advance(60)
    assert await service.heartbeat(store(db), claimed.id, claimed.lease_token or "")
    # Heartbeat within the lease pushed lease_until out by another window.
    fresh = await store(db).get_job(claimed.id)
    assert fresh is not None
    assert fresh.lease_until == clock.now + timedelta(seconds=90)

    clock.advance(200)  # far past the extended lease
    assert not await service.heartbeat(
        store(db), claimed.id, claimed.lease_token or ""
    )


# --- JOB-03: crash after commit, re-run does not duplicate (07 §2) ------------


async def test_job03_crash_after_step_commit_rerun_does_not_duplicate() -> None:
    service, db, clock = make_service()
    job = await enqueue_simple(service, db)
    first = await service.claim(store(db))
    assert first is not None

    # Worker A commits step 1 durably, then "crashes" before finishing.
    await service.complete_step(
        store(db),
        first,
        step_key="enumerate",
        input_hash="h1",
        output_ref="obj://step-1",
    )
    assert len(db.steps) == 1

    # Lease expires; the reaper requeues; worker B claims with a new lease.
    clock.advance(120)
    await service.reap_expired(store(db))
    clock.advance(61)  # past the retry_wait backoff floor
    await service.requeue_due(store(db))
    second = await service.claim(store(db))
    assert second is not None
    assert second.id == job.id
    assert second.attempt == 2
    assert second.lease_token != first.lease_token

    # Worker B re-runs the same step: same input_hash converges to ONE row.
    rerun = await service.complete_step(
        store(db),
        second,
        step_key="enumerate",
        input_hash="h1",
        output_ref="obj://step-1",
    )
    assert rerun.state == "succeeded"
    assert len(db.steps) == 1, "idempotent commit must not duplicate the step"
    # And the job can be finished cleanly.
    final = await service.finish(store(db), second, state="succeeded")
    assert final.state == "succeeded"
    assert final.lease_token is None, "terminal state releases the lease"


async def test_job03_late_first_worker_is_fenced_out() -> None:
    """Worker A wakes up after B claimed: A's commit hits the fencing
    predicate (JOB-02 semantics applied to the JOB-03 timeline)."""
    service, db, clock = make_service()
    await enqueue_simple(service, db)
    first = await service.claim(store(db))
    assert first is not None

    clock.advance(120)
    await service.reap_expired(store(db))
    clock.advance(61)
    await service.requeue_due(store(db))
    second = await service.claim(store(db))
    assert second is not None

    with pytest.raises(LeaseLost):
        await service.complete_step(
            store(db),
            first,  # stale token
            step_key="enumerate",
            input_hash="h1",
            output_ref="obj://step-1-stale",
        )
    assert (first.id, "enumerate", "h1") not in db.steps


# --- cancellation at step boundaries (07 §1) ----------------------------------


async def test_request_cancel_marks_running_job() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None

    assert await service.request_cancel(store(db), claimed.id)
    fresh = await store(db).get_job(claimed.id)
    assert fresh is not None
    assert fresh.cancel_requested_at == clock.now
    # Idempotent: a second request does not overwrite the timestamp.
    assert not await service.request_cancel(store(db), claimed.id)


async def test_cancel_fails_on_terminal_job() -> None:
    service, db, _clock = make_service()
    job = await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    await service.finish(store(db), claimed, state="succeeded")
    assert not await service.request_cancel(store(db), job.id)


async def test_cancellation_takes_effect_at_step_boundary() -> None:
    """cancel_requested_at is observed at the next step boundary; the
    worker finishes the job as cancelled, not succeeded."""
    from contextlib import asynccontextmanager

    service, db, clock = make_service()
    job = await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    await service.request_cancel(store(db), claimed.id)

    @asynccontextmanager
    async def open_store(scope):
        yield InMemoryJobsStore(db)

    ctx = RunContext(
        job=claimed,
        service=service,
        open_store=open_store,
        deadline_at=clock.now + timedelta(minutes=15),
        clock=clock,
    )
    with pytest.raises(JobCancelled):
        await ctx.boundary()

    final = await service.finish(store(db), claimed, state="cancelled")
    assert final.state == "cancelled"
    assert "cancelled" in [e.type for e in db.events[job.id]]


async def test_cancelled_queued_job_finishes_cancelled_after_claim() -> None:
    """A queued job's cancellation request survives the claim: the worker
    sees cancel_requested_at and finishes cancelled at its first boundary
    (07 §1: 取消在 step 边界生效 — finish itself stays a fenced, worker-side
    operation)."""
    service, db, _clock = make_service()
    job = await enqueue_simple(service, db)
    assert await service.request_cancel(store(db), job.id)
    claimed = await service.claim(store(db))
    assert claimed is not None, "claim still hands the job to a worker"
    assert await service.cancel_requested(store(db), claimed.id)
    final = await service.finish(store(db), claimed, state="cancelled")
    assert final.state == "cancelled"


# --- state machine (07 §1) -----------------------------------------------------


async def test_illegal_state_transitions_rejected() -> None:
    service, db, _clock = make_service()
    job = await enqueue_simple(service, db)
    with pytest.raises(InvalidStateTransition):
        await service.finish(store(db), job, state="succeeded")  # queued→succeeded
    claimed = await service.claim(store(db))
    assert claimed is not None
    with pytest.raises(InvalidStateTransition):
        await service.finish(store(db), claimed, state="queued")  # running→queued
    await service.finish(store(db), claimed, state="waiting_review")
    assert db.jobs[job.id]["state"] == "waiting_review"
    assert db.jobs[job.id]["lease_token"] is None, "waiting_review occupies nothing"
    assert "review_required" in [e.type for e in db.events[job.id]]


async def test_partial_requires_specific_gaps() -> None:
    service, db, _clock = make_service()
    await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    with pytest.raises(ValidationFailed):
        await service.finish(store(db), claimed, state="partial")
    final = await service.finish(
        store(db),
        claimed,
        state="partial",
        error={"gaps": ["source A enumeration failed"]},
    )
    assert final.state == "partial"


async def test_progress_from_later_steps_keeps_earlier_keys() -> None:
    """Step N's progress merge starts from the committed progress, not the
    claim-time snapshot — keys written by earlier steps survive."""
    service, db, _clock = make_service()
    await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    await service.complete_step(
        store(db), claimed, step_key="s1", input_hash="h1",
        output_ref="obj://1", progress={"done": 1, "note": "step one"},
    )
    await service.complete_step(
        store(db), claimed, step_key="s2", input_hash="h2",
        output_ref="obj://2", progress={"done": 2},
    )
    assert db.jobs[claimed.id]["progress"] == {
        "done": 2,
        "note": "step one",
    }


# --- job_events seq monotonicity (07 §7) ---------------------------------------


async def test_job_events_seq_is_monotonic() -> None:
    service, db, _clock = make_service()
    job = await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    for i in range(3):
        await service.complete_step(
            store(db),
            claimed,
            step_key=f"s{i}",
            input_hash=f"h{i}",
            output_ref=f"obj://{i}",
        )
    await service.finish(store(db), claimed, state="succeeded")

    seqs = [e.seq for e in db.events[job.id]]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "seq values must be unique"
    assert seqs[0] == 1
    # queued + 3× artifact_ready + completed
    assert [e.type for e in db.events[job.id]] == [
        "queued",
        "artifact_ready",
        "artifact_ready",
        "artifact_ready",
        "completed",
    ]


def test_store_rejects_duplicate_event_seq() -> None:
    """The PK (job_id, seq) simulation: a duplicate insert raises, which is
    what makes in-transaction max+1 allocation safe (07 §7)."""
    import asyncio as aio

    db = InMemoryJobsDatabase()
    job_id = UUID(int=0)
    db.jobs[job_id] = {
        "owner_id": OWNER,
        "industry_id": None,
        "kind": "discover",
        "state": "queued",
    }
    db.events[job_id] = []
    s = InMemoryJobsStore(db)
    event = JobEventRecord(
        owner_id=OWNER,
        industry_id=None,
        job_id=job_id,
        seq=1,
        type="queued",
        data={},
        created_at=T0,
    )
    aio.run(s.insert_event(event))
    with pytest.raises(ValueError, match="seq"):
        aio.run(s.insert_event(event))


# --- retry classification table (07 §6) ----------------------------------------


def _decide(error_class: str, *, attempt: int, retry_after: float | None = None):
    return classify_failure(
        error_class, attempt=attempt, retry_after=retry_after, rng=random.Random(7)
    )


def test_retry_classification_transient_backoff() -> None:
    d = _decide("transient", attempt=1)
    assert d.next_state == "retry_wait"
    assert d.delay_seconds is not None and 30 <= d.delay_seconds < 60
    # Exponential growth with a cap at 30 minutes.
    d5 = _decide("transient", attempt=4)
    assert d5.delay_seconds is not None and d5.delay_seconds <= 30 * 60
    d_last = _decide("transient", attempt=5)
    assert d_last.next_state == "failed", "5 attempts max (07 §6)"


def test_retry_classification_retry_after_wins() -> None:
    d = _decide("transient", attempt=2, retry_after=120)
    assert d.next_state == "retry_wait"
    assert d.delay_seconds == 120


def test_retry_classification_access_blocked_no_retry() -> None:
    d = _decide("access_blocked", attempt=1)
    assert d.next_state == "failed"
    assert d.delay_seconds is None
    assert d.error_code == "access_blocked"


def test_retry_classification_parser_error_one_retry() -> None:
    first = _decide("parser_error", attempt=1)
    assert first.next_state == "retry_wait", "one same-version retry allowed"
    second = _decide("parser_error", attempt=2)
    assert second.next_state == "failed"


def test_retry_classification_scope_violation_fails_with_audit() -> None:
    d = _decide("scope_violation", attempt=1)
    assert d.next_state == "failed"
    assert d.audit is True
    assert d.delay_seconds is None


def test_retry_classification_user_cancellation() -> None:
    d = _decide("cancelled", attempt=1)
    assert d.next_state == "cancelled"


def test_retry_classification_handler_level_classes_are_recorded() -> None:
    """schema_output / validation_failed are NOOA/handler-level (07 §6:
    Predict 校验重试2、quote 修复1): classification reports them, the job
    state machine does not fail the job."""
    d = _decide("schema_output", attempt=1)
    assert d.next_state is None
    assert d.repair_budget == 2
    v = _decide("validation_failed", attempt=1)
    assert v.next_state is None
    assert v.repair_budget == 1


def test_retry_classification_unknown_class_fails_closed() -> None:
    d = _decide("something_else", attempt=1)
    assert d.next_state == "failed"


async def test_fail_schedules_retry_wait_with_available_at() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db, max_attempts=5)
    claimed = await service.claim(store(db))
    assert claimed is not None
    delayed = await service.fail(
        store(db), claimed, error_class="transient", message="503"
    )
    assert delayed.state == "retry_wait"
    assert delayed.available_at > clock.now
    assert delayed.lease_token is None
    assert delayed.error is not None and delayed.error["code"] == "transient"


async def test_retry_wait_requeues_when_available() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db, max_attempts=5)
    claimed = await service.claim(store(db))
    assert claimed is not None
    delayed = await service.fail(
        store(db), claimed, error_class="transient", message="timeout"
    )
    # Not due yet.
    assert await service.requeue_due(store(db)) == 0
    clock.advance((delayed.available_at - clock.now).total_seconds() + 1)
    assert await service.requeue_due(store(db)) == 1
    assert db.jobs[delayed.id]["state"] == "queued"


async def test_fail_access_blocked_is_terminal() -> None:
    service, db, _clock = make_service()
    await enqueue_simple(service, db)
    claimed = await service.claim(store(db))
    assert claimed is not None
    final = await service.fail(
        store(db), claimed, error_class="access_blocked", message="403"
    )
    assert final.state == "failed"
    assert final.error is not None and final.error["code"] == "access_blocked"


# --- lease reaping (07 §1 worker 消失) ------------------------------------------


async def test_reap_expired_moves_running_to_retry_wait() -> None:
    service, db, clock = make_service()
    await enqueue_simple(service, db, max_attempts=5)
    claimed = await service.claim(store(db))
    assert claimed is not None
    clock.advance(91)
    assert await service.reap_expired(store(db)) == 1
    fresh = await store(db).get_job(claimed.id)
    assert fresh is not None
    assert fresh.state == "retry_wait"
    assert fresh.error is not None and fresh.error["code"] == "lease_expired"
    assert fresh.available_at > clock.now


async def test_reap_expired_fails_job_at_max_attempts() -> None:
    service, db, clock = make_service()
    job = await enqueue_simple(service, db, max_attempts=1)
    claimed = await service.claim(store(db))
    assert claimed is not None
    assert claimed.attempt == 1  # == max_attempts
    clock.advance(91)
    await service.reap_expired(store(db))
    assert db.jobs[job.id]["state"] == "failed"


# --- store protocol sanity ------------------------------------------------------


def test_in_memory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryJobsStore(InMemoryJobsDatabase()), JobsStore)
    assert TERMINAL_STATES == {"succeeded", "partial", "failed", "cancelled"}
