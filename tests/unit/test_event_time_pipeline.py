"""End-to-end occurrence-time pipeline: proposal → TimeValue → store → timeline.

The final review's C-1 survived per-layer unit tests (each layer assumed
the neighbouring one supplied the data), so these tests chain the REAL
layers: the event_build handler through the JobRunner writes event
revisions whose occurred data flows into the timeline semantics the SQL
reader implements — grouping (known dates vs the 未知 group), TIM-01
window intersection, and the TIM-02 late-discovery marker — plus the SQL
projection of ``occurred_start``/``occurred_end`` the adapter must write.
"""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from intel.contracts.models import TimeValue
from intel.domain.time import occurred_span, parse_occurrence_time
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService
from intel.services.knowledge import InMemoryKnowledgeStore
from intel.services.timeline import effective_sort_key, intersects
from intel.workers.runner import JobRunner
from intel.workflows.event_build import EventBuildWiring, make_event_build_handler

OWNER = uuid4()
INDUSTRY = uuid4()


class _Proposal:
    def __init__(self, **kwargs) -> None:
        self.event_type = kwargs.get("event_type", "other")
        self.title = kwargs.get("title", "")
        self.summary = kwargs.get("summary", "")
        self.claim_ids = kwargs.get("claim_ids", [])
        self.occurred = kwargs.get("occurred", "")
        self.identity_fields = kwargs.get("identity_fields", {})


class _Proposals:
    def __init__(self, proposals: list[_Proposal]) -> None:
        self.proposals = proposals


class _Agent:
    def __init__(self, proposals: _Proposals) -> None:
        self._proposals = proposals

    async def propose_events(self, request, claims) -> _Proposals:
        return self._proposals


class _StoreDouble:
    def __init__(self, knowledge: InMemoryKnowledgeStore, claims: list[dict]):
        self.knowledge = knowledge
        self._claims = claims

    async def get_extraction_claims(self, run_id):
        return self._claims


def _revision_rows(knowledge: InMemoryKnowledgeStore) -> list[dict]:
    return list(knowledge.event_revisions.values())


async def _run_handler(
    knowledge: InMemoryKnowledgeStore, proposals: _Proposals
) -> JobRunner:
    run_id = uuid4()
    store = _StoreDouble(knowledge, [{"id": str(uuid4()), "text": "陈述"}])

    @asynccontextmanager
    async def open(scope):
        yield store

    wiring = EventBuildWiring(open_store=open, agent=_Agent(proposals))
    db = InMemoryJobsDatabase()
    service = JobService(clock=lambda: datetime.now(UTC), rng=random.Random(1))
    runner = JobRunner(service, _open_jobs(db))
    runner.register("event_build", make_event_build_handler(wiring))
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(OWNER, INDUSTRY),
        kind="event_build",
        payload={"extraction_run_id": str(run_id)},
        idempotency_key=f"event_build:{run_id}",
    )
    return runner


def _open_jobs(db: InMemoryJobsDatabase):
    @asynccontextmanager
    async def open(scope):
        yield InMemoryJobsStore(db)

    return open


class TestOccurrencePipeline:
    async def test_dated_and_unknown_events_reach_timeline_semantics(self):
        """Chain: handler → store rows → timeline grouping / TIM-01 / 迟到."""
        knowledge = InMemoryKnowledgeStore()
        runner = await _run_handler(
            knowledge,
            _Proposals(
                [
                    _Proposal(
                        event_type="commercial_announcement",
                        title="三月公告",
                        occurred="公司于2026年3月5日宣布",
                    ),
                    _Proposal(
                        event_type="commercial_announcement",
                        title="未知时间事件",
                        occurred="去年某个时候",
                    ),
                ]
            ),
        )
        result = await runner.run_once()
        assert result is not None and result.state == "succeeded"

        rows = _revision_rows(knowledge)
        assert len(rows) == 2
        by_title = {row["title"]: row for row in rows}

        dated = by_title["三月公告"]
        assert dated["occurred_time"]["precision"] == "day"
        assert dated["occurred_start"] == datetime(2026, 3, 5, tzinfo=UTC)
        assert dated["occurred_end"] == datetime(2026, 3, 6, tzinfo=UTC)
        assert dated["first_discovered_at"] is not None

        unknown = by_title["未知时间事件"]
        assert unknown["occurred_time"]["precision"] == "unknown"
        assert unknown["occurred_start"] is None
        assert unknown["occurred_end"] is None

        # -- the semantics list_event_cards implements in SQL -------------
        dated_tv = TimeValue.model_validate(dated["occurred_time"])
        unknown_tv = TimeValue.model_validate(unknown["occurred_time"])
        # Right group: known date sorts before the separate 未知 group.
        assert effective_sort_key(dated_tv) < effective_sort_key(unknown_tv)
        # TIM-01: a window overlapping March 5 hits the day-precision event.
        assert intersects(
            dated_tv,
            datetime(2026, 3, 5, 12, tzinfo=UTC),
            datetime(2026, 3, 6, 12, tzinfo=UTC),
        )
        assert not intersects(
            dated_tv,
            datetime(2026, 4, 1, tzinfo=UTC),
            datetime(2026, 5, 1, tzinfo=UTC),
        )
        # Unknown dates never match ranged filters.
        assert not intersects(
            unknown_tv,
            datetime(1900, 1, 1, tzinfo=UTC),
            datetime(2100, 1, 1, tzinfo=UTC),
        )

        # late_discovery: the projection + discovery date carry enough to
        # compute the marker (repositories._late_discovery is the SQL
        # reader's rule; the same inputs drive it there).
        from intel.repositories.knowledge import _late_discovery

        assert _late_discovery(
            dated["occurred_start"],
            dated["occurred_end"],
            datetime(2026, 9, 1, tzinfo=UTC),
        )
        assert not _late_discovery(
            dated["occurred_start"],
            dated["occurred_end"],
            datetime(2026, 3, 5, 12, tzinfo=UTC),
        )
        assert not _late_discovery(None, None, datetime(2026, 9, 1, tzinfo=UTC))

    async def test_unparseable_expression_downgrades_not_crashes(self):
        knowledge = InMemoryKnowledgeStore()
        runner = await _run_handler(
            knowledge,
            _Proposals(
                [_Proposal(title="乱时间", occurred="2026年13月40日 and stuff")]
            ),
        )
        result = await runner.run_once()
        assert result is not None and result.state == "succeeded"
        row = _revision_rows(knowledge)[0]
        assert row["occurred_time"]["precision"] == "unknown"
        assert row["occurred_start"] is None


class TestParserPrecision:
    """03 §5: month/year cover the whole period; no day is invented."""

    def test_iso_and_cn_shapes(self):
        cases = [
            ("2026-03-05", "day"),
            ("公司于2026年3月5日宣布", "day"),
            ("2026年3月", "month"),
            ("2026-03", "month"),
            ("2026年", "year"),
            ("2026-03-05T10:30:00Z", "instant"),
            ("2026年3月5日 10时30分", "instant"),
            ("", "unknown"),
            ("no date", "unknown"),
            ("2026-13月40日", "unknown"),
        ]
        for text, precision in cases:
            tv = parse_occurrence_time(text)
            assert tv.precision == precision, text

    def test_timezone_offset_converted_to_utc(self):
        tv = parse_occurrence_time("发布于 2026-02-28T18:00:00+08:00")
        assert tv.precision == "instant"
        assert tv.start == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)

    def test_occurred_span_matches_orm_convention(self):
        instant = TimeValue(
            start=datetime(2026, 3, 5, 10, tzinfo=UTC), precision="instant"
        )
        assert occurred_span(instant) == (
            datetime(2026, 3, 5, 10, tzinfo=UTC),
            None,
        )
        assert occurred_span(TimeValue(precision="unknown")) == (None, None)
        month = parse_occurrence_time("2026年3月")
        assert occurred_span(month) == (
            datetime(2026, 3, 1, tzinfo=UTC),
            datetime(2026, 4, 1, tzinfo=UTC),
        )


class TestSqlProjection:
    """The SQL adapter writes the projection columns + validated JSONB."""

    async def test_insert_event_with_revision_projects_occurred_bounds(self):
        from intel.repositories.knowledge import SqlAlchemyKnowledgeStore

        class _Result:
            rowcount = 1

            def first(self):
                return None

            def one_or_none(self):
                return (str(OWNER),)  # require_owner_guc reads the GUC back

        class _Conn:
            def __init__(self) -> None:
                self.statements: list[object] = []

            async def execute(self, stmt, params=None):
                self.statements.append(stmt)
                return _Result()

        conn = _Conn()
        scope = IndustryScope(OWNER, INDUSTRY)
        store = SqlAlchemyKnowledgeStore(conn, scope)
        occurred = parse_occurrence_time("2026年3月5日")
        event_id, revision_id = await store.insert_event_with_revision(
            scope,
            event_type="commercial_announcement",
            title="三月公告",
            summary="",
            identity_key=None,
            rationale="no strong identity key",
            input_manifest={},
            occurred=occurred.model_dump(mode="json"),
        )
        assert event_id and revision_id
        inserts = [
            stmt
            for stmt in conn.statements
            if "INSERT INTO EVENT_REVISIONS" in str(stmt).upper()
        ]
        assert inserts, "event revision insert missing"
        compiled = inserts[0].compile(dialect=postgresql.dialect())
        params = compiled.params
        assert params["occurred_start"] == datetime(2026, 3, 5, tzinfo=UTC)
        assert params["occurred_end"] == datetime(2026, 3, 6, tzinfo=UTC)
        import json

        occurred_json = params["occurred_time"]
        if isinstance(occurred_json, (str, bytes)):
            occurred_json = json.loads(occurred_json)
        assert occurred_json["precision"] == "day"
        assert params["first_discovered_at"] is not None

    async def test_invalid_occurred_payload_downgrades_to_unknown(self):
        from intel.repositories.knowledge import SqlAlchemyKnowledgeStore

        class _Result:
            rowcount = 1

            def first(self):
                return None

            def one_or_none(self):
                return (str(OWNER),)

        class _Conn:
            def __init__(self) -> None:
                self.statements: list[object] = []

            async def execute(self, stmt, params=None):
                self.statements.append(stmt)
                return _Result()

        conn = _Conn()
        scope = IndustryScope(OWNER, INDUSTRY)
        store = SqlAlchemyKnowledgeStore(conn, scope)
        # Known precision without a start is structurally invalid — the
        # adapter downgrades instead of crashing the commit.
        event_id, _ = await store.insert_event_with_revision(
            scope,
            event_type="other",
            title="t",
            summary="",
            identity_key=None,
            rationale="",
            input_manifest={},
            occurred={"precision": "day"},
        )
        assert event_id
        inserts = [
            stmt
            for stmt in conn.statements
            if "INSERT INTO EVENT_REVISIONS" in str(stmt).upper()
        ]
        params = inserts[0].compile(dialect=postgresql.dialect()).params
        assert params["occurred_start"] is None
        assert params["occurred_end"] is None


class TestTimelineSortAxis:
    """M-5: the reader exposes sort=occurred|discovered (05 §5)."""

    def _repo(self, statements: list):
        from intel.repositories.knowledge import SqlAlchemyKnowledgeRepository

        repo = SqlAlchemyKnowledgeRepository.__new__(SqlAlchemyKnowledgeRepository)

        class _Rows:
            def mappings(self):
                return []

            def __len__(self):
                return 0

        class _Conn:
            async def execute(self, stmt, params=None):
                statements.append(stmt)
                return _Rows()

        repo._conn = _Conn()

        class _Scope:
            owner_id = OWNER
            industry_id = INDUSTRY

        repo.scope = _Scope()

        async def _bind():
            return INDUSTRY

        repo._bind = _bind
        return repo

    def test_route_declares_sort_parameter(self):
        from fastapi.routing import APIRoute

        from intel.api.routes import knowledge

        timeline = next(
            route
            for route in knowledge.timeline_router.routes
            if isinstance(route, APIRoute) and route.path.endswith("/timeline")
        )
        assert "sort" in timeline.endpoint.__annotations__

    async def test_discovered_axis_orders_by_first_discovered(self):
        statements: list = []
        repo = self._repo(statements)
        cards, cursor = await repo.list_event_cards(sort="discovered", limit=10)
        assert cards == [] and cursor is None
        sql = str(statements[0].compile(dialect=postgresql.dialect()))
        assert "first_discovered" in sql
        assert "ORDER BY" in sql

    async def test_latest_revision_window_has_id_tiebreak(self):
        # M-2: deterministic latest-revision pick on shared recorded_at.
        statements: list = []
        repo = self._repo(statements)
        await repo.list_event_cards(limit=10)
        sql = str(statements[0].compile(dialect=postgresql.dialect()))
        window = sql.split("OVER")[1].split("PARTITION BY")[1]
        assert "recorded_at DESC" in window
        assert "id DESC" in window

    async def test_invalid_sort_rejected(self):
        import pytest

        statements: list = []
        repo = self._repo(statements)
        with pytest.raises(ValueError):
            await repo.list_event_cards(sort="bogus", limit=10)
