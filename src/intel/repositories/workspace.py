"""Workspace repository: industries, industry revisions, topics, topic revisions.

Storage-facing snapshot records cross the boundary (the identity-module
pattern); services never see ORM rows. Optimistic concurrency lives in the
update methods: they filter ``row_version == expected_version`` and return
``None`` on a miss so the service raises ``VersionConflict`` with the
current version it re-reads.

Revision tables are INSERT-only version tables (spec 03 §1); the append
methods insert the new revision, move the parent's ``current_revision_id``
pointer and bump the parent's ``row_version`` under the same expected
version, so a racing PATCH cannot split a pointer update from its revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update

from intel.db.models.workspace import (
    Industry,
    IndustryRevision,
    Topic,
    TopicRevision,
)
from intel.repositories.base import ScopedRepository

# --------------------------------------------------------------------------
# storage-facing records
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IndustryRecord:
    """Snapshot of a non-deleted ``industries`` row.

    ``owner_id`` is stamped by the repository on insert (from its scope) —
    services never supply it, mirroring the SQL column defaulting.
    """

    id: UUID = field(default_factory=uuid4)
    owner_id: UUID | None = None
    name: str = ""
    status: str = "draft"
    current_revision_id: UUID | None = None
    deleted_at: datetime | None = None
    row_version: int = 1


@dataclass(slots=True)
class IndustryRevisionRecord:
    id: UUID = field(default_factory=uuid4)
    industry_id: UUID | None = None
    version: int = 1
    description: str = ""
    included_scope: list[str] = field(default_factory=list)
    excluded_scope: list[str] = field(default_factory=list)
    profile: dict | None = None
    settings: dict = field(
        default_factory=lambda: {"pool_scope": "all_public"}
    )
    schema_version: int = 1


@dataclass(slots=True)
class TopicRecord:
    id: UUID = field(default_factory=uuid4)
    industry_id: UUID | None = None
    name: str = ""
    status: str = "active"
    current_revision_id: UUID | None = None
    priority: int = 0
    row_version: int = 1


@dataclass(slots=True)
class TopicRevisionRecord:
    id: UUID = field(default_factory=uuid4)
    industry_id: UUID | None = None
    topic_id: UUID | None = None
    version: int = 1
    description: str = ""
    positive_examples: list[str] = field(default_factory=list)
    negative_examples: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    entity_ids: list[UUID] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    analysis_template: str = "technical"


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


@runtime_checkable
class WorkspaceRepository(Protocol):
    """Everything the workspace service needs from storage.

    Implementations own transaction scope and stamp ``owner_id`` from their
    bound scope; ids are application-generated. All reads exclude
    soft-deleted rows; all updates are optimistic (``None`` on version miss).
    """

    # industries
    async def industry_owned(self, industry_id: UUID) -> bool: ...
    async def get_industry(self, industry_id: UUID) -> IndustryRecord | None: ...
    async def list_industries(self) -> list[IndustryRecord]: ...
    async def industry_name_taken(
        self, name: str, *, exclude_id: UUID | None = None
    ) -> bool: ...
    async def insert_industry(
        self, industry: IndustryRecord, revision: IndustryRevisionRecord
    ) -> IndustryRecord: ...
    async def update_industry(
        self,
        industry_id: UUID,
        expected_version: int,
        *,
        name: str | None = None,
        status: str | None = None,
    ) -> IndustryRecord | None: ...
    async def latest_industry_revision(
        self, industry_id: UUID
    ) -> IndustryRevisionRecord | None: ...
    async def append_industry_revision(
        self,
        revision: IndustryRevisionRecord,
        industry_id: UUID,
        expected_version: int,
    ) -> IndustryRecord | None: ...

    # topics
    async def get_topic(
        self, industry_id: UUID, topic_id: UUID
    ) -> TopicRecord | None: ...
    async def list_topics(self, industry_id: UUID) -> list[TopicRecord]: ...
    async def topic_name_taken(
        self, industry_id: UUID, name: str, *, exclude_id: UUID | None = None
    ) -> bool: ...
    async def insert_topic(
        self, topic: TopicRecord, revision: TopicRevisionRecord
    ) -> TopicRecord: ...
    async def update_topic(
        self,
        industry_id: UUID,
        topic_id: UUID,
        expected_version: int,
        *,
        name: str | None = None,
        status: str | None = None,
        priority: int | None = None,
    ) -> TopicRecord | None: ...
    async def latest_topic_revision(
        self, industry_id: UUID, topic_id: UUID
    ) -> TopicRevisionRecord | None: ...
    async def append_topic_revision(
        self,
        revision: TopicRevisionRecord,
        industry_id: UUID,
        topic_id: UUID,
        expected_version: int,
    ) -> TopicRecord | None: ...


# --------------------------------------------------------------------------
# SQLAlchemy adapter
# --------------------------------------------------------------------------


def _industry_record(row: Industry) -> IndustryRecord:
    return IndustryRecord(
        id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        status=row.status,
        current_revision_id=row.current_revision_id,
        deleted_at=row.deleted_at,
        row_version=row.row_version,
    )


def _industry_revision_record(
    row: IndustryRevision,
) -> IndustryRevisionRecord:
    return IndustryRevisionRecord(
        id=row.id,
        industry_id=row.industry_id,
        version=row.version,
        description=row.description,
        included_scope=list(row.included_scope),
        excluded_scope=list(row.excluded_scope),
        profile=dict(row.profile) if row.profile is not None else None,
        settings=dict(row.settings),
        schema_version=row.schema_version,
    )


def _topic_record(row: Topic) -> TopicRecord:
    return TopicRecord(
        id=row.id,
        industry_id=row.industry_id,
        name=row.name,
        status=row.status,
        current_revision_id=row.current_revision_id,
        priority=row.priority,
        row_version=row.row_version,
    )


def _topic_revision_record(row: TopicRevision) -> TopicRevisionRecord:
    return TopicRevisionRecord(
        id=row.id,
        industry_id=row.industry_id,
        topic_id=row.topic_id,
        version=row.version,
        description=row.description,
        positive_examples=list(row.positive_examples),
        negative_examples=list(row.negative_examples),
        aliases=list(row.aliases),
        entity_ids=list(row.entity_ids),
        questions=list(row.questions),
        analysis_template=row.analysis_template,
    )


async def _fetch_industry(
    conn, owner_id: UUID, industry_id: UUID
) -> IndustryRecord | None:
    stmt = select(Industry).where(
        Industry.owner_id == owner_id,
        Industry.id == industry_id,
        Industry.deleted_at.is_(None),
    )
    row = (await conn.execute(stmt)).scalars().one_or_none()
    return None if row is None else _industry_record(row)


async def _fetch_topic(
    conn, owner_id: UUID, industry_id: UUID, topic_id: UUID
) -> TopicRecord | None:
    stmt = select(Topic).where(
        Topic.owner_id == owner_id,
        Topic.industry_id == industry_id,
        Topic.id == topic_id,
    )
    row = (await conn.execute(stmt)).scalars().one_or_none()
    return None if row is None else _topic_record(row)


class SqlAlchemyWorkspaceRepository(ScopedRepository):
    """WorkspaceRepository on one connection, bound to one IndustryScope.

    The connection's transaction is the unit of work; the HTTP dependency
    commits. Industry operations run owner-scoped even when the repository
    was constructed with an industry (industries are O tables); topic
    operations require the industry scope and filter on it.
    """

    # -- industries ----------------------------------------------------------

    async def industry_owned(self, industry_id: UUID) -> bool:
        return await self.get_industry(industry_id) is not None

    async def get_industry(self, industry_id: UUID) -> IndustryRecord | None:
        await self._bind()
        return await _fetch_industry(self._conn, self.owner_id, industry_id)

    async def list_industries(self) -> list[IndustryRecord]:
        await self._bind()
        stmt = (
            select(Industry)
            .where(
                Industry.owner_id == self.owner_id,
                Industry.deleted_at.is_(None),
            )
            .order_by(Industry.created_at, Industry.id)
        )
        rows = (await self._conn.execute(stmt)).scalars().all()
        return [_industry_record(r) for r in rows]

    async def industry_name_taken(
        self, name: str, *, exclude_id: UUID | None = None
    ) -> bool:
        await self._bind()
        stmt = (
            select(Industry.id)
            .where(
                Industry.owner_id == self.owner_id,
                Industry.name == name,
                Industry.status != "archived",
                Industry.deleted_at.is_(None),
            )
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(Industry.id != exclude_id)
        return (await self._conn.execute(stmt)).one_or_none() is not None

    async def insert_industry(
        self, industry: IndustryRecord, revision: IndustryRevisionRecord
    ) -> IndustryRecord:
        await self._bind()
        assert revision.industry_id == industry.id
        industry.owner_id = self.owner_id
        revision.industry_id = industry.id
        # Industry first (FK target), revision second, pointer last — the
        # circular current_revision_id FK makes the pointer a separate UPDATE.
        await self._conn.execute(
            insert(Industry).values(
                id=industry.id,
                owner_id=self.owner_id,
                name=industry.name,
                status=industry.status,
                current_revision_id=None,
                deleted_at=None,
            )
        )
        await self._insert_industry_revision(revision)
        await self._conn.execute(
            update(Industry)
            .where(
                Industry.owner_id == self.owner_id, Industry.id == industry.id
            )
            .values(current_revision_id=revision.id)
        )
        industry.current_revision_id = revision.id
        return industry

    async def _insert_industry_revision(
        self, revision: IndustryRevisionRecord
    ) -> None:
        await self._conn.execute(
            insert(IndustryRevision).values(
                id=revision.id,
                owner_id=self.owner_id,
                industry_id=revision.industry_id,
                version=revision.version,
                description=revision.description,
                included_scope=revision.included_scope,
                excluded_scope=revision.excluded_scope,
                profile=revision.profile,
                settings=revision.settings,
                schema_version=revision.schema_version,
            )
        )

    async def update_industry(
        self,
        industry_id: UUID,
        expected_version: int,
        *,
        name: str | None = None,
        status: str | None = None,
    ) -> IndustryRecord | None:
        await self._bind()
        values: dict = {"row_version": Industry.row_version + 1}
        if name is not None:
            values["name"] = name
        if status is not None:
            values["status"] = status
        stmt = (
            update(Industry)
            .where(
                Industry.owner_id == self.owner_id,
                Industry.id == industry_id,
                Industry.deleted_at.is_(None),
                Industry.row_version == expected_version,
            )
            .values(**values)
            .returning(
                Industry.id,
                Industry.owner_id,
                Industry.name,
                Industry.status,
                Industry.current_revision_id,
                Industry.deleted_at,
                Industry.row_version,
            )
        )
        row = (await self._conn.execute(stmt)).one_or_none()
        if row is None:
            return None
        return IndustryRecord(
            id=row.id,
            owner_id=row.owner_id,
            name=row.name,
            status=row.status,
            current_revision_id=row.current_revision_id,
            deleted_at=row.deleted_at,
            row_version=row.row_version,
        )

    async def latest_industry_revision(
        self, industry_id: UUID
    ) -> IndustryRevisionRecord | None:
        await self._bind()
        stmt = (
            select(IndustryRevision)
            .where(
                IndustryRevision.owner_id == self.owner_id,
                IndustryRevision.industry_id == industry_id,
            )
            .order_by(IndustryRevision.version.desc())
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _industry_revision_record(row)

    async def append_industry_revision(
        self,
        revision: IndustryRevisionRecord,
        industry_id: UUID,
        expected_version: int,
    ) -> IndustryRecord | None:
        """Insert the revision and move the current pointer — no row_version
        bump: the caller's preceding ``update_industry`` already validated
        the expected version and took the row lock, so one optimistic check
        covers the whole PATCH."""
        await self._bind()
        revision.industry_id = industry_id
        await self._insert_industry_revision(revision)
        stmt = (
            update(Industry)
            .where(
                Industry.owner_id == self.owner_id,
                Industry.id == industry_id,
                Industry.deleted_at.is_(None),
                Industry.row_version == expected_version,
            )
            .values(current_revision_id=revision.id)
            .returning(
                Industry.id,
                Industry.owner_id,
                Industry.name,
                Industry.status,
                Industry.current_revision_id,
                Industry.deleted_at,
                Industry.row_version,
            )
        )
        row = (await self._conn.execute(stmt)).one_or_none()
        if row is None:
            return None
        return IndustryRecord(
            id=row.id,
            owner_id=row.owner_id,
            name=row.name,
            status=row.status,
            current_revision_id=row.current_revision_id,
            deleted_at=row.deleted_at,
            row_version=row.row_version,
        )

    # -- topics ---------------------------------------------------------------

    async def get_topic(
        self, industry_id: UUID, topic_id: UUID
    ) -> TopicRecord | None:
        industry_id = await self._bind_industry()
        return await _fetch_topic(
            self._conn, self.owner_id, industry_id, topic_id
        )

    async def list_topics(self, industry_id: UUID) -> list[TopicRecord]:
        industry_id = await self._bind_industry()
        stmt = (
            select(Topic)
            .where(
                Topic.owner_id == self.owner_id,
                Topic.industry_id == industry_id,
            )
            .order_by(Topic.created_at, Topic.id)
        )
        rows = (await self._conn.execute(stmt)).scalars().all()
        return [_topic_record(r) for r in rows]

    async def topic_name_taken(
        self, industry_id: UUID, name: str, *, exclude_id: UUID | None = None
    ) -> bool:
        industry_id = await self._bind_industry()
        stmt = (
            select(Topic.id)
            .where(
                Topic.owner_id == self.owner_id,
                Topic.industry_id == industry_id,
                Topic.name == name,
                Topic.status != "archived",
            )
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(Topic.id != exclude_id)
        return (await self._conn.execute(stmt)).one_or_none() is not None

    async def insert_topic(
        self, topic: TopicRecord, revision: TopicRevisionRecord
    ) -> TopicRecord:
        industry_id = await self._bind_industry()
        assert revision.industry_id == topic.industry_id == industry_id
        revision.industry_id = industry_id
        revision.topic_id = topic.id
        await self._conn.execute(
            insert(Topic).values(
                id=topic.id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                name=topic.name,
                status=topic.status,
                current_revision_id=None,
                priority=topic.priority,
            )
        )
        await self._conn.execute(
            insert(TopicRevision).values(
                id=revision.id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                topic_id=topic.id,
                version=revision.version,
                description=revision.description,
                positive_examples=revision.positive_examples,
                negative_examples=revision.negative_examples,
                aliases=revision.aliases,
                entity_ids=revision.entity_ids,
                questions=revision.questions,
                analysis_template=revision.analysis_template,
                schema_version=1,
            )
        )
        await self._conn.execute(
            update(Topic)
            .where(
                Topic.owner_id == self.owner_id,
                Topic.industry_id == industry_id,
                Topic.id == topic.id,
            )
            .values(current_revision_id=revision.id)
        )
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
        industry_id = await self._bind_industry()
        values: dict = {"row_version": Topic.row_version + 1}
        if name is not None:
            values["name"] = name
        if status is not None:
            values["status"] = status
        if priority is not None:
            values["priority"] = priority
        stmt = (
            update(Topic)
            .where(
                Topic.owner_id == self.owner_id,
                Topic.industry_id == industry_id,
                Topic.id == topic_id,
                Topic.row_version == expected_version,
            )
            .values(**values)
        )
        await self._conn.execute(stmt)
        # No RETURNING-with-ORM-entity shortcut here: read the row back
        # through the same scoped fetch the GET path uses.
        updated = await _fetch_topic(
            self._conn, self.owner_id, industry_id, topic_id
        )
        if updated is None or updated.row_version != expected_version + 1:
            return None
        return updated

    async def latest_topic_revision(
        self, industry_id: UUID, topic_id: UUID
    ) -> TopicRevisionRecord | None:
        industry_id = await self._bind_industry()
        stmt = (
            select(TopicRevision)
            .where(
                TopicRevision.owner_id == self.owner_id,
                TopicRevision.industry_id == industry_id,
                TopicRevision.topic_id == topic_id,
            )
            .order_by(TopicRevision.version.desc())
            .limit(1)
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        return None if row is None else _topic_revision_record(row)

    async def append_topic_revision(
        self,
        revision: TopicRevisionRecord,
        industry_id: UUID,
        topic_id: UUID,
        expected_version: int,
    ) -> TopicRecord | None:
        industry_id = await self._bind_industry()
        revision.industry_id = industry_id
        revision.topic_id = topic_id
        await self._conn.execute(
            insert(TopicRevision).values(
                id=revision.id,
                owner_id=self.owner_id,
                industry_id=industry_id,
                topic_id=topic_id,
                version=revision.version,
                description=revision.description,
                positive_examples=revision.positive_examples,
                negative_examples=revision.negative_examples,
                aliases=revision.aliases,
                entity_ids=revision.entity_ids,
                questions=revision.questions,
                analysis_template=revision.analysis_template,
                schema_version=1,
            )
        )
        stmt = (
            update(Topic)
            .where(
                Topic.owner_id == self.owner_id,
                Topic.industry_id == industry_id,
                Topic.id == topic_id,
                Topic.row_version == expected_version,
            )
            .values(current_revision_id=revision.id)
        )
        await self._conn.execute(stmt)
        updated = await _fetch_topic(
            self._conn, self.owner_id, industry_id, topic_id
        )
        if updated is None or updated.row_version != expected_version:
            return None
        return updated
