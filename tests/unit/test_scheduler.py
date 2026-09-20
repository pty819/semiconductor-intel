"""Unit tests: SchedulerTick against fake queries (spec 04 §2/§7, 07 §4).

Semantics under test:
- discover goes to feeds with next_poll_at <= now AND (user_enabled OR an
  active subscription in an active industry) — spec 04 §2/§7: a feed with
  no active subscription and user_enabled=false must NOT be polled (the
  predicate itself is asserted in test_jobs_sql.py).
- daily report_build for active industries inside the configured local-time
  window; the idempotency key (industry/report_type/period) makes a second
  tick in the same period a no-op (07 §3).
- watch checks re-enqueue per watch on a fixed cadence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService, build_idempotency_key
from intel.workers.scheduler import (
    DailyReportTarget,
    DueFeed,
    DueWatch,
    SchedulerTick,
)

T0 = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
OWNER = uuid4()
INDUSTRY = uuid4()
OTHER_INDUSTRY = uuid4()


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeQueries:
    def __init__(
        self,
        feeds: list[DueFeed] | None = None,
        reports: list[DailyReportTarget] | None = None,
        watches: list[DueWatch] | None = None,
    ) -> None:
        self.feeds = feeds or []
        self.reports = reports or []
        self.watches = watches or []

    async def due_feeds(self, now: datetime) -> list[DueFeed]:
        return list(self.feeds)

    async def daily_report_targets(
        self, now: datetime, *, local_hour: int
    ) -> list[DailyReportTarget]:
        return list(self.reports)

    async def due_watches(
        self, now: datetime, *, interval: timedelta
    ) -> list[DueWatch]:
        return list(self.watches)


def make_tick(queries: FakeQueries) -> tuple[SchedulerTick, InMemoryJobsDatabase]:
    import random

    db = InMemoryJobsDatabase()
    clock = FakeClock()
    service = JobService(clock=clock, rng=random.Random(1))
    tick = SchedulerTick(queries, service, clock=clock)
    return tick, db


def _feed(
    *, owner: UUID = OWNER, interval: int = 21600, config_version: int = 1
) -> DueFeed:
    return DueFeed(
        owner_id=owner,
        feed_id=uuid4(),
        interval_seconds=interval,
        config_version=config_version,
    )


async def test_tick_enqueues_discover_for_due_feeds() -> None:
    feed = _feed()
    tick, db = make_tick(FakeQueries(feeds=[feed]))
    result = await tick.run_once(InMemoryJobsStore(db))
    assert result.discovered == 1
    job = db.jobs[result.job_ids[0]]
    assert job["kind"] == "discover"
    assert job["owner_id"] == feed.owner_id
    assert job["industry_id"] is None, "discover is a kind-level (owner-wide) job"
    assert job["state"] == "queued"
    assert job["input"]["feed_id"] == str(feed.feed_id)
    expected_key = build_idempotency_key(
        "discover",
        {
            "owner": str(feed.owner_id),
            "feed": str(feed.feed_id),
            "schedule_slot": str(feed.schedule_slot(T0)),
            "config_version": str(feed.config_version),
        },
    )
    assert job["idempotency_key"] == expected_key


async def test_tick_is_idempotent_within_schedule_slot() -> None:
    feed = _feed()
    tick, db = make_tick(FakeQueries(feeds=[feed]))
    first = await tick.run_once(InMemoryJobsStore(db))
    second = await tick.run_once(InMemoryJobsStore(db))
    assert first.discovered == 1
    # Same slot: the idempotency key returns the existing job, no new row.
    assert second.discovered == 0
    assert len(db.jobs) == 1


async def test_tick_new_slot_after_interval_enqueues_again() -> None:
    feed = _feed(interval=3600)
    tick, db = make_tick(FakeQueries(feeds=[feed]))
    await tick.run_once(InMemoryJobsStore(db))
    assert feed.schedule_slot(T0 + timedelta(seconds=3600)) == (
        feed.schedule_slot(T0) + 1
    )
    later = await tick.run_once(
        InMemoryJobsStore(db), now=T0 + timedelta(seconds=3601)
    )
    assert later.discovered == 1
    assert len(db.jobs) == 2


async def test_tick_enqueues_daily_reports_in_window() -> None:
    target = DailyReportTarget(
        owner_id=OWNER, industry_id=INDUSTRY, local_date="2026-09-20"
    )
    tick, db = make_tick(FakeQueries(reports=[target]))
    result = await tick.run_once(InMemoryJobsStore(db))
    assert result.reports == 1
    job = db.jobs[result.job_ids[0]]
    assert job["kind"] == "report_build"
    assert job["owner_id"] == OWNER
    assert job["industry_id"] == INDUSTRY
    assert job["idempotency_key"] == build_idempotency_key(
        "report_build",
        {
            "industry": str(INDUSTRY),
            "report_type": "daily",
            "period": "2026-09-20",
            "input_manifest_hash": "scheduled",
        },
    )
    # Second tick in the same period: no duplicate.
    again = await tick.run_once(InMemoryJobsStore(db))
    assert again.reports == 0
    assert len(db.jobs) == 1


async def test_tick_enqueues_watch_checks() -> None:
    watch = DueWatch(owner_id=OWNER, industry_id=INDUSTRY, watch_id=uuid4())
    tick, db = make_tick(FakeQueries(watches=[watch]))
    result = await tick.run_once(InMemoryJobsStore(db))
    assert result.watches == 1
    job = db.jobs[result.job_ids[0]]
    assert job["kind"] == "watch_check"
    assert job["industry_id"] == INDUSTRY
    assert job["input"]["watch_id"] == str(watch.watch_id)
    # Same cadence slot: idempotent.
    again = await tick.run_once(InMemoryJobsStore(db))
    assert again.watches == 0


async def test_tick_result_counts_mixed_work() -> None:
    tick, db = make_tick(
        FakeQueries(
            feeds=[_feed()],
            reports=[
                DailyReportTarget(
                    owner_id=OWNER, industry_id=INDUSTRY, local_date="2026-09-20"
                )
            ],
            watches=[DueWatch(OWNER, OTHER_INDUSTRY, uuid4())],
        )
    )
    result = await tick.run_once(InMemoryJobsStore(db))
    assert (result.discovered, result.reports, result.watches) == (1, 1, 1)
    assert len(result.job_ids) == 3
