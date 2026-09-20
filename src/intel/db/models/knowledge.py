"""Industry knowledge tables (spec 03 §4) + physical association tables (§9).

All tables are I scope (owner_id + industry_id, composite FK to industries,
UNIQUE(owner_id, industry_id, id)). Version tables are INSERT-only with
UNIQUE(parent_id, version) (spec 03 §1). Spec 03 §9: ID sets the spec lists
as entity_ids[]/claim_revision_ids[] MUST land in physical link tables with
real composite FKs — the tables in this module keep no ARRAY mirror of those
sets; topic_revisions.entity_ids (0001) keeps its ARRAY column and gains the
topic_revision_entities link table in this migration.

FK policy (spec 03 §9): references that point AT parsed artifacts, claim
revisions, event revisions or evidence rows use ON DELETE RESTRICT —
user-level cleanup deletes in dependency order, never a blanket CASCADE that
would destroy historical evidence.

Deferred FKs: claims.current_revision_id → claim_revisions,
events.current_revision_id → event_revisions,
event_topics.current_revision_id → event_topic_revisions,
event_relations.current_revision_id → event_relation_revisions and
watches.resolution_report_id → reports are circular/forward references;
they are declared here as regular constraints and created by
op.create_foreign_key at the end of migration 0002 (use_alter=True is
silently dropped by CreateTable rendering, so it must NOT be used).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
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
    TimestampMixin,
    UUIDPrimaryKey,
    VersionMixin,
)

_ENTITY_KIND = (
    "kind IN ('company', 'product', 'person', 'institution', 'method',"
    " 'dataset', 'component')"
)
_CLAIM_STATE = "state IN ('active', 'disputed', 'corrected', 'retracted')"
_CLAIM_KIND = "kind IN ('source_statement', 'inference')"
_EVIDENCE_RELATION = "relation IN ('supports', 'refutes', 'context')"
_FAMILY_BASIS = (
    "basis IN ('explicit_reference', 'exact_reprint', 'inferred')"
)
_FAMILY_STATUS = "status IN ('confirmed', 'uncertain')"
_EVENT_LIFECYCLE = "lifecycle IN ('active', 'merged', 'retracted')"
_RELATION_TYPE = (
    "type IN ('updates', 'validates', 'refutes', 'applies', 'extends',"
    " 'parallel', 'replaces')"
)
_RELATION_BASIS = "basis IN ('explicit', 'inferred')"


class IndustryDocument(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """Industry binding of a raw-pool document (spec 03 §4).

    UNIQUE(industry_id, document_id); 相关性不同不复制 raw blob — relevance
    lives here, the blob stays single in the owner pool. The parse FK carries
    owner_id only (I→O 原始材料引用): the service validates in-transaction
    that the parse belongs to a document already bound to this industry."""

    __tablename__ = "industry_documents"

    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    current_parse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    association_reason: Mapped[str] = mapped_column(Text, nullable=False)
    associated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "industry_id", "document_id", name="uq_industry_documents_industry_document"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "document_id"], ["documents.owner_id", "documents.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "current_parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_industry_documents_owner_industry", "owner_id", "industry_id"),
        Index("ix_industry_documents_owner_document", "owner_id", "document_id"),
    )


class DocumentTopic(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_topics"

    industry_document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_block_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    decision_origin: Mapped[str] = mapped_column(Text, nullable=False)
    locked_by_user: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        # UNIQUE(doc, topic) (spec 03 §4); changes go to
        # document_association_history, this row is the projection.
        UniqueConstraint(
            "industry_document_id", "topic_id", name="uq_document_topics_document_topic"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "industry_document_id"],
            [
                "industry_documents.owner_id",
                "industry_documents.industry_id",
                "industry_documents.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_revision_id"],
            [
                "topic_revisions.owner_id",
                "topic_revisions.industry_id",
                "topic_revisions.id",
            ],
        ),
        Index("ix_document_topics_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_document_topics_owner_document", "owner_id", "industry_document_id"
        ),
    )


class Entity(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """同行业实体；同名不自动合并 (spec 03 §4)."""

    __tablename__ = "entities"

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    identifiers: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), nullable=False
    )
    attributes: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_ENTITY_KIND, name="kind"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        Index("ix_entities_owner_industry", "owner_id", "industry_id"),
        Index("ix_entities_owner_name", "owner_id", "industry_id", "canonical_name"),
    )


class EntityAlias(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """允许歧义别名关联多个实体；禁止全局 alias 唯一 (spec 03 §4)."""

    __tablename__ = "entity_aliases"

    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    qualifier: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        Index("ix_entity_aliases_owner_industry", "owner_id", "industry_id"),
        Index("ix_entity_aliases_owner_entity", "owner_id", "industry_id", "entity_id"),
        Index(
            "ix_entity_aliases_owner_norm", "owner_id", "industry_id", "normalized_alias"
        ),
    )


class Claim(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "claims"

    # Deferred FK to claim_revisions (circular) — see module docstring.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_CLAIM_STATE, name="state"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        # Circular: created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            ["claim_revisions.owner_id", "claim_revisions.industry_id",
             "claim_revisions.id"],
        ),
        Index("ix_claims_owner_industry", "owner_id", "industry_id"),
    )


class ClaimRevision(IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base):
    """新文本新版本 (spec 03 §4); subject/object entity sets live in
    claim_revision_entities (spec 03 §9), asserted_by also has a direct
    FK column here per the §4 field list."""

    __tablename__ = "claim_revisions"

    claim_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    asserted_by_entity_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    # "object" is the spec's field name; the attribute avoids shadowing it.
    object_value: Mapped[dict] = mapped_column("object", JSONB, nullable=False)
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    valid_time: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # AssessmentDimensions DTO (spec 03 §4 note) — no uncalibrated odds.
    assessment: Mapped[dict] = mapped_column(JSONB, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint("claim_id", "version", name="uq_claim_revisions_claim_version"),
        CheckConstraint(_CLAIM_KIND, name="kind"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "claim_id"],
            ["claims.owner_id", "claims.industry_id", "claims.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "asserted_by_entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_claim_revisions_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_claim_revisions_owner_claim", "owner_id", "industry_id", "claim_id"
        ),
    )


class Evidence(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """A located quote in a parsed artifact backing a claim revision.

    文本定位校验与语义支持分开 (spec 03 §4): locator_verified_at records the
    literal-match check only; semantic_support_status carries the separate
    semantic judgement. extraction_run_id points at the extraction job
    (jobs table); RESTRICT on parse/claim-revision targets per spec 03 §9."""

    __tablename__ = "evidence"

    claim_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parsed_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    locator_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    semantic_support_status: Mapped[str] = mapped_column(Text, nullable=False)
    source_family_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    extraction_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_EVIDENCE_RELATION, name="relation"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
        ForeignKeyConstraint(
            ["owner_id", "parsed_artifact_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "source_family_id"],
            [
                "source_families.owner_id",
                "source_families.industry_id",
                "source_families.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "extraction_run_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_evidence_owner_industry", "owner_id", "industry_id"),
        Index("ix_evidence_owner_claim_revision", "owner_id", "claim_revision_id"),
        Index("ix_evidence_owner_parse", "owner_id", "parsed_artifact_id"),
    )


class SourceFamily(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "source_families"

    label: Mapped[str] = mapped_column(Text, nullable=False)
    origin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_entity_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_FAMILY_BASIS, name="basis"),
        CheckConstraint(_FAMILY_STATUS, name="status"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "origin_entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        Index("ix_source_families_owner_industry", "owner_id", "industry_id"),
    )


class Event(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """合并不删 ID (spec 03 §4): merged rows keep lifecycle='merged' and
    point at the surviving event via merged_into_id."""

    __tablename__ = "events"

    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Deferred FK to event_revisions (circular) — see module docstring.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    lifecycle: Mapped[str] = mapped_column(Text, nullable=False)
    merged_into_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_EVENT_LIFECYCLE, name="lifecycle"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "merged_into_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
        ),
        # Circular: created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            ["event_revisions.owner_id", "event_revisions.industry_id",
             "event_revisions.id"],
        ),
        Index("ix_events_owner_industry", "owner_id", "industry_id"),
    )


class EventRevision(IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base):
    """occurred_start/occurred_end are projection columns of occurred_time
    (spec 03 §8): time-range queries index them instead of rescanning the
    JSON. entity/claim-revision sets live in event_revision_entities /
    event_revision_claims (spec 03 §9).

    identity_key: 未知为 NULL, 不以通用拼接键强行唯一 (spec 03 §8). A plain
    UNIQUE(owner_id, industry_id, identity_key) is impossible because every
    revision of the same event repeats its identity_key; uniqueness of
    identity_key across *currently active* events is enforced by the
    repository/service layer, with the lookup index below."""

    __tablename__ = "event_revisions"

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_time: Mapped[dict] = mapped_column(JSONB, nullable=False)
    published_time: Mapped[dict] = mapped_column(JSONB, nullable=False)
    effective_time: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    occurred_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    identity_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint("event_id", "version", name="uq_event_revisions_event_version"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_event_revisions_owner_industry", "owner_id", "industry_id"),
        # event_revisions 按所属事件及 recorded_at 排序 (spec 03 §8).
        Index(
            "ix_event_revisions_owner_event_recorded",
            "owner_id",
            "industry_id",
            "event_id",
            "recorded_at",
        ),
        Index(
            "ix_event_revisions_owner_occurred", "owner_id", "industry_id",
            "occurred_start",
        ),
        Index(
            "ix_event_revisions_owner_identity", "owner_id", "industry_id",
            "identity_key",
        ),
    )


class EventTopic(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "event_topics"

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Deferred FK to event_topic_revisions (circular) — see module docstring.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint("event_id", "topic_id", name="uq_event_topics_event_topic"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
        ),
        # Circular: created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            [
                "event_topic_revisions.owner_id",
                "event_topic_revisions.industry_id",
                "event_topic_revisions.id",
            ],
        ),
        Index("ix_event_topics_owner_industry", "owner_id", "industry_id"),
        Index("ix_event_topics_owner_event", "owner_id", "industry_id", "event_id"),
    )


class EventTopicRevision(IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base):
    """多主题解释共享事件事实；人工锁定不被批处理覆盖 (spec 03 §4). The
    claim-revision set lives in event_topic_revision_claims (spec 03 §9)."""

    __tablename__ = "event_topic_revisions"

    event_topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    interpretation: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    locked_by_user: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "event_topic_id", "version", name="uq_event_topic_revisions_topic_version"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_topic_id"],
            [
                "event_topics.owner_id",
                "event_topics.industry_id",
                "event_topics.id",
            ],
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
        Index("ix_event_topic_revisions_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_event_topic_revisions_owner_parent",
            "owner_id",
            "industry_id",
            "event_topic_id",
        ),
    )


class EventRelation(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """禁自环；不以日期自动生边 (spec 03 §4)."""

    __tablename__ = "event_relations"

    from_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    to_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # Deferred FK to event_relation_revisions (circular) — module docstring.
    current_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint("from_event_id <> to_event_id", name="no_self_loop"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "from_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_relations_from_event",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "to_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_relations_to_event",
        ),
        # Circular: created by op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "current_revision_id"],
            [
                "event_relation_revisions.owner_id",
                "event_relation_revisions.industry_id",
                "event_relation_revisions.id",
            ],
        ),
        Index("ix_event_relations_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_event_relations_owner_from", "owner_id", "industry_id", "from_event_id"
        ),
    )


class EventRelationRevision(
    IndustryScopeMixin, UUIDPrimaryKey, VersionMixin, Base
):
    """citation_evidence_ids[] of the spec lives in relation_revision_evidence
    (spec 03 §9)."""

    __tablename__ = "event_relation_revisions"

    relation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    from_event_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    to_event_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "relation_id", "version", name="uq_event_relation_revisions_rel_version"
        ),
        CheckConstraint(_RELATION_TYPE, name="type"),
        CheckConstraint(_RELATION_BASIS, name="basis"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "relation_id"],
            [
                "event_relations.owner_id",
                "event_relations.industry_id",
                "event_relations.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "from_event_revision_id"],
            [
                "event_revisions.owner_id",
                "event_revisions.industry_id",
                "event_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_event_relation_revisions_from",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "to_event_revision_id"],
            [
                "event_revisions.owner_id",
                "event_revisions.industry_id",
                "event_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_event_relation_revisions_to",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "created_by_job_id"],
            ["jobs.owner_id", "jobs.industry_id", "jobs.id"],
        ),
        Index("ix_event_relation_revisions_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_event_relation_revisions_owner_relation",
            "owner_id",
            "industry_id",
            "relation_id",
        ),
    )


class Watch(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """首版定期检查仅本地归档；在线调查由用户启动 (spec 03 §4). topic/entity
    sets live in watch_topics / watch_entities (spec 03 §9). resolution FK to
    reports is deferred (reports is a later table) — see module docstring."""

    __tablename__ = "watches"

    title: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_report_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        # Forward reference to reports (conversation.py); created by
        # op.create_foreign_key in migration 0002.
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "resolution_report_id"],
            ["reports.owner_id", "reports.industry_id", "reports.id"],
        ),
        Index("ix_watches_owner_industry", "owner_id", "industry_id"),
    )


class Override(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """所有应用 override 有审计；自动新建议不得覆盖 (spec 03 §4).
    object_id is polymorphic by design (object_type discriminates) — no
    single-table FK is possible; integrity is enforced by the service layer
    per object_type."""

    __tablename__ = "overrides"

    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        Index("ix_overrides_owner_industry", "owner_id", "industry_id"),
    )


class EventReadState(IndustryScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """个人已读状态不修改 event_revision (spec 03 §4)."""

    __tablename__ = "event_read_states"

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    is_read: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        UniqueConstraint(
            "industry_id", "event_id", name="uq_event_read_states_industry_event"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
        ),
        Index("ix_event_read_states_owner_industry", "owner_id", "industry_id"),
    )


# --- Physical association tables (spec 03 §9) -------------------------------
# Pure link tables: composite PK, I scope, real composite FKs. They carry no
# common mutability columns (spec 03 §1 例外: 纯关联表使用复合主键).


class TopicRevisionEntity(IndustryScopeMixin, Base):
    __tablename__ = "topic_revision_entities"

    topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("topic_revision_id", "entity_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        Index("ix_topic_revision_entities_owner_industry", "owner_id", "industry_id"),
    )


class ClaimRevisionEntity(IndustryScopeMixin, Base):
    __tablename__ = "claim_revision_entities"

    claim_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("claim_revision_id", "entity_id"),
        CheckConstraint(
            "role IN ('subject', 'asserted_by', 'object')", name="role"
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        Index("ix_claim_revision_entities_owner_industry", "owner_id", "industry_id"),
    )


class EventRevisionClaim(IndustryScopeMixin, Base):
    __tablename__ = "event_revision_claims"

    event_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    claim_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("event_revision_id", "claim_revision_id"),
        CheckConstraint("role IN ('primary', 'supporting')", name="role"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
            ["owner_id", "industry_id", "claim_revision_id"],
            [
                "claim_revisions.owner_id",
                "claim_revisions.industry_id",
                "claim_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index("ix_event_revision_claims_owner_industry", "owner_id", "industry_id"),
    )


class EventRevisionEntity(IndustryScopeMixin, Base):
    __tablename__ = "event_revision_entities"

    event_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("event_revision_id", "entity_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        Index("ix_event_revision_entities_owner_industry", "owner_id", "industry_id"),
    )


class EventTopicRevisionClaim(IndustryScopeMixin, Base):
    __tablename__ = "event_topic_revision_claims"

    event_topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    claim_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("event_topic_revision_id", "claim_revision_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
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
            ["owner_id", "industry_id", "claim_revision_id"],
            [
                "claim_revisions.owner_id",
                "claim_revisions.industry_id",
                "claim_revisions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_event_topic_revision_claims_owner_industry", "owner_id", "industry_id"
        ),
    )


class RelationRevisionEvidence(IndustryScopeMixin, Base):
    __tablename__ = "relation_revision_evidence"

    relation_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("relation_revision_id", "evidence_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "relation_revision_id"],
            [
                "event_relation_revisions.owner_id",
                "event_relation_revisions.industry_id",
                "event_relation_revisions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "evidence_id"],
            ["evidence.owner_id", "evidence.industry_id", "evidence.id"],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_relation_revision_evidence_owner_industry", "owner_id", "industry_id"
        ),
    )


class WatchTopic(IndustryScopeMixin, Base):
    __tablename__ = "watch_topics"

    watch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("watch_id", "topic_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "watch_id"],
            ["watches.owner_id", "watches.industry_id", "watches.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
        ),
        Index("ix_watch_topics_owner_industry", "owner_id", "industry_id"),
    )


class WatchEntity(IndustryScopeMixin, Base):
    __tablename__ = "watch_entities"

    watch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("watch_id", "entity_id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "watch_id"],
            ["watches.owner_id", "watches.industry_id", "watches.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "entity_id"],
            ["entities.owner_id", "entities.industry_id", "entities.id"],
        ),
        Index("ix_watch_entities_owner_industry", "owner_id", "industry_id"),
    )


# --- History tables (spec 03 §9) --------------------------------------------
# Append-only: UUID PK + recorded_at, no updated_at/row_version (INSERT-only;
# the current document_topics/event_topics rows are just projections).


class DocumentAssociationHistory(IndustryScopeMixin, UUIDPrimaryKey, Base):
    __tablename__ = "document_association_history"

    industry_document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    locked_by_user: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "industry_document_id"],
            [
                "industry_documents.owner_id",
                "industry_documents.industry_id",
                "industry_documents.id",
            ],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
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
            ["owner_id", "industry_id", "supersedes_id"],
            [
                "document_association_history.owner_id",
                "document_association_history.industry_id",
                "document_association_history.id",
            ],
            # Explicit name: the convention-derived one exceeds 63 chars.
            name="fk_document_association_history_supersedes",
        ),
        Index(
            "ix_document_association_history_owner_industry", "owner_id", "industry_id"
        ),
        Index(
            "ix_document_association_history_owner_document",
            "owner_id",
            "industry_id",
            "industry_document_id",
        ),
    )


class EventAssociationHistory(IndustryScopeMixin, UUIDPrimaryKey, Base):
    __tablename__ = "event_association_history"

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    topic_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    relevance: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    locked_by_user: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "topic_id"],
            ["topics.owner_id", "topics.industry_id", "topics.id"],
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
            ["owner_id", "industry_id", "supersedes_id"],
            [
                "event_association_history.owner_id",
                "event_association_history.industry_id",
                "event_association_history.id",
            ],
            name="fk_event_association_history_supersedes",
        ),
        Index(
            "ix_event_association_history_owner_industry", "owner_id", "industry_id"
        ),
        Index(
            "ix_event_association_history_owner_event",
            "owner_id",
            "industry_id",
            "event_id",
        ),
    )


class EventMergeOperation(IndustryScopeMixin, UUIDPrimaryKey, Base):
    """source_revision_before/target_revision_before pin the current event
    revisions at merge time (spec 03 §9)."""

    __tablename__ = "event_merge_operations"

    source_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    target_event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_revision_before: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    target_revision_before: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    membership_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    operation_status: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    undone_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "source_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_merge_operations_source_event",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "target_event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
            name="fk_event_merge_operations_target_event",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "source_revision_before"],
            [
                "event_revisions.owner_id",
                "event_revisions.industry_id",
                "event_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_event_merge_operations_source_revision",
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "target_revision_before"],
            [
                "event_revisions.owner_id",
                "event_revisions.industry_id",
                "event_revisions.id",
            ],
            ondelete="RESTRICT",
            name="fk_event_merge_operations_target_revision",
        ),
        Index("ix_event_merge_operations_owner_industry", "owner_id", "industry_id"),
    )


class EventLifecycleHistory(IndustryScopeMixin, UUIDPrimaryKey, Base):
    """每次 merged/retracted/active 变更及 recorded_at (spec 03 §9):
    as_of reads must walk this history — never filter everything by the
    current merged_into (spec 03 §5)."""

    __tablename__ = "event_lifecycle_history"

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lifecycle: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "industry_id", "id"),
        CheckConstraint(_EVENT_LIFECYCLE, name="lifecycle"),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"], ["industries.owner_id", "industries.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "event_id"],
            ["events.owner_id", "events.industry_id", "events.id"],
        ),
        Index("ix_event_lifecycle_history_owner_industry", "owner_id", "industry_id"),
        Index(
            "ix_event_lifecycle_history_owner_event",
            "owner_id",
            "industry_id",
            "event_id",
        ),
    )
