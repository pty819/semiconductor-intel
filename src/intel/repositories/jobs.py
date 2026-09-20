"""Jobs storage: records, statement builders, protocol, adapters (spec 07).

Two roles, one store class (spec 10 §1 双角色):

- **Dispatcher role** — the store is constructed without a scope and runs on
  a connection whose database role is not subject to the ``intel_app`` RLS
  policies (the table owner). Claiming, lease reaping and requeue read the
  whole queue; the policies are ``TO intel_app`` and never grant
  ``BYPASSRLS`` (spec 10 §1: 后台领取全局 jobs 使用独立 dispatcher 角色).
- **Worker role** — the store carries an :class:`IndustryScope` and binds
  the transaction-local RLS GUCs before every write, exactly like the other
  scoped repositories. Enqueue (API path) and all fenced step/terminal
  commits run here. jobs is an O/I hybrid: the owner GUC is what RLS
  checks; ``industry_id`` rides along for kind-level vs industry-level
  jobs (spec 03 §7).

Statement builders (``claim_select_stmt`` …) are module-level so
tests/unit/test_jobs_sql.py can compile them against the PostgreSQL
dialect and pin the load-bearing fragments: ``FOR UPDATE OF jobs SKIP
LOCKED``, the fencing predicates ``lease_token = … AND lease_until >
now()`` and the idempotent step conflict target.

:class:`InMemoryJobsStore` is the dev/test adapter (the queue-side
counterpart of :class:`intel.services.acquisition.InMemoryEnqueuer`): it
simulates SKIP LOCKED with per-session row locks, the fencing predicate,
and the UNIQUE constraints the real schema carries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import literal_column

from intel.db.models.jobs import Job, JobEvent, JobStep
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope

#: States from which a user may request cancellation (spec 07 §1).
CANCELLABLE_STATES = ("queued", "running", "retry_wait", "waiting_review")

_JOB_TABLE = Job.__table__
_STEP_TABLE = JobStep.__table__
_EVENT_TABLE = JobEvent.__table__


# --------------------------------------------------------------------------
# storage-facing records
# --------------------------------------------------------------------------


@dataclass(slots=True)
class JobRecord:
    """One ``jobs`` row as the queue layer sees it."""

    owner_id: UUID
    idempotency_key: str
    kind: str = ""
    id: UUID = field(default_factory=uuid4)
    industry_id: UUID | None = None
    state: str = "queued"
    input: dict = field(default_factory=dict)
    available_at: datetime | None = None
    attempt: int = 0
    max_attempts: int = 3
    lease_token: str | None = None
    lease_until: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    progress: dict = field(default_factory=dict)
    error: dict | None = None
    output_ref: str | None = None


@dataclass(slots=True)
class JobStepRecord:
    """One ``job_steps`` row; UNIQUE(job_id, step_key, input_hash) (07 §2)."""

    owner_id: UUID
    job_id: UUID
    step_key: str
    input_hash: str
    id: UUID = field(default_factory=uuid4)
    industry_id: UUID | None = None
    state: str = "running"
    output_ref: str | None = None
    attempt: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class JobEventRecord:
    """One ``job_events`` row; PK(job_id, seq), append-only (07 §7)."""

    owner_id: UUID
    job_id: UUID
    seq: int
    type: str
    industry_id: UUID | None = None
    data: dict = field(default_factory=dict)
    created_at: datetime | None = None


# --------------------------------------------------------------------------
# statement builders (compile-checked by tests/unit/test_jobs_sql.py)
# --------------------------------------------------------------------------


def claim_select_stmt(now: datetime):
    """The claim scan: oldest available queued row, lock or skip (07 §2).

    ``FOR UPDATE OF jobs SKIP LOCKED LIMIT 1`` — two concurrent claims
    leave exactly one winner (JOB-01). Runs in the dispatcher transaction.
    """
    return (
        select(_JOB_TABLE)
        .where(_JOB_TABLE.c.state == "queued", _JOB_TABLE.c.available_at <= now)
        .order_by(_JOB_TABLE.c.available_at, _JOB_TABLE.c.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=_JOB_TABLE)
    )


def claim_update_stmt(
    job_id: UUID, lease_token: str, lease_until: datetime, now: datetime
):
    """Claim side-effects: running, attempt+1, fresh lease (07 §2)."""
    return (
        update(_JOB_TABLE)
        .where(_JOB_TABLE.c.id == job_id)
        .values(
            state="running",
            attempt=_JOB_TABLE.c.attempt + 1,
            lease_token=lease_token,
            lease_until=lease_until,
            heartbeat_at=now,
        )
    )


def fenced_update_stmt(
    *,
    job_id: UUID,
    owner_id: UUID,
    lease_token: str,
    now: datetime,
    values: Mapping[str, Any],
):
    """The fencing mutation: only the current lease holder can write (07 §2).

    ``WHERE lease_token = :token AND lease_until > now()`` — a worker whose
    lease expired (JOB-02) or was re-claimed (JOB-03 stale loser) matches
    zero rows and the caller sees rowcount 0.
    """
    return (
        update(_JOB_TABLE)
        .where(
            _JOB_TABLE.c.id == job_id,
            _JOB_TABLE.c.owner_id == owner_id,
            _JOB_TABLE.c.lease_token == lease_token,
            _JOB_TABLE.c.lease_until > func.now(),
        )
        .values(**values)
    )


def heartbeat_stmt(
    *, job_id: UUID, owner_id: UUID, lease_token: str,
    lease_until: datetime, now: datetime,
):
    """Heartbeat: fence-checked lease extension (07 §2 每30秒)."""
    return fenced_update_stmt(
        job_id=job_id,
        owner_id=owner_id,
        lease_token=lease_token,
        now=now,
        values={"lease_until": lease_until, "heartbeat_at": now},
    )


def expire_update_stmt(*, job_id: UUID, now: datetime, values: Mapping[str, Any]):
    """Reaper mutation: only if the lease is *still* expired at write time."""
    return (
        update(_JOB_TABLE)
        .where(
            _JOB_TABLE.c.id == job_id,
            _JOB_TABLE.c.state == "running",
            _JOB_TABLE.c.lease_until <= now,
        )
        .values(**values)
    )


def step_upsert_stmt():
    """Idempotent step commit: same (job_id, step_key, input_hash) inserts
    exactly one row (07 §2, JOB-03)."""
    return pg_insert(_STEP_TABLE).on_conflict_do_nothing(
        constraint="uq_job_steps_step_input"
    )


def event_seq_select_stmt(job_id: UUID):
    """In-transaction seq allocation: max(seq)+1 under PK(job_id, seq)
    (07 §7 序号事务内单调分配)."""
    return select(
        func.coalesce(func.max(_EVENT_TABLE.c.seq), literal_column("0"))
        + literal_column("1")
    ).where(_EVENT_TABLE.c.job_id == job_id)


def request_cancel_stmt(*, job_id: UUID, owner_id: UUID, at: datetime):
    """Cancellation lands on cancellable states only; first write wins."""
    return (
        update(_JOB_TABLE)
        .where(
            _JOB_TABLE.c.id == job_id,
            _JOB_TABLE.c.owner_id == owner_id,
            _JOB_TABLE.c.state.in_(CANCELLABLE_STATES),
            _JOB_TABLE.c.cancel_requested_at.is_(None),
        )
        .values(cancel_requested_at=at)
    )


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


@runtime_checkable
class JobsStore(Protocol):
    """Primitive row operations over the jobs tables.

    One instance represents one short transaction (spec 07 §2: transactions
    never span network or LLM calls — callers open a store, run one method
    or a small same-transaction group, and commit). Every fenced method
    returns the affected-row count; 0 means the lease predicate rejected
    the write.
    """

    # -- enqueue / reads (worker role or dispatcher role) ------------------
    async def insert_job(self, record: JobRecord) -> JobRecord | None:
        """INSERT; ``None`` when UNIQUE(owner_id, kind, idempotency_key)
        conflicts (07 §3: same key returns the existing job)."""
        ...

    async def find_idempotent_job(
        self, owner_id: UUID, kind: str, idempotency_key: str
    ) -> JobRecord | None: ...

    async def get_job(self, job_id: UUID) -> JobRecord | None: ...
    async def get_step(
        self, job_id: UUID, step_key: str, input_hash: str
    ) -> JobStepRecord | None: ...

    # -- claim / requeue / reap (dispatcher role, unscoped) ------------------
    async def claim_next(
        self, *, now: datetime, lease_token: str, lease_until: datetime
    ) -> JobRecord | None:
        """SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1, then write the lease
        (state=running, attempt+1) in the same transaction."""
        ...

    async def expired_running_jobs(self, now: datetime) -> list[JobRecord]: ...
    async def expire_update(
        self, job_id: UUID, *, now: datetime, values: Mapping[str, Any]
    ) -> int: ...

    async def requeue_due(self, now: datetime) -> int:
        """retry_wait → queued where available_at <= now (07 §1)."""
        ...

    # -- fenced mutations (worker role) ---------------------------------------
    async def fenced_update(
        self,
        job_id: UUID,
        *,
        lease_token: str,
        now: datetime,
        values: Mapping[str, Any],
    ) -> int: ...

    async def fenced_heartbeat(
        self,
        job_id: UUID,
        *,
        lease_token: str,
        lease_until: datetime,
        now: datetime,
    ) -> int: ...

    # -- steps / events ---------------------------------------------------------
    async def upsert_step(self, step: JobStepRecord) -> JobStepRecord:
        """Insert or converge on (job_id, step_key, input_hash); returns
        the row as it stands after the write (idempotent commit, 07 §2)."""
        ...

    async def next_event_seq(self, job_id: UUID) -> int: ...
    async def insert_event(self, event: JobEventRecord) -> None: ...

    # -- cancellation -------------------------------------------------------------
    async def request_cancel(self, job_id: UUID, *, at: datetime) -> int: ...


# --------------------------------------------------------------------------
# SQLAlchemy adapter
# --------------------------------------------------------------------------


def _job_record(row: Any) -> JobRecord:
    return JobRecord(
        id=row.id,
        owner_id=row.owner_id,
        industry_id=row.industry_id,
        kind=row.kind,
        state=row.state,
        input=row.input or {},
        idempotency_key=row.idempotency_key,
        available_at=row.available_at,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        lease_token=row.lease_token,
        lease_until=row.lease_until,
        heartbeat_at=row.heartbeat_at,
        cancel_requested_at=row.cancel_requested_at,
        progress=row.progress or {},
        error=row.error,
        output_ref=row.output_ref,
    )


def _step_record(row: Any) -> JobStepRecord:
    return JobStepRecord(
        id=row.id,
        owner_id=row.owner_id,
        industry_id=row.industry_id,
        job_id=row.job_id,
        step_key=row.step_key,
        state=row.state,
        input_hash=row.input_hash,
        output_ref=row.output_ref,
        attempt=row.attempt,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


class SqlAlchemyJobsStore:
    """JobsStore on one connection (``repositories.jobs`` adapter).

    ``scope=None`` builds the dispatcher-role store (claim/requeue/reap on
    a connection whose role can see the whole queue); a scope builds the
    worker-role store that binds the RLS GUCs before writes.
    """

    def __init__(
        self, conn: AsyncConnection, scope: IndustryScope | None = None
    ) -> None:
        self._conn = conn
        self._scope = scope

    @property
    def scope(self) -> IndustryScope | None:
        return self._scope

    async def _bind_worker(self) -> None:
        if self._scope is not None:
            await set_scope(
                self._conn, self._scope.owner_id, self._scope.industry_id
            )
            await require_owner_guc(self._conn)

    # -- enqueue / reads -------------------------------------------------------

    async def insert_job(self, record: JobRecord) -> JobRecord | None:
        await self._bind_worker()
        stmt = (
            pg_insert(_JOB_TABLE)
            .values(
                id=record.id,
                owner_id=record.owner_id,
                industry_id=record.industry_id,
                kind=record.kind,
                state=record.state,
                input=record.input,
                idempotency_key=record.idempotency_key,
                available_at=record.available_at,
                max_attempts=record.max_attempts,
                progress=record.progress,
            )
            .on_conflict_do_nothing(constraint="uq_jobs_idempotency")
        )
        result = await self._conn.execute(stmt)
        if result.rowcount == 0:
            return None
        return record

    async def find_idempotent_job(
        self, owner_id: UUID, kind: str, idempotency_key: str
    ) -> JobRecord | None:
        await self._bind_worker()
        stmt = (
            select(_JOB_TABLE)
            .where(
                _JOB_TABLE.c.owner_id == owner_id,
                _JOB_TABLE.c.kind == kind,
                _JOB_TABLE.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).first()
        return None if row is None else _job_record(row)

    async def get_job(self, job_id: UUID) -> JobRecord | None:
        await self._bind_worker()
        stmt = select(_JOB_TABLE).where(_JOB_TABLE.c.id == job_id).limit(1)
        if self._scope is not None:
            stmt = stmt.where(_JOB_TABLE.c.owner_id == self._scope.owner_id)
        row = (await self._conn.execute(stmt)).first()
        return None if row is None else _job_record(row)

    async def get_step(
        self, job_id: UUID, step_key: str, input_hash: str
    ) -> JobStepRecord | None:
        await self._bind_worker()
        stmt = (
            select(_STEP_TABLE)
            .where(
                _STEP_TABLE.c.job_id == job_id,
                _STEP_TABLE.c.step_key == step_key,
                _STEP_TABLE.c.input_hash == input_hash,
            )
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).first()
        return None if row is None else _step_record(row)

    # -- claim / requeue / reap (dispatcher role) --------------------------------

    async def claim_next(
        self, *, now: datetime, lease_token: str, lease_until: datetime
    ) -> JobRecord | None:
        row = (await self._conn.execute(claim_select_stmt(now))).first()
        if row is None:
            return None
        await self._conn.execute(
            claim_update_stmt(row.id, lease_token, lease_until, now)
        )
        return await self.get_job(row.id) or _job_record(row)

    async def expired_running_jobs(self, now: datetime) -> list[JobRecord]:
        stmt = (
            select(_JOB_TABLE)
            .where(
                _JOB_TABLE.c.state == "running",
                _JOB_TABLE.c.lease_until.is_not(None),
                _JOB_TABLE.c.lease_until <= now,
            )
            .order_by(_JOB_TABLE.c.lease_until)
        )
        rows = (await self._conn.execute(stmt)).all()
        return [_job_record(r) for r in rows]

    async def expire_update(
        self, job_id: UUID, *, now: datetime, values: Mapping[str, Any]
    ) -> int:
        result = await self._conn.execute(
            expire_update_stmt(job_id=job_id, now=now, values=values)
        )
        return result.rowcount

    async def requeue_due(self, now: datetime) -> int:
        stmt = (
            update(_JOB_TABLE)
            .where(
                _JOB_TABLE.c.state == "retry_wait",
                _JOB_TABLE.c.available_at <= now,
            )
            .values(state="queued")
        )
        result = await self._conn.execute(stmt)
        return result.rowcount

    # -- fenced mutations (worker role) -------------------------------------------

    async def fenced_update(
        self,
        job_id: UUID,
        *,
        lease_token: str,
        now: datetime,
        values: Mapping[str, Any],
    ) -> int:
        await self._bind_worker()
        owner_id = self._require_owner(job_id)
        result = await self._conn.execute(
            fenced_update_stmt(
                job_id=job_id,
                owner_id=owner_id,
                lease_token=lease_token,
                now=now,
                values=values,
            )
        )
        return result.rowcount

    async def fenced_heartbeat(
        self,
        job_id: UUID,
        *,
        lease_token: str,
        lease_until: datetime,
        now: datetime,
    ) -> int:
        await self._bind_worker()
        owner_id = self._require_owner(job_id)
        result = await self._conn.execute(
            heartbeat_stmt(
                job_id=job_id,
                owner_id=owner_id,
                lease_token=lease_token,
                lease_until=lease_until,
                now=now,
            )
        )
        return result.rowcount

    def _require_owner(self, job_id: UUID) -> UUID:
        if self._scope is None:
            raise RuntimeError(
                f"fenced writes need the worker role (store for job {job_id}"
                " was built without a scope)"
            )
        return self._scope.owner_id

    # -- steps / events -------------------------------------------------------------

    async def upsert_step(self, step: JobStepRecord) -> JobStepRecord:
        await self._bind_worker()
        stmt = step_upsert_stmt().values(
            id=step.id,
            owner_id=step.owner_id,
            industry_id=step.industry_id,
            job_id=step.job_id,
            step_key=step.step_key,
            state=step.state,
            input_hash=step.input_hash,
            output_ref=step.output_ref,
            attempt=step.attempt,
            started_at=step.started_at,
            completed_at=step.completed_at,
        )
        result = await self._conn.execute(stmt)
        if result.rowcount == 0:
            # Conflict on (job_id, step_key, input_hash): converge the
            # outcome columns on the existing row (idempotent commit).
            existing = await self.get_step(
                step.job_id, step.step_key, step.input_hash
            )
            assert existing is not None
            await self._conn.execute(
                update(_STEP_TABLE)
                .where(_STEP_TABLE.c.id == existing.id)
                .values(
                    state=step.state,
                    output_ref=step.output_ref,
                    started_at=step.started_at,
                    completed_at=step.completed_at,
                )
            )
        converged = await self.get_step(step.job_id, step.step_key, step.input_hash)
        assert converged is not None
        return converged

    async def next_event_seq(self, job_id: UUID) -> int:
        await self._bind_worker()
        row = (await self._conn.execute(event_seq_select_stmt(job_id))).first()
        return int(row[0]) if row is not None else 1

    async def insert_event(self, event: JobEventRecord) -> None:
        await self._bind_worker()
        await self._conn.execute(
            pg_insert(_EVENT_TABLE).values(
                owner_id=event.owner_id,
                industry_id=event.industry_id,
                job_id=event.job_id,
                seq=event.seq,
                type=event.type,
                data=event.data,
                created_at=event.created_at,
            )
        )

    # -- cancellation ------------------------------------------------------------------

    async def request_cancel(self, job_id: UUID, *, at: datetime) -> int:
        await self._bind_worker()
        owner_id = self._require_owner(job_id)
        result = await self._conn.execute(
            request_cancel_stmt(job_id=job_id, owner_id=owner_id, at=at)
        )
        return result.rowcount

    async def list_jobs(
        self,
        *,
        industry_id: UUID | None = None,
        kind: str | None = None,
        state: str | None = None,
        limit: int = 200,
    ) -> list[JobRecord]:
        await self._bind_worker()
        stmt = select(_JOB_TABLE)
        if self._scope is not None:
            stmt = stmt.where(_JOB_TABLE.c.owner_id == self._scope.owner_id)
        if industry_id is not None:
            stmt = stmt.where(_JOB_TABLE.c.industry_id == industry_id)
        if kind:
            stmt = stmt.where(_JOB_TABLE.c.kind == kind)
        if state:
            stmt = stmt.where(_JOB_TABLE.c.state == state)
        stmt = stmt.order_by(_JOB_TABLE.c.available_at.desc()).limit(limit)
        rows = (await self._conn.execute(stmt)).all()
        return [_job_record(row) for row in rows]

    async def list_events_after(
        self, job_id: UUID, after_seq: int, *, limit: int
    ) -> list[dict[str, Any]]:
        await self._bind_worker()
        stmt = (
            select(_EVENT_TABLE)
            .where(_EVENT_TABLE.c.job_id == job_id, _EVENT_TABLE.c.seq > after_seq)
            .order_by(_EVENT_TABLE.c.seq.asc())
            .limit(limit)
        )
        if self._scope is not None:
            stmt = stmt.where(_EVENT_TABLE.c.owner_id == self._scope.owner_id)
        rows = (await self._conn.execute(stmt)).all()
        return [
            {"seq": row.seq, "kind": row.type, "payload": row.data or {}}
            for row in rows
        ]

    async def earliest_event_seq(self, job_id: UUID) -> int | None:
        await self._bind_worker()
        stmt = select(func.min(_EVENT_TABLE.c.seq)).where(
            _EVENT_TABLE.c.job_id == job_id
        )
        if self._scope is not None:
            stmt = stmt.where(_EVENT_TABLE.c.owner_id == self._scope.owner_id)
        row = (await self._conn.execute(stmt)).first()
        if row is None or row[0] is None:
            return None
        return int(row[0])


class SqlAlchemyJobEventLog:
    """JobEventLog over job_events for SSE replay (07 §7)."""

    def __init__(self, store: SqlAlchemyJobsStore) -> None:
        self._store = store

    async def events_after(
        self, job_id: UUID, after_seq: int, *, limit: int
    ) -> list[dict[str, Any]]:
        return await self._store.list_events_after(job_id, after_seq, limit=limit)

    async def earliest_seq(self, job_id: UUID) -> int | None:
        return await self._store.earliest_event_seq(job_id)

    async def job_is_active(self, job_id: UUID) -> bool:
        job = await self._store.get_job(job_id)
        if job is None:
            return False
        return job.state not in {"succeeded", "partial", "failed", "cancelled"}


# --------------------------------------------------------------------------
# in-memory adapter (dev/test)
# --------------------------------------------------------------------------


def _job_row(record: JobRecord) -> dict:
    return {
        "id": record.id,
        "owner_id": record.owner_id,
        "industry_id": record.industry_id,
        "kind": record.kind,
        "state": record.state,
        "input": dict(record.input),
        "idempotency_key": record.idempotency_key,
        "available_at": record.available_at,
        "attempt": record.attempt,
        "max_attempts": record.max_attempts,
        "lease_token": record.lease_token,
        "lease_until": record.lease_until,
        "heartbeat_at": record.heartbeat_at,
        "cancel_requested_at": record.cancel_requested_at,
        "progress": dict(record.progress),
        "error": record.error,
        "output_ref": record.output_ref,
    }


def _row_record(row: dict) -> JobRecord:
    return JobRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        industry_id=row["industry_id"],
        kind=row["kind"],
        state=row["state"],
        input=dict(row["input"]),
        idempotency_key=row["idempotency_key"],
        available_at=row["available_at"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        lease_token=row["lease_token"],
        lease_until=row["lease_until"],
        heartbeat_at=row["heartbeat_at"],
        cancel_requested_at=row["cancel_requested_at"],
        progress=dict(row["progress"]),
        error=row["error"],
        output_ref=row["output_ref"],
    )


class InMemoryJobsDatabase:
    """Shared row state the in-memory stores act on (test surface)."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, dict] = {}
        self.idempotency: dict[tuple[UUID, str, str], UUID] = {}
        self.steps: dict[tuple[UUID, str, str], dict] = {}
        self.events: dict[UUID, list[JobEventRecord]] = {}
        #: job_id -> session id holding the FOR UPDATE lock (SKIP LOCKED).
        self.locks: dict[UUID, str] = {}


class InMemoryJobsStore:
    """JobsStore over :class:`InMemoryJobsDatabase` with simulated isolation.

    ``claim_next`` takes the row lock at select time (another session's
    claim skips the row) and releases it when the method returns — the
    moment the claiming service transaction would commit. Fenced updates
    evaluate the same predicate the SQL carries, so JOB-02 semantics hold
    without a database.
    """

    def __init__(self, db: InMemoryJobsDatabase, *, session_id: str = "s") -> None:
        self.db = db
        self.session_id = session_id

    async def insert_job(self, record: JobRecord) -> JobRecord | None:
        key = (record.owner_id, record.kind, record.idempotency_key)
        if key in self.db.idempotency:
            return None
        self.db.idempotency[key] = record.id
        self.db.jobs[record.id] = _job_row(record)
        if record.id not in self.db.events:
            self.db.events[record.id] = []
        return record

    async def find_idempotent_job(
        self, owner_id: UUID, kind: str, idempotency_key: str
    ) -> JobRecord | None:
        jid = self.db.idempotency.get((owner_id, kind, idempotency_key))
        if jid is None:
            return None
        row = self.db.jobs.get(jid)
        return None if row is None else _row_record(row)

    async def get_job(self, job_id: UUID) -> JobRecord | None:
        row = self.db.jobs.get(job_id)
        return None if row is None else _row_record(row)

    async def get_step(
        self, job_id: UUID, step_key: str, input_hash: str
    ) -> JobStepRecord | None:
        row = self.db.steps.get((job_id, step_key, input_hash))
        if row is None:
            return None
        return JobStepRecord(**row)

    async def claim_next(
        self, *, now: datetime, lease_token: str, lease_until: datetime
    ) -> JobRecord | None:
        candidates = sorted(
            (
                row
                for jid, row in self.db.jobs.items()
                if row["state"] == "queued"
                and row["available_at"] is not None
                and row["available_at"] <= now
                and self.db.locks.get(jid) in (None, self.session_id)
            ),
            key=lambda r: (r["available_at"], r["id"]),
        )
        if not candidates:
            return None
        row = candidates[0]
        self.db.locks[row["id"]] = self.session_id  # FOR UPDATE (SKIP LOCKED)
        await asyncio.sleep(0)  # interleaving point between lock and write
        row.update(
            state="running",
            attempt=row["attempt"] + 1,
            lease_token=lease_token,
            lease_until=lease_until,
            heartbeat_at=now,
        )
        self.db.locks.pop(row["id"], None)  # claim transaction commits here
        return _row_record(row)

    async def expired_running_jobs(self, now: datetime) -> list[JobRecord]:
        return [
            _row_record(row)
            for row in self.db.jobs.values()
            if row["state"] == "running"
            and row["lease_until"] is not None
            and row["lease_until"] <= now
        ]

    async def expire_update(
        self, job_id: UUID, *, now: datetime, values: Mapping[str, Any]
    ) -> int:
        row = self.db.jobs.get(job_id)
        if (
            row is None
            or row["state"] != "running"
            or row["lease_until"] is None
            or row["lease_until"] > now
        ):
            return 0
        row.update(dict(values))
        return 1

    async def requeue_due(self, now: datetime) -> int:
        count = 0
        for row in self.db.jobs.values():
            if (
                row["state"] == "retry_wait"
                and row["available_at"] is not None
                and row["available_at"] <= now
            ):
                row["state"] = "queued"
                count += 1
        return count

    async def fenced_update(
        self,
        job_id: UUID,
        *,
        lease_token: str,
        now: datetime,
        values: Mapping[str, Any],
    ) -> int:
        row = self.db.jobs.get(job_id)
        if not self._fence_ok(row, lease_token=lease_token, now=now):
            return 0
        row.update(dict(values))
        return 1

    async def fenced_heartbeat(
        self,
        job_id: UUID,
        *,
        lease_token: str,
        lease_until: datetime,
        now: datetime,
    ) -> int:
        return await self.fenced_update(
            job_id,
            lease_token=lease_token,
            now=now,
            values={"lease_until": lease_until, "heartbeat_at": now},
        )

    @staticmethod
    def _fence_ok(row: dict | None, *, lease_token: str, now: datetime) -> bool:
        return (
            row is not None
            and row["lease_token"] == lease_token
            and row["lease_until"] is not None
            and row["lease_until"] > now
        )

    async def upsert_step(self, step: JobStepRecord) -> JobStepRecord:
        key = (step.job_id, step.step_key, step.input_hash)
        row = self.db.steps.get(key)
        if row is None:
            self.db.steps[key] = {
                "id": step.id,
                "owner_id": step.owner_id,
                "industry_id": step.industry_id,
                "job_id": step.job_id,
                "step_key": step.step_key,
                "state": step.state,
                "input_hash": step.input_hash,
                "output_ref": step.output_ref,
                "attempt": step.attempt,
                "started_at": step.started_at,
                "completed_at": step.completed_at,
            }
            return step
        row.update(
            state=step.state,
            output_ref=step.output_ref,
            completed_at=step.completed_at,
            started_at=step.started_at or row["started_at"],
        )
        return JobStepRecord(**row)

    async def next_event_seq(self, job_id: UUID) -> int:
        events = self.db.events.get(job_id, [])
        return max((e.seq for e in events), default=0) + 1

    async def insert_event(self, event: JobEventRecord) -> None:
        events = self.db.events.setdefault(event.job_id, [])
        if any(e.seq == event.seq for e in events):
            raise ValueError(
                f"duplicate job_events seq {event.seq} for job {event.job_id}"
                " (PK(job_id, seq), 07 §7)"
            )
        events.append(event)

    async def request_cancel(self, job_id: UUID, *, at: datetime) -> int:
        row = self.db.jobs.get(job_id)
        if (
            row is None
            or row["state"] not in CANCELLABLE_STATES
            or row["cancel_requested_at"] is not None
        ):
            return 0
        row["cancel_requested_at"] = at
        return 1
