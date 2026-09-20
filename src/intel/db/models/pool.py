"""Raw collection material pool (spec 03 §3) — all tables O scope.

These tables hold the owner's raw ingested material. They are reachable by
ingestion/retrieval services only; product queries go through industry
bindings (spec 10 §1). Dedup is never cross-owner: blobs UNIQUE includes
owner_id, and documents keep private visibility scopes apart from public
versions of the same URL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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
    OwnerScopeMixin,
    TimestampMixin,
    UUIDPrimaryKey,
    VectorType,
)

# retrieval_scope / parse_status enumerations (spec 03 §3).
_RETRIEVAL_SCOPE = (
    "retrieval_scope IN ('metadata', 'abstract', 'partial', 'fulltext')"
)
_PARSE_STATUS = "parse_status IN ('ok', 'partial', 'failed')"
_DIFF_KIND = "kind IN ('content_change', 'parser_change', 'mixed')"
_DECISION_OUTCOME = "outcome IN ('direct', 'background', 'uncertain', 'unrelated')"


class DiscoveryItem(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "discovery_items"

    feed_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    # FK to source_runs(owner_id, id), declared in __table_args__.
    run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    origin_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    discovered_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_hint: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # UNIQUE(owner_id, feed_id, canonical_url) 对 feed 模式 (spec 03 §3);
        # non-feed rows have NULL feed_id and are idempotent at the service
        # layer (origin_request_id + URL).
        UniqueConstraint(
            "owner_id", "feed_id", "canonical_url",
            name="uq_discovery_items_feed_canonical_url",
        ),
        ForeignKeyConstraint(
            ["owner_id", "feed_id"], ["owner_feeds.owner_id", "owner_feeds.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "run_id"], ["source_runs.owner_id", "source_runs.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "target_industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        Index("ix_discovery_items_owner_feed", "owner_id", "feed_id"),
        Index("ix_discovery_items_owner_state", "owner_id", "state"),
    )


class Blob(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "blobs"

    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retention_class: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # UNIQUE(owner_id, sha256, media_type); 不跨用户 dedup (spec 03 §3).
        UniqueConstraint(
            "owner_id", "sha256", "media_type", name="uq_blobs_dedup"
        ),
        Index("ix_blobs_owner_id", "owner_id"),
    )


class Document(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "documents"

    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    identity_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    identity_value: Mapped[str] = mapped_column(Text, nullable=False)
    # public 或 industry:UUID — 私人调查同 URL 不与公共版本自动合并 (spec 03 §3).
    visibility_scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    origin_kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    current_capture_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        UniqueConstraint(
            "owner_id",
            "visibility_scope_key",
            "identity_namespace",
            "identity_value",
            name="uq_documents_identity",
        ),
        ForeignKeyConstraint(
            ["owner_id", "target_industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        # Declared after captures exists: keeps the current pointer within
        # this owner's captures. Circular reference — use_alter FKs are
        # silently dropped by CreateTable rendering, so it is a regular
        # constraint here and created by op.create_foreign_key in 0002.
        ForeignKeyConstraint(
            ["owner_id", "current_capture_id"],
            ["captures.owner_id", "captures.id"],
        ),
        Index("ix_documents_owner_id", "owner_id"),
        Index(
            "ix_documents_owner_target_industry", "owner_id", "target_industry_id"
        ),
    )


class DocumentOrigin(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """Records that the same document was discovered via several entries."""

    __tablename__ = "document_origins"

    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    discovery_item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    feed_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    target_industry_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # 唯一 document+item (spec 03 §3).
        UniqueConstraint(
            "document_id", "discovery_item_id",
            name="uq_document_origins_document_item",
        ),
        ForeignKeyConstraint(
            ["owner_id", "document_id"], ["documents.owner_id", "documents.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "discovery_item_id"],
            ["discovery_items.owner_id", "discovery_items.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "feed_id"], ["owner_feeds.owner_id", "owner_feeds.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "target_industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        Index("ix_document_origins_owner_document", "owner_id", "document_id"),
    )


class Capture(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "captures"

    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    raw_blob_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    effective_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_scope: Mapped[str] = mapped_column(Text, nullable=False)
    access_policy: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # 不同响应外壳若实际内容同也保留 origin 记录 → the UNIQUE keeps
        # (document, content, access_policy) distinct shells collapse (03 §3).
        UniqueConstraint(
            "document_id", "content_hash", "access_policy",
            name="uq_captures_content",
        ),
        CheckConstraint(_RETRIEVAL_SCOPE, name="retrieval_scope"),
        ForeignKeyConstraint(
            ["owner_id", "document_id"], ["documents.owner_id", "documents.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "raw_blob_id"], ["blobs.owner_id", "blobs.id"]
        ),
        Index("ix_captures_owner_document", "owner_id", "document_id"),
    )


class FetchObservation(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    """每次访问记录; 304 binds to an existing capture, no fake content
    version (spec 03 §3)."""

    __tablename__ = "fetch_observations"

    document_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    discovery_item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    capture_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        ForeignKeyConstraint(
            ["owner_id", "document_id"], ["documents.owner_id", "documents.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "discovery_item_id"],
            ["discovery_items.owner_id", "discovery_items.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "capture_id"], ["captures.owner_id", "captures.id"]
        ),
        Index(
            "ix_fetch_observations_owner_discovery",
            "owner_id",
            "discovery_item_id",
        ),
        Index(
            "ix_fetch_observations_owner_document", "owner_id", "document_id"
        ),
    )


class ParsedArtifact(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "parsed_artifacts"

    capture_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parser_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("parser_versions.id"),
        nullable=False,
    )
    normalized_blob_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # Column name is the spec's "metadata"; the attribute avoids clashing
    # with DeclarativeBase.metadata.
    artifact_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False
    )
    parse_status: Mapped[str] = mapped_column(Text, nullable=False)
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    parsed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # UNIQUE(capture_id, parser_version_id); 新 parser 不覆盖旧 parse.
        UniqueConstraint(
            "capture_id", "parser_version_id",
            name="uq_parsed_artifacts_capture_parser",
        ),
        CheckConstraint(_PARSE_STATUS, name="parse_status"),
        ForeignKeyConstraint(
            ["owner_id", "capture_id"], ["captures.owner_id", "captures.id"]
        ),
        ForeignKeyConstraint(
            ["owner_id", "normalized_blob_id"], ["blobs.owner_id", "blobs.id"]
        ),
        Index("ix_parsed_artifacts_owner_capture", "owner_id", "capture_id"),
        Index(
            "ix_parsed_artifacts_parser_version_id", "parser_version_id"
        ),
    )


class DocumentDiff(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "document_diffs"

    from_parse_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    to_parse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    diff_algorithm_version: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    changed_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )
    field_changes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # UNIQUE(from,to,algorithm) (spec 03 §3).
        UniqueConstraint(
            "from_parse_id",
            "to_parse_id",
            "diff_algorithm_version",
            name="uq_document_diffs_from_to_algo",
        ),
        CheckConstraint(_DIFF_KIND, name="kind"),
        ForeignKeyConstraint(
            ["owner_id", "from_parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name="fk_document_diffs_from_parse",
        ),
        ForeignKeyConstraint(
            ["owner_id", "to_parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
            name="fk_document_diffs_to_parse",
        ),
        Index("ix_document_diffs_owner_from", "owner_id", "from_parse_id"),
    )


class Chunk(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "chunks"

    parsed_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    block_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    chunk_text: Mapped[str] = mapped_column("text", Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    # Text (not array): it feeds the trigram 短语/精确复查通道 (spec 03 §8).
    normalized_terms: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable vector; dimension is fixed by the config/index_generation
    # migration (spec 03 §8) — 缺 embedding 不妨碍正文保存.
    embedding: Mapped[Any | None] = mapped_column(VectorType, nullable=True)
    embedding_model_version: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        # UNIQUE(parse_id, ordinal) (spec 03 §3).
        UniqueConstraint(
            "parsed_artifact_id", "ordinal", name="uq_chunks_parse_ordinal"
        ),
        ForeignKeyConstraint(
            ["owner_id", "parsed_artifact_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
        ),
        # chunks 按 owner+parse 过滤 (spec 03 §8).
        Index("ix_chunks_owner_parse", "owner_id", "parsed_artifact_id"),
    )


class ProcessingDecision(OwnerScopeMixin, UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "processing_decisions"

    parse_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    industry_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    industry_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    candidate_claims: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # FKs added by migration 0002 (model_runs / overrides are its tables).
    model_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    override_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    # Required by UNIQUE(parse, industry_revision, analysis_version)
    # (spec 03 §3): bump when the same parse is re-decided under a new
    # analysis pipeline version.
    analysis_version: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        UniqueConstraint(
            "parse_id",
            "industry_revision_id",
            "analysis_version",
            name="uq_processing_decisions_parse_revision",
        ),
        CheckConstraint(_DECISION_OUTCOME, name="outcome"),
        ForeignKeyConstraint(
            ["owner_id", "parse_id"],
            ["parsed_artifacts.owner_id", "parsed_artifacts.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id"],
            ["industries.owner_id", "industries.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_revision_id"],
            ["industry_revisions.owner_id", "industry_revisions.id"],
        ),
        # O-style composite FK to model_runs (kind-scoped runs have NULL
        # industry); I-style to overrides (decisions carry industry_id).
        ForeignKeyConstraint(
            ["owner_id", "model_run_id"],
            ["model_runs.owner_id", "model_runs.id"],
        ),
        ForeignKeyConstraint(
            ["owner_id", "industry_id", "override_id"],
            ["overrides.owner_id", "overrides.industry_id", "overrides.id"],
        ),
        Index("ix_processing_decisions_owner_parse", "owner_id", "parse_id"),
    )
