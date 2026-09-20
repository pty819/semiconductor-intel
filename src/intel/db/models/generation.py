"""Generation provenance tables (doc 15 §3) — the displayable layer.

model_runs (jobs.py) still records individual model calls; generation_runs
records displayable application execution steps. A step can link several
model retries via generation_run_models — the app must keep old attempts,
never drop them (doc 15 §3).

output_generations: doc 15 §3 defines a polymorphic
publication_or_revision_id but mandates real FKs, not polymorphic UUIDs.
Single-table compromise: one typed, real composite-FK column per citable
output kind (report/evolution/message/event/topic-interpretation revisions)
plus a num_nonnulls()=1 CHECK — every row carries exactly one FK-backed
target; splitting into per-kind tables remains a mechanical later step if a
kind needs extra columns.
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
    TimestampMixin,
    UUIDPrimaryKey,
)

_TRACE_STATE = (
    "trace_state IN ('recording', 'pending', 'available', 'missing',"
    " 'expired', 'redacted')"
)
_VIEWER_IMPORT_STATE = (
    "viewer_import_state IN ('not_requested', 'pending', 'ready', 'failed')"
)
_OUTPUT_ROLE = "role IN ('producer', 'verifier', 'upstream')"


class GenerationRun(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """trace_state/viewer_import_state per doc 15 §3: available only when the
    exported file actually exists, contains the matching session/spans and
    passed the access check; viewer usability additionally needs import or
    live ingestion (ready)."""

    __tablename__ = "generation_runs"

    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    trace_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_span_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_state: Mapped[str] = mapped_column(Text, nullable=False)
    viewer_import_state: Mapped[str] = mapped_column(Text, nullable=False)
    model_route_display: Mapped[str | None] = mapped_column(Text, nullable=True)
    nooa_commit: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_TRACE_STATE, name="trace_state"),
        CheckConstraint(_VIEWER_IMPORT_STATE, name="viewer_import_state"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_generation_runs_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_generation_runs_owner_job", "owner_id", "industry_id", "job_id"
        ),
    )


class GenerationRunModel(IndustryScopeMixin, Base):
    """Links one step's model calls/retries (doc 15 §3): same I scope and
    composite FKs on both sides; dropping an old attempt loses provenance."""

    __tablename__ = "generation_run_models"

    generation_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    model_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("generation_run_id", "model_run_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "generation_run_id"],
            [
                "generation_runs.owner_id",
                "generation_runs.industry_id",
                "generation_runs.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "model_run_id"],
            ["model_runs.owner_id", "model_runs.industry_id", "model_runs.id"],
        ),
        Index(
            "ix_generation_run_models_owner_industry", "owner_id", "industry_id"
        ),
    )


class OutputGeneration(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """output_path is a JSON Pointer (e.g. /blocks/3, /stages/1) and is only
    valid against immutable output revisions — 重排需要新 revision (doc 15
    §3). Exactly one typed target column is set per row (CHECK below)."""

    __tablename__ = "output_generations"

    report_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    evolution_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    event_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    event_topic_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    generation_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_OUTPUT_ROLE, name="role"),
        CheckConstraint(
            "num_nonnulls(report_revision_id, evolution_revision_id, message_id,"
            " event_revision_id, event_topic_revision_id) = 1",
            name="single_target",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "report_revision_id"],
            [
                "report_revisions.owner_id",
                "report_revisions.industry_id",
                "report_revisions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "evolution_revision_id"],
            [
                "evolution_revisions.owner_id",
                "evolution_revisions.industry_id",
                "evolution_revisions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_revision_id"],
            [
                "event_revisions.owner_id",
                "event_revisions.industry_id",
                "event_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_topic_revision_id"],
            [
                "event_topic_revisions.owner_id",
                "event_topic_revisions.industry_id",
                "event_topic_revisions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "generation_run_id"],
            [
                "generation_runs.owner_id",
                "generation_runs.industry_id",
                "generation_runs.id",
            ],
        ),
        Index("ix_output_generations_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_output_generations_owner_run",
            "owner_id",
            "industry_id",
            "generation_run_id",
        ),
    )
