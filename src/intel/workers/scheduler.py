"""Scheduler tick: turn due database state into enqueued jobs (spec 07 §4).

``SchedulerTick.run_once(store)`` is a pure function of DB state read
through the :class:`SchedulerQueries` protocol:

- **discover** for feeds with ``next_poll_at <= now`` that are still
  pollable — user_enabled OR an active subscription in an active industry
  (spec 04 §2/§7: 行业 pause 停止新定时任务, but a feed the user enabled or
  another active industry uses keeps collecting). The OR/EXISTS predicate
  lives in :func:`due_feeds_stmt`.
- **report_build** (daily) for active industries inside the configured
  local-time window; the industry/report_type/period idempotency key makes
  repeated ticks in one period a no-op.
- **watch_check** for due watches on a fixed cadence (spec 03 §4 首版定期
  检查仅本地归档).

The tick runs on a dispatcher-role store (spec 10 §1): it enqueues across
owners, so it must not be scoped to one of them. Individual job execution
is Task 7+; the tick only ever inserts queued rows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import DateTime, Select, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.auth import User
from intel.db.models.knowledge import Watch
from intel.db.models.sources import IndustrySource, OwnerFeed
from intel.db.models.workspace import Industry
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import JobsStore, SqlAlchemyJobsStore
from intel.services.jobs import JobService, build_idempotency_key

#: Local hour at which the daily report window opens (per-industry tz).
DEFAULT_REPORT_LOCAL_HOUR = 8

#: Watch re-check cadence (spec 05: 定期检查; no upstream number — knob).
DEFAULT_WATCH_INTERVAL = timedelta(hours=6)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DueFeed:
    """A feed the scheduler should discover now."""

    owner_id: UUID
    feed_id: UUID
    interval_seconds: int
    config_version: int

    def schedule_slot(self, now: datetime) -> int:
        """The polling slot ``now`` falls into — the idempotency key's
        dedup window (07 §3 discover: owner/feed/schedule_slot)."""
        return int(now.timestamp() // self.interval_seconds)


@dataclass(frozen=True, slots=True)
class DailyReportTarget:
    """An active industry whose local time is inside the report window."""

    owner_id: UUID
    industry_id: UUID
    local_date: str


@dataclass(frozen=True, slots=True)
class DueWatch:
    """A watch due for its periodic local check."""

    owner_id: UUID
    industry_id: UUID
    watch_id: UUID


@runtime_checkable
class SchedulerQueries(Protocol):
    """What the tick needs from storage (fakes in unit tests)."""

    async def due_feeds(self, now: datetime) -> list[DueFeed]: ...

    async def daily_report_targets(
        self, now: datetime, *, local_hour: int
    ) -> list[DailyReportTarget]: ...

    async def due_watches(
        self, now: datetime, *, interval: timedelta
    ) -> list[DueWatch]: ...


@dataclass(slots=True)
class SchedulerTickResult:
    """What one tick dispatched (created rows; replays are not counted)."""

    discovered: int = 0
    reports: int = 0
    watches: int = 0
    job_ids: list[UUID] = field(default_factory=list)


# --------------------------------------------------------------------------
# SQL: scheduler reads (dispatcher role — cross-owner)
# --------------------------------------------------------------------------


def due_feeds_stmt(now: datetime) -> Select:
    """Feeds due for a discover job (spec 04 §2/§7).

    Pollable = status active AND next_poll_at due AND (user_enabled OR an
    active subscription in an active, non-deleted industry).
    """
    active_sub = (
        select(1)
        .select_from(IndustrySource)
        .join(
            Industry,
            (Industry.owner_id == IndustrySource.owner_id)
            & (Industry.id == IndustrySource.industry_id),
        )
        .where(
            IndustrySource.owner_id == OwnerFeed.owner_id,
            IndustrySource.feed_id == OwnerFeed.id,
            IndustrySource.status == "active",
            Industry.status == "active",
            Industry.deleted_at.is_(None),
        )
        .exists()
    )
    return (
        select(
            OwnerFeed.owner_id,
            OwnerFeed.id.label("feed_id"),
            OwnerFeed.interval_seconds,
            OwnerFeed.row_version.label("config_version"),
        )
        .where(
            OwnerFeed.status == "active",
            OwnerFeed.next_poll_at.is_not(None),
            OwnerFeed.next_poll_at <= now,
            OwnerFeed.user_enabled.is_(True) | active_sub,
        )
        .order_by(OwnerFeed.next_poll_at, OwnerFeed.id)
    )


def daily_report_targets_stmt(now: datetime, *, local_hour: int) -> Select:
    """Active industries whose local hour equals the report window hour.

    ``CAST(:now AS timestamptz) AT TIME ZONE users.timezone`` is the local
    wall time; its date (``local_date``) becomes the period component of
    the report_build idempotency key — one report per industry per local
    day.
    """
    now_tstz = cast(literal(now), DateTime(timezone=True))
    local_wall = now_tstz.op("AT TIME ZONE")(User.timezone)
    return (
        select(
            Industry.owner_id,
            Industry.id.label("industry_id"),
            func.to_char(local_wall, "YYYY-MM-DD").label("local_date"),
        )
        .join(User, User.id == Industry.owner_id)
        .where(
            Industry.status == "active",
            Industry.deleted_at.is_(None),
            func.extract("hour", local_wall) == local_hour,
        )
    )


def due_watches_stmt(now: datetime, *, interval: timedelta) -> Select:
    """Active watches in active industries whose last check is older than
    the cadence (or never ran; spec 03 §4)."""
    return (
        select(
            Watch.owner_id,
            Watch.industry_id,
            Watch.id.label("watch_id"),
        )
        .join(
            Industry,
            (Industry.owner_id == Watch.owner_id)
            & (Industry.id == Watch.industry_id),
        )
        .where(
            Industry.status == "active",
            Industry.deleted_at.is_(None),
            Watch.status == "active",
            (Watch.last_checked_at.is_(None))
            | (Watch.last_checked_at <= now - interval),
        )
        .order_by(Watch.last_checked_at, Watch.id)
    )


# --------------------------------------------------------------------------
# SQL adapter for the queries protocol (dispatcher role)
# --------------------------------------------------------------------------


class SqlAlchemySchedulerQueries:
    """SchedulerQueries on one unscoped (dispatcher-role) connection.

    Cross-owner reads: owner_feeds/industries/users/watches carry intel_app
    RLS policies, and the dispatcher's connection role is the table owner,
    which those policies do not constrain (spec 10 §1).
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def due_feeds(self, now: datetime) -> list[DueFeed]:
        rows = (await self._conn.execute(due_feeds_stmt(now))).all()
        return [
            DueFeed(
                owner_id=r.owner_id,
                feed_id=r.feed_id,
                interval_seconds=r.interval_seconds,
                config_version=r.config_version,
            )
            for r in rows
        ]

    async def daily_report_targets(
        self, now: datetime, *, local_hour: int
    ) -> list[DailyReportTarget]:
        rows = (
            await self._conn.execute(
                daily_report_targets_stmt(now, local_hour=local_hour)
            )
        ).all()
        return [
            DailyReportTarget(
                owner_id=r.owner_id,
                industry_id=r.industry_id,
                local_date=r.local_date,
            )
            for r in rows
        ]

    async def due_watches(
        self, now: datetime, *, interval: timedelta
    ) -> list[DueWatch]:
        rows = (
            await self._conn.execute(due_watches_stmt(now, interval=interval))
        ).all()
        return [
            DueWatch(
                owner_id=r.owner_id, industry_id=r.industry_id, watch_id=r.watch_id
            )
            for r in rows
        ]


def scheduler_store(conn: AsyncConnection) -> SqlAlchemyJobsStore:
    """The dispatcher-role store a tick enqueues through."""
    return SqlAlchemyJobsStore(conn)


# --------------------------------------------------------------------------
# tick
# --------------------------------------------------------------------------


class SchedulerTick:
    """One scheduler pass; ``run_once`` enqueues what is due and nothing
    else (idempotent via the 07 §3 keys)."""

    def __init__(
        self,
        queries: SchedulerQueries,
        jobs: JobService,
        *,
        report_local_hour: int = DEFAULT_REPORT_LOCAL_HOUR,
        watch_interval: timedelta = DEFAULT_WATCH_INTERVAL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queries = queries
        self._jobs = jobs
        self._report_local_hour = report_local_hour
        self._watch_interval = watch_interval
        self._clock = clock if clock is not None else _utcnow

    async def run_once(
        self, store: JobsStore, *, now: datetime | None = None
    ) -> SchedulerTickResult:
        now = now if now is not None else self._clock()
        result = SchedulerTickResult()

        for feed in await self._queries.due_feeds(now):
            job, created = await self._jobs.enqueue(
                store,
                IndustryScope(owner_id=feed.owner_id),
                kind="discover",
                payload={
                    "feed_id": str(feed.feed_id),
                    "schedule_slot": feed.schedule_slot(now),
                },
                idempotency_key=build_idempotency_key(
                    "discover",
                    {
                        "owner": str(feed.owner_id),
                        "feed": str(feed.feed_id),
                        "schedule_slot": str(feed.schedule_slot(now)),
                        "config_version": str(feed.config_version),
                    },
                ),
            )
            result.discovered += int(created)
            if created:
                result.job_ids.append(job.id)

        for target in await self._queries.daily_report_targets(
            now, local_hour=self._report_local_hour
        ):
            job, created = await self._jobs.enqueue(
                store,
                IndustryScope(
                    owner_id=target.owner_id, industry_id=target.industry_id
                ),
                kind="report_build",
                payload={
                    "report_type": "daily",
                    "period": target.local_date,
                },
                idempotency_key=build_idempotency_key(
                    "report_build",
                    {
                        "industry": str(target.industry_id),
                        "report_type": "daily",
                        "period": target.local_date,
                        # Pre-manifest slot marker: the report handler
                        # (Task 10) computes the real input_manifest_hash
                        # once the corpus state is read.
                        "input_manifest_hash": "scheduled",
                    },
                ),
            )
            result.reports += int(created)
            if created:
                result.job_ids.append(job.id)

        slot = int(now.timestamp() // self._watch_interval.total_seconds())
        for watch in await self._queries.due_watches(
            now, interval=self._watch_interval
        ):
            job, created = await self._jobs.enqueue(
                store,
                IndustryScope(
                    owner_id=watch.owner_id, industry_id=watch.industry_id
                ),
                kind="watch_check",
                payload={
                    "watch_id": str(watch.watch_id),
                    "schedule_slot": slot,
                },
                idempotency_key=build_idempotency_key(
                    "watch_check",
                    {
                        "industry": str(watch.industry_id),
                        "watch": str(watch.watch_id),
                        "schedule_slot": str(slot),
                    },
                ),
            )
            result.watches += int(created)
            if created:
                result.job_ids.append(job.id)

        return result
