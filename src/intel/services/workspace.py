"""Workspace service: industries and topics (spec 01 §5, 03 §2, 08 §1/§2).

Semantics enforced here, above any storage:

- Industry 状态机 draft/active/paused/archived with an explicit allowed-map
  (activate←draft|paused, pause←active, archive←active|paused,
  restore←archived→paused); illegal transitions raise
  ``InvalidStateTransition``.
- Topic 状态机 active/paused/archived; archived is terminal (no topic
  restore in v1).
- PATCH merges revision-bearing fields and appends a new immutable revision
  only when that content actually changed; ``null`` vs omitted is
  distinguished via ``model_fields_set`` (08 §1).
- Every mutation is optimistic: the repository filters on
  ``row_version == expected_version``; a miss raises ``VersionConflict``
  carrying the current version.
- Names are unique among the owner's active industries (and per-industry
  active topics); archived rows free the name.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from intel.contracts import (
    BusinessProfile,
    IndustryCreate,
    IndustryPatch,
    IndustrySettings,
    IndustryView,
    JobAccepted,
    LifecycleCommand,
    TopicCreate,
    TopicPatch,
    TopicView,
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
from intel.services.acquisition import JobEnqueuer
from intel.services.errors import (
    AlreadyExists,
    InvalidStateTransition,
    NotFound,
    VersionConflict,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: action -> (statuses it may start from, status it lands in) (01 §5,
#: 07 §5: 恢复先改 paused，再选择 active — so activate is legal from paused).
INDUSTRY_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "activate": (frozenset({"draft", "paused"}), "active"),
    "pause": (frozenset({"active"}), "paused"),
    "archive": (frozenset({"active", "paused"}), "archived"),
    "restore": (frozenset({"archived"}), "paused"),
}

#: current topic status -> statuses PATCH may move it to.
TOPIC_ALLOWED_TARGETS: dict[str, frozenset[str]] = {
    "active": frozenset({"active", "paused", "archived"}),
    "paused": frozenset({"active", "paused", "archived"}),
    "archived": frozenset(),  # terminal: v1 defines no topic restore
}


class WorkspaceService:
    """Industry/topic lifecycle and revision management, storage-agnostic."""

    def __init__(
        self,
        repo: WorkspaceRepository,
        enqueuer: JobEnqueuer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._enqueuer = enqueuer
        self._clock = clock if clock is not None else _utcnow

    # -- industries ----------------------------------------------------------

    async def create_industry(self, cmd: IndustryCreate) -> IndustryView:
        if await self._repo.industry_name_taken(cmd.name):
            raise AlreadyExists(
                f"an active industry named {cmd.name!r} already exists"
            )
        industry = IndustryRecord(name=cmd.name, status="draft")
        revision = IndustryRevisionRecord(
            industry_id=industry.id,
            version=1,
            description=cmd.description,
            included_scope=list(cmd.included_scope),
            excluded_scope=list(cmd.excluded_scope),
            profile=(
                cmd.profile.model_dump() if cmd.profile is not None else None
            ),
            settings=cmd.settings.model_dump(),
        )
        stored = await self._repo.insert_industry(industry, revision)
        return _industry_view(stored, revision)

    async def get_industry(self, industry_id: UUID) -> IndustryView:
        record = await self._require_industry(industry_id)
        revision = await self._require_industry_revision(record)
        return _industry_view(record, revision)

    async def list_industries(self) -> list[IndustryView]:
        views = []
        for record in await self._repo.list_industries():
            revision = await self._require_industry_revision(record)
            views.append(_industry_view(record, revision))
        return views

    async def patch_industry(
        self, industry_id: UUID, cmd: IndustryPatch
    ) -> IndustryView:
        record = await self._require_industry(industry_id)
        provided = cmd.model_fields_set

        new_name = record.name
        if "name" in provided and cmd.name is not None and cmd.name != record.name:
            await self._check_industry_name(cmd.name, industry_id)
            new_name = cmd.name

        updated = await self._repo.update_industry(
            industry_id,
            cmd.expected_version,
            name=new_name if new_name != record.name else None,
        )
        if updated is None:
            raise await self._industry_conflict(industry_id)

        current = await self._require_industry_revision(updated)
        merged = IndustryRevisionRecord(
            industry_id=industry_id,
            version=current.version + 1,
            description=(
                cmd.description if "description" in provided else current.description
            ),
            included_scope=(
                list(cmd.included_scope)
                if "included_scope" in provided and cmd.included_scope is not None
                else current.included_scope
            ),
            excluded_scope=(
                list(cmd.excluded_scope)
                if "excluded_scope" in provided and cmd.excluded_scope is not None
                else current.excluded_scope
            ),
            profile=(
                cmd.profile.model_dump()
                if "profile" in provided and cmd.profile is not None
                else (None if "profile" in provided else current.profile)
            ),
            settings=(
                cmd.settings.model_dump()
                if "settings" in provided and cmd.settings is not None
                else current.settings
            ),
            schema_version=current.schema_version,
        )
        if _revision_changed(current, merged):
            appended = await self._repo.append_industry_revision(
                merged, industry_id, updated.row_version
            )
            if appended is None:
                raise await self._industry_conflict(industry_id)
            return _industry_view(appended, merged)
        return _industry_view(updated, current)

    async def lifecycle(
        self, industry_id: UUID, cmd: LifecycleCommand
    ) -> IndustryView:
        record = await self._require_industry(industry_id)
        allowed_from, target = INDUSTRY_TRANSITIONS[cmd.action]
        if record.status not in allowed_from:
            raise InvalidStateTransition(
                f"cannot {cmd.action} an industry in state {record.status!r}",
                action=cmd.action,
                current=record.status,
            )
        if cmd.action == "restore":
            await self._check_industry_name(record.name, industry_id)
        updated = await self._repo.update_industry(
            industry_id, cmd.expected_version, status=target
        )
        if updated is None:
            raise await self._industry_conflict(industry_id)
        revision = await self._require_industry_revision(updated)
        return _industry_view(updated, revision)

    # -- topics ---------------------------------------------------------------

    async def create_topic(
        self, industry_id: UUID, cmd: TopicCreate
    ) -> TopicView:
        industry = await self._require_industry(industry_id)
        if industry.status == "archived":
            raise InvalidStateTransition(
                "cannot add topics to an archived industry",
                action="create_topic",
                current=industry.status,
            )
        if await self._repo.topic_name_taken(industry_id, cmd.name):
            raise AlreadyExists(
                f"an active topic named {cmd.name!r} already exists in this industry"
            )
        topic = TopicRecord(
            industry_id=industry_id, name=cmd.name, status="active",
            priority=cmd.priority,
        )
        revision = TopicRevisionRecord(
            industry_id=industry_id,
            topic_id=topic.id,
            version=1,
            description=cmd.description,
            positive_examples=list(cmd.positive_examples),
            negative_examples=list(cmd.negative_examples),
            aliases=list(cmd.aliases),
            entity_ids=list(cmd.entity_ids),
            questions=list(cmd.questions),
            analysis_template=cmd.analysis_template,
        )
        stored = await self._repo.insert_topic(topic, revision)
        return _topic_view(stored, revision)

    async def get_topic(self, industry_id: UUID, topic_id: UUID) -> TopicView:
        topic = await self._require_topic(industry_id, topic_id)
        revision = await self._require_topic_revision(topic)
        return _topic_view(topic, revision)

    async def list_topics(self, industry_id: UUID) -> list[TopicView]:
        await self._require_industry(industry_id)
        views = []
        for topic in await self._repo.list_topics(industry_id):
            revision = await self._require_topic_revision(topic)
            views.append(_topic_view(topic, revision))
        return views

    async def patch_topic(
        self, industry_id: UUID, topic_id: UUID, cmd: TopicPatch
    ) -> TopicView:
        topic = await self._require_topic(industry_id, topic_id)
        provided = cmd.model_fields_set

        new_status = topic.status
        if "status" in provided and cmd.status is not None and cmd.status != topic.status:
            if cmd.status not in TOPIC_ALLOWED_TARGETS[topic.status]:
                raise InvalidStateTransition(
                    f"cannot move a {topic.status!r} topic to {cmd.status!r}",
                    action=f"status:{cmd.status}",
                    current=topic.status,
                )
            new_status = cmd.status

        new_name = topic.name
        if "name" in provided and cmd.name is not None and cmd.name != topic.name:
            if await self._repo.topic_name_taken(
                industry_id, cmd.name, exclude_id=topic_id
            ):
                raise AlreadyExists(
                    f"an active topic named {cmd.name!r} already exists "
                    "in this industry"
                )
            new_name = cmd.name

        new_priority = (
            cmd.priority
            if "priority" in provided and cmd.priority is not None
            else None
        )

        updated = await self._repo.update_topic(
            industry_id,
            topic_id,
            cmd.expected_version,
            name=new_name if new_name != topic.name else None,
            status=new_status if new_status != topic.status else None,
            priority=new_priority,
        )
        if updated is None:
            raise await self._topic_conflict(industry_id, topic_id)

        current = await self._require_topic_revision(updated)
        merged = TopicRevisionRecord(
            industry_id=industry_id,
            topic_id=topic_id,
            version=current.version + 1,
            description=(
                cmd.description if "description" in provided else current.description
            ),
            positive_examples=(
                list(cmd.positive_examples)
                if "positive_examples" in provided and cmd.positive_examples is not None
                else current.positive_examples
            ),
            negative_examples=(
                list(cmd.negative_examples)
                if "negative_examples" in provided and cmd.negative_examples is not None
                else current.negative_examples
            ),
            aliases=(
                list(cmd.aliases)
                if "aliases" in provided and cmd.aliases is not None
                else current.aliases
            ),
            entity_ids=(
                list(cmd.entity_ids)
                if "entity_ids" in provided and cmd.entity_ids is not None
                else current.entity_ids
            ),
            questions=(
                list(cmd.questions)
                if "questions" in provided and cmd.questions is not None
                else current.questions
            ),
            analysis_template=(
                cmd.analysis_template
                if "analysis_template" in provided and cmd.analysis_template is not None
                else current.analysis_template
            ),
        )
        if _topic_revision_changed(current, merged):
            appended = await self._repo.append_topic_revision(
                merged, industry_id, topic_id, updated.row_version
            )
            if appended is None:
                raise await self._topic_conflict(industry_id, topic_id)
            return _topic_view(appended, merged)
        return _topic_view(updated, current)

    # -- replay (local re-classification job; queue is Task 6) ----------------

    async def replay_topic(
        self,
        industry_id: UUID,
        topic_id: UUID,
        window: WindowRequest,
        *,
        scope: IndustryScope,
        idempotency_key: str,
    ) -> JobAccepted:
        """Schedule a local replay: re-classify already-fetched material for
        the topic's current revision. No fetching, no topic web search (U05)."""
        topic = await self._require_topic(industry_id, topic_id)
        revision = await self._require_topic_revision(topic)
        payload: dict = {
            "topic_id": str(topic_id),
            "topic_revision_id": str(revision.id),
            "from_time": (
                window.from_time.isoformat() if window.from_time else None
            ),
            "to_time": (
                window.to_time.isoformat() if window.to_time else None
            ),
        }
        return await self._enqueuer.enqueue(
            scope,
            kind="topic_replay",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    # -- helpers ---------------------------------------------------------------

    async def _require_industry(self, industry_id: UUID) -> IndustryRecord:
        record = await self._repo.get_industry(industry_id)
        if record is None:
            raise NotFound("industry not found")
        return record

    async def _check_industry_name(
        self, name: str, industry_id: UUID
    ) -> None:
        if await self._repo.industry_name_taken(name, exclude_id=industry_id):
            raise AlreadyExists(
                f"an active industry named {name!r} already exists"
            )

    async def _require_industry_revision(
        self, record: IndustryRecord
    ) -> IndustryRevisionRecord:
        revision = await self._repo.latest_industry_revision(record.id)
        if revision is None:
            raise NotFound("industry revision not found")
        return revision

    async def _industry_conflict(self, industry_id: UUID) -> VersionConflict:
        record = await self._repo.get_industry(industry_id)
        current = record.row_version if record is not None else 1
        return VersionConflict(current)

    async def _require_topic(
        self, industry_id: UUID, topic_id: UUID
    ) -> TopicRecord:
        topic = await self._repo.get_topic(industry_id, topic_id)
        if topic is None:
            raise NotFound("topic not found")
        return topic

    async def _require_topic_revision(
        self, topic: TopicRecord
    ) -> TopicRevisionRecord:
        revision = await self._repo.latest_topic_revision(
            topic.industry_id, topic.id
        )
        if revision is None:
            raise NotFound("topic revision not found")
        return revision

    async def _topic_conflict(
        self, industry_id: UUID, topic_id: UUID
    ) -> VersionConflict:
        topic = await self._repo.get_topic(industry_id, topic_id)
        current = topic.row_version if topic is not None else 1
        return VersionConflict(current)


# --------------------------------------------------------------------------
# view builders and change detection
# --------------------------------------------------------------------------


def _industry_view(
    record: IndustryRecord, revision: IndustryRevisionRecord
) -> IndustryView:
    return IndustryView(
        id=record.id,
        name=record.name,
        status=record.status,
        revision_id=record.current_revision_id or revision.id,
        row_version=record.row_version,
        description=revision.description,
        included_scope=list(revision.included_scope),
        excluded_scope=list(revision.excluded_scope),
        profile=(
            BusinessProfile.model_validate(revision.profile)
            if revision.profile is not None
            else None
        ),
        settings=IndustrySettings.model_validate(revision.settings),
    )


def _topic_view(topic: TopicRecord, revision: TopicRevisionRecord) -> TopicView:
    return TopicView(
        id=topic.id,
        industry_id=topic.industry_id,
        name=topic.name,
        status=topic.status,
        revision_id=topic.current_revision_id or revision.id,
        row_version=topic.row_version,
        priority=topic.priority,
        description=revision.description,
        positive_examples=list(revision.positive_examples),
        negative_examples=list(revision.negative_examples),
        aliases=list(revision.aliases),
        entity_ids=list(revision.entity_ids),
        questions=list(revision.questions),
        analysis_template=revision.analysis_template,
    )


def _revision_changed(
    current: IndustryRevisionRecord, merged: IndustryRevisionRecord
) -> bool:
    return (
        current.description,
        current.included_scope,
        current.excluded_scope,
        current.profile,
        current.settings,
    ) != (
        merged.description,
        merged.included_scope,
        merged.excluded_scope,
        merged.profile,
        merged.settings,
    )


def _topic_revision_changed(
    current: TopicRevisionRecord, merged: TopicRevisionRecord
) -> bool:
    return (
        current.description,
        current.positive_examples,
        current.negative_examples,
        current.aliases,
        current.entity_ids,
        current.questions,
        current.analysis_template,
    ) != (
        merged.description,
        merged.positive_examples,
        merged.negative_examples,
        merged.aliases,
        merged.entity_ids,
        merged.questions,
        merged.analysis_template,
    )
