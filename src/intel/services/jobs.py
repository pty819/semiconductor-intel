"""Job service: the queue semantics on top of a JobsStore (spec 07).

The store (``repositories.jobs``) owns SQL and isolation; this module owns
the rules:

- **State machine** (07 §1): ``queued → running → succeeded | partial |
  failed | waiting_review | cancelled``; retryable failures park in
  ``retry_wait`` until ``available_at`` requeues them; ``waiting_review``
  occupies no worker; ``partial`` must carry specific gaps.
- **Fencing** (07 §2): every step/terminal commit runs the lease predicate
  through the store and raises :class:`LeaseLost` on rowcount 0 — a stale
  worker cannot write business results. Transactions never span network or
  LLM calls: each method takes the store (one short transaction) and
  returns before any external work happens.
- **Idempotency** (07 §3): enqueue conflicts on
  UNIQUE(owner_id, kind, idempotency_key) return the existing job;
  :data:`KIND_SPECS` carries the per-kind key composition; re-running a
  committed step converges on (job_id, step_key, input_hash).
- **Retry classification** (07 §6): the table below maps an error class to
  its next state / delay / budget.
- **Events** (07 §7): job_events rows are appended with seq allocated
  in-transaction (max+1 under the PK).

The two-role pattern (spec 10 §1): ``claim``/``requeue_due``/``reap_expired``
run on a dispatcher-role store (unscoped; the connection's database role
owns the queue tables); everything after the claim runs owner-scoped —
``enqueue`` takes the scope explicitly, fenced methods read it off the job.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from intel.contracts import JobAccepted
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import (
    JobEventRecord,
    JobRecord,
    JobsStore,
    JobStepRecord,
    SqlAlchemyJobsStore,
)
from intel.services.acquisition import job_accepted
from intel.services.errors import InvalidStateTransition, ValidationFailed
from intel.workers.leases import LEASE_SECONDS, new_lease

#: Spec 07 §1 terminal states.
TERMINAL_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})

#: Allowed transitions (spec 07 §1). waiting_review → running covers the
#: resume path; continuation jobs are new jobs, not transitions.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset(
        {"succeeded", "partial", "failed", "waiting_review", "retry_wait",
         "cancelled"}
    ),
    "retry_wait": frozenset({"queued", "cancelled"}),
    "waiting_review": frozenset({"running", "cancelled"}),
}

#: job_events types (spec 07 §7) — business summaries only.
EV_QUEUED = "queued"
EV_STAGE_STARTED = "stage_started"
EV_PROGRESS = "progress"
EV_ARTIFACT_READY = "artifact_ready"
EV_REVIEW_REQUIRED = "review_required"
EV_COMPLETED = "completed"
EV_FAILED = "failed"
EV_CANCELLED = "cancelled"

_TERMINAL_EVENT = {
    "succeeded": EV_COMPLETED,
    "partial": EV_COMPLETED,
    "failed": EV_FAILED,
    "cancelled": EV_CANCELLED,
    "waiting_review": EV_REVIEW_REQUIRED,
}


class LeaseLost(RuntimeError):
    """A fenced write matched zero rows: the caller no longer holds the
    lease (expired or re-claimed) and must stop immediately (07 §2)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# idempotency keys per kind (spec 07 §3 表)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KindSpec:
    """The documented key composition and attempt budget for one kind."""

    kind: str
    key_parts: tuple[str, ...]
    #: Max attempts for network-facing kinds is 5 (07 §6 最多5次).
    max_attempts: int = 3

    def build_key(self, parts: Mapping[str, str]) -> str:
        missing = set(self.key_parts) - set(parts)
        extra = set(parts) - set(self.key_parts)
        if missing or extra:
            raise ValidationFailed(
                f"idempotency key for kind {self.kind!r} needs exactly"
                f" {list(self.key_parts)}; missing={sorted(missing)},"
                f" extra={sorted(extra)}"
            )
        joined = "/".join(parts[name] for name in self.key_parts)
        return f"{self.kind}:{joined}"


#: The 14-kind table (spec 07 §3). “强制重跑” mints a new refresh_epoch /
#: run nonce rather than mutating these keys.
KIND_SPECS: dict[str, KindSpec] = {
    spec.kind: spec
    for spec in (
        KindSpec("discover", ("owner", "feed", "schedule_slot", "config_version"), 5),
        KindSpec("fetch", ("owner", "discovery_item", "refresh_epoch"), 5),
        KindSpec("parse", ("owner", "capture", "parser_version")),
        KindSpec(
            "index", ("owner", "parse", "chunker_version", "embedding_version")
        ),
        KindSpec(
            "route",
            ("owner", "industry", "parse", "industry_revision", "analysis_version"),
        ),
        KindSpec(
            "topic_recall",
            ("industry", "topic_revision", "window", "index_generation"),
        ),
        KindSpec("extract", ("industry", "parse", "extraction_version")),
        KindSpec(
            "event_build", ("industry", "extraction_commit", "event_policy_version")
        ),
        KindSpec("evolution_build", ("industry", "topic_revision", "input_manifest_hash")),
        KindSpec(
            "report_build",
            ("industry", "report_type", "period", "input_manifest_hash"),
        ),
        KindSpec("archive_answer", ("industry", "message_id")),
        KindSpec("investigate", ("industry", "message_id", "request_version")),
        KindSpec("reclassify", ("industry", "config_revision", "replay_window")),
        KindSpec("apply_review", ("industry", "review_id", "decision_version")),
        # Scheduler-internal periodic watch check (spec 03 §4 首版定期检查
        # 仅本地归档): keyed per watch per cadence slot.
        KindSpec("watch_check", ("industry", "watch", "schedule_slot")),
    )
}


def build_idempotency_key(kind: str, parts: Mapping[str, str]) -> str:
    """Compose the idempotency key for ``kind`` from exactly its spec parts
    (07 §3). Unknown kinds or wrong part sets are rejected — a mistyped key
    would silently break the replay semantics."""
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise ValidationFailed(f"unknown job kind {kind!r} (spec 07 §3)")
    return spec.build_key(parts)


# --------------------------------------------------------------------------
# retry classification (spec 07 §6)
# --------------------------------------------------------------------------

#: Exponential backoff floor/ceiling for transient errors (07 §6: 30秒起，
#: 最长30分钟).
TRANSIENT_BACKOFF_BASE_S = 30.0
TRANSIENT_BACKOFF_CAP_S = 30 * 60.0
#: 最多5次 for timeout/429/temporary-5xx (07 §6).
TRANSIENT_MAX_ATTEMPTS = 5
#: parser_error: 最多1次同版本重试 (07 §6) → the second attempt is its last.
PARSER_MAX_ATTEMPTS = 2
#: schema 输出错误: NOOA Predict 校验重试上限2 (handler-side, 07 §6).
SCHEMA_REPAIR_BUDGET = 2
#: quote/业务验证失败: 最多额外1次修复，仍失败保留 proposal (07 §6).
VALIDATION_REPAIR_BUDGET = 1
#: Stale-lease requeue delay floor (07 §1: worker 消失 → retry_wait).
LEASE_EXPIRY_BACKOFF_BASE_S = 30.0


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """What the state machine does with a failure of this class.

    ``next_state`` is ``None`` for handler-level classes (schema_output,
    validation_failed): the job stays running while the NOOA-side retries
    or the repair budget applies — recorded via ``repair_budget``.
    """

    error_class: str
    next_state: str | None
    delay_seconds: float | None = None
    error_code: str = ""
    audit: bool = False
    repair_budget: int = 0


def _jittered_backoff(attempt: int, rng: random.Random) -> float:
    """Exponential backoff with jitter: 30s·2^(attempt-1), jittered up to
    2× and capped at 30 minutes — never below the 30s floor (07 §6)."""
    raw = min(
        TRANSIENT_BACKOFF_BASE_S * (2 ** max(attempt - 1, 0)),
        TRANSIENT_BACKOFF_CAP_S,
    )
    return min(raw * (1.0 + rng.random()), TRANSIENT_BACKOFF_CAP_S)


def classify_failure(
    error_class: str,
    *,
    attempt: int,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> RetryDecision:
    """Map one failure to its retry/fail/cancel decision (07 §6 table).

    ``attempt`` is the job's current attempt counter (post-claim).
    ``retry_after`` (seconds) wins over computed backoff when present.
    """
    rng = rng if rng is not None else random.Random()
    if error_class == "transient":  # timeout / 429 / temporary 5xx
        if attempt >= TRANSIENT_MAX_ATTEMPTS:
            return RetryDecision("transient", "failed", error_code="transient")
        delay = (
            float(retry_after)
            if retry_after is not None
            else _jittered_backoff(attempt, rng)
        )
        return RetryDecision(
            "transient", "retry_wait", delay_seconds=delay, error_code="transient"
        )
    if error_class == "schema_output":
        # NOOA Predict 校验重试上限2 — handler-side; the job is not failed.
        return RetryDecision(
            "schema_output", None, error_code="schema_output",
            repair_budget=SCHEMA_REPAIR_BUDGET,
        )
    if error_class == "validation_failed":
        # quote/业务验证失败: 最多额外1次修复，仍失败保留 proposal.
        return RetryDecision(
            "validation_failed", None, error_code="validation_failed",
            repair_budget=VALIDATION_REPAIR_BUDGET,
        )
    if error_class == "access_blocked":  # 401/403/付费墙
        return RetryDecision("access_blocked", "failed", error_code="access_blocked")
    if error_class == "parser_error":
        if attempt >= PARSER_MAX_ATTEMPTS:
            return RetryDecision("parser_error", "failed", error_code="parser_error")
        return RetryDecision(
            "parser_error",
            "retry_wait",
            delay_seconds=TRANSIENT_BACKOFF_BASE_S,
            error_code="parser_error",
        )
    if error_class == "scope_violation":
        # 立即失败并审计，不重试 — the audit_log write rides the same
        # short transaction in the handler layer (Task 7+ wiring).
        return RetryDecision(
            "scope_violation", "failed", error_code="scope_violation", audit=True
        )
    if error_class == "cancelled":
        return RetryDecision("cancelled", "cancelled", error_code="cancelled")
    # Unknown classes fail closed: never loop on an unclassified error.
    return RetryDecision(error_class, "failed", error_code=error_class)


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


def _check_transition(current: str, target: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransition(
            f"jobs cannot move {current!r} → {target!r}",
            action="job_transition",
            current=current,
        )


class JobService:
    """Queue semantics over a :class:`JobsStore`; storage-agnostic.

    Every public method takes the store explicitly: the caller opens the
    (short) transaction, the method never awaits anything but the store —
    no network, no LLM, no cross-transaction state (spec 07 §2).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock if clock is not None else _utcnow
        self._rng = rng if rng is not None else random.Random()

    # -- enqueue (07 §3) --------------------------------------------------------

    async def enqueue(
        self,
        store: JobsStore,
        scope: IndustryScope,
        *,
        kind: str,
        payload: dict,
        idempotency_key: str,
        max_attempts: int | None = None,
    ) -> tuple[JobRecord, bool]:
        """Insert a job; a UNIQUE(owner_id, kind, idempotency_key) conflict
        returns the existing job with ``created=False``.

        ``idempotency_key`` is caller-provided: API requests pass their
        Idempotency-Key header through unchanged (07 §3), the scheduler and
        handlers compose keys with :func:`build_idempotency_key`.
        """
        now = self._clock()
        spec = KIND_SPECS.get(kind)
        record = JobRecord(
            owner_id=scope.owner_id,
            industry_id=scope.industry_id,
            kind=kind,
            state="queued",
            input=dict(payload),
            idempotency_key=idempotency_key,
            available_at=now,
            max_attempts=(
                max_attempts if max_attempts is not None
                else (spec.max_attempts if spec is not None else 3)
            ),
        )
        inserted = await store.insert_job(record)
        if inserted is None:
            existing = await store.find_idempotent_job(
                scope.owner_id, kind, idempotency_key
            )
            if existing is None:  # pragma: no cover - constraint guarantees it
                raise RuntimeError(
                    "idempotency conflict but no existing job found"
                )
            return existing, False
        await self.append_event(
            store,
            inserted,
            type=EV_QUEUED,
            data={"kind": kind, "industry_id": _opt(scope.industry_id)},
        )
        return inserted, True

    # -- claim (07 §2, dispatcher role) --------------------------------------------

    async def claim(self, store: JobsStore, *, now: datetime | None = None) -> JobRecord | None:
        """Claim the oldest due queued job: SELECT ... FOR UPDATE SKIP
        LOCKED LIMIT 1, write a fresh 90s lease and attempt+1."""
        now = now if now is not None else self._clock()
        lease = new_lease(now)
        return await store.claim_next(
            now=now, lease_token=lease.token, lease_until=lease.until
        )

    # -- lease upkeep (07 §2, worker role) ------------------------------------------

    async def heartbeat(
        self,
        store: JobsStore,
        job_id: UUID,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Extend the lease by one window; ``False`` means the lease was
        lost and the caller must abandon the job (fencing)."""
        now = now if now is not None else self._clock()
        lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return (
            await store.fenced_heartbeat(
                job_id, lease_token=lease_token, lease_until=lease_until, now=now
            )
            > 0
        )

    # -- steps (07 §2: the step commit transaction) ----------------------------------

    async def start_step(
        self,
        store: JobsStore,
        job: JobRecord,
        *,
        step_key: str,
        input_hash: str,
    ) -> JobStepRecord:
        """Mark a step running and emit ``stage_started``.

        The lease is fence-checked first (a heartbeat bump doubles as the
        liveness proof); a stale worker cannot even open a step.
        """
        now = self._clock()
        await self._fence(store, job, now)
        step = JobStepRecord(
            owner_id=job.owner_id,
            industry_id=job.industry_id,
            job_id=job.id,
            step_key=step_key,
            state="running",
            input_hash=input_hash,
            attempt=job.attempt,
            started_at=now,
        )
        step = await store.upsert_step(step)
        await self.append_event(
            store, job, type=EV_STAGE_STARTED, data={"step": step_key}
        )
        return step

    async def complete_step(
        self,
        store: JobsStore,
        job: JobRecord,
        *,
        step_key: str,
        input_hash: str,
        output_ref: str | None = None,
        progress: Mapping[str, object] | None = None,
    ) -> JobStepRecord:
        """The step commit: ONE fenced transaction that proves the lease,
        converges the (job_id, step_key, input_hash) row, bumps progress
        and appends ``artifact_ready`` (07 §2: 业务结果、step succeeded、
        outbox job_events 一起提交).

        Re-running a committed step is an idempotent no-op (JOB-03): the
        row exists as succeeded and nothing is duplicated.
        """
        now = self._clock()
        await self._fence(store, job, now)
        existing = await store.get_step(job.id, step_key, input_hash)
        if existing is not None and existing.state == "succeeded":
            return existing
        base = await self._fresh_progress(store, job)
        values: dict[str, object] = {"heartbeat_at": now}
        if progress is not None:
            values["progress"] = {**base, **dict(progress)}
        await self._fenced(store, job, now, values)
        step = await store.upsert_step(
            JobStepRecord(
                owner_id=job.owner_id,
                industry_id=job.industry_id,
                job_id=job.id,
                step_key=step_key,
                state="succeeded",
                input_hash=input_hash,
                output_ref=output_ref,
                attempt=job.attempt,
                started_at=now,
                completed_at=now,
            )
        )
        if output_ref is not None:
            await self.append_event(
                store,
                job,
                type=EV_ARTIFACT_READY,
                data={"step": step_key, "output_ref": output_ref},
            )
        if progress is not None:
            await self.append_event(
                store, job, type=EV_PROGRESS, data=dict(progress)
            )
        return step

    # -- terminal transitions (07 §1) ----------------------------------------------------

    async def finish(
        self,
        store: JobsStore,
        job: JobRecord,
        *,
        state: str,
        output_ref: str | None = None,
        error: dict | None = None,
        progress: Mapping[str, object] | None = None,
    ) -> JobRecord:
        """Move the job to a terminal (or waiting_review) state with
        fencing; the lease is released (waiting_review occupies nothing)."""
        _check_transition(job.state, state)
        if state == "partial":
            gaps = (error or {}).get("gaps")
            if not isinstance(gaps, list) or not gaps:
                raise ValidationFailed(
                    "partial jobs must carry specific gaps (error['gaps'],"
                    " spec 07 §1)"
                )
        now = self._clock()
        values: dict[str, object] = {
            "state": state,
            "lease_token": None,
            "lease_until": None,
            "heartbeat_at": now,
        }
        if output_ref is not None:
            values["output_ref"] = output_ref
        if error is not None:
            values["error"] = error
        if progress is not None:
            base = await self._fresh_progress(store, job)
            values["progress"] = {**base, **dict(progress)}
        await self._fenced(store, job, now, values)
        event_type = _TERMINAL_EVENT[state]
        data: dict[str, object] = {"kind": job.kind}
        if state == "partial":
            data["gaps"] = (error or {}).get("gaps")
        if error is not None:
            data["error"] = error
        await self.append_event(store, job, type=event_type, data=data)
        updated = await store.get_job(job.id)
        assert updated is not None
        return updated

    async def fail(
        self,
        store: JobsStore,
        job: JobRecord,
        *,
        error_class: str,
        message: str,
        retry_after: float | None = None,
        details: dict | None = None,
    ) -> JobRecord:
        """Apply the retry classification (07 §6) to a running job.

        Handler-level classes (schema_output / validation_failed) raise
        :class:`ValidationFailed`: the job is not a failure, the handler
        owns the Predict retries / repair budget.
        """
        decision = classify_failure(
            error_class, attempt=job.attempt, retry_after=retry_after, rng=self._rng
        )
        if decision.next_state is None:
            raise ValidationFailed(
                f"error class {error_class!r} is handler-level"
                f" (repair budget {decision.repair_budget}); the job is not"
                " failed (spec 07 §6)"
            )
        error = {"code": decision.error_code or error_class, "message": message}
        if details:
            error["details"] = details
        if decision.next_state == "cancelled":
            return await self.finish(store, job, state="cancelled", error=error)
        if decision.next_state == "failed":
            if decision.audit:
                error["audit"] = True
            return await self.finish(store, job, state="failed", error=error)
        assert decision.delay_seconds is not None
        now = self._clock()
        available_at = now + timedelta(seconds=decision.delay_seconds)
        await self._fenced(
            store,
            job,
            now,
            {
                "state": "retry_wait",
                "available_at": available_at,
                "lease_token": None,
                "lease_until": None,
                "heartbeat_at": now,
                "error": error,
            },
        )
        await self.append_event(
            store,
            job,
            type=EV_FAILED,
            data={"retry": True, "delay_seconds": decision.delay_seconds, **error},
        )
        updated = await store.get_job(job.id)
        assert updated is not None
        return updated

    # -- cancellation (07 §1: step boundaries) -------------------------------------------

    async def request_cancel(
        self, store: JobsStore, job_id: UUID, *, at: datetime | None = None
    ) -> bool:
        """Record cancel_requested_at on a cancellable job; the worker
        observes it at the next step boundary. First write wins."""
        at = at if at is not None else self._clock()
        return await store.request_cancel(job_id, at=at) > 0

    async def cancel_requested(
        self, store: JobsStore, job_id: UUID
    ) -> bool:
        fresh = await store.get_job(job_id)
        return fresh is not None and fresh.cancel_requested_at is not None

    # -- queue upkeep (07 §1: retry_wait requeue, lease reaping) ---------------------------

    async def requeue_due(self, store: JobsStore, *, now: datetime | None = None) -> int:
        """retry_wait → queued where available_at has passed (07 §1)."""
        now = now if now is not None else self._clock()
        return await store.requeue_due(now)

    async def reap_expired(
        self, store: JobsStore, *, now: datetime | None = None
    ) -> int:
        """Dead-worker recovery: expired running jobs go to retry_wait
        (available_at = now + backoff) or failed at max_attempts.

        The attempt counter increments on the next claim, so "增加
        attempt" (07 §1) is counted once per claim, not twice."""
        now = now if now is not None else self._clock()
        expired = await store.expired_running_jobs(now)
        count = 0
        for job in expired:
            error = {"code": "lease_expired", "attempt": job.attempt}
            if job.attempt >= job.max_attempts:
                values: dict[str, object] = {
                    "state": "failed",
                    "lease_token": None,
                    "lease_until": None,
                    "error": error,
                }
            else:
                delay = _jittered_backoff(job.attempt, self._rng)
                values = {
                    "state": "retry_wait",
                    "available_at": now + timedelta(
                        seconds=max(delay, LEASE_EXPIRY_BACKOFF_BASE_S)
                    ),
                    "lease_token": None,
                    "lease_until": None,
                    "error": error,
                }
            count += await store.expire_update(job.id, now=now, values=values)
        return count

    # -- events (07 §7) --------------------------------------------------------------------

    async def append_event(
        self,
        store: JobsStore,
        job: JobRecord,
        *,
        type: str,
        data: Mapping[str, object],
    ) -> JobEventRecord:
        """Append one outbox event; seq is allocated in this transaction
        (max+1 under PK(job_id, seq)) so commits are monotonic."""
        seq = await store.next_event_seq(job.id)
        event = JobEventRecord(
            owner_id=job.owner_id,
            industry_id=job.industry_id,
            job_id=job.id,
            seq=seq,
            type=type,
            data=dict(data),
            created_at=self._clock(),
        )
        await store.insert_event(event)
        return event

    # -- internals -----------------------------------------------------------------------------

    async def _fresh_progress(
        self, store: JobsStore, job: JobRecord
    ) -> dict[str, object]:
        """Progress as committed, not as the caller's snapshot holds it.

        The claimed record ages as steps commit; merging step N's progress
        over the claim-time dict would drop keys an earlier step wrote.
        """
        fresh = await store.get_job(job.id)
        return dict(fresh.progress) if fresh is not None else dict(job.progress)

    async def _fence(
        self, store: JobsStore, job: JobRecord, now: datetime
    ) -> None:
        """Prove the lease with a heartbeat-shaped write before touching
        business rows (07 §2: 所有 step 提交更新带 fencing WHERE)."""
        if not job.lease_token:
            raise LeaseLost(f"job {job.id} carries no lease")
        await self._fenced(store, job, now, {"heartbeat_at": now})

    async def _fenced(
        self,
        store: JobsStore,
        job: JobRecord,
        now: datetime,
        values: Mapping[str, object],
    ) -> None:
        if not job.lease_token:
            raise LeaseLost(f"job {job.id} carries no lease")
        rowcount = await store.fenced_update(
            job.id, lease_token=job.lease_token, now=now, values=values
        )
        if rowcount == 0:
            raise LeaseLost(
                f"lease for job {job.id} no longer current; refusing to"
                " write business results (spec 07 §2)"
            )


def _opt(value: UUID | None) -> str | None:
    return None if value is None else str(value)


class JobServiceEnqueuer:
    """:class:`intel.services.acquisition.JobEnqueuer` over the real queue.

    Task 5's seam keeps speaking the protocol; the app wiring constructs
    this adapter per request over the request's connection/transaction.
    Fakes remain the test double (``InMemoryEnqueuer`` / overridden
    ``get_enqueuer``).
    """

    def __init__(self, service: JobService, conn: AsyncConnection) -> None:
        self._service = service
        self._conn = conn

    async def enqueue(
        self,
        scope: IndustryScope,
        *,
        kind: str,
        payload: dict,
        idempotency_key: str,
    ) -> JobAccepted:
        store = SqlAlchemyJobsStore(self._conn, scope)
        job, _created = await self._service.enqueue(
            store,
            scope,
            kind=kind,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return job_accepted(job.id, job.state)
