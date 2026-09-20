"""Unit tests: SourcesService against a dict-backed fake repo (spec 03 §2, 08 §2).

Spec references:
- docs/03-data-model.md §2  owner_feeds/industry_sources/source_runs 语义、
  UNIQUE(owner_id,seed_url,access_scope_key)、UNIQUE(industry_id,feed_id)
- docs/01-product.md §5  新订阅默认回填（行业 settings.backfill_days，默认 90 天）
- docs/08-api.md §1/§2  poll 长任务 202 JobAccepted、不按主题过滤

No real database: FakeSourcesRepo implements the SourcesRepository protocol;
InMemoryEnqueuer stands in for the Task 6 job queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from intel.contracts import (
    FeedCreate,
    FeedPatch,
    PollRequest,
    SourceRunView,
    SourceSubscriptionCreate,
    SourceSubscriptionPatch,
)
from intel.domain.urlnorm import normalize_url
from intel.repositories.base import IndustryScope
from intel.repositories.sources import (
    FeedRecord,
    IndustryContext,
    SourceRunRecord,
    SourcesRepository,
    SourceTemplateRecord,
    SubscriptionRecord,
)
from intel.services.acquisition import InMemoryEnqueuer
from intel.services.errors import (
    AlreadyExists,
    InvalidStateTransition,
    NotFound,
    ParserUnavailable,
    ValidationFailed,
    VersionConflict,
)
from intel.services.sources import SourcesService

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OWNER = uuid4()


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeSourcesRepo:
    """Dict-backed SourcesRepository — the observable storage state."""

    def __init__(self) -> None:
        self.owner_id = OWNER
        self.feeds: dict[UUID, FeedRecord] = {}
        self.subscriptions: dict[UUID, SubscriptionRecord] = {}
        self.runs: list[SourceRunRecord] = []
        self.templates: dict[str, SourceTemplateRecord] = {}
        # parser_key -> published parser_version id (None = unpublished).
        self.parsers: dict[str, UUID | None] = {}
        # industry_id -> context the subscription path needs.
        self.industry_contexts: dict[UUID, IndustryContext] = {}

    # -- global catalog -------------------------------------------------------

    async def list_templates(self) -> list[SourceTemplateRecord]:
        return list(self.templates.values())

    async def get_template(self, template_id: str) -> SourceTemplateRecord | None:
        return self.templates.get(template_id)

    async def latest_published_parser_version(
        self, parser_key: str
    ) -> UUID | None:
        return self.parsers.get(parser_key)

    # -- feeds (O scope) ------------------------------------------------------

    async def insert_feed(self, feed: FeedRecord) -> None:
        self.feeds[feed.id] = feed

    async def get_feed(self, feed_id: UUID) -> FeedRecord | None:
        return self.feeds.get(feed_id)

    async def list_feeds(self) -> list[FeedRecord]:
        return list(self.feeds.values())

    async def feed_seed_taken(
        self, seed_url: str, access_scope_key: str
    ) -> bool:
        return any(
            f.seed_url == seed_url and f.access_scope_key == access_scope_key
            for f in self.feeds.values()
        )

    async def update_feed(
        self,
        feed_id: UUID,
        expected_version: int,
        *,
        interval_seconds: int | None = None,
        user_enabled: bool | None = None,
        parser_version_id: UUID | None = None,
        status: str | None = None,
    ) -> FeedRecord | None:
        record = self.feeds.get(feed_id)
        if record is None or record.row_version != expected_version:
            return None
        if interval_seconds is not None:
            record.interval_seconds = interval_seconds
        if user_enabled is not None:
            record.user_enabled = user_enabled
        if parser_version_id is not None:
            record.parser_version_id = parser_version_id
        if status is not None:
            record.status = status
        record.row_version += 1
        return record

    # -- subscriptions (I scope) ---------------------------------------------

    async def get_industry_context(
        self, industry_id: UUID
    ) -> IndustryContext | None:
        return self.industry_contexts.get(industry_id)

    async def insert_subscription(self, sub: SubscriptionRecord) -> None:
        self.subscriptions[sub.id] = sub

    async def get_subscription(
        self, industry_id: UUID, subscription_id: UUID
    ) -> SubscriptionRecord | None:
        record = self.subscriptions.get(subscription_id)
        if record is None or record.industry_id != industry_id:
            return None
        return record

    async def list_subscriptions(
        self, industry_id: UUID
    ) -> list[SubscriptionRecord]:
        return [
            s for s in self.subscriptions.values()
            if s.industry_id == industry_id
        ]

    async def subscription_feed_taken(
        self, industry_id: UUID, feed_id: UUID
    ) -> bool:
        return any(
            s.industry_id == industry_id and s.feed_id == feed_id
            for s in self.subscriptions.values()
        )

    async def update_subscription(
        self,
        industry_id: UUID,
        subscription_id: UUID,
        expected_version: int,
        *,
        status: str,
    ) -> SubscriptionRecord | None:
        record = await self.get_subscription(industry_id, subscription_id)
        if record is None or record.row_version != expected_version:
            return None
        record.status = status
        record.row_version += 1
        return record

    # -- runs (O scope) -------------------------------------------------------

    async def list_runs(self, feed_id: UUID) -> list[SourceRunRecord]:
        return [r for r in self.runs if r.feed_id == feed_id]


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def repo() -> FakeSourcesRepo:
    repo = FakeSourcesRepo()
    repo.parsers["rss"] = uuid4()
    repo.templates["tpl-semi-weekly"] = SourceTemplateRecord(
        id="tpl-semi-weekly",
        name="SemiWeekly",
        homepage="https://example.com",
        canonical_seed="https://example.com/rss",
        kind="rss",
        tags=["news"],
        access_notes="",
    )
    repo.industry_contexts[INDUSTRY] = IndustryContext(
        status="active", backfill_days=90
    )
    return repo


@pytest.fixture()
def enqueuer() -> InMemoryEnqueuer:
    return InMemoryEnqueuer()


@pytest.fixture()
def service(
    repo: FakeSourcesRepo, enqueuer: InMemoryEnqueuer, clock: FakeClock
) -> SourcesService:
    return SourcesService(repo, enqueuer, clock=clock)


INDUSTRY = uuid4()

FEED = FeedCreate(
    template_id="tpl-semi-weekly",
    seed_url="HTTPS://Example.com:443/rss?utm_source=x",
    adapter_type="rss",
    interval_seconds=3600,
)


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------


class TestFeeds:
    async def test_create_normalizes_seed_and_resolves_parser(
        self, service: SourcesService, repo: FakeSourcesRepo
    ) -> None:
        view = await service.create_feed(FEED)
        assert view.status == "active"
        assert view.seed_url == normalize_url(FEED.seed_url)
        assert view.template_id == "tpl-semi-weekly"
        stored = repo.feeds[view.id]
        assert stored.parser_version_id == repo.parsers["rss"]
        assert stored.access_scope_key == "public"
        assert stored.user_enabled is True

    async def test_create_with_credential_uses_credential_scope(
        self, service: SourcesService, repo: FakeSourcesRepo
    ) -> None:
        await service.create_feed(FEED)
        view = await service.create_feed(
            FEED.model_copy(update={"credential_ref": "vault:7"})
        )
        assert repo.feeds[view.id].access_scope_key == "credential:vault:7"
        # Same seed but a different access scope is a distinct feed.

    async def test_create_rejects_non_http_seed(
        self, service: SourcesService
    ) -> None:
        with pytest.raises(ValidationFailed):
            await service.create_feed(
                FeedCreate(seed_url="not a url", adapter_type="rss")
            )

    async def test_create_unknown_template_404s(
        self, service: SourcesService
    ) -> None:
        with pytest.raises(NotFound):
            await service.create_feed(
                FeedCreate(
                    template_id="missing", seed_url="https://a.example/rss",
                    adapter_type="rss",
                )
            )

    async def test_create_without_published_parser_503s(
        self, service: SourcesService, repo: FakeSourcesRepo
    ) -> None:
        repo.parsers.pop("rss")
        with pytest.raises(ParserUnavailable):
            await service.create_feed(FEED)

    async def test_duplicate_seed_same_scope_rejected(
        self, service: SourcesService
    ) -> None:
        await service.create_feed(FEED)
        with pytest.raises(AlreadyExists):
            await service.create_feed(
                FEED.model_copy(update={"template_id": None})
            )

    async def test_get_missing_feed_404s(self, service: SourcesService) -> None:
        with pytest.raises(NotFound):
            await service.get_feed(uuid4())

    async def test_patch_updates_fields(
        self, service: SourcesService
    ) -> None:
        view = await service.create_feed(FEED)
        patched = await service.patch_feed(
            view.id,
            FeedPatch(
                expected_version=view.row_version,
                interval_seconds=7200,
                user_enabled=False,
                status="paused",
            ),
        )
        assert patched.interval_seconds == 7200
        assert patched.user_enabled is False
        assert patched.status == "paused"
        assert patched.row_version == view.row_version + 1

    async def test_patch_version_conflict(self, service: SourcesService) -> None:
        view = await service.create_feed(FEED)
        with pytest.raises(VersionConflict):
            await service.patch_feed(
                view.id, FeedPatch(expected_version=99, user_enabled=False)
            )


# --------------------------------------------------------------------------
# poll (job dispatch; real queue is Task 6)
# --------------------------------------------------------------------------


class TestPoll:
    async def test_poll_enqueues_source_poll(
        self, service: SourcesService, enqueuer: InMemoryEnqueuer
    ) -> None:
        view = await service.create_feed(FEED)
        scope = IndustryScope(owner_id=OWNER)
        accepted = await service.poll(
            view.id, PollRequest(mode="trial"), scope, idempotency_key="k1"
        )
        assert accepted.state == "queued"
        (record,) = enqueuer.records
        assert record.kind == "source_poll"
        assert record.scope == scope
        assert record.payload["feed_id"] == str(view.id)
        assert record.payload["mode"] == "trial"
        assert record.idempotency_key == "k1"

    async def test_poll_missing_feed_404s(self, service: SourcesService) -> None:
        with pytest.raises(NotFound):
            await service.poll(
                uuid4(), PollRequest(), IndustryScope(OWNER), idempotency_key="k"
            )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


class TestRuns:
    async def test_runs_map_to_views_with_coverage(
        self, service: SourcesService, repo: FakeSourcesRepo
    ) -> None:
        feed = await service.create_feed(FEED)
        feed_id = feed.id
        job_id = uuid4()
        repo.runs.append(
            SourceRunRecord(
                id=uuid4(),
                feed_id=feed_id,
                job_id=job_id,
                started_at=T0,
                finished_at=T0 + timedelta(minutes=5),
                outcome="success",
                discovered_count=10,
                fetched_count=8,
                unresolved_count=0,
                coverage_end=T0,
                cursor_before=None,
                cursor_after="c1",
                error_code=None,
            )
        )
        repo.runs.append(
            SourceRunRecord(
                id=uuid4(),
                feed_id=feed_id,
                job_id=uuid4(),
                started_at=T0,
                finished_at=None,
                outcome="partial",
                discovered_count=4,
                fetched_count=2,
                unresolved_count=2,
                coverage_end=None,
                cursor_before=None,
                cursor_after=None,
                error_code=None,
            )
        )
        runs = await service.list_runs(feed_id)
        assert all(isinstance(r, SourceRunView) for r in runs)
        by_outcome = {r.outcome: r for r in runs}
        assert by_outcome["success"].coverage.status == "complete"
        assert by_outcome["success"].coverage.processed == 8
        assert by_outcome["partial"].coverage.status == "pending"

    async def test_runs_scoped_to_feed(
        self, service: SourcesService, repo: FakeSourcesRepo
    ) -> None:
        mine = await service.create_feed(FEED)
        other = await service.create_feed(
            FEED.model_copy(
                update={"seed_url": "https://other.example/rss", "template_id": None}
            )
        )
        repo.runs.append(
            SourceRunRecord(
                id=uuid4(), feed_id=other.id, job_id=uuid4(), started_at=T0,
                finished_at=T0, outcome="no_change", discovered_count=0,
                fetched_count=0, unresolved_count=0, coverage_end=None,
                cursor_before=None, cursor_after=None, error_code=None,
            )
        )
        assert await service.list_runs(mine.id) == []


# --------------------------------------------------------------------------
# subscriptions (industry-level)
# --------------------------------------------------------------------------


class TestSubscriptions:
    async def test_default_backfill_from_industry_settings(
        self, service: SourcesService
    ) -> None:
        feed = await service.create_feed(FEED)
        view = await service.create_subscription(
            INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
        )
        assert view.status == "active"
        assert view.backfill_from == T0 - timedelta(days=90)
        assert view.industry_id == INDUSTRY

    async def test_explicit_backfill_from_wins(
        self, service: SourcesService
    ) -> None:
        feed = await service.create_feed(FEED)
        when = T0 - timedelta(days=365)
        view = await service.create_subscription(
            INDUSTRY,
            SourceSubscriptionCreate(feed_id=feed.id, backfill_from=when),
        )
        assert view.backfill_from == when

    async def test_foreign_or_missing_feed_404s(
        self, service: SourcesService
    ) -> None:
        with pytest.raises(NotFound):
            await service.create_subscription(
                INDUSTRY, SourceSubscriptionCreate(feed_id=uuid4())
            )

    async def test_duplicate_subscription_rejected(
        self, service: SourcesService
    ) -> None:
        feed = await service.create_feed(FEED)
        await service.create_subscription(
            INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
        )
        with pytest.raises(AlreadyExists):
            await service.create_subscription(
                INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
            )

    async def test_archived_industry_rejects_subscription(
        self, service: SourcesService, repo: FakeSourcesRepo
    ) -> None:
        feed = await service.create_feed(FEED)
        repo.industry_contexts[INDUSTRY] = IndustryContext(
            status="archived", backfill_days=90
        )
        with pytest.raises(InvalidStateTransition):
            await service.create_subscription(
                INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
            )

    async def test_patch_subscription_pauses(
        self, service: SourcesService
    ) -> None:
        feed = await service.create_feed(FEED)
        view = await service.create_subscription(
            INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
        )
        paused = await service.patch_subscription(
            INDUSTRY,
            view.id,
            SourceSubscriptionPatch(
                expected_version=view.row_version, status="paused"
            ),
        )
        assert paused.status == "paused"

    async def test_patch_subscription_conflict(
        self, service: SourcesService
    ) -> None:
        feed = await service.create_feed(FEED)
        view = await service.create_subscription(
            INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
        )
        with pytest.raises(VersionConflict):
            await service.patch_subscription(
                INDUSTRY,
                view.id,
                SourceSubscriptionPatch(expected_version=5, status="paused"),
            )

    async def test_list_subscriptions(
        self, service: SourcesService
    ) -> None:
        feed = await service.create_feed(FEED)
        await service.create_subscription(
            INDUSTRY, SourceSubscriptionCreate(feed_id=feed.id)
        )
        views = await service.list_subscriptions(INDUSTRY)
        assert [v.feed_id for v in views] == [feed.id]


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


class TestTemplates:
    async def test_list_templates_has_no_user_state(
        self, service: SourcesService
    ) -> None:
        (template,) = await service.list_templates()
        assert template.id == "tpl-semi-weekly"
        assert template.kind == "rss"


# --------------------------------------------------------------------------
# protocol wiring
# --------------------------------------------------------------------------


def test_repositories_satisfy_the_protocol() -> None:
    assert isinstance(FakeSourcesRepo(), SourcesRepository)
