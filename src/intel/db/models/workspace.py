"""Workspace tables: industries, industry_revisions, topics, topic_revisions.

Spec 03 §2. Scopes: industries + industry_revisions are O (owner); topics +
topic_revisions are I (owner+industry). Both revision tables are immutable
version tables: parent link + UNIQUE(parent_id, version), INSERT-only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from intel.db.base import (
    Base,
    IndustryScopeMixin,
    OwnerScopeMixin,
    TimestampMixin,
    UUIDPrimaryKey,
    VersionMixin,
)

# Status vocabularies come from the contracts DTOs (IndustryView / TopicView).
_INDUSTRY_STATUS = "status IN ('draft', 'active', 'paused', 'archived')"
_TOPIC_STATUS = "status IN ('active', 'paused', 'archived')"


class Industry(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "industries"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Set once the first revision exists; composite FK (declared with
    # use_alter below) keeps the pointer inside this owner's revisions.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # O scope: UNIQUE(owner_id, id) — composite FK target (spec 03 §1).
        UniqueConstraint("owner_id", "id"),
        # name owner 内活跃唯一 (spec 03 §2): archived/soft-deleted names free.
        Index(
            "uq_industries_active_name",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("status <> 'archived' AND deleted_at IS NULL"),
        ),
        Index("ix_industries_owner_id", "owner_id"),
        CheckConstraint(_INDUSTRY_STATUS, name="status"),
        ForeignKeyConstraint(
            ["owner_id", "current_revision_id"],
            ["industry_revisions.owner_id", "industry_revisions.id"],
            use_alter=True,
        ),
    )


class IndustryRevision(
    OwnerScopeMixin, UUIDPrimaryKey, VersionMixin, Base
):
    __tablename__ = "industry_revisions"

    industry_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    included_scope: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    excluded_scope: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # settings JSONB, pool_scope=all_public 默认 (spec 03 §2).
    settings: Mapped[dict] = mapped_column(
        JSONB,
        server_default=text("""'{"pool_scope": "all_public"}'"""),
        nullable=False,
    )

    __table_args__ = (
        # 版本表: UNIQUE(parent_id, version) (spec 03 §1).
        UniqueConstraint(
            "industry_id", "version",
            name="uq_industry_revisions_industry_version",
        ),
        UniqueConstraint("owner_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        Index("ix_industry_revisions_owner_industry", "owner_id", "industry_id"),
    )


class Topic(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "topics"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    priority: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )

    __table_args__ = (
        # I scope: UNIQUE(owner_id, industry_id, id) (spec 03 §1).
        UniqueConstraint("owner_id", "industry_id", "id"),
        # 同行业活跃名称唯一 (spec 03 §2).
        Index(
            "uq_topics_active_name",
            "owner_id",
            "industry_id",
            "name",
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
        Index("ix_topics_owner_industry", "owner_id", "industry_id"),
        CheckConstraint(_TOPIC_STATUS, name="status"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            [
                "topic_revisions.owner_id",
                "topic_revisions.industry_id",
                "topic_revisions.id",
            ],
            use_alter=True,
        ),
    )


class TopicRevision(IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base):
    __tablename__ = "topic_revisions"

    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    positive_examples: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    negative_examples: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    # entity_ids[] is the logical field per spec 03 §2; the physical link
    # table topic_revision_entities (spec 03 §9) arrives with the knowledge
    # tables migration.
    entity_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), server_default=text("'{}'"), nullable=False
    )
    questions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    analysis_template: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("topic_id", "version", name="uq_topic_revisions_topic_version"),
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
        ),
        Index(
            "ix_topic_revisions_owner_industry_topic",
            "owner_id",
            "industry_id",
            "topic_id",
        ),
    )
