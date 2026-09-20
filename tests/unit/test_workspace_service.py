"""Unit tests: WorkspaceService against a dict-backed fake repo (spec 01 §5, 08 §2).

Spec references:
- docs/01-product.md §5  industry draft/active/paused/archived、topic
  active/paused/archived、name owner 内活跃唯一、软删除
- docs/08-api.md §1  expected_version 乐观并发、修改生成 revision
- docs/03-data-model.md §2  industry_revisions / topic_revisions 版本表

No real database here: the service runs against FakeWorkspaceRepo implementing
the WorkspaceRepository protocol. The fake enqueuer (acquisition.InMemoryEnqueuer)
stands in for the Task 6 job queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from intel.contracts import (
    BusinessProfile,
    IndustryCreate,
    IndustryPatch,
    IndustrySettings,
    LifecycleCommand,
    TopicCreate,
    TopicPatch,
    WindowRequest,
)
from intel.repositories.base import IndustryScope
from intel.repositories.workspace import (
    IndustryRecord,
    IndustryRevisionRecord,
    TopicRecord,
    TopicRevisionRecord,
    WorkspaceRepository,
)
from intel.services.acquisition import InMemoryEnqueuer
from intel.services.errors import (
    AlreadyExists,
    InvalidStateTransition,
    NotFound,
    VersionConflict,
)
from intel.services.identity import Principal
from intel.services.workspace import WorkspaceService

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OWNER = uuid4()


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class FakeWorkspaceRepo:
    """Dict-backed WorkspaceRepository — the observable storage state."""

    def __init__(self, owner_id: UUID = OWNER) -> None:
        self.owner_id = owner_id
        self.industries: dict[UUID, IndustryRecord] = {}
        self.industry_revisions: dict[UUID, IndustryRevisionRecord] = {}
        self.topics: dict[tuple[UUID, UUID], TopicRecord] = {}
        self.topic_revisions: dict[UUID, TopicRevisionRecord] = {}

    # -- industries ----------------------------------------------------------

    async def industry_owned(self, industry_id: UUID) -> bool:
        record = self.industries.get(industry_id)
        return record is not None and self._mine(record)

    def _mine(self, record: IndustryRecord) -> bool:
        # The scope filter the SQL repo gets from RLS + WHERE clauses.
        return True  # this fake only ever holds one owner's rows

    async def get_industry(self, industry_id: UUID) -> IndustryRecord | None:
        record = self.industries.get(industry_id)
        if record is None or record.deleted_at is not None:
            return None
        return record

    async def list_industries(self) -> list[IndustryRecord]:
        return [
            r for r in self.industries.values() if r.deleted_at is None
        ]

    async def industry_name_taken(
        self, name: str, *, exclude_id: UUID | None = None
    ) -> bool:
        return any(
            r.name == name
            and r.id != exclude_id
            and r.status != "archived"
            and r.deleted_at is None
            for r in self.industries.values()
        )

    async def insert_industry(
        self, industry: IndustryRecord, revision: IndustryRevisionRecord
    ) -> IndustryRecord:
        assert industry.id not in self.industries
        assert revision.industry_id == industry.id
        self.industries[industry.id] = industry
        self.industry_revisions[revision.id] = revision
        industry.current_revision_id = revision.id
        return industry

    async def update_industry(
        self,
        industry_id: UUID,
        expected_version: int,
        *,
        name: str | None = None,
        status: str | None = None,
    ) -> IndustryRecord | None:
        record = self.industries.get(industry_id)
        if (
            record is None
            or record.deleted_at is not None
            or record.row_version != expected_version
        ):
            return None
        if name is not None:
            record.name = name
        if status is not None:
            record.status = status
        record.row_version += 1
        return record

    async def latest_industry_revision(
        self, industry_id: UUID
    ) -> IndustryRevisionRecord | None:
        revisions = [
            r
            for r in self.industry_revisions.values()
            if r.industry_id == industry_id
        ]
        return max(revisions, key=lambda r: r.version) if revisions else None

    async def append_industry_revision(
        self,
        revision: IndustryRevisionRecord,
        industry_id: UUID,
        expected_version: int,
    ) -> IndustryRecord | None:
        record = self.industries.get(industry_id)
        if (
            record is None
            or record.deleted_at is not None
            or record.row_version != expected_version
        ):
            return None
        self.industry_revisions[revision.id] = revision
        record.current_revision_id = revision.id
        return record  # no bump: update_industry already versioned this PATCH

    # -- topics ---------------------------------------------------------------

    async def get_topic(self, industry_id: UUID, topic_id: UUID) -> TopicRecord | None:
        return self.topics.get((industry_id, topic_id))

    async def list_topics(self, industry_id: UUID) -> list[TopicRecord]:
        return [
            t for (ind, _), t in self.topics.items() if ind == industry_id
        ]

    async def topic_name_taken(
        self, industry_id: UUID, name: str, *, exclude_id: UUID | None = None
    ) -> bool:
        return any(
            t.name == name and t.id != exclude_id and t.status != "archived"
            for (ind, _), t in self.topics.items()
            if ind == industry_id
        )

    async def insert_topic(
        self, topic: TopicRecord, revision: TopicRevisionRecord
    ) -> TopicRecord:
        self.topics[(topic.industry_id, topic.id)] = topic
        self.topic_revisions[revision.id] = revision
        topic.current_revision_id = revision.id
        return topic

    async def update_topic(
        self,
        industry_id: UUID,
        topic_id: UUID,
        expected_version: int,
        *,
        name: str | None = None,
        status: str | None = None,
        priority: int | None = None,
    ) -> TopicRecord | None:
        record = self.topics.get((industry_id, topic_id))
        if record is None or record.row_version != expected_version:
            return None
        if name is not None:
            record.name = name
        if status is not None:
            record.status = status
        if priority is not None:
            record.priority = priority
        record.row_version += 1
        return record

    async def latest_topic_revision(
        self, industry_id: UUID, topic_id: UUID
    ) -> TopicRevisionRecord | None:
        revisions = [
            r
            for r in self.topic_revisions.values()
            if r.industry_id == industry_id and r.topic_id == topic_id
        ]
        return max(revisions, key=lambda r: r.version) if revisions else None

    async def append_topic_revision(
        self,
        revision: TopicRevisionRecord,
        industry_id: UUID,
        topic_id: UUID,
        expected_version: int,
    ) -> TopicRecord | None:
        record = self.topics.get((industry_id, topic_id))
        if record is None or record.row_version != expected_version:
            return None
        self.topic_revisions[revision.id] = revision
        record.current_revision_id = revision.id
        return record  # no bump: update_topic already versioned this PATCH


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def repo() -> FakeWorkspaceRepo:
    return FakeWorkspaceRepo()


@pytest.fixture()
def enqueuer() -> InMemoryEnqueuer:
    return InMemoryEnqueuer()


@pytest.fixture()
def service(
    repo: FakeWorkspaceRepo, enqueuer: InMemoryEnqueuer, clock: FakeClock
) -> WorkspaceService:
    return WorkspaceService(repo, enqueuer, clock=clock)


CREATE = IndustryCreate(
    name="Etching",
    description="等离子刻蚀设备与工艺",
    included_scope=["plasma etching"],
    settings=IndustrySettings(backfill_days=30, daily_report=False),
)


async def activated(service: WorkspaceService, view=None):
    """Create + activate an industry, returning its view."""
    view = view or await service.create_industry(CREATE)
    return await service.lifecycle(
        view.id,
        LifecycleCommand(action="activate", expected_version=view.row_version),
    )


# --------------------------------------------------------------------------
# industries: create / get / list
# --------------------------------------------------------------------------


class TestIndustryCrud:
    async def test_create_starts_as_draft_with_revision_one(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        view = await service.create_industry(CREATE)
        assert view.status == "draft"
        assert view.row_version == 1
        assert view.name == "Etching"
        assert view.settings.backfill_days == 30
        assert view.settings.pool_scope == "all_public"
        revision = repo.industry_revisions[view.revision_id]
        assert revision.version == 1
        assert repo.industries[view.id].current_revision_id == view.revision_id

    async def test_create_defaults_pool_scope_and_backfill(
        self, service: WorkspaceService
    ) -> None:
        view = await service.create_industry(
            IndustryCreate(name="Deposition", description="沉积")
        )
        assert view.settings == IndustrySettings()
        assert view.included_scope == []
        assert view.profile is None

    async def test_duplicate_active_name_rejected(
        self, service: WorkspaceService
    ) -> None:
        await service.create_industry(CREATE)
        with pytest.raises(AlreadyExists):
            await service.create_industry(
                IndustryCreate(name="Etching", description="重名")
            )

    async def test_archived_name_becomes_reusable(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        view = await service.lifecycle(
            view.id,
            LifecycleCommand(
                action="archive", expected_version=view.row_version
            ),
        )
        assert view.status == "archived"
        reborn = await service.create_industry(
            IndustryCreate(name="Etching", description="重建同名行业")
        )
        assert reborn.status == "draft"

    async def test_get_missing_raises_not_found(
        self, service: WorkspaceService
    ) -> None:
        with pytest.raises(NotFound):
            await service.get_industry(uuid4())

    async def test_list_returns_owner_industries_with_revisions(
        self, service: WorkspaceService
    ) -> None:
        await service.create_industry(CREATE)
        await service.create_industry(
            IndustryCreate(name="Litho", description="光刻")
        )
        views = await service.list_industries()
        assert [v.name for v in views] == ["Etching", "Litho"]
        assert all(v.revision_id for v in views)


# --------------------------------------------------------------------------
# industries: PATCH + revisions + optimistic version
# --------------------------------------------------------------------------


class TestIndustryPatch:
    async def test_patch_revision_fields_creates_new_revision(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        view = await service.create_industry(CREATE)
        patched = await service.patch_industry(
            view.id,
            IndustryPatch(
                expected_version=view.row_version,
                description="更新后的描述",
                settings=IndustrySettings(backfill_days=180),
            ),
        )
        assert patched.row_version == view.row_version + 1
        assert patched.revision_id != view.revision_id
        new_revision = repo.industry_revisions[patched.revision_id]
        assert new_revision.version == 2
        assert new_revision.description == "更新后的描述"
        assert new_revision.settings["backfill_days"] == 180

    async def test_patch_name_only_does_not_create_revision(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        view = await service.create_industry(CREATE)
        patched = await service.patch_industry(
            view.id,
            IndustryPatch(expected_version=view.row_version, name="Etch v2"),
        )
        assert patched.name == "Etch v2"
        assert patched.revision_id == view.revision_id
        assert len(repo.industry_revisions) == 1

    async def test_patch_noop_fields_keep_revision(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        view = await service.create_industry(CREATE)
        patched = await service.patch_industry(
            view.id,
            IndustryPatch(expected_version=view.row_version),
        )
        assert patched.revision_id == view.revision_id
        assert len(repo.industry_revisions) == 1

    async def test_patch_explicit_null_clears_profile(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        view = await service.create_industry(
            IndustryCreate(
                name="P",
                description="d",
                profile=BusinessProfile(products=["a"]),
            )
        )
        assert view.profile is not None
        # Passing profile=None explicitly (not omitting it) clears it; the
        # service distinguishes null from omitted via model_fields_set.
        patched = await service.patch_industry(
            view.id,
            IndustryPatch(expected_version=view.row_version, profile=None),
        )
        assert patched.profile is None

    async def test_patch_version_conflict_carries_current(
        self, service: WorkspaceService
    ) -> None:
        view = await service.create_industry(CREATE)
        with pytest.raises(VersionConflict) as exc_info:
            await service.patch_industry(
                view.id,
                IndustryPatch(
                    expected_version=view.row_version + 5,
                    description="stale write",
                ),
            )
        assert exc_info.value.details["current_version"] == view.row_version

    async def test_patch_missing_raises_not_found(
        self, service: WorkspaceService
    ) -> None:
        with pytest.raises(NotFound):
            await service.patch_industry(
                uuid4(), IndustryPatch(expected_version=1, description="x")
            )


# --------------------------------------------------------------------------
# industries: lifecycle 状态机 (01 §5)
# --------------------------------------------------------------------------


class TestIndustryLifecycle:
    async def test_activate_from_draft(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        assert view.status == "active"

    async def test_activate_from_active_is_illegal(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        with pytest.raises(InvalidStateTransition):
            await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action="activate", expected_version=view.row_version
                ),
            )

    async def test_pause_requires_active(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        paused = await service.lifecycle(
            view.id,
            LifecycleCommand(action="pause", expected_version=view.row_version),
        )
        assert paused.status == "paused"
        with pytest.raises(InvalidStateTransition):
            await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action="pause", expected_version=paused.row_version
                ),
            )

    async def test_activate_from_paused(
        self, service: WorkspaceService
    ) -> None:
        # 07 §5: 恢复先改 paused，再选择 active — activate must be legal
        # from paused, otherwise paused is a dead end.
        view = await activated(service)
        paused = await service.lifecycle(
            view.id,
            LifecycleCommand(action="pause", expected_version=view.row_version),
        )
        reactivated = await service.lifecycle(
            view.id,
            LifecycleCommand(
                action="activate", expected_version=paused.row_version
            ),
        )
        assert reactivated.status == "active"

    async def test_full_lifecycle_path_round_trip(
        self, service: WorkspaceService
    ) -> None:
        # draft→active→paused→active→archived→restore(paused)→active.
        view = await service.create_industry(CREATE)

        async def step(current, action):
            return await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action=action, expected_version=current.row_version
                ),
            )

        view = await step(view, "activate")
        assert view.status == "active"
        view = await step(view, "pause")
        assert view.status == "paused"
        view = await step(view, "activate")
        assert view.status == "active"
        view = await step(view, "archive")
        assert view.status == "archived"
        view = await step(view, "restore")
        assert view.status == "paused"
        view = await step(view, "activate")
        assert view.status == "active"
        assert view.row_version == 7  # one bump per transition

    @pytest.mark.parametrize("via_pause", [True, False])
    async def test_archive_from_active_or_paused(
        self, service: WorkspaceService, via_pause: bool
    ) -> None:
        view = await activated(service)
        if via_pause:
            view = await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action="pause", expected_version=view.row_version
                ),
            )
        archived = await service.lifecycle(
            view.id,
            LifecycleCommand(action="archive", expected_version=view.row_version),
        )
        assert archived.status == "archived"

    async def test_archive_from_draft_is_illegal(
        self, service: WorkspaceService
    ) -> None:
        view = await service.create_industry(CREATE)
        with pytest.raises(InvalidStateTransition):
            await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action="archive", expected_version=view.row_version
                ),
            )

    async def test_restore_goes_to_paused(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        view = await service.lifecycle(
            view.id,
            LifecycleCommand(action="archive", expected_version=view.row_version),
        )
        restored = await service.lifecycle(
            view.id,
            LifecycleCommand(
                action="restore", expected_version=view.row_version
            ),
        )
        assert restored.status == "paused"

    async def test_restore_from_draft_is_illegal(
        self, service: WorkspaceService
    ) -> None:
        view = await service.create_industry(CREATE)
        with pytest.raises(InvalidStateTransition):
            await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action="restore", expected_version=view.row_version
                ),
            )

    async def test_restore_blocked_when_name_was_retaken(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        view = await service.lifecycle(
            view.id,
            LifecycleCommand(action="archive", expected_version=view.row_version),
        )
        await service.create_industry(
            IndustryCreate(name=CREATE.name, description="抢占同名")
        )
        with pytest.raises(AlreadyExists):
            await service.lifecycle(
                view.id,
                LifecycleCommand(
                    action="restore", expected_version=view.row_version
                ),
            )

    async def test_lifecycle_version_conflict(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        with pytest.raises(VersionConflict):
            await service.lifecycle(
                view.id,
                LifecycleCommand(action="pause", expected_version=999),
            )

    async def test_lifecycle_missing_raises_not_found(
        self, service: WorkspaceService
    ) -> None:
        with pytest.raises(NotFound):
            await service.lifecycle(
                uuid4(), LifecycleCommand(action="activate", expected_version=1)
            )


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------


class TestTopics:
    async def test_create_topic_active_with_revision(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        industry = await service.create_industry(CREATE)
        topic = await service.create_topic(
            industry.id,
            TopicCreate(name="腔体", description="腔体材料", priority=3),
        )
        assert topic.status == "active"
        assert topic.industry_id == industry.id
        assert topic.row_version == 1
        assert repo.topic_revisions[topic.revision_id].version == 1

    async def test_create_topic_in_archived_industry_rejected(
        self, service: WorkspaceService
    ) -> None:
        view = await activated(service)
        view = await service.lifecycle(
            view.id,
            LifecycleCommand(action="archive", expected_version=view.row_version),
        )
        with pytest.raises(InvalidStateTransition):
            await service.create_topic(
                view.id, TopicCreate(name="late", description="太晚")
            )

    async def test_topic_name_unique_per_industry_only(
        self, service: WorkspaceService
    ) -> None:
        first = await service.create_industry(CREATE)
        second = await service.create_industry(
            IndustryCreate(name="Other", description="另一行业")
        )
        await service.create_topic(
            first.id, TopicCreate(name="监控", description="d")
        )
        await service.create_topic(
            second.id, TopicCreate(name="监控", description="同名不同行业")
        )
        with pytest.raises(AlreadyExists):
            await service.create_topic(
                first.id, TopicCreate(name="监控", description="同行业重名")
            )

    async def test_topic_transitions(
        self,
        service: WorkspaceService,
    ) -> None:
        industry = await service.create_industry(CREATE)
        topic = await service.create_topic(
            industry.id, TopicCreate(name="t", description="d")
        )

        async def set_status(topic, status):
            return await service.patch_topic(
                industry.id,
                topic.id,
                TopicPatch(expected_version=topic.row_version, status=status),
            )

        topic = await set_status(topic, "paused")
        assert topic.status == "paused"
        topic = await set_status(topic, "active")
        assert topic.status == "active"
        topic = await set_status(topic, "archived")
        assert topic.status == "archived"
        with pytest.raises(InvalidStateTransition):
            await set_status(topic, "active")

    async def test_patch_topic_creates_revision(
        self, service: WorkspaceService, repo: FakeWorkspaceRepo
    ) -> None:
        industry = await service.create_industry(CREATE)
        topic = await service.create_topic(
            industry.id, TopicCreate(name="t", description="d1")
        )
        patched = await service.patch_topic(
            industry.id,
            topic.id,
            TopicPatch(
                expected_version=topic.row_version,
                description="d2",
                positive_examples=["例"],
                priority=7,
            ),
        )
        assert patched.description == "d2"
        assert patched.priority == 7
        assert patched.revision_id != topic.revision_id
        assert repo.topic_revisions[patched.revision_id].version == 2

    async def test_patch_topic_version_conflict(
        self, service: WorkspaceService
    ) -> None:
        industry = await service.create_industry(CREATE)
        topic = await service.create_topic(
            industry.id, TopicCreate(name="t", description="d")
        )
        with pytest.raises(VersionConflict):
            await service.patch_topic(
                industry.id,
                topic.id,
                TopicPatch(expected_version=42, description="stale"),
            )

    async def test_get_missing_topic_raises_not_found(
        self, service: WorkspaceService
    ) -> None:
        industry = await service.create_industry(CREATE)
        with pytest.raises(NotFound):
            await service.get_topic(industry.id, uuid4())


# --------------------------------------------------------------------------
# replay (job dispatch via the enqueuer port; real queue is Task 6)
# --------------------------------------------------------------------------


class TestReplay:
    async def test_replay_enqueues_topic_replay_job(
        self,
        service: WorkspaceService,
        enqueuer: InMemoryEnqueuer,
    ) -> None:
        industry = await service.create_industry(CREATE)
        topic = await service.create_topic(
            industry.id, TopicCreate(name="t", description="d")
        )
        scope = IndustryScope(owner_id=OWNER, industry_id=industry.id)
        accepted = await service.replay_topic(
            industry.id,
            topic.id,
            WindowRequest(),
            scope=scope,
            idempotency_key="replay-1",
        )
        assert accepted.state == "queued"
        assert accepted.events_url.endswith(f"/jobs/{accepted.job_id}/events")
        (record,) = enqueuer.records
        assert record.kind == "topic_replay"
        assert record.scope == scope
        assert record.payload["topic_id"] == str(topic.id)
        assert record.payload["topic_revision_id"] == str(topic.revision_id)
        assert record.idempotency_key == "replay-1"

    async def test_replay_missing_topic_raises_not_found(
        self, service: WorkspaceService
    ) -> None:
        industry = await service.create_industry(CREATE)
        with pytest.raises(NotFound):
            await service.replay_topic(
                industry.id,
                uuid4(),
                WindowRequest(),
                idempotency_key="replay-2",
                scope=IndustryScope(OWNER, industry.id),
            )


# --------------------------------------------------------------------------
# protocol wiring
# --------------------------------------------------------------------------


def test_repositories_satisfy_the_protocol() -> None:
    assert isinstance(FakeWorkspaceRepo(), WorkspaceRepository)


def test_principal_importable_for_scope_wiring() -> None:
    # deps.get_principal hands this to get_scope; smoke the import surface.
    assert Principal(user_id=OWNER).session_id is None
