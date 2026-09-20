"""Sources service: feeds, subscriptions, poll dispatch (spec 03 §2, 08 §2).

Semantics:

- Feeds are owner-level entrances (本人的公共采集入口配置). The seed URL is
  normalized (urlnorm) and stored with a derived ``access_scope_key`` —
  ``public`` without credentials, ``credential:<ref>`` with — so the same
  seed under different access policies is a distinct feed (UNIQUE(owner_id,
  seed_url, access_scope_key), spec 03 §2). Credentials are secret
  references only, never inline.
- Creating a feed resolves the latest published parser version for its
  adapter type; none published → 503 ``parser_unavailable``.
- Subscriptions bind a feed into one industry (I scope), UNIQUE(industry_id,
  feed_id); the default ``backfill_from`` is now - settings.backfill_days
  (90 days by default, 01 §5 — an adjustable default, not a hard cutoff).
- ``poll`` validates ownership then dispatches a ``source_poll`` job via the
  JobEnqueuer port; it never fetches inline (08 §1: 202, 不在一个 HTTP 请求
  里等完整抓取) and never filters by topic (03 §2).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from intel.contracts import (
    Coverage,
    FeedCreate,
    FeedPatch,
    FeedView,
    JobAccepted,
    PollRequest,
    SourceRunView,
    SourceSubscriptionCreate,
    SourceSubscriptionPatch,
    SourceSubscriptionView,
    SourceTemplate,
)
from intel.domain.urlnorm import normalize_url
from intel.repositories.base import IndustryScope
from intel.repositories.sources import (
    FeedRecord,
    SourcesRepository,
    SubscriptionRecord,
)
from intel.services.acquisition import JobEnqueuer
from intel.services.errors import (
    AlreadyExists,
    InvalidStateTransition,
    NotFound,
    ParserUnavailable,
    ValidationFailed,
    VersionConflict,
)

#: Coverage.status per run outcome; finished_at missing means still pending.
_RUN_COVERAGE_STATUS = {
    "success": "complete",
    "no_change": "complete",
    "partial": "partial",
    "failed": "unknown",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _feed_view(feed: FeedRecord) -> FeedView:
    return FeedView(
        id=feed.id,
        template_id=feed.template_id,
        seed_url=feed.seed_url,
        adapter_type=feed.adapter_type,
        interval_seconds=feed.interval_seconds,
        user_enabled=feed.user_enabled,
        credential_ref=feed.credential_ref,
        row_version=feed.row_version,
        status=feed.status,
        next_poll_at=feed.next_poll_at,
    )


def _subscription_view(sub: SubscriptionRecord) -> SourceSubscriptionView:
    return SourceSubscriptionView(
        id=sub.id,
        feed_id=sub.feed_id,
        backfill_from=sub.backfill_from,
        industry_id=sub.industry_id,
        row_version=sub.row_version,
        status=sub.status,
    )


class SourcesService:
    """Feed/subscription management and poll dispatch, storage-agnostic."""

    def __init__(
        self,
        repo: SourcesRepository,
        enqueuer: JobEnqueuer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._enqueuer = enqueuer
        self._clock = clock if clock is not None else _utcnow

    # -- global catalog ---------------------------------------------------------

    async def list_templates(self) -> list[SourceTemplate]:
        return [
            SourceTemplate(
                id=t.id,
                name=t.name,
                homepage=t.homepage,
                canonical_seed=t.canonical_seed,
                kind=t.kind,
                tags=list(t.tags),
                access_notes=t.access_notes,
            )
            for t in await self._repo.list_templates()
        ]

    # -- feeds (owner level) ----------------------------------------------------

    async def create_feed(self, cmd: FeedCreate) -> FeedView:
        try:
            seed_url = normalize_url(cmd.seed_url)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if (
            cmd.template_id is not None
            and await self._repo.get_template(cmd.template_id) is None
        ):
            raise NotFound("source template not found")
        parser_version_id = await self._repo.latest_published_parser_version(
            cmd.adapter_type
        )
        if parser_version_id is None:
            raise ParserUnavailable(
                f"no published parser version for adapter {cmd.adapter_type!r}"
            )
        access_scope_key = (
            "public" if cmd.credential_ref is None
            else f"credential:{cmd.credential_ref}"
        )
        if await self._repo.feed_seed_taken(seed_url, access_scope_key):
            raise AlreadyExists(
                "a feed for this seed URL and access scope already exists"
            )
        feed = FeedRecord(
            template_id=cmd.template_id,
            seed_url=seed_url,
            adapter_type=cmd.adapter_type,
            credential_ref=cmd.credential_ref,
            access_scope_key=access_scope_key,
            parser_version_id=parser_version_id,
            interval_seconds=cmd.interval_seconds,
            status="active",
            user_enabled=cmd.user_enabled,
        )
        await self._repo.insert_feed(feed)
        return _feed_view(feed)

    async def get_feed(self, feed_id: UUID) -> FeedView:
        return _feed_view(await self._require_feed(feed_id))

    async def list_feeds(self) -> list[FeedView]:
        return [_feed_view(f) for f in await self._repo.list_feeds()]

    async def patch_feed(self, feed_id: UUID, cmd: FeedPatch) -> FeedView:
        await self._require_feed(feed_id)
        provided = cmd.model_fields_set
        updated = await self._repo.update_feed(
            feed_id,
            cmd.expected_version,
            interval_seconds=(
                cmd.interval_seconds
                if "interval_seconds" in provided and cmd.interval_seconds is not None
                else None
            ),
            user_enabled=(
                cmd.user_enabled
                if "user_enabled" in provided and cmd.user_enabled is not None
                else None
            ),
            parser_version_id=(
                cmd.parser_version_id
                if "parser_version_id" in provided and cmd.parser_version_id is not None
                else None
            ),
            status=(
                cmd.status if "status" in provided and cmd.status is not None else None
            ),
        )
        if updated is None:
            raise await self._feed_conflict(feed_id)
        return _feed_view(updated)

    async def poll(
        self,
        feed_id: UUID,
        cmd: PollRequest,
        scope: IndustryScope,
        *,
        idempotency_key: str,
    ) -> JobAccepted:
        """Dispatch a poll job (trial / incremental / backfill). No inline
        fetch, no topic filtering (08 §2 / 03 §2)."""
        await self._require_feed(feed_id)
        payload: dict = {
            "feed_id": str(feed_id),
            "mode": cmd.mode,
            "from_time": (
                cmd.from_time.isoformat() if cmd.from_time is not None else None
            ),
        }
        return await self._enqueuer.enqueue(
            scope,
            kind="source_poll",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    async def list_runs(self, feed_id: UUID) -> list[SourceRunView]:
        await self._require_feed(feed_id)
        views = []
        for run in await self._repo.list_runs(feed_id):
            status = (
                "pending"
                if run.finished_at is None
                else _RUN_COVERAGE_STATUS.get(run.outcome, "unknown")
            )
            views.append(
                SourceRunView(
                    id=run.id,
                    feed_id=run.feed_id,
                    outcome=run.outcome,
                    coverage=Coverage(
                        status=status,
                        processed=run.fetched_count,
                        failed=run.unresolved_count,
                        watermark=run.coverage_end,
                    ),
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    error_code=run.error_code,
                )
            )
        return views

    # -- subscriptions (industry level) ------------------------------------------

    async def create_subscription(
        self, industry_id: UUID, cmd: SourceSubscriptionCreate
    ) -> SourceSubscriptionView:
        context = await self._repo.get_industry_context(industry_id)
        if context is None:
            raise NotFound("industry not found")
        if context.status == "archived":
            raise InvalidStateTransition(
                "cannot subscribe sources in an archived industry",
                action="create_subscription",
                current=context.status,
            )
        if await self._repo.get_feed(cmd.feed_id) is None:
            # 404 for foreign and unknown feeds alike (08 §1: 不存在与无权
            # 访问统一 404).
            raise NotFound("feed not found")
        if await self._repo.subscription_feed_taken(industry_id, cmd.feed_id):
            raise AlreadyExists("this feed is already subscribed in the industry")
        backfill_from = cmd.backfill_from
        if backfill_from is None:
            backfill_from = self._clock() - timedelta(days=context.backfill_days)
        sub = SubscriptionRecord(
            industry_id=industry_id,
            feed_id=cmd.feed_id,
            status="active",
            backfill_from=backfill_from,
            subscribed_at=self._clock(),
        )
        await self._repo.insert_subscription(sub)
        return _subscription_view(sub)

    async def list_subscriptions(
        self, industry_id: UUID
    ) -> list[SourceSubscriptionView]:
        return [
            _subscription_view(s)
            for s in await self._repo.list_subscriptions(industry_id)
        ]

    async def patch_subscription(
        self,
        industry_id: UUID,
        subscription_id: UUID,
        cmd: SourceSubscriptionPatch,
    ) -> SourceSubscriptionView:
        sub = await self._repo.get_subscription(industry_id, subscription_id)
        if sub is None:
            raise NotFound("subscription not found")
        updated = await self._repo.update_subscription(
            industry_id, subscription_id, cmd.expected_version, status=cmd.status
        )
        if updated is None:
            sub = await self._repo.get_subscription(industry_id, subscription_id)
            current = sub.row_version if sub is not None else 1
            raise VersionConflict(current)
        return _subscription_view(updated)

    # -- helpers ---------------------------------------------------------------

    async def _require_feed(self, feed_id: UUID) -> FeedRecord:
        feed = await self._repo.get_feed(feed_id)
        if feed is None:
            raise NotFound("feed not found")
        return feed

    async def _feed_conflict(self, feed_id: UUID) -> VersionConflict:
        feed = await self._repo.get_feed(feed_id)
        current = feed.row_version if feed is not None else 1
        return VersionConflict(current)
