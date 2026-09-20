"""Job runner skeleton: claim → dispatch → supervise (spec 07 §1/§2).

The loop is deliberately thin — the durable-queue semantics live in
:class:`intel.services.jobs.JobService`; kind handlers are Tasks 7+. This
module provides:

- ``run_once``: claim on a dispatcher-role store, then hand the job to the
  registered async handler inside :class:`RunContext`.
- The heartbeat task: while the handler runs, the runner extends the lease
  every :data:`~intel.workers.leases.HEARTBEAT_SECONDS` (07 §2 每30秒
  heartbeat) — a handler spanning minutes keeps its lease instead of
  silently losing it to the reaper. A heartbeat that comes back False (or
  raises) means the lease is gone: the handler is cancelled and the row
  left to the reaper.
- ``RunContext.boundary``: the step-boundary check (07 §1 用户取消在 step
  边界生效 + 总截止时间) — handlers call it between steps.
- The supervisor: every job runs under ``asyncio.timeout`` with a
  per-kind deadline (default 15 minutes); at the deadline the task is
  killed and the job recorded failed (07 §1 supervisor 杀作业进程).
  Finalizing is lease-guarded: a run whose lease was taken over (reaped →
  re-claimed by another worker) never writes the new owner's row.
- A no-op default handler: unregistered kinds finish ``succeeded`` with an
  output note, so the queue is observable before Task 7 lands.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from intel.repositories.base import IndustryScope
from intel.repositories.jobs import JobRecord, JobsStore
from intel.services.errors import InvalidStateTransition
from intel.services.jobs import TERMINAL_STATES, JobService, LeaseLost
from intel.workers.leases import HEARTBEAT_SECONDS

#: Supervisor total deadline per job (07 §1); per-kind overrides via
#: ``JobRunner(deadlines=...)``.
DEFAULT_JOB_DEADLINE = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobCancelled(Exception):
    """The user requested cancellation; observed at a step boundary."""


class JobDeadlineExceeded(Exception):
    """The job passed its total deadline (supervisor)."""


class JobFailure(Exception):
    """A handler-classified failure (07 §6 error class travels with it)."""

    def __init__(
        self,
        error_class: str,
        message: str,
        *,
        retry_after: float | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(f"{error_class}: {message}")
        self.error_class = error_class
        self.message = message
        self.retry_after = retry_after
        self.details = details


#: Opens one short-lived store bound to the given role: ``None`` is the
#: dispatcher role (claim/reap), a scope is the worker role (step commits).
StoreOpener = Callable[[IndustryScope | None], AbstractAsyncContextManager[JobsStore]]


@dataclass(slots=True)
class RunContext:
    """What a handler sees of its job.

    ``open_store`` keeps transactions short and explicit (07 §2): the
    handler opens a worker-role store per commit, never holds one across
    network or LLM calls, and calls ``boundary()`` between steps. The
    runner heartbeats the lease on the handler's behalf; ``lease_lost`` is
    the runner-internal flag set when that heartbeat loses the fence.
    """

    job: JobRecord
    service: JobService
    open_store: StoreOpener
    deadline_at: datetime
    clock: Callable[[], datetime] = field(default_factory=_utcnow)
    lease_lost: bool = False

    @property
    def scope(self) -> IndustryScope:
        return IndustryScope(self.job.owner_id, self.job.industry_id)

    async def boundary(self) -> None:
        """Step boundary: cancellation check + deadline check (07 §1).

        Raises :class:`JobCancelled` / :class:`JobDeadlineExceeded`; the
        runner turns them into terminal states.
        """
        if self.clock() >= self.deadline_at:
            raise JobDeadlineExceeded(
                f"job {self.job.id} passed its total deadline"
            )
        async with self.open_store(self.scope) as store:
            fresh = await store.get_job(self.job.id)
        if fresh is not None and fresh.cancel_requested_at is not None:
            raise JobCancelled(f"job {self.job.id} was cancelled")


@runtime_checkable
class JobHandler(Protocol):
    """One kind's executor; Tasks 7+ register the real handlers."""

    def __call__(self, ctx: RunContext) -> Awaitable[None]: ...


class JobRunner:
    """Claim-and-dispatch loop; one ``run_once`` per queue poll."""

    def __init__(
        self,
        service: JobService,
        open_store: StoreOpener,
        *,
        handlers: Mapping[str, JobHandler] | None = None,
        deadlines: Mapping[str, timedelta] | None = None,
        default_deadline: timedelta = DEFAULT_JOB_DEADLINE,
        heartbeat_interval: float = HEARTBEAT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._open_store = open_store
        self._handlers = dict(handlers or {})
        self._deadlines = dict(deadlines or {})
        self._default_deadline = default_deadline
        self._heartbeat_interval = heartbeat_interval
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock if clock is not None else _utcnow

    def register(self, kind: str, handler: JobHandler) -> None:
        """Attach a handler for a kind (Task 7+ wiring point)."""
        self._handlers[kind] = handler

    def deadline_for(self, kind: str) -> timedelta:
        return self._deadlines.get(kind, self._default_deadline)

    async def run_once(self) -> JobRecord | None:
        """Claim one job and run it; ``None`` when the queue is empty."""
        async with self._open_store(None) as store:  # dispatcher role
            job = await self._service.claim(store)
        if job is None:
            return None
        return await self._execute(job)

    async def _execute(self, job: JobRecord) -> JobRecord:
        deadline = self.deadline_for(job.kind)
        ctx = RunContext(
            job=job,
            service=self._service,
            open_store=self._open_store,
            deadline_at=self._clock() + deadline,
            clock=self._clock,
        )
        handler = self._handlers.get(job.kind, self._default_handler)
        handler_task = asyncio.ensure_future(handler(ctx))
        # 07 §2: heartbeat every 30s while the handler runs — a handler
        # spanning minutes keeps its lease (the reaper must not requeue a
        # live worker's job and double the model spend).
        heartbeat_task = asyncio.ensure_future(
            self._keep_lease(ctx, handler_task)
        )
        try:
            try:
                async with asyncio.timeout(deadline.total_seconds()):
                    await handler_task
            except JobCancelled:
                await self._finalize(
                    ctx.job, state="cancelled", error={"code": "cancelled"}
                )
            except (JobDeadlineExceeded, TimeoutError):
                await self._stop_task(handler_task)
                await self._finalize(
                    ctx.job,
                    state="failed",
                    error={
                        "code": "deadline_exceeded",
                        "budget_seconds": deadline.total_seconds(),
                    },
                )
            except asyncio.CancelledError:
                if not ctx.lease_lost:
                    # Foreign cancellation (shutdown): let it propagate;
                    # the finally below stops both tasks.
                    raise
                # Our heartbeat lost the fence and cancelled the handler:
                # leave the row to the reaper (07 §1 worker 消失).
            except JobFailure as failure:
                await self._service_fail(ctx.job, failure)
            except LeaseLost:
                # The lease was lost mid-run: leave the row alone; the
                # reaper requeues it (07 §1). Never write through a dead
                # lease, not even the failure itself.
                pass
            except Exception as exc:  # noqa: BLE001 - supervisor backstop
                # Unclassified handler bugs fail closed (no retry loop).
                await self._finalize(
                    ctx.job,
                    state="failed",
                    error={"code": "internal_error", "message": repr(exc)},
                )
        finally:
            await self._stop_task(heartbeat_task)
            await self._stop_task(handler_task)
        async with self._open_store(ctx.scope) as store:
            fresh = await store.get_job(job.id)
        return fresh if fresh is not None else job

    async def _keep_lease(
        self, ctx: RunContext, handler_task: asyncio.Task
    ) -> None:
        """Extend the claim's lease until the handler settles (07 §2).

        ``False`` from the fenced heartbeat means the lease was reaped or
        re-claimed: further model spend is wasted and every commit would
        be fenced out anyway, so the handler is cancelled and the row is
        left to its new owner. Store errors are retried on the next tick —
        a persistently unreachable store eventually loses the lease and
        ends the run the same way.
        """
        token = ctx.job.lease_token or ""
        while True:
            await self._sleep(self._heartbeat_interval)
            if handler_task.done():
                # The run settled while we slept — no more beats (a beat
                # after a terminal finish would hit the released lease).
                return
            try:
                async with ctx.open_store(ctx.scope) as store:
                    alive = await ctx.service.heartbeat(
                        store, ctx.job.id, token
                    )
            except Exception:  # noqa: BLE001, S112 - best-effort liveness
                continue
            if not alive:
                ctx.lease_lost = True
                if not handler_task.done():
                    handler_task.cancel()
                return

    async def _service_fail(self, job: JobRecord, failure: JobFailure) -> None:
        try:
            async with self._open_store(
                IndustryScope(job.owner_id, job.industry_id)
            ) as store:
                await self._service.fail(
                    store,
                    job,
                    error_class=failure.error_class,
                    message=failure.message,
                    retry_after=failure.retry_after,
                    details=failure.details,
                )
        except LeaseLost:
            pass
        except Exception as exc:  # noqa: BLE001 - mis-classified JobFailure
            # A handler-level error class (schema_output / validation_failed,
            # spec 07 §6) reached the job layer — a handler bug, not a retry.
            await self._finalize(
                job,
                state="failed",
                error={
                    "code": "internal_error",
                    "message": f"misclassified failure: {exc!r}",
                },
            )

    async def _finalize(
        self, job: JobRecord, *, state: str, error: dict | None = None
    ) -> None:
        """Terminalize the run — only if this run still owns the lease.

        Zombie guard (the lease lifecycle this runner owns): when the
        original run lost its lease, the row belongs to the reaper or to
        the worker that re-claimed it. Finalizing with the *current*
        row's token would fail (or cancel) the new owner's run, so a
        token mismatch — including the None left behind by reap/retry_wait
        — is a no-op. Illegal transitions from surviving non-terminal
        states are absorbed the same way instead of escaping run_once.
        """
        expected_token = job.lease_token
        try:
            async with self._open_store(
                IndustryScope(job.owner_id, job.industry_id)
            ) as store:
                fresh = await store.get_job(job.id)
                if fresh is None or fresh.state in TERMINAL_STATES:
                    return  # gone or already finished
                if fresh.lease_token != expected_token:
                    return  # reaped / re-claimed: not our row anymore
                await self._service.finish(
                    store, fresh, state=state, error=error
                )
        except (LeaseLost, InvalidStateTransition):
            # Fence lost between the read and the write, or a surviving
            # state that cannot move to `state` — either way, not ours to
            # force.
            pass

    async def _stop_task(self, task: asyncio.Task) -> None:
        """Cancel a runner-owned task and swallow its outcome.

        Used in cleanup paths where the outcome was already consumed (or
        is being re-raised by an outer branch); awaiting a finished task
        would re-raise its exception, so everything is suppressed here.
        """
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
            pass

    async def _default_handler(self, ctx: RunContext) -> None:
        """No-op handler: unregistered kinds complete observably (Task 7+
        replaces this by registering real handlers)."""
        await ctx.boundary()
        async with ctx.open_store(ctx.scope) as store:
            await ctx.service.finish(
                store,
                ctx.job,
                state="succeeded",
                progress={"note": "no handler registered for kind"},
            )
