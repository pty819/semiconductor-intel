"""Job/coverage/model-run tables (spec 03 §7).

jobs/job_steps/job_events/model_runs/coverage_batches are O/I hybrids:
owner_id NOT NULL + nullable industry_id for kind-level scoping (feed
polling runs owner-wide; extraction runs carry an industry). RLS scopes
them on owner only (spec 10 §1 layering: RLS 防跨用户, repository/service
防同行业内越界). recall_hits is a full I table; api_idempotency is O.

Exception (spec 03 §1): job_events uses PK (job_id, seq) and carries only
created_at — no id/updated_at/row_version.

jobs serves as a composite-FK target for both O-style (owner_id, id) and
I-style (owner_id, industry_id, id) references, so both UNIQUE constraints
exist; same for model_runs (target of generation_run_models).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from intel.db.base import (
    Base,
    IndustryScopeMixin,
    OwnerScopeMixin,
    TimestampMixin,
    UUIDPrimaryKey,
)


class Job(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """UNIQUE(owner_id, kind, idempotency_key); 领取索引(state, available_at)
    (spec 03 §7)."""

    __tablename__ = "jobs"

    # Nullable for kind-level (owner-wide) jobs; FK carries both scope cols.
    industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    max_attempts: Mapped[int] = mapped_column(
        Integer, server_default="3", nullable=False
    )
    lease_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    progress: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), nullable=False
    )
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # I-style composite-FK target for industry-scoped referencing rows.
        # Explicit name: the convention would collide with uq_jobs_owner_id.
        UniqueConstraint(
            "owner_id", "industry_id", "id", name="uq_jobs_owner_industry_id"
        ),
        UniqueConstraint(
            "owner_id", "kind", "idempotency_key", name="uq_jobs_idempotency"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        Index("ix_jobs_owner_industry", "owner_id", "industry_id"),
        # 领取索引 (claim index, spec 03 §7).
        Index("ix_jobs_state_available_at", "state", "available_at"),
    )


class JobStep(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """UNIQUE(job_id, step_key, input_hash) (spec 03 §7)."""

    __tablename__ = "job_steps"

    industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    output_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        UniqueConstraint(
            "job_id", "step_key", "input_hash", name="uq_job_steps_step_input"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_job_steps_owner_industry", "owner_id", "industry_id"),
        Index("ix_job_steps_owner_job", "owner_id", "job_id"),
    )


class JobEvent(OwnerScopeMixin, Base):
    """PK(job_id, seq) (spec 03 §1 例外); SSE 可恢复 — created_at only,
    no mutability columns (append-only)."""

    __tablename__ = "job_events"

    industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("job_id", "seq"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_job_events_owner_job", "owner_id", "job_id"),
    )


class ModelRun(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """一次具体模型执行 (spec 03 §7 / 15 §1): 输入可追溯；usage 缺失为
    null，不能当 0. The displayable application-step layer on top of this is
    generation_runs (doc 15 §3)."""

    __tablename__ = "model_runs"

    industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    nooa_session_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_route: Mapped[str] = mapped_column(Text, nullable=False)
    provider_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    nooa_commit: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    settings_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # I-style composite-FK target (generation_run_models); explicit name
        # to avoid colliding with uq_model_runs_owner_id.
        UniqueConstraint(
            "owner_id", "industry_id", "id", name="uq_model_runs_owner_industry_id"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_model_runs_owner_industry", "owner_id", "industry_id"),
        Index("ix_model_runs_owner_job", "owner_id", "job_id"),
    )


class CoverageBatch(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """检索/分类未完成不得标记完整 (spec 03 §7)."""

    __tablename__ = "coverage_batches"

    industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    feed_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    topic_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    done_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    failed_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    unknown_count: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    watermark: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config_revision: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "feed_id"], ["owner_feeds.owner_id", "owner_feeds.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
        ),
        Index("ix_coverage_batches_owner_industry", "owner_id", "industry_id"),
    )


class RecallHit(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """UNIQUE(job, parse, chunk, channel)，保留通道 provenance (spec 03 §7).
    parse/chunk FKs carry owner_id only (I→O 原始材料引用)."""

    __tablename__ = "recall_hits"

    recall_job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        # NULL chunk_id rows (doc-level recalls) dedupe at the service layer.
        UniqueConstraint(
            "recall_job_id", "parse_id", "chunk_id", "channel",
            name="uq_recall_hits_job_parse_chunk_channel",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "recall_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "chunk_id"], ["chunks.owner_id", "chunks.id"]
        ),
        Index("ix_recall_hits_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_recall_hits_owner_job", "owner_id", "industry_id", "recall_job_id"
        ),
    )


class ApiIdempotency(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """UNIQUE(owner, route, key)；同键不同请求 409 (spec 03 §7)."""

    __tablename__ = "api_idempotency"

    route: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[Any] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        UniqueConstraint(
            "owner_id", "route", "key", name="uq_api_idempotency_route_key"
        ),
        Index("ix_api_idempotency_owner_expires", "owner_id", "expires_at"),
    )
