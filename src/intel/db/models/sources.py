"""Source management tables (spec 03 §2).

source_templates and parser_versions are G (global static, no RLS — only the
platform admin entry writes them); owner_feeds and source_runs are O;
industry_sources is I. Credentials are stored as secret references only
(credential_ref), never inline (spec 03 §2).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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
)

_ADAPTER_TYPES = (
    "adapter_type IN ('rss', 'atom', 'api', 'html_list', 'sitemap', 'page_monitor')"
)
_FEED_STATUS = "status IN ('active', 'paused')"


class SourceTemplate(TimestampMixin, Base):
    """G-scope source catalog. Exception: id is a stable string derived from
    the normalized canonical seed, so catalog updates keep identity
    (spec 03 §1 例外). Holds no user subscription state."""

    __tablename__ = "source_templates"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    homepage: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_seed: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    access_notes: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )

    __table_args__ = (
        # canonical_seed+kind 唯一 (spec 03 §2).
        UniqueConstraint(
            "canonical_seed", "kind", name="uq_source_templates_seed_kind"
        ),
    )


class ParserVersion(UUIDPrimaryKey, TimestampMixin, Base):
    """G-scope parser release. published 后不修改 (spec 03 §2)."""

    __tablename__ = "parser_versions"

    parser_key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    code_commit: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    fixture_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        # UNIQUE(parser_key, version) (spec 03 §2).
        UniqueConstraint("parser_key", "version", name="uq_parser_versions_key_version"),
    )


class OwnerFeed(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "owner_feeds"

    template_id: Mapped[str | None] = mapped_column(
        String(255), ForeignKey("source_templates.id"), nullable=True
    )
    seed_url: Mapped[str] = mapped_column(Text, nullable=False)
    adapter_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Secret reference only — never the credential itself (spec 03 §2).
    credential_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("parser_versions.id"), nullable=False
    )
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discovery_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_version: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )
    config: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), nullable=False
    )
    # owner_feeds 增加 user_enabled boolean (spec 03 §2): user-level采集开关,
    # independent of any industry subscription state.
    user_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # UNIQUE(owner_id, seed_url, access_scope_key) (spec 03 §2).
        UniqueConstraint(
            "owner_id", "seed_url", "access_scope_key",
            name="uq_owner_feeds_seed_scope",
        ),
        CheckConstraint(_ADAPTER_TYPES, name="adapter_type"),
        CheckConstraint(_FEED_STATUS, name="status"),
        Index("ix_owner_feeds_owner_id", "owner_id"),
        Index("ix_owner_feeds_parser_version_id", "parser_version_id"),
        Index("ix_owner_feeds_template_id", "template_id"),
    )


class IndustrySource(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "industry_sources"

    # FK(owner_id, feed_id) → owner_feeds(owner_id, id) (spec 03 §2).
    feed_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    backfill_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        # UNIQUE(industry_id, feed_id) (spec 03 §2).
        UniqueConstraint(
            "industry_id", "feed_id",
            name="uq_industry_sources_industry_feed",
        ),
        CheckConstraint(_FEED_STATUS, name="status"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "feed_id"], ["owner_feeds.owner_id", "owner_feeds.id"]
        ),
        Index("ix_industry_sources_owner_industry", "owner_id", "industry_id"),
    )


class SourceRun(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "source_runs"

    feed_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # job_id → jobs added by migration 0002 (O-style composite FK).
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    fetched_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    unresolved_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    coverage_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cursor_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # outcome=success/no_change/partial/failed; 不能无更新当失败 (spec 03 §2).
        CheckConstraint(
            "outcome IN ('success', 'no_change', 'partial', 'failed')",
            name="outcome",
        ),
        ForeignKeyConstraint(
            ["owner_id", "feed_id"], ["owner_feeds.owner_id", "owner_feeds.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "job_id"], ["jobs.owner_id", "jobs.id"]
        ),
        Index("ix_source_runs_owner_feed", "owner_id", "feed_id"),
    )
