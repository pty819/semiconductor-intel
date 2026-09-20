"""Evolution/report/conversation tables (spec 03 §6) + doc 16 §7 additions.

I scope throughout except audit_log (O/I hybrid: industry_id nullable —
audit rows exist for owner-level events too). Version tables are
INSERT-only with UNIQUE(parent_id, version). conversation_summary_revisions
has no version counter — it is identified by (conversation_id,
through_message_id) and is append-only.

publication_citations (spec 03 §9) is split into report_citations /
evolution_citations / message_citations — 首版选分表避免多态FK; JSON
responses are generated from these joins.

Deferred FKs (circular/forward — created by op.create_foreign_key at the
end of migration 0002; use_alter must NOT be used, see knowledge.py):
evolutions.current_revision_id → evolution_revisions,
reports.current_revision_id → report_revisions,
conversations.last_committed_message_id → messages,
conversations.current_state_revision_id → conversation_state_revisions,
watches.resolution_report_id → reports (declared in knowledge.py).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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

_REPORT_TYPE = "type IN ('daily', 'topic', 'investigation')"
_REVIEW_STATUS = (
    "status IN ('pending', 'accepted', 'rejected', 'obsolete', 'applied', 'failed')"
)
_MESSAGE_ROLE = "role IN ('user', 'assistant')"


class Evolution(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "evolutions"

    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Deferred FK to evolution_revisions (circular) — module docstring.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint("topic_id", name="uq_evolutions_topic"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
        ),
        # Circular: created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            [
                "evolution_revisions.owner_id",
                "evolution_revisions.industry_id",
                "evolution_revisions.id",
            ],
        ),
        Index("ix_evolutions_owner_industry", "owner_id", "industry_id"),
    )


class EvolutionRevision(IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base):
    """阶段/边引用固定 event/claim/evidence 版本；published 后不改
    (spec 03 §6)."""

    __tablename__ = "evolution_revisions"

    evolution_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stages: Mapped[list] = mapped_column(JSONB, nullable=False)
    nodes: Mapped[list] = mapped_column(JSONB, nullable=False)
    edges: Mapped[list] = mapped_column(JSONB, nullable=False)
    open_questions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "evolution_id", "version", name="uq_evolution_revisions_evolution_version"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "evolution_id"],
            ["evolutions.owner_id", "evolutions.industry_id", "evolutions.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            [
                "topic_revisions.owner_id",
                "topic_revisions.industry_id",
                "topic_revisions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "supersedes_id"],
            [
                "evolution_revisions.owner_id",
                "evolution_revisions.industry_id",
                "evolution_revisions.id",
            ],
        ),
        Index("ix_evolution_revisions_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_evolution_revisions_owner_evolution",
            "owner_id",
            "industry_id",
            "evolution_id",
        ),
    )


class Report(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """type=daily/topic/investigation；无外发 (spec 03 §6)."""

    __tablename__ = "reports"

    type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Deferred FK to report_revisions (circular) — module docstring.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_REPORT_TYPE, name="type"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        # Circular: created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            [
                "report_revisions.owner_id",
                "report_revisions.industry_id",
                "report_revisions.id",
            ],
        ),
        Index("ix_reports_owner_industry", "owner_id", "industry_id"),
    )


class ReportRevision(IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base):
    """citations 每个指向固定证据版本 (spec 03 §6) — the physical rows live
    in report_citations (spec 03 §9)."""

    __tablename__ = "report_revisions"

    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    content: Mapped[list] = mapped_column(JSONB, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "report_id", "version", name="uq_report_revisions_report_version"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "report_id"],
            ["reports.owner_id", "reports.industry_id", "reports.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_report_revisions_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_report_revisions_owner_report", "owner_id", "industry_id", "report_id"
        ),
    )


class Conversation(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """创建后禁止变更 industry_id (spec 03 §6). v1.2 conversation additions
    (doc 16 §7): state_version / last_committed_message_id /
    current_state_revision_id — the latter two are deferred FKs (module
    docstring)."""

    __tablename__ = "conversations"

    title: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state_version: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    last_committed_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    current_state_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        # Circular: both created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "last_committed_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_state_revision_id"],
            [
                "conversation_state_revisions.owner_id",
                "conversation_state_revisions.industry_id",
                "conversation_state_revisions.id",
            ],
        ),
        Index("ix_conversations_owner_industry", "owner_id", "industry_id"),
    )


class Message(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """role=user/assistant；最终回答不可覆盖，用新消息更正 (spec 03 §6).
    v1.2 (doc 16 §7): parent_message_id (null on the first turn, else the
    last committed assistant message) and turn_index."""

    __tablename__ = "messages"

    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    citation_manifest: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), nullable=False
    )
    as_of: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    parent_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_MESSAGE_ROLE, name="role"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "conversation_id"],
            [
                "conversations.owner_id",
                "conversations.industry_id",
                "conversations.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "parent_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
        ),
        Index("ix_messages_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_messages_owner_conversation",
            "owner_id",
            "industry_id",
            "conversation_id",
            "turn_index",
        ),
    )


class ConversationStateRevision(
    IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base
):
    """Structured conversation state per committed turn (doc 16 §7).
    active_topic_ids/reference_ids stay ARRAY columns — they are projection
    lists rebuilt from the conversation, not §9-mandated business-object
    sets (constraints JSONB carries {id,text,kind,source_message_id,status})."""

    __tablename__ = "conversation_state_revisions"

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    last_message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    resolved_question: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[dict] = mapped_column(JSONB, nullable=False)
    active_topic_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), server_default=text("'{}'"), nullable=False
    )
    # 有序 references — ordered citation/message ids for the committed turn.
    reference_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), server_default=text("'{}'"), nullable=False
    )
    open_questions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    summary_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "conversation_id",
            "version",
            name="uq_conversation_state_revisions_conversation_version",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "conversation_id"],
            [
                "conversations.owner_id",
                "conversations.industry_id",
                "conversations.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "last_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "summary_revision_id"],
            [
                "conversation_summary_revisions.owner_id",
                "conversation_summary_revisions.industry_id",
                "conversation_summary_revisions.id",
            ],
            # Explicit name: the convention-derived one exceeds 63 chars.
            name="fk_conversation_state_revisions_summary",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index(
            "ix_conversation_state_revisions_owner_industry", "owner_id", "industry_id"
        ),
        Index(
            "ix_conversation_state_revisions_owner_conversation",
            "owner_id",
            "industry_id",
            "conversation_id",
        ),
    )


class ConversationSummaryRevision(IndustryScopeMixin, UUIDPrimaryKey, Base):
    """Application-level conversation compression (doc 16 §6-§7): append-only,
    identified by (conversation_id, through_message_id), no version counter
    and no mutability columns. The full original messages are preserved."""

    __tablename__ = "conversation_summary_revisions"

    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    through_message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    preserved_constraint_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    reference_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), server_default=text("'{}'"), nullable=False
    )
    input_message_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), server_default=text("'{}'"), nullable=False
    )
    generation_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    validation_status: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "conversation_id"],
            [
                "conversations.owner_id",
                "conversations.industry_id",
                "conversations.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "through_message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "generation_run_id"],
            [
                "generation_runs.owner_id",
                "generation_runs.industry_id",
                "generation_runs.id",
            ],
        ),
        Index(
            "ix_conversation_summary_revisions_owner_industry", "owner_id",
            "industry_id",
        ),
        Index(
            "ix_conversation_summary_revisions_owner_conversation",
            "owner_id",
            "industry_id",
            "conversation_id",
        ),
    )


class ReviewTask(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """v1.3 (spec 03 §6): proposal is the serialized typed Correction union
    from contracts (kind-discriminated) — never a kind-less free dict."""

    __tablename__ = "review_tasks"

    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    proposal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expected_versions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    applied_job_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_REVIEW_STATUS, name="status"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "applied_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_review_tasks_owner_industry", "owner_id", "industry_id"),
        Index("ix_review_tasks_owner_status", "owner_id", "industry_id", "status"),
    )


class AuditLog(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """O/I hybrid (spec 03 §6): owner-level rows have industry_id NULL.
    append-only — 只记录必要差异，不记录秘密. RLS scopes on owner only;
    industry visibility is the repository's responsibility (spec 10 §1
    layering: RLS 防跨用户, service 防同行业内越界)."""

    __tablename__ = "audit_log"

    industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    before_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    details: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        Index("ix_audit_log_owner_id", "owner_id"),
        Index("ix_audit_log_owner_target", "owner_id", "target_type", "target_id"),
    )


class DependencyEdge(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """correction→派生结果 stale edges (spec 03 §6). source/derived revision
    refs are generic string-typed (object_type + revision_id) exactly as the
    spec text defines — spec 03 §9 allows splitting into concrete FK tables
    when a specific edge family needs DB-level integrity; that split is
    deferred until a consumer requires it (documented deviation)."""

    __tablename__ = "dependency_edges"

    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    derived_type: Mapped[str] = mapped_column(Text, nullable=False)
    derived_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        Index("ix_dependency_edges_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_dependency_edges_owner_source",
            "owner_id",
            "industry_id",
            "source_type",
            "source_revision_id",
        ),
    )


class DerivedStatus(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """独立于不可变 report/evolution 内容 (spec 03 §6). revision_id is
    generic (object_type discriminates) — same documented pattern as
    dependency_edges; refresh_job_id is a real FK."""

    __tablename__ = "derived_status"

    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    stale_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_job_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "refresh_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_derived_status_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_derived_status_owner_object",
            "owner_id",
            "industry_id",
            "object_type",
            "revision_id",
        ),
    )


# --- publication_citations split (spec 03 §9: 首版选分表避免多态FK) ----------


class ReportCitation(IndustryScopeMixin, Base):
    __tablename__ = "report_citations"

    report_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    citation_label: Mapped[str] = mapped_column(Text, nullable=False)
    claim_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("report_revision_id", "evidence_id"),
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
            ["owner_id", "industry_id", "evidence_id"],
            ["evidence.owner_id", "evidence.industry_id", "evidence.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            [
                "claim_revisions.owner_id",
                "claim_revisions.industry_id",
                "claim_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index("ix_report_citations_owner_industry", "owner_id", "industry_id"),
    )


class EvolutionCitation(IndustryScopeMixin, Base):
    __tablename__ = "evolution_citations"

    evolution_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    citation_label: Mapped[str] = mapped_column(Text, nullable=False)
    claim_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("evolution_revision_id", "evidence_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
            ["owner_id", "industry_id", "evidence_id"],
            ["evidence.owner_id", "evidence.industry_id", "evidence.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            [
                "claim_revisions.owner_id",
                "claim_revisions.industry_id",
                "claim_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index("ix_evolution_citations_owner_industry", "owner_id", "industry_id"),
    )


class MessageCitation(IndustryScopeMixin, Base):
    __tablename__ = "message_citations"

    message_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    citation_label: Mapped[str] = mapped_column(Text, nullable=False)
    claim_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("message_id", "evidence_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "message_id"],
            ["messages.owner_id", "messages.industry_id", "messages.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "evidence_id"],
            ["evidence.owner_id", "evidence.industry_id", "evidence.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_revision_id"],
            [
                "claim_revisions.owner_id",
                "claim_revisions.industry_id",
                "claim_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index("ix_message_citations_owner_industry", "owner_id", "industry_id"),
    )
