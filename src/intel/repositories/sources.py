"""Sources repository: feeds, industry subscriptions, runs, catalog reads.

``owner_feeds`` / ``source_runs`` are O scope; ``industry_sources`` is I
scope; ``source_templates`` / ``parser_versions`` are G (global static, no
RLS — spec 03 §2). The catalog reads exist only to resolve template ids and
the latest published parser for an adapter when a feed is created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import desc, insert, select, update

from intel.db.models.sources import (
    IndustrySource,
    OwnerFeed,
    SourceRun,
    SourceTemplate,
)
from intel.db.models.workspace import Industry, IndustryRevision
from intel.repositories.base import ScopedRepository

# --------------------------------------------------------------------------
# storage-facing records
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SourceTemplateRecord:
    """Catalog entry (G scope) with no user subscription state."""

    id: str
    name: str
    homepage: str
    canonical_seed: str
    kind: str
    tags: list[str] = field(default_factory=list)
    access_notes: str = ""


@dataclass(slots=True)
class FeedRecord:
    id: UUID = field(default_factory=uuid4)
    template_id: str | None = None
    seed_url: str = ""
    adapter_type: str = ""
    credential_ref: str | None = None
    access_scope_key: str = "public"
    parser_version_id: UUID | None = None
    interval_seconds: int = 21600
    status: str = "active"
    next_poll_at: datetime | None = None
    user_enabled: bool = True
    row_version: int = 1


@dataclass(slots=True)
class SubscriptionRecord:
    id: UUID = field(default_factory=uuid4)
    industry_id: UUID | None = None
    feed_id: UUID | None = None
    status: str = "active"
    backfill_from: datetime | None = None
    subscribed_at: datetime | None = None
    row_version: int = 1


@dataclass(slots=True)
class SourceRunRecord:
    id: UUID
    feed_id: UUID
    job_id: UUID
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    discovered_count: int
    fetched_count: int
    unresolved_count: int
    coverage_end: datetime | None
    cursor_before: str | None
    cursor_after: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class IndustryContext:
    """What the subscription path needs from the parent industry."""

    status: str
    backfill_days: int


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


@runtime_checkable
class SourcesRepository(Protocol):
    """Everything the sources service needs from storage."""

    # global catalog
    async def list_templates(self) -> list[SourceTemplateRecord]: ...
    async def get_template(self, template_id: str) -> SourceTemplateRecord | None: ...
    async def latest_published_parser_version(self, parser_key: str) -> UUID | None: ...

    # feeds (O scope)
    async def insert_feed(self, feed: FeedRecord) -> None: ...
    async def get_feed(self, feed_id: UUID) -> FeedRecord | None: ...
    async def list_feeds(self) -> list[FeedRecord]: ...
    async def feed_seed_taken(self, seed_url: str, access_scope_key: str) -> bool: ...
    async def update_feed(
        self,
        feed_id: UUID,
        expected_version: int,
        *,
        interval_seconds: int | None = None,
        user_enabled: bool | None = None,
        parser_version_id: UUID | None = None,
        status: str | None = None,
    ) -> FeedRecord | None: ...

    # subscriptions (I scope)
    async def get_industry_context(
        self, industry_id: UUID
    ) -> IndustryContext | None: ...
    async def insert_subscription(self, sub: SubscriptionRecord) -> None: ...
    async def get_subscription(
        self, industry_id: UUID, subscription_id: UUID
    ) -> SubscriptionRecord | None: ...
    async def list_subscriptions(
        self, industry_id: UUID
    ) -> list[SubscriptionRecord]: ...
    async def subscription_feed_taken(
        self, industry_id: UUID, feed_id: UUID
    ) -> bool: ...
    async def update_subscription(
        self,
        industry_id: UUID,
        subscription_id: UUID,
        expected_version: int,
        *,
        status: str,
    ) -> SubscriptionRecord | None: ...

    # runs (O scope)
    async def list_runs(self, feed_id: UUID) -> list[SourceRunRecord]: ...


# --------------------------------------------------------------------------
# SQLAlchemy adapter
# --------------------------------------------------------------------------


def _template_record(row: SourceTemplate) -> SourceTemplateRecord:
    return SourceTemplateRecord(
        id=row.id,
        name=row.name,
        homepage=row.homepage,
        canonical_seed=row.canonical_seed,
        kind=row.kind,
        tags=list(row.tags),
        access_notes=row.access_notes,
    )


def _feed_record(row: OwnerFeed) -> FeedRecord:
    return FeedRecord(
        id=row.id,
        template_id=row.template_id,
        seed_url=row.seed_url,
        adapter_type=row.adapter_type,
        credential_ref=row.credential_ref,
        access_scope_key=row.access_scope_key,
        parser_version_id=row.parser_version_id,
        interval_seconds=row.interval_seconds,
        status=row.status,
        next_poll_at=row.next_poll_at,
        user_enabled=row.user_enabled,
        row_version=row.row_version,
    )


def _subscription_record(row: IndustrySource) -> SubscriptionRecord:
    return SubscriptionRecord(
        id=row.id,
        industry_id=row.industry_id,
        feed_id=row.feed_id,
        status=row.status,
        backfill_from=row.backfill_from,
        subscribed_at=row.subscribed_at,
        row_version=row.row_version,
    )


def _run_record(row: SourceRun) -> SourceRunRecord:
    return SourceRunRecord(
        id=row.id,
        feed_id=row.feed_id,
        job_id=row.job_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=row.outcome,
        discovered_count=row.discovered_count,
        fetched_count=row.fetched_count,
        unresolved_count=row.unresolved_count,
        coverage_end=row.coverage_end,
        cursor_before=row.cursor_before,
        cursor_after=row.cursor_after,
        error_code=row.error_code,
    )


class SqlAlchemySourcesRepository(ScopedRepository):
    """SourcesRepository on one connection, bound to one IndustryScope.

    Feed and run operations run owner-scoped even when the repository was
    constructed with an industry (they are O tables); subscription
    operations require the industry scope.
    """

    # -- global catalog (G scope, no RLS) --------------------------------------

    async def list_templates(self) -> list[SourceTemplateRecord]:
        stmt = (
            select(SourceTemplate)
            .where(SourceTemplate.enabled)
            .order_by(SourceTemplate.id)
        )
        rows = (await self._conn.execute(stmt)).scalars().all()
        return [_template_record(r) for r in rows]

    async def get_template(self, template_id: str) -> SourceTemplateRecord | None:
        stmt = select(SourceTemplate).where(SourceTemplate.id == template_id)
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _template_record(row)

    async def latest_published_parser_version(
        self, parser_key: str
    ) -> UUID | None:
        from intel.db.models.sources import ParserVersion

        stmt = (
            select(ParserVersion.id)
            .where(
                ParserVersion.parser_key == parser_key,
                ParserVersion.status == "published",
            )
            .order_by(desc(ParserVersion.version))
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).one_or_none()
        return None if row is None else row.id

    # -- feeds (O scope) ------------------------------------------------------

    async def insert_feed(self, feed: FeedRecord) -> None:
        await self._bind()
        assert feed.parser_version_id is not None
        await self._conn.execute(
            insert(OwnerFeed).values(
                id=feed.id,
                owner_id=self.owner_id,
                template_id=feed.template_id,
                seed_url=feed.seed_url,
                adapter_type=feed.adapter_type,
                credential_ref=feed.credential_ref,
                access_scope_key=feed.access_scope_key,
                parser_version_id=feed.parser_version_id,
                interval_seconds=feed.interval_seconds,
                status=feed.status,
                next_poll_at=feed.next_poll_at,
                user_enabled=feed.user_enabled,
            )
        )

    async def get_feed(self, feed_id: UUID) -> FeedRecord | None:
        await self._bind()
        stmt = select(OwnerFeed).where(
            OwnerFeed.owner_id == self.owner_id, OwnerFeed.id == feed_id
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _feed_record(row)

    async def list_feeds(self) -> list[FeedRecord]:
        await self._bind()
        stmt = (
            select(OwnerFeed)
            .where(OwnerFeed.owner_id == self.owner_id)
            .order_by(OwnerFeed.created_at, OwnerFeed.id)
        )
        rows = (await self._conn.execute(stmt)).scalars().all()
        return [_feed_record(r) for r in rows]

    async def feed_seed_taken(
        self, seed_url: str, access_scope_key: str
    ) -> bool:
        await self._bind()
        stmt = (
            select(OwnerFeed.id)
            .where(
                OwnerFeed.owner_id == self.owner_id,
                OwnerFeed.seed_url == seed_url,
                OwnerFeed.access_scope_key == access_scope_key,
            )
            .limit(1)
        )
        return (await self._conn.execute(stmt)).one_or_none() is not None

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
        await self._bind()
        values: dict = {"row_version": OwnerFeed.row_version + 1}
        if interval_seconds is not None:
            values["interval_seconds"] = interval_seconds
        if user_enabled is not None:
            values["user_enabled"] = user_enabled
        if parser_version_id is not None:
            values["parser_version_id"] = parser_version_id
        if status is not None:
            values["status"] = status
        stmt = (
            update(OwnerFeed)
            .where(
                OwnerFeed.owner_id == self.owner_id,
                OwnerFeed.id == feed_id,
                OwnerFeed.row_version == expected_version,
            )
            .values(**values)
        )
        await self._conn.execute(stmt)
        updated = await self._fetch_feed(feed_id)
        if updated is None or updated.row_version != expected_version + 1:
            return None
        return updated

    async def _fetch_feed(self, feed_id: UUID) -> FeedRecord | None:
        stmt = select(OwnerFeed).where(
            OwnerFeed.owner_id == self.owner_id, OwnerFeed.id == feed_id
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _feed_record(row)

    # -- subscriptions (I scope) ----------------------------------------------

    async def get_industry_context(
        self, industry_id: UUID
    ) -> IndustryContext | None:
        await self._bind()
        stmt = (
            select(Industry.status, IndustryRevision.settings)
            .join(
                IndustryRevision,
                Industry.current_revision_id == IndustryRevision.id,
            )
            .where(
                Industry.owner_id == self.owner_id,
                Industry.id == industry_id,
                Industry.deleted_at.is_(None),
            )
        )
        row = (await self._conn.execute(stmt)).one_or_none()
        if row is None:
            return None
        settings = row.settings or {}
        return IndustryContext(
            status=row.status,
            backfill_days=int(settings.get("backfill_days", 90)),
        )

    async def insert_subscription(self, sub: SubscriptionRecord) -> None:
        industry_id = await self._bind_industry()
        sub.industry_id = industry_id
        await self._conn.execute(
            insert(IndustrySource).values(
                id=sub.id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                feed_id=sub.feed_id,
                status=sub.status,
                backfill_from=sub.backfill_from,
                subscribed_at=sub.subscribed_at,
            )
        )

    async def get_subscription(
        self, industry_id: UUID, subscription_id: UUID
    ) -> SubscriptionRecord | None:
        industry_id = await self._bind_industry()
        stmt = select(IndustrySource).where(
            IndustrySource.owner_id == self.owner_id,
            IndustrySource.industry_id == industry_id,
            IndustrySource.id == subscription_id,
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _subscription_record(row)

    async def list_subscriptions(
        self, industry_id: UUID
    ) -> list[SubscriptionRecord]:
        industry_id = await self._bind_industry()
        stmt = (
            select(IndustrySource)
            .where(
                IndustrySource.owner_id == self.owner_id,
                IndustrySource.industry_id == industry_id,
            )
            .order_by(IndustrySource.subscribed_at, IndustrySource.id)
        )
        rows = (await self._conn.execute(stmt)).scalars().all()
        return [_subscription_record(r) for r in rows]

    async def subscription_feed_taken(
        self, industry_id: UUID, feed_id: UUID
    ) -> bool:
        industry_id = await self._bind_industry()
        stmt = (
            select(IndustrySource.id)
            .where(
                IndustrySource.owner_id == self.owner_id,
                IndustrySource.industry_id == industry_id,
                IndustrySource.feed_id == feed_id,
            )
            .limit(1)
        )
        return (await self._conn.execute(stmt)).one_or_none() is not None

    async def update_subscription(
        self,
        industry_id: UUID,
        subscription_id: UUID,
        expected_version: int,
        *,
        status: str,
    ) -> SubscriptionRecord | None:
        industry_id = await self._bind_industry()
        stmt = (
            update(IndustrySource)
            .where(
                IndustrySource.owner_id == self.owner_id,
                IndustrySource.industry_id == industry_id,
                IndustrySource.id == subscription_id,
                IndustrySource.row_version == expected_version,
            )
            .values(
                status=status, row_version=IndustrySource.row_version + 1
            )
        )
        await self._conn.execute(stmt)
        updated = await self._fetch_subscription(industry_id, subscription_id)
        if updated is None or updated.row_version != expected_version + 1:
            return None
        return updated

    async def _fetch_subscription(
        self, industry_id: UUID, subscription_id: UUID
    ) -> SubscriptionRecord | None:
        stmt = select(IndustrySource).where(
            IndustrySource.owner_id == self.owner_id,
            IndustrySource.industry_id == industry_id,
            IndustrySource.id == subscription_id,
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _subscription_record(row)

    # -- runs (O scope) ---------------------------------------------------------

    async def list_runs(self, feed_id: UUID) -> list[SourceRunRecord]:
        await self._bind()
        stmt = (
            select(SourceRun)
            .where(SourceRun.owner_id == self.owner_id, SourceRun.feed_id == feed_id)
            .order_by(SourceRun.started_at.desc(), SourceRun.id)
        )
        rows = (await self._conn.execute(stmt)).scalars().all()
        return [_run_record(r) for r in rows]
