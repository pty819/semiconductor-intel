"""Raw-pool repository: discovery items, blobs, documents, captures,
fetch observations, feed discovery state (spec 03 §3, 04 §3/§4).

The workflow handlers consume the :class:`PoolRepository` protocol;
``SqlAlchemyPoolRepository`` is the production adapter (one short
transaction per handler step), ``InMemoryPoolDatabase`` is the test
double with snapshot/rollback so the one-transaction rule (04 §3: 同一
事务写候选、创建抓取 jobs、保存游标) is observable offline.

Identity rules encoded here:

- ``discovery_items`` dedup on UNIQUE(owner_id, feed_id, canonical_url);
  upsert returns ``(record, created)`` so the discover handler spawns
  fetch jobs for new items only.
- ``documents`` identity is (visibility_scope_key, identity_namespace,
  identity_value) — "public" pool by default; credentialed/private scope
  never merges with the public document (03 §3).
- ``captures`` UNIQUE(document_id, content_hash, access_policy): an
  unchanged 200 binds the prior capture (write an observation, not a new
  version) — ING-04.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import desc, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from intel.db.models.pool import (
    Blob,
    Capture,
    DiscoveryItem,
    Document,
    DocumentDiff,
    DocumentOrigin,
    FetchObservation,
    ParsedArtifact,
)
from intel.db.models.sources import OwnerFeed, SourceRun
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository

# --------------------------------------------------------------------------
# storage-facing records
# --------------------------------------------------------------------------


@dataclass(slots=True)
class FeedDiscoveryState:
    """What the discover handler needs from owner_feeds (04 §3)."""

    feed_id: UUID
    owner_id: UUID
    seed_url: str
    adapter_type: str
    config: dict = field(default_factory=dict)
    access_scope_key: str = "public"
    cursor: str | None = None
    cursor_version: int = 1
    user_enabled: bool = True
    status: str = "active"
    parser_version_id: UUID | None = None


@dataclass(slots=True)
class DiscoveryItemRecord:
    owner_id: UUID
    discovered_url: str
    canonical_url: str
    origin_kind: str = "feed"
    id: UUID = field(default_factory=uuid4)
    feed_id: UUID | None = None
    run_id: UUID | None = None
    target_industry_id: UUID | None = None
    title_hint: str | None = None
    published_hint: datetime | None = None
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    state: str = "discovered"
    error_code: str | None = None


@dataclass(slots=True)
class BlobRecord:
    owner_id: UUID
    object_key: str
    sha256: str
    media_type: str
    byte_size: int
    retention_class: str = "raw"
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class DocumentRecord:
    owner_id: UUID
    canonical_url: str
    identity_namespace: str
    identity_value: str
    visibility_scope_key: str
    origin_kind: str
    target_industry_id: UUID | None = None
    current_capture_id: UUID | None = None
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class CaptureRecord:
    owner_id: UUID
    document_id: UUID
    raw_blob_id: UUID
    response_status: int
    effective_url: str
    content_hash: str
    content_type: str
    retrieval_scope: str = "fulltext"
    access_policy: str = "public"
    etag: str | None = None
    last_modified: datetime | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class FetchObservationRecord:
    owner_id: UUID
    discovery_item_id: UUID
    outcome: str
    document_id: UUID | None = None
    capture_id: UUID | None = None
    status_code: int | None = None
    etag: str | None = None
    error_code: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class ParsedArtifactRecord:
    """parsed_artifacts row (03 §3): block 永久绑定本次 parse."""

    owner_id: UUID
    capture_id: UUID
    parser_version_id: UUID
    normalized_blob_id: UUID
    text_hash: str
    blocks: list[dict[str, Any]]
    artifact_metadata: dict[str, Any]
    parse_status: str
    coverage: dict[str, Any]
    quality_flags: list[str] = field(default_factory=list)
    parsed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class DocumentDiffRecord:
    """document_diffs row (03 §3): kind=content_change/parser_change/mixed."""

    owner_id: UUID
    from_parse_id: UUID
    to_parse_id: UUID
    diff_algorithm_version: str
    kind: str
    changed_blocks: list[dict[str, Any]] = field(default_factory=list)
    field_changes: list[dict[str, Any]] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class SourceRunRecord:
    owner_id: UUID
    feed_id: UUID
    job_id: UUID
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)
    finished_at: datetime | None = None
    outcome: str = "partial"
    discovered_count: int = 0
    fetched_count: int = 0
    unresolved_count: int = 0
    coverage_end: datetime | None = None
    cursor_before: str | None = None
    cursor_after: str | None = None
    error_code: str | None = None


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


class PoolRepository(Protocol):
    """What the ingest workflow needs from the raw pool."""

    async def load_feed_state(self, feed_id: UUID) -> FeedDiscoveryState | None: ...

    async def save_cursor(
        self, feed_id: UUID, cursor: str | None
    ) -> bool: ...

    async def upsert_discovery_item(
        self, item: DiscoveryItemRecord
    ) -> tuple[DiscoveryItemRecord, bool]: ...

    async def insert_source_run(
        self, run: SourceRunRecord
    ) -> SourceRunRecord: ...

    async def finish_source_run(
        self,
        run_id: UUID,
        *,
        outcome: str,
        cursor_after: str | None,
        discovered_count: int,
        coverage_end: datetime | None = None,
        error_code: str | None = None,
    ) -> None: ...

    async def get_discovery_item(
        self, item_id: UUID
    ) -> DiscoveryItemRecord | None: ...

    async def set_discovery_state(
        self, item_id: UUID, state: str, *, error_code: str | None = None
    ) -> None: ...

    async def latest_capture_for_item(
        self, item_id: UUID
    ) -> CaptureRecord | None: ...

    async def find_blob(
        self, sha256: str, media_type: str
    ) -> BlobRecord | None: ...

    async def insert_blob(self, blob: BlobRecord) -> BlobRecord: ...

    async def find_document(
        self, visibility_scope_key: str, identity_namespace: str, identity_value: str
    ) -> DocumentRecord | None: ...

    async def insert_document(self, document: DocumentRecord) -> DocumentRecord: ...

    async def insert_capture(self, capture: CaptureRecord) -> CaptureRecord: ...

    async def set_current_capture(self, document_id: UUID, capture_id: UUID) -> None: ...

    async def insert_origin(
        self, *, document_id: UUID, discovery_item_id: UUID, feed_id: UUID | None
    ) -> None: ...

    async def insert_observation(
        self, observation: FetchObservationRecord
    ) -> FetchObservationRecord: ...

    async def get_capture(self, capture_id: UUID) -> CaptureRecord | None: ...

    async def get_blob(self, blob_id: UUID) -> BlobRecord | None: ...

    async def set_capture_retrieval_scope(
        self, capture_id: UUID, retrieval_scope: str
    ) -> None: ...

    async def insert_parsed_artifact(
        self, record: ParsedArtifactRecord
    ) -> tuple[ParsedArtifactRecord, bool]: ...

    async def list_parses_for_capture(
        self, capture_id: UUID
    ) -> list[ParsedArtifactRecord]: ...

    async def latest_parse_for_document(
        self, document_id: UUID, *, exclude_capture_id: UUID
    ) -> ParsedArtifactRecord | None: ...

    async def insert_document_diff(
        self, record: DocumentDiffRecord
    ) -> tuple[DocumentDiffRecord, bool]: ...


# --------------------------------------------------------------------------
# SqlAlchemy adapter (production)
# --------------------------------------------------------------------------


class SqlAlchemyPoolRepository(ScopedRepository, PoolRepository):
    """Pool writes over one AsyncConnection, RLS-bound to the scope."""

    async def _bind_owner(self) -> None:
        await set_scope(self.conn, self.owner_id, self.scope.industry_id)
        await require_owner_guc(self.conn)

    async def load_feed_state(self, feed_id: UUID) -> FeedDiscoveryState | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(OwnerFeed).where(
                    OwnerFeed.owner_id == self.owner_id, OwnerFeed.id == feed_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return FeedDiscoveryState(
            feed_id=row.id,
            owner_id=row.owner_id,
            seed_url=row.seed_url,
            adapter_type=row.adapter_type,
            config=dict(row.config or {}),
            access_scope_key=row.access_scope_key,
            cursor=row.discovery_cursor,
            cursor_version=row.cursor_version,
            user_enabled=row.user_enabled,
            status=row.status,
            parser_version_id=row.parser_version_id,
        )

    async def save_cursor(self, feed_id: UUID, cursor: str | None) -> bool:
        await self._bind_owner()
        result = await self.conn.execute(
            update(OwnerFeed)
            .where(OwnerFeed.owner_id == self.owner_id, OwnerFeed.id == feed_id)
            .values(
                discovery_cursor=cursor,
                cursor_version=OwnerFeed.cursor_version + 1,
                updated_at=func.now(),
            )
        )
        return result.rowcount > 0

    async def upsert_discovery_item(
        self, item: DiscoveryItemRecord
    ) -> tuple[DiscoveryItemRecord, bool]:
        await self._bind_owner()
        stmt = (
            pg_insert(DiscoveryItem)
            .values(
                id=item.id,
                owner_id=self.owner_id,
                feed_id=item.feed_id,
                run_id=item.run_id,
                origin_kind=item.origin_kind,
                target_industry_id=item.target_industry_id,
                discovered_url=item.discovered_url,
                canonical_url=item.canonical_url,
                title_hint=item.title_hint,
                published_hint=item.published_hint,
                first_seen_at=item.first_seen_at,
                state=item.state,
            )
            .on_conflict_do_nothing(
                index_elements=["owner_id", "feed_id", "canonical_url"]
            )
            .returning(DiscoveryItem.id)
        )
        inserted = (await self.conn.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return item, True
        existing = (
            await self.conn.execute(
                select(DiscoveryItem).where(
                    DiscoveryItem.owner_id == self.owner_id,
                    DiscoveryItem.feed_id == item.feed_id,
                    DiscoveryItem.canonical_url == item.canonical_url,
                )
            )
        ).scalar_one()
        return _item_record(existing), False

    async def insert_source_run(self, run: SourceRunRecord) -> SourceRunRecord:
        await self._bind_owner()
        await self.conn.execute(
            insert(SourceRun).values(
                id=run.id,
                owner_id=run.owner_id,
                feed_id=run.feed_id,
                job_id=run.job_id,
                started_at=run.started_at,
                outcome="partial",
                discovered_count=0,
                cursor_before=run.cursor_before,
            )
        )
        return run

    async def finish_source_run(
        self,
        run_id: UUID,
        *,
        outcome: str,
        cursor_after: str | None,
        discovered_count: int,
        coverage_end: datetime | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._bind_owner()
        await self.conn.execute(
            update(SourceRun)
            .where(SourceRun.owner_id == self.owner_id, SourceRun.id == run_id)
            .values(
                finished_at=func.now(),
                outcome=outcome,
                discovered_count=discovered_count,
                cursor_after=cursor_after,
                coverage_end=coverage_end,
                error_code=error_code,
            )
        )

    async def get_discovery_item(
        self, item_id: UUID
    ) -> DiscoveryItemRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(DiscoveryItem).where(
                    DiscoveryItem.owner_id == self.owner_id,
                    DiscoveryItem.id == item_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _item_record(row)

    async def set_discovery_state(
        self, item_id: UUID, state: str, *, error_code: str | None = None
    ) -> None:
        await self._bind_owner()
        await self.conn.execute(
            update(DiscoveryItem)
            .where(
                DiscoveryItem.owner_id == self.owner_id,
                DiscoveryItem.id == item_id,
            )
            .values(
                state=state,
                error_code=error_code,
                updated_at=func.now(),
            )
        )

    async def latest_capture_for_item(self, item_id: UUID) -> CaptureRecord | None:
        await self._bind_owner()
        document_id = (
            await self.conn.execute(
                select(DocumentOrigin.document_id).where(
                    DocumentOrigin.owner_id == self.owner_id,
                    DocumentOrigin.discovery_item_id == item_id,
                )
            )
        ).scalar_one_or_none()
        if document_id is None:
            return None
        row = (
            await self.conn.execute(
                select(Capture)
                .where(
                    Capture.owner_id == self.owner_id,
                    Capture.document_id == document_id,
                )
                .order_by(desc(Capture.fetched_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else _capture_record(row)

    async def find_blob(self, sha256: str, media_type: str) -> BlobRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(Blob).where(
                    Blob.owner_id == self.owner_id,
                    Blob.sha256 == sha256,
                    Blob.media_type == media_type,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _blob_record(row)

    async def insert_blob(self, blob: BlobRecord) -> BlobRecord:
        await self._bind_owner()
        await self.conn.execute(
            insert(Blob).values(
                id=blob.id,
                owner_id=blob.owner_id,
                object_key=blob.object_key,
                sha256=blob.sha256,
                media_type=blob.media_type,
                byte_size=blob.byte_size,
                retention_class=blob.retention_class,
            )
        )
        return blob

    async def find_document(
        self, visibility_scope_key: str, identity_namespace: str, identity_value: str
    ) -> DocumentRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(Document).where(
                    Document.owner_id == self.owner_id,
                    Document.visibility_scope_key == visibility_scope_key,
                    Document.identity_namespace == identity_namespace,
                    Document.identity_value == identity_value,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _document_record(row)

    async def insert_document(self, document: DocumentRecord) -> DocumentRecord:
        await self._bind_owner()
        await self.conn.execute(
            insert(Document).values(
                id=document.id,
                owner_id=document.owner_id,
                canonical_url=document.canonical_url,
                identity_namespace=document.identity_namespace,
                identity_value=document.identity_value,
                visibility_scope_key=document.visibility_scope_key,
                origin_kind=document.origin_kind,
                target_industry_id=document.target_industry_id,
                first_seen_at=document.first_seen_at,
            )
        )
        return document

    async def insert_capture(self, capture: CaptureRecord) -> CaptureRecord:
        await self._bind_owner()
        await self.conn.execute(
            insert(Capture).values(
                id=capture.id,
                owner_id=capture.owner_id,
                document_id=capture.document_id,
                raw_blob_id=capture.raw_blob_id,
                response_status=capture.response_status,
                fetched_at=capture.fetched_at,
                effective_url=capture.effective_url,
                content_hash=capture.content_hash,
                etag=capture.etag,
                last_modified=capture.last_modified,
                content_type=capture.content_type,
                retrieval_scope=capture.retrieval_scope,
                access_policy=capture.access_policy,
            )
        )
        return capture

    async def set_current_capture(
        self, document_id: UUID, capture_id: UUID
    ) -> None:
        await self._bind_owner()
        await self.conn.execute(
            update(Document)
            .where(Document.owner_id == self.owner_id, Document.id == document_id)
            .values(current_capture_id=capture_id, updated_at=func.now())
        )

    async def insert_origin(
        self, *, document_id: UUID, discovery_item_id: UUID, feed_id: UUID | None
    ) -> None:
        await self._bind_owner()
        stmt = (
            pg_insert(DocumentOrigin)
            .values(
                id=uuid4(),
                owner_id=self.owner_id,
                document_id=document_id,
                discovery_item_id=discovery_item_id,
                feed_id=feed_id,
            )
            .on_conflict_do_nothing(
                index_elements=["document_id", "discovery_item_id"]
            )
        )
        await self.conn.execute(stmt)

    async def insert_observation(
        self, observation: FetchObservationRecord
    ) -> FetchObservationRecord:
        await self._bind_owner()
        await self.conn.execute(
            insert(FetchObservation).values(
                id=observation.id,
                owner_id=observation.owner_id,
                document_id=observation.document_id,
                discovery_item_id=observation.discovery_item_id,
                capture_id=observation.capture_id,
                fetched_at=observation.fetched_at,
                status_code=observation.status_code,
                outcome=observation.outcome,
                etag=observation.etag,
                error_code=observation.error_code,
            )
        )
        return observation

    async def get_capture(self, capture_id: UUID) -> CaptureRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(Capture).where(
                    Capture.owner_id == self.owner_id,
                    Capture.id == capture_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _capture_record(row)

    async def get_blob(self, blob_id: UUID) -> BlobRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(Blob).where(
                    Blob.owner_id == self.owner_id, Blob.id == blob_id
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _blob_record(row)

    async def set_capture_retrieval_scope(
        self, capture_id: UUID, retrieval_scope: str
    ) -> None:
        await self._bind_owner()
        await self.conn.execute(
            update(Capture)
            .where(
                Capture.owner_id == self.owner_id, Capture.id == capture_id
            )
            .values(retrieval_scope=retrieval_scope)
        )

    async def insert_parsed_artifact(
        self, record: ParsedArtifactRecord
    ) -> tuple[ParsedArtifactRecord, bool]:
        """UNIQUE(capture_id, parser_version_id): a conflict binds the
        existing parse (新 parser 不覆盖旧 parse, 03 §3)."""
        await self._bind_owner()
        stmt = (
            pg_insert(ParsedArtifact)
            .values(
                id=record.id,
                owner_id=record.owner_id,
                capture_id=record.capture_id,
                parser_version_id=record.parser_version_id,
                normalized_blob_id=record.normalized_blob_id,
                text_hash=record.text_hash,
                blocks=record.blocks,
                artifact_metadata=record.artifact_metadata,
                parse_status=record.parse_status,
                coverage=record.coverage,
                quality_flags=record.quality_flags,
                parsed_at=record.parsed_at,
            )
            .on_conflict_do_nothing(
                index_elements=["capture_id", "parser_version_id"]
            )
            .returning(ParsedArtifact.id)
        )
        inserted = (await self.conn.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return record, True
        existing = (
            await self.conn.execute(
                select(ParsedArtifact).where(
                    ParsedArtifact.owner_id == self.owner_id,
                    ParsedArtifact.capture_id == record.capture_id,
                    ParsedArtifact.parser_version_id
                    == record.parser_version_id,
                )
            )
        ).scalar_one()
        return _parse_record(existing), False

    async def list_parses_for_capture(
        self, capture_id: UUID
    ) -> list[ParsedArtifactRecord]:
        await self._bind_owner()
        rows = (
            await self.conn.execute(
                select(ParsedArtifact)
                .where(
                    ParsedArtifact.owner_id == self.owner_id,
                    ParsedArtifact.capture_id == capture_id,
                )
                .order_by(ParsedArtifact.parsed_at)
            )
        ).scalars()
        return [_parse_record(row) for row in rows]

    async def latest_parse_for_document(
        self, document_id: UUID, *, exclude_capture_id: UUID
    ) -> ParsedArtifactRecord | None:
        await self._bind_owner()
        row = (
            await self.conn.execute(
                select(ParsedArtifact)
                .join(
                    Capture,
                    onclause=(
                        (Capture.owner_id == ParsedArtifact.owner_id)
                        & (Capture.id == ParsedArtifact.capture_id)
                    ),
                )
                .where(
                    ParsedArtifact.owner_id == self.owner_id,
                    Capture.document_id == document_id,
                    ParsedArtifact.capture_id != exclude_capture_id,
                    ParsedArtifact.parse_status != "failed",
                )
                .order_by(desc(ParsedArtifact.parsed_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else _parse_record(row)

    async def insert_document_diff(
        self, record: DocumentDiffRecord
    ) -> tuple[DocumentDiffRecord, bool]:
        """UNIQUE(from, to, algorithm): replaying the same diff is a
        no-op that binds the existing row (03 §3)."""
        await self._bind_owner()
        stmt = (
            pg_insert(DocumentDiff)
            .values(
                id=record.id,
                owner_id=record.owner_id,
                from_parse_id=record.from_parse_id,
                to_parse_id=record.to_parse_id,
                diff_algorithm_version=record.diff_algorithm_version,
                kind=record.kind,
                changed_blocks=record.changed_blocks,
                field_changes=record.field_changes,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "from_parse_id",
                    "to_parse_id",
                    "diff_algorithm_version",
                ]
            )
            .returning(DocumentDiff.id)
        )
        inserted = (await self.conn.execute(stmt)).scalar_one_or_none()
        return record, inserted is not None


def _item_record(row: DiscoveryItem) -> DiscoveryItemRecord:
    return DiscoveryItemRecord(
        id=row.id,
        owner_id=row.owner_id,
        feed_id=row.feed_id,
        run_id=row.run_id,
        origin_kind=row.origin_kind,
        target_industry_id=row.target_industry_id,
        discovered_url=row.discovered_url,
        canonical_url=row.canonical_url,
        title_hint=row.title_hint,
        published_hint=row.published_hint,
        first_seen_at=row.first_seen_at,
        state=row.state,
        error_code=row.error_code,
    )


def _blob_record(row: Blob) -> BlobRecord:
    return BlobRecord(
        id=row.id,
        owner_id=row.owner_id,
        object_key=row.object_key,
        sha256=row.sha256,
        media_type=row.media_type,
        byte_size=row.byte_size,
        retention_class=row.retention_class,
    )


def _document_record(row: Document) -> DocumentRecord:
    return DocumentRecord(
        id=row.id,
        owner_id=row.owner_id,
        canonical_url=row.canonical_url,
        identity_namespace=row.identity_namespace,
        identity_value=row.identity_value,
        visibility_scope_key=row.visibility_scope_key,
        origin_kind=row.origin_kind,
        target_industry_id=row.target_industry_id,
        current_capture_id=row.current_capture_id,
        first_seen_at=row.first_seen_at,
    )


def _capture_record(row: Capture) -> CaptureRecord:
    return CaptureRecord(
        id=row.id,
        owner_id=row.owner_id,
        document_id=row.document_id,
        raw_blob_id=row.raw_blob_id,
        response_status=row.response_status,
        fetched_at=row.fetched_at,
        effective_url=row.effective_url,
        content_hash=row.content_hash,
        etag=row.etag,
        last_modified=row.last_modified,
        content_type=row.content_type,
        retrieval_scope=row.retrieval_scope,
        access_policy=row.access_policy,
    )


def _parse_record(row: ParsedArtifact) -> ParsedArtifactRecord:
    return ParsedArtifactRecord(
        id=row.id,
        owner_id=row.owner_id,
        capture_id=row.capture_id,
        parser_version_id=row.parser_version_id,
        normalized_blob_id=row.normalized_blob_id,
        text_hash=row.text_hash,
        blocks=list(row.blocks or []),
        artifact_metadata=dict(row.artifact_metadata or {}),
        parse_status=row.parse_status,
        coverage=dict(row.coverage or {}),
        quality_flags=list(row.quality_flags or []),
        parsed_at=row.parsed_at,
    )


# --------------------------------------------------------------------------
# in-memory adapter (unit tests)
# --------------------------------------------------------------------------


class InMemoryPoolDatabase:
    """Row state with snapshot/rollback for one-transaction tests."""

    def __init__(self) -> None:
        self.feeds: dict[UUID, dict[str, Any]] = {}
        self.items: dict[UUID, dict[str, Any]] = {}
        self.item_index: dict[tuple[UUID, UUID | None, str], UUID] = {}
        self.runs: dict[UUID, dict[str, Any]] = {}
        self.blobs: dict[UUID, dict[str, Any]] = {}
        self.documents: dict[UUID, dict[str, Any]] = {}
        self.document_index: dict[tuple[UUID, str, str, str], UUID] = {}
        self.captures: dict[UUID, dict[str, Any]] = {}
        self.observations: dict[UUID, dict[str, Any]] = {}
        self.origins: dict[UUID, dict[str, Any]] = {}
        self.parses: dict[UUID, dict[str, Any]] = {}
        self.parse_index: dict[tuple[UUID, UUID], UUID] = {}
        self.diffs: dict[UUID, dict[str, Any]] = {}
        self.diff_index: dict[tuple[UUID, UUID, str], UUID] = {}

    _TABLES = (
        "feeds",
        "items",
        "item_index",
        "runs",
        "blobs",
        "documents",
        "document_index",
        "captures",
        "observations",
        "origins",
        "parses",
        "parse_index",
        "diffs",
        "diff_index",
    )

    def snapshot(self) -> dict[str, Any]:
        """Deep copy of every table — the start of a simulated transaction."""
        return {name: copy.deepcopy(getattr(self, name)) for name in self._TABLES}

    def rollback(self, snapshot: dict[str, Any]) -> None:
        for name in self._TABLES:
            setattr(self, name, snapshot[name])


class InMemoryPoolStore:
    """PoolRepository over :class:`InMemoryPoolDatabase`, scoped."""

    def __init__(self, db: InMemoryPoolDatabase, scope: IndustryScope) -> None:
        self.db = db
        self.scope = scope

    @property
    def owner_id(self) -> UUID:
        return self.scope.owner_id

    async def load_feed_state(self, feed_id: UUID) -> FeedDiscoveryState | None:
        row = self.db.feeds.get(feed_id)
        if row is None or row["owner_id"] != self.owner_id:
            return None
        return FeedDiscoveryState(
            feed_id=row["id"],
            owner_id=row["owner_id"],
            seed_url=row["seed_url"],
            adapter_type=row["adapter_type"],
            config=dict(row.get("config", {})),
            access_scope_key=row.get("access_scope_key", "public"),
            cursor=row.get("discovery_cursor"),
            cursor_version=row.get("cursor_version", 1),
            user_enabled=row.get("user_enabled", True),
            status=row.get("status", "active"),
            parser_version_id=row.get("parser_version_id"),
        )

    async def save_cursor(self, feed_id: UUID, cursor: str | None) -> bool:
        row = self.db.feeds.get(feed_id)
        if row is None or row["owner_id"] != self.owner_id:
            return False
        row["discovery_cursor"] = cursor
        row["cursor_version"] = row.get("cursor_version", 1) + 1
        return True

    async def upsert_discovery_item(
        self, item: DiscoveryItemRecord
    ) -> tuple[DiscoveryItemRecord, bool]:
        key = (self.owner_id, item.feed_id, item.canonical_url)
        existing_id = self.db.item_index.get(key)
        if existing_id is not None:
            return _mem_item(self.db.items[existing_id]), False
        row = {
            "id": item.id,
            "owner_id": self.owner_id,
            "feed_id": item.feed_id,
            "run_id": item.run_id,
            "origin_kind": item.origin_kind,
            "target_industry_id": item.target_industry_id,
            "discovered_url": item.discovered_url,
            "canonical_url": item.canonical_url,
            "title_hint": item.title_hint,
            "published_hint": item.published_hint,
            "first_seen_at": item.first_seen_at,
            "state": item.state,
            "error_code": item.error_code,
        }
        self.db.items[item.id] = row
        self.db.item_index[key] = item.id
        return _mem_item(row), True

    async def insert_source_run(self, run: SourceRunRecord) -> SourceRunRecord:
        self.db.runs[run.id] = {
            "id": run.id,
            "owner_id": run.owner_id,
            "feed_id": run.feed_id,
            "job_id": run.job_id,
            "started_at": run.started_at,
            "finished_at": None,
            "outcome": "partial",
            "discovered_count": 0,
            "cursor_before": run.cursor_before,
            "cursor_after": None,
        }
        return run

    async def finish_source_run(
        self,
        run_id: UUID,
        *,
        outcome: str,
        cursor_after: str | None,
        discovered_count: int,
        coverage_end: datetime | None = None,
        error_code: str | None = None,
    ) -> None:
        row = self.db.runs.get(run_id)
        if row is not None:
            row.update(
                finished_at=datetime.now(UTC),
                outcome=outcome,
                cursor_after=cursor_after,
                discovered_count=discovered_count,
                coverage_end=coverage_end,
                error_code=error_code,
            )

    async def get_discovery_item(
        self, item_id: UUID
    ) -> DiscoveryItemRecord | None:
        row = self.db.items.get(item_id)
        if row is None or row["owner_id"] != self.owner_id:
            return None
        return _mem_item(row)

    async def set_discovery_state(
        self, item_id: UUID, state: str, *, error_code: str | None = None
    ) -> None:
        row = self.db.items.get(item_id)
        if row is not None and row["owner_id"] == self.owner_id:
            row["state"] = state
            row["error_code"] = error_code

    async def latest_capture_for_item(self, item_id: UUID) -> CaptureRecord | None:
        document_ids = [
            origin["document_id"]
            for origin in self.db.origins.values()
            if origin["discovery_item_id"] == item_id
            and origin["owner_id"] == self.owner_id
        ]
        captures = sorted(
            (
                row
                for row in self.db.captures.values()
                if row["owner_id"] == self.owner_id
                and row["document_id"] in document_ids
            ),
            key=lambda r: r["fetched_at"],
        )
        return _mem_capture(captures[-1]) if captures else None

    async def find_blob(self, sha256: str, media_type: str) -> BlobRecord | None:
        for row in self.db.blobs.values():
            if (
                row["owner_id"] == self.owner_id
                and row["sha256"] == sha256
                and row["media_type"] == media_type
            ):
                return _mem_blob(row)
        return None

    async def insert_blob(self, blob: BlobRecord) -> BlobRecord:
        self.db.blobs[blob.id] = {
            "id": blob.id,
            "owner_id": blob.owner_id,
            "object_key": blob.object_key,
            "sha256": blob.sha256,
            "media_type": blob.media_type,
            "byte_size": blob.byte_size,
            "retention_class": blob.retention_class,
        }
        return blob

    async def find_document(
        self, visibility_scope_key: str, identity_namespace: str, identity_value: str
    ) -> DocumentRecord | None:
        key = (
            self.owner_id,
            visibility_scope_key,
            identity_namespace,
            identity_value,
        )
        document_id = self.db.document_index.get(key)
        if document_id is None:
            return None
        return _mem_document(self.db.documents[document_id])

    async def insert_document(self, document: DocumentRecord) -> DocumentRecord:
        self.db.documents[document.id] = {
            "id": document.id,
            "owner_id": document.owner_id,
            "canonical_url": document.canonical_url,
            "identity_namespace": document.identity_namespace,
            "identity_value": document.identity_value,
            "visibility_scope_key": document.visibility_scope_key,
            "origin_kind": document.origin_kind,
            "target_industry_id": document.target_industry_id,
            "current_capture_id": None,
            "first_seen_at": document.first_seen_at,
        }
        self.db.document_index[
            (
                document.owner_id,
                document.visibility_scope_key,
                document.identity_namespace,
                document.identity_value,
            )
        ] = document.id
        return document

    async def insert_capture(self, capture: CaptureRecord) -> CaptureRecord:
        self.db.captures[capture.id] = {
            "id": capture.id,
            "owner_id": capture.owner_id,
            "document_id": capture.document_id,
            "raw_blob_id": capture.raw_blob_id,
            "response_status": capture.response_status,
            "fetched_at": capture.fetched_at,
            "effective_url": capture.effective_url,
            "content_hash": capture.content_hash,
            "etag": capture.etag,
            "last_modified": capture.last_modified,
            "content_type": capture.content_type,
            "retrieval_scope": capture.retrieval_scope,
            "access_policy": capture.access_policy,
        }
        return capture

    async def set_current_capture(
        self, document_id: UUID, capture_id: UUID
    ) -> None:
        row = self.db.documents.get(document_id)
        if row is not None and row["owner_id"] == self.owner_id:
            row["current_capture_id"] = capture_id

    async def insert_observation(
        self, observation: FetchObservationRecord
    ) -> FetchObservationRecord:
        self.db.observations[observation.id] = {
            "id": observation.id,
            "owner_id": observation.owner_id,
            "document_id": observation.document_id,
            "discovery_item_id": observation.discovery_item_id,
            "capture_id": observation.capture_id,
            "fetched_at": observation.fetched_at,
            "status_code": observation.status_code,
            "outcome": observation.outcome,
            "etag": observation.etag,
            "error_code": observation.error_code,
        }
        return observation

    async def insert_origin(
        self, *, document_id: UUID, discovery_item_id: UUID, feed_id: UUID | None
    ) -> None:
        """Link a discovery item to its document (deduped)."""
        for origin in self.db.origins.values():
            if (
                origin["owner_id"] == self.owner_id
                and origin["document_id"] == document_id
                and origin["discovery_item_id"] == discovery_item_id
            ):
                return
        self.db.origins[uuid4()] = {
            "owner_id": self.owner_id,
            "document_id": document_id,
            "discovery_item_id": discovery_item_id,
            "feed_id": feed_id,
        }

    async def get_capture(self, capture_id: UUID) -> CaptureRecord | None:
        row = self.db.captures.get(capture_id)
        if row is None or row["owner_id"] != self.owner_id:
            return None
        return _mem_capture(row)

    async def get_blob(self, blob_id: UUID) -> BlobRecord | None:
        row = self.db.blobs.get(blob_id)
        if row is None or row["owner_id"] != self.owner_id:
            return None
        return _mem_blob(row)

    async def set_capture_retrieval_scope(
        self, capture_id: UUID, retrieval_scope: str
    ) -> None:
        row = self.db.captures.get(capture_id)
        if row is not None and row["owner_id"] == self.owner_id:
            row["retrieval_scope"] = retrieval_scope

    async def insert_parsed_artifact(
        self, record: ParsedArtifactRecord
    ) -> tuple[ParsedArtifactRecord, bool]:
        key = (record.capture_id, record.parser_version_id)
        existing_id = self.db.parse_index.get(key)
        if existing_id is not None:
            return _mem_parse(self.db.parses[existing_id]), False
        row = {
            "id": record.id,
            "owner_id": self.owner_id,
            "capture_id": record.capture_id,
            "parser_version_id": record.parser_version_id,
            "normalized_blob_id": record.normalized_blob_id,
            "text_hash": record.text_hash,
            "blocks": copy.deepcopy(record.blocks),
            "artifact_metadata": copy.deepcopy(record.artifact_metadata),
            "parse_status": record.parse_status,
            "coverage": copy.deepcopy(record.coverage),
            "quality_flags": list(record.quality_flags),
            "parsed_at": record.parsed_at,
        }
        self.db.parses[record.id] = row
        self.db.parse_index[key] = record.id
        return _mem_parse(row), True

    async def list_parses_for_capture(
        self, capture_id: UUID
    ) -> list[ParsedArtifactRecord]:
        rows = [
            row
            for row in self.db.parses.values()
            if row["owner_id"] == self.owner_id
            and row["capture_id"] == capture_id
        ]
        rows.sort(key=lambda r: r["parsed_at"])
        return [_mem_parse(row) for row in rows]

    async def latest_parse_for_document(
        self, document_id: UUID, *, exclude_capture_id: UUID
    ) -> ParsedArtifactRecord | None:
        capture_ids = {
            capture_id
            for capture_id, row in self.db.captures.items()
            if row["owner_id"] == self.owner_id
            and row["document_id"] == document_id
            and capture_id != exclude_capture_id
        }
        candidates = [
            row
            for row in self.db.parses.values()
            if row["owner_id"] == self.owner_id
            and row["capture_id"] in capture_ids
            and row["parse_status"] != "failed"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r["parsed_at"])
        return _mem_parse(candidates[-1])

    async def insert_document_diff(
        self, record: DocumentDiffRecord
    ) -> tuple[DocumentDiffRecord, bool]:
        key = (
            record.from_parse_id,
            record.to_parse_id,
            record.diff_algorithm_version,
        )
        existing_id = self.db.diff_index.get(key)
        if existing_id is not None:
            return _mem_diff(self.db.diffs[existing_id]), False
        row = {
            "id": record.id,
            "owner_id": self.owner_id,
            "from_parse_id": record.from_parse_id,
            "to_parse_id": record.to_parse_id,
            "diff_algorithm_version": record.diff_algorithm_version,
            "kind": record.kind,
            "changed_blocks": copy.deepcopy(record.changed_blocks),
            "field_changes": copy.deepcopy(record.field_changes),
        }
        self.db.diffs[record.id] = row
        self.db.diff_index[key] = record.id
        return _mem_diff(row), True


def _mem_item(row: Mapping[str, Any]) -> DiscoveryItemRecord:
    return DiscoveryItemRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        feed_id=row["feed_id"],
        run_id=row["run_id"],
        origin_kind=row["origin_kind"],
        target_industry_id=row["target_industry_id"],
        discovered_url=row["discovered_url"],
        canonical_url=row["canonical_url"],
        title_hint=row["title_hint"],
        published_hint=row["published_hint"],
        first_seen_at=row["first_seen_at"],
        state=row["state"],
        error_code=row["error_code"],
    )


def _mem_blob(row: Mapping[str, Any]) -> BlobRecord:
    return BlobRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        object_key=row["object_key"],
        sha256=row["sha256"],
        media_type=row["media_type"],
        byte_size=row["byte_size"],
        retention_class=row["retention_class"],
    )


def _mem_document(row: Mapping[str, Any]) -> DocumentRecord:
    return DocumentRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        canonical_url=row["canonical_url"],
        identity_namespace=row["identity_namespace"],
        identity_value=row["identity_value"],
        visibility_scope_key=row["visibility_scope_key"],
        origin_kind=row["origin_kind"],
        target_industry_id=row["target_industry_id"],
        current_capture_id=row["current_capture_id"],
        first_seen_at=row["first_seen_at"],
    )


def _mem_capture(row: Mapping[str, Any]) -> CaptureRecord:
    return CaptureRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        document_id=row["document_id"],
        raw_blob_id=row["raw_blob_id"],
        response_status=row["response_status"],
        fetched_at=row["fetched_at"],
        effective_url=row["effective_url"],
        content_hash=row["content_hash"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        content_type=row["content_type"],
        retrieval_scope=row["retrieval_scope"],
        access_policy=row["access_policy"],
    )


def _mem_parse(row: Mapping[str, Any]) -> ParsedArtifactRecord:
    return ParsedArtifactRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        capture_id=row["capture_id"],
        parser_version_id=row["parser_version_id"],
        normalized_blob_id=row["normalized_blob_id"],
        text_hash=row["text_hash"],
        blocks=copy.deepcopy(row["blocks"]),
        artifact_metadata=copy.deepcopy(row["artifact_metadata"]),
        parse_status=row["parse_status"],
        coverage=copy.deepcopy(row["coverage"]),
        quality_flags=list(row["quality_flags"]),
        parsed_at=row["parsed_at"],
    )


def _mem_diff(row: Mapping[str, Any]) -> DocumentDiffRecord:
    return DocumentDiffRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        from_parse_id=row["from_parse_id"],
        to_parse_id=row["to_parse_id"],
        diff_algorithm_version=row["diff_algorithm_version"],
        kind=row["kind"],
        changed_blocks=copy.deepcopy(row["changed_blocks"]),
        field_changes=copy.deepcopy(row["field_changes"]),
    )


def seed_feed(
    db: InMemoryPoolDatabase,
    *,
    owner_id: UUID,
    adapter_type: str = "rss",
    seed_url: str = "https://example.com/feed.xml",
    config: dict | None = None,
    access_scope_key: str = "public",
) -> UUID:
    """Insert a feed row for discovery tests; returns its id."""
    feed_id = uuid4()
    db.feeds[feed_id] = {
        "id": feed_id,
        "owner_id": owner_id,
        "seed_url": seed_url,
        "adapter_type": adapter_type,
        "config": dict(config or {}),
        "access_scope_key": access_scope_key,
        "discovery_cursor": None,
        "cursor_version": 1,
        "user_enabled": True,
        "status": "active",
        "parser_version_id": uuid4(),
    }
    return feed_id
