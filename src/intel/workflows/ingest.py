"""Ingest workflow handlers: discover + fetch + parse (spec 04 §2-§4, 07 §6).

Registered on the :class:`~intel.workers.runner.JobRunner`:

- ``discover`` (and ``source_poll`` — the API's poll kind maps to the
  same discover semantics, Task 6 ruling): page through the adapter,
  and for EACH page commit ONE transaction that writes the candidates,
  spawns fetch jobs for new items, and saves the cursor (04 §3 同一事务;
  the transaction never spans a network call).
- ``fetch``: guarded conditional fetch with the browser fallback branch,
  then one commit transaction persisting blob/document/capture/
  fetch_observation — 304 and unchanged-200 bind the prior capture
  (ING-04). Scope/auth violations write an audit_log row in the same
  transaction as the failure evidence, then fail closed (07 §6 落点,
  this task's first real anchor). A persisted capture spawns its parse
  job in the same commit (04 §4).
- ``parse``: read the capture blob back from the object store, run the
  pure :class:`~intel.parsing.Parser` (no DB access, 14 §2), then one
  commit transaction persisting the normalized blob + parsed_artifact
  row (UNIQUE(capture, parser_version) keeps both parses of a capture),
  document_diffs against earlier parses (PAR-01), the refined capture
  retrieval_scope, and the follow-up index job. A parse that yields
  parse_status=failed is still persisted evidence — the JOB succeeds
  with the failure recorded in progress (PAR-03: 不强行生成全文分析,
  and no retry loop on permanently broken content).

Politeness (04 §2): per-domain semaphore (2) + 2s minimum interval;
429 honors Retry-After through the existing transient classification.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from intel.db.rls import set_app_role
from intel.domain.urlnorm import normalize_url
from intel.parsing import (
    BLOCK_DIFF_ALGORITHM,
    BUILTIN_PARSER_VERSION_ID,
    Block,
    Parser,
    ParserInput,
    diff_blocks,
)
from intel.parsing import (
    ParsedArtifact as ParsedArtifactDTO,
)
from intel.repositories.audit import AuditWriter, SqlAlchemyAuditWriter
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import JobsStore, SqlAlchemyJobsStore
from intel.repositories.pool import (
    BlobRecord,
    CaptureRecord,
    DiscoveryItemRecord,
    DocumentDiffRecord,
    DocumentRecord,
    FetchObservationRecord,
    ParsedArtifactRecord,
    PoolRepository,
    SourceRunRecord,
    SqlAlchemyPoolRepository,
)
from intel.retrieval.chunker import CHUNKER_VERSION
from intel.retrieval.indexer import NOOP_EMBEDDING_VERSION
from intel.services.errors import ValidationFailed
from intel.services.jobs import JobService, build_idempotency_key
from intel.sources.adapters import make_adapter, validate_adapter_config
from intel.sources.blobstore import ObjectStore
from intel.sources.dto import CaptureResult, FeedPlan, FetchRequest
from intel.sources.fetcher import format_http_date
from intel.sources.pageclient import PageBodyTooLarge, PageClient, PageFetchError
from intel.sources.politeness import PolitenessGate
from intel.sources.ssrf import UrlBlockedError
from intel.workers.runner import JobFailure, JobHandler, RunContext


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class IngestTxn:
    """One short transaction's worth of stores (07 §2).

    Pool rows, spawned jobs, audit rows and object writes all belong to
    the same commit; the object store is attached here because blob
    bytes and blob rows must agree.
    """

    pool: PoolRepository
    jobs: JobsStore
    audit: AuditWriter
    object_store: ObjectStore | None = None


OpenIngestTxn = Callable[
    [IndustryScope], AbstractAsyncContextManager[IngestTxn]
]


class FetcherProtocol(Protocol):
    async def fetch(self, request: FetchRequest) -> CaptureResult: ...


@dataclass(slots=True)
class IngestWiring:
    """Everything the handlers need, injected by the composition root."""

    open_ingest: OpenIngestTxn
    page_client_factory: Callable[[], PageClient]
    fetcher_factory: Callable[[], FetcherProtocol]
    object_store: ObjectStore
    politeness: PolitenessGate
    #: The pure parser (no DB access); override for parser tests.
    parser: Parser = field(default_factory=Parser)
    #: Per-fetch budget; the job deadline caps it further.
    fetch_timeout_seconds: float = 120.0
    max_bytes: int = 10_000_000
    clock: Callable[[], datetime] = field(default_factory=_utcnow)


def register_ingest_handlers(runner, wiring: IngestWiring) -> None:
    """Attach discover/fetch/parse handlers; ``source_poll`` maps to
    discover (Task 6 ruling: the API's poll endpoint enqueues
    source_poll today)."""
    discover = make_discover_handler(wiring)
    runner.register("discover", discover)
    runner.register("source_poll", discover)
    runner.register("fetch", make_fetch_handler(wiring))
    runner.register("parse", make_parse_handler(wiring))


def sql_ingest_txn_factory(
    engine: AsyncEngine, *, object_store: ObjectStore
) -> OpenIngestTxn:
    """Production wiring: one connection/transaction per open, all three
    stores plus the object store bound to the same commit."""

    @asynccontextmanager
    async def open_ingest(scope: IndustryScope):
        async with engine.connect() as conn, conn.begin():
            await set_app_role(conn)
            yield IngestTxn(
                pool=SqlAlchemyPoolRepository(conn, scope),
                jobs=SqlAlchemyJobsStore(conn, scope),
                audit=SqlAlchemyAuditWriter(conn, scope),
                object_store=object_store,
            )

    return open_ingest


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------


def _schedule_epoch(payload: dict) -> str:
    for key in ("refresh_epoch", "schedule_slot"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return "0"


async def _audit(
    txn: IngestTxn,
    ctx: RunContext,
    action: str,
    details: dict,
    *,
    target_id: UUID,
    target_type: str = "job",
) -> None:
    await txn.audit.write(
        action=action,
        actor_type="job",
        actor_id=ctx.job.id,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )


def _page_failure(exc: PageFetchError) -> JobFailure:
    if isinstance(exc, PageBodyTooLarge):
        # Non-retryable: the same page will still exceed max_bytes.
        return JobFailure("page_body_too_large", str(exc))
    if exc.status == 429:
        return JobFailure(
            "transient", "feed page rate limited", retry_after=exc.retry_after
        )
    if exc.status in (401, 403):
        return JobFailure("access_blocked", f"feed page HTTP {exc.status}")
    if exc.status >= 500 or exc.status < 0:
        return JobFailure("transient", f"feed page HTTP {exc.status}")
    return JobFailure(f"page_http_{exc.status}", f"feed page HTTP {exc.status}")


def make_discover_handler(wiring: IngestWiring) -> JobHandler:
    jobs_service = JobService(clock=wiring.clock)

    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        feed_id = UUID(str(payload["feed_id"]))

        async with wiring.open_ingest(ctx.scope) as txn:
            feed = await txn.pool.load_feed_state(feed_id)
        if feed is None:
            raise JobFailure("feed_missing", f"feed {feed_id} not found")
        if not feed.user_enabled or feed.status != "active":
            await _finish(ctx, progress={"skipped": "feed not pollable"})
            return

        try:
            config = validate_adapter_config(feed.adapter_type, feed.config)
        except ValidationFailed as exc:
            raise JobFailure(
                "adapter_config_invalid", exc.message, details=exc.details
            ) from exc

        plan = FeedPlan(
            feed_id=feed.feed_id,
            owner_id=feed.owner_id,
            seed=feed.seed_url,
            adapter=feed.adapter_type,
            config_version=feed.cursor_version,
            config=config,
            cursor=feed.cursor,
        )
        adapter = make_adapter(feed.adapter_type, wiring.page_client_factory())

        # page_monitor re-fetches its (single) item every poll with a
        # fresh epoch (spec 14 §7 page_monitor 按自己的频率); feed items
        # fetch once per discovery.
        monitor = feed.adapter_type == "page_monitor"
        epoch = _schedule_epoch(payload) if monitor else "0"

        run: SourceRunRecord | None = None
        cursor = feed.cursor
        total_new = 0
        total_items = 0
        pages = 0
        warnings: list[str] = []
        finished_exhausted = False

        while pages < plan.limits.max_pages and total_items < plan.limits.max_items:
            try:
                page = await adapter.discover(plan, cursor)
            except PageFetchError as exc:
                raise _page_failure(exc) from exc
            except UrlBlockedError as exc:
                async with wiring.open_ingest(ctx.scope) as txn:
                    await _audit(
                        txn,
                        ctx,
                        "discover_ssrf_blocked",
                        {"url": exc.url, "reason": exc.reason},
                        target_id=ctx.job.id,
                    )
                raise JobFailure("scope_violation", str(exc)) from exc
            pages += 1
            warnings.extend(page.warnings)

            new_items: list[DiscoveryItemRecord] = []
            async with wiring.open_ingest(ctx.scope) as txn:  # 04 §3 同一事务
                if run is None:
                    run = await txn.pool.insert_source_run(
                        SourceRunRecord(
                            owner_id=ctx.job.owner_id,
                            feed_id=feed.feed_id,
                            job_id=ctx.job.id,
                            cursor_before=feed.cursor,
                        )
                    )
                for hint in page.items:
                    try:
                        canonical = normalize_url(hint.url)
                    except ValueError:
                        warnings.append(f"invalid candidate URL: {hint.url!r}")
                        continue
                    record, created = await txn.pool.upsert_discovery_item(
                        DiscoveryItemRecord(
                            owner_id=ctx.job.owner_id,
                            feed_id=feed.feed_id,
                            run_id=run.id,
                            origin_kind=feed.adapter_type,
                            discovered_url=hint.url,
                            canonical_url=canonical,
                            title_hint=hint.title_hint,
                            published_hint=hint.date_hint,
                        )
                    )
                    total_items += 1
                    if created or monitor:
                        new_items.append(record)
                for record in new_items:
                    await jobs_service.enqueue(
                        txn.jobs,
                        ctx.scope,
                        kind="fetch",
                        payload={
                            "discovery_item_id": str(record.id),
                            "feed_id": str(feed.feed_id),
                            "url": record.discovered_url,
                            "visibility_scope_key": feed.access_scope_key,
                        },
                        idempotency_key=build_idempotency_key(
                            "fetch",
                            {
                                "owner": str(ctx.job.owner_id),
                                "discovery_item": str(record.id),
                                "refresh_epoch": epoch,
                            },
                        ),
                    )
                await txn.pool.save_cursor(feed.feed_id, page.next_cursor)
            total_new += len(new_items)
            if page.exhausted or page.next_cursor is None:
                finished_exhausted = page.exhausted
                break
            cursor = page.next_cursor
            await ctx.boundary()

        outcome = "partial"
        if finished_exhausted:
            outcome = "no_change" if (pages == 1 and total_new == 0) else "success"
        if run is not None:
            async with wiring.open_ingest(ctx.scope) as txn:
                await txn.pool.finish_source_run(
                    run.id,
                    outcome=outcome,
                    cursor_after=cursor,
                    discovered_count=total_new,
                    coverage_end=wiring.clock(),
                )
        await _finish(
            ctx,
            progress={
                "pages": pages,
                "items": total_items,
                "new_items": total_new,
                "cursor": cursor,
                "warnings": warnings[:20],
            },
        )

    return handler


async def _finish(ctx: RunContext, *, progress: dict) -> None:
    async with ctx.open_store(ctx.scope) as store:
        await ctx.service.finish(store, ctx.job, state="succeeded", progress=progress)


async def _finish_partial(ctx: RunContext, *, gaps: list[str]) -> None:
    async with ctx.open_store(ctx.scope) as store:
        await ctx.service.finish(
            store, ctx.job, state="partial", error={"gaps": gaps, "code": gaps[0]}
        )


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _failure_for(result: CaptureResult) -> JobFailure | None:
    if result.outcome == "error":
        if result.error_code == "http_429":
            return JobFailure(
                "transient", "rate limited", retry_after=result.retry_after
            )
        if (result.status or 0) >= 500 or result.error_code in (
            "timeout",
            "network",
            "redirect_limit",
        ):
            return JobFailure("transient", f"fetch error: {result.error_code}")
        return JobFailure(
            f"fetch_{result.error_code or 'error'}",
            f"fetch error: {result.error_code}",
        )
    if result.outcome == "access_denied":
        return JobFailure("access_blocked", result.error_code or "access_denied")
    return None


def make_fetch_handler(wiring: IngestWiring) -> JobHandler:
    jobs_service = JobService(clock=wiring.clock)

    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        item_id = UUID(str(payload["discovery_item_id"]))
        visibility = str(payload.get("visibility_scope_key", "public"))

        async with wiring.open_ingest(ctx.scope) as txn:
            item = await txn.pool.get_discovery_item(item_id)
            prior = await txn.pool.latest_capture_for_item(item_id)
        if item is None:
            raise JobFailure("fetch_item_missing", f"discovery item {item_id} gone")

        conditional: dict[str, str] = {}
        if prior is not None:
            if prior.etag:
                conditional["If-None-Match"] = prior.etag
            if prior.last_modified is not None:
                conditional["If-Modified-Since"] = format_http_date(
                    prior.last_modified
                )

        deadline_at = min(
            ctx.deadline_at,
            wiring.clock() + timedelta(seconds=wiring.fetch_timeout_seconds),
        )
        request = FetchRequest(
            item_id=item_id,
            url=item.discovered_url,
            conditional_headers=conditional,
            visibility_scope_key=visibility,
            max_bytes=wiring.max_bytes,
            deadline_at=deadline_at,
        )
        host = urlsplit(request.url).hostname or ""

        try:
            async with wiring.politeness.request(host):
                result = await wiring.fetcher_factory().fetch(request)
        except UrlBlockedError as exc:
            async with wiring.open_ingest(ctx.scope) as txn:
                await txn.pool.set_discovery_state(
                    item_id, "failed", error_code="ssrf_blocked"
                )
                await txn.pool.insert_observation(
                    FetchObservationRecord(
                        owner_id=ctx.job.owner_id,
                        discovery_item_id=item_id,
                        outcome="error",
                        error_code="ssrf_blocked",
                    )
                )
                await _audit(
                    txn,
                    ctx,
                    "fetch_ssrf_blocked",
                    {"url": exc.url, "reason": exc.reason},
                    target_id=item_id,
                    target_type="discovery_item",
                )
            raise JobFailure("scope_violation", str(exc)) from exc

        async with wiring.open_ingest(ctx.scope) as txn:
            await _persist_capture(
                txn, ctx, item, prior, result, visibility, jobs_service
            )
        failure = _failure_for(result)
        if failure is not None:
            raise failure
        if result.outcome == "browser_unavailable":
            await _finish_partial(ctx, gaps=["browser_unavailable"])
            return
        await _finish(ctx, progress={"outcome": result.outcome, "url": result.final_url})

    return handler


async def _persist_capture(
    txn: IngestTxn,
    ctx: RunContext,
    item: DiscoveryItemRecord,
    prior: CaptureRecord | None,
    result: CaptureResult,
    visibility: str,
    jobs_service: JobService | None = None,
) -> None:
    """One commit transaction for one fetch outcome (ING-04 included)."""
    etag = result.headers.get("etag")
    observation = FetchObservationRecord(
        owner_id=ctx.job.owner_id,
        discovery_item_id=item.id,
        status_code=result.status,
        outcome=result.outcome,
        etag=etag,
        error_code=result.error_code,
        fetched_at=result.captured_at,
    )

    if result.outcome == "no_change":
        observation.capture_id = prior.id if prior is not None else None
        observation.document_id = prior.document_id if prior is not None else None
        await txn.pool.insert_observation(observation)
        return

    if result.outcome in ("error", "access_denied", "browser_unavailable"):
        await txn.pool.insert_observation(observation)
        if result.outcome == "access_denied":
            await txn.pool.set_discovery_state(
                item.id, "access_denied", error_code=result.error_code
            )
            # Scope/auth violations write their audit row in the same
            # transaction as the evidence (07 §6 落点).
            await _audit(
                txn,
                ctx,
                "fetch_access_denied",
                {
                    "status": result.status,
                    "error_code": result.error_code,
                    "access_flags": result.access_flags,
                    "final_url": result.final_url,
                },
                target_id=item.id,
                target_type="discovery_item",
            )
        elif result.outcome == "browser_unavailable":
            await txn.pool.set_discovery_state(
                item.id, "pending_js", error_code="browser_unavailable"
            )
        else:
            await txn.pool.set_discovery_state(
                item.id, "failed", error_code=result.error_code
            )
        return

    # captured — an unchanged 200 binds the prior capture (ING-04).
    if prior is not None and prior.content_hash == result.hash:
        observation.capture_id = prior.id
        observation.document_id = prior.document_id
        observation.outcome = "no_change"
        await txn.pool.insert_observation(observation)
        return

    body = result.body or b""
    media_type = result.media_type or "application/octet-stream"
    blob = await txn.pool.find_blob(str(result.hash), media_type)
    if blob is None:
        if txn.object_store is None:
            raise RuntimeError("IngestTxn has no object store attached")
        object_key = txn.object_store.write(body, media_type=media_type)
        blob = await txn.pool.insert_blob(
            BlobRecord(
                owner_id=ctx.job.owner_id,
                object_key=object_key,
                sha256=str(result.hash),
                media_type=media_type,
                byte_size=len(body),
            )
        )

    document = await txn.pool.find_document(visibility, "url", item.canonical_url)
    if document is None:
        document = await txn.pool.insert_document(
            DocumentRecord(
                owner_id=ctx.job.owner_id,
                canonical_url=item.canonical_url,
                identity_namespace="url",
                identity_value=item.canonical_url,
                visibility_scope_key=visibility,
                origin_kind=item.origin_kind,
            )
        )
    await txn.pool.insert_origin(
        document_id=document.id,
        discovery_item_id=item.id,
        feed_id=item.feed_id,
    )

    capture = await txn.pool.insert_capture(
        CaptureRecord(
            owner_id=ctx.job.owner_id,
            document_id=document.id,
            raw_blob_id=blob.id,
            response_status=result.status or 200,
            fetched_at=result.captured_at,
            effective_url=result.final_url or item.discovered_url,
            content_hash=str(result.hash),
            etag=etag,
            last_modified=_parse_http_date(result.headers.get("last-modified")),
            content_type=media_type,
            retrieval_scope="fulltext",
            access_policy=visibility,
        )
    )
    await txn.pool.set_current_capture(document.id, capture.id)
    observation.document_id = document.id
    observation.capture_id = capture.id
    await txn.pool.insert_observation(observation)
    await txn.pool.set_discovery_state(item.id, "captured")
    if jobs_service is not None:
        # Same commit spawns the capture's parse job (04 §4): the parser
        # version is the feed's pinned one when known, else the built-in
        # release (whose parser_versions row the release process seeds).
        parser_version_id = BUILTIN_PARSER_VERSION_ID
        if item.feed_id is not None:
            feed = await txn.pool.load_feed_state(item.feed_id)
            if feed is not None and feed.parser_version_id is not None:
                parser_version_id = feed.parser_version_id
        await jobs_service.enqueue(
            txn.jobs,
            ctx.scope,
            kind="parse",
            payload={
                "capture_id": str(capture.id),
                "parser_version_id": str(parser_version_id),
            },
            idempotency_key=build_idempotency_key(
                "parse",
                {
                    "owner": str(ctx.job.owner_id),
                    "capture": str(capture.id),
                    "parser_version": str(parser_version_id),
                },
            ),
        )


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

#: Retrieval-stack versions stamped into the index job's idempotency key —
#: the chunker that segments blocks and the embedding client the index
#: handler runs with (the noop client until Task 11 wires the L1 route;
#: either way the version rides the key, so a version bump re-indexes).
INDEX_CHUNKER_VERSION = CHUNKER_VERSION
INDEX_EMBEDDING_VERSION = NOOP_EMBEDDING_VERSION

#: Soft cap on flags echoed into job progress (observability only).
_MAX_PROGRESS_FLAGS = 20


def make_parse_handler(wiring: IngestWiring) -> JobHandler:
    """``parse`` kind: blob → pure parser → one commit transaction.

    The parser itself never touches the DB (14 §2); every persist lands
    in a single transaction: normalized blob, parsed_artifacts row,
    document_diffs, the capture's refined retrieval_scope and the index
    job. A parse_status=failed artifact is persisted evidence and the
    job still succeeds — only genuine execution errors raise
    ``parser_error`` for the one allowed retry (07 §6)."""

    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        capture_id = UUID(str(payload["capture_id"]))
        parser_version_id = UUID(
            str(payload.get("parser_version_id", BUILTIN_PARSER_VERSION_ID))
        )

        async with wiring.open_ingest(ctx.scope) as txn:
            capture = await txn.pool.get_capture(capture_id)
        if capture is None:
            raise JobFailure("capture_missing", f"capture {capture_id} gone")
        async with wiring.open_ingest(ctx.scope) as txn:
            blob = await txn.pool.get_blob(capture.raw_blob_id)
        if blob is None:
            raise JobFailure("blob_missing", f"blob for capture {capture_id}")

        try:
            raw = wiring.object_store.read(blob.object_key)
        except Exception as exc:
            raise JobFailure("blob_unreadable", str(exc)) from exc

        # Pure CPU work: no transaction held across the parse. A genuine
        # parser crash is the retryable parser_error class (07 §6: 同版本
        # 最多重试1次); failed *outcomes* (login page, garbage) are data.
        try:
            artifact = wiring.parser.parse(
                ParserInput(
                    capture_id=capture_id,
                    media_type=capture.content_type,
                    raw=raw,
                    # Same label scheme as rehydrated prior parses so
                    # same-parser diffs compare equal versions and the
                    # classifier's metadata-only branch is reachable.
                    parser_version=_parser_label(parser_version_id),
                )
            )
        except JobFailure:
            raise
        except Exception as exc:
            raise JobFailure("parser_error", repr(exc)) from exc
        await ctx.boundary()

        normalized = artifact.model_dump_json().encode()
        digest = hashlib.sha256(normalized).hexdigest()
        jobs_service = JobService(clock=wiring.clock)
        async with wiring.open_ingest(ctx.scope) as txn:
            if txn.object_store is None:  # pragma: no cover
                raise RuntimeError("IngestTxn has no object store attached")
            norm_blob = await txn.pool.find_blob(digest, "application/json")
            if norm_blob is None:
                object_key = txn.object_store.write(
                    normalized, media_type="application/json"
                )
                norm_blob = await txn.pool.insert_blob(
                    BlobRecord(
                        owner_id=ctx.job.owner_id,
                        object_key=object_key,
                        sha256=digest,
                        media_type="application/json",
                        byte_size=len(normalized),
                        retention_class="parsed",
                    )
                )
            record, created = await txn.pool.insert_parsed_artifact(
                ParsedArtifactRecord(
                    owner_id=ctx.job.owner_id,
                    capture_id=capture_id,
                    parser_version_id=parser_version_id,
                    normalized_blob_id=norm_blob.id,
                    text_hash=artifact.text_hash,
                    blocks=[block.model_dump() for block in artifact.blocks],
                    artifact_metadata=dict(artifact.metadata),
                    parse_status=artifact.parse_status,
                    coverage=dict(artifact.coverage),
                    quality_flags=list(artifact.quality_flags),
                )
            )
            if created:
                await _persist_diffs(txn, ctx, capture_id, record, artifact)
                if artifact.parse_status in ("ok", "partial"):
                    # The index handler (retrieval.indexer.register_index_
                    # handlers) is attached by the composition root; until
                    # that wiring lands the runner's default handler marks
                    # these succeeded with a note (Task 6 convention —
                    # observable queue).
                    await jobs_service.enqueue(
                        txn.jobs,
                        ctx.scope,
                        kind="index",
                        payload={
                            "parse_id": str(record.id),
                            "chunker_version": INDEX_CHUNKER_VERSION,
                            "embedding_version": INDEX_EMBEDDING_VERSION,
                        },
                        idempotency_key=build_idempotency_key(
                            "index",
                            {
                                "owner": str(ctx.job.owner_id),
                                "parse": str(record.id),
                                "chunker_version": INDEX_CHUNKER_VERSION,
                                "embedding_version": INDEX_EMBEDDING_VERSION,
                            },
                        ),
                    )
            await txn.pool.set_capture_retrieval_scope(
                capture_id, artifact.retrieval_scope
            )

        await _finish(
            ctx,
            progress={
                "parse_id": str(record.id),
                "parse_status": artifact.parse_status,
                "retrieval_scope": artifact.retrieval_scope,
                "blocks": len(artifact.blocks),
                "quality_flags": artifact.quality_flags[:_MAX_PROGRESS_FLAGS],
            },
        )

    return handler


async def _persist_diffs(
    txn: IngestTxn,
    ctx: RunContext,
    capture_id: UUID,
    record: ParsedArtifactRecord,
    artifact,
) -> None:
    """Diff the new parse against earlier parses and persist rows under
    UNIQUE(from, to, algorithm). Failed parses never pair — a login page
    must not mint a "content removed" event."""
    if artifact.parse_status == "failed":
        return
    candidates: list[ParsedArtifactRecord] = [
        row
        for row in await txn.pool.list_parses_for_capture(capture_id)
        if row.id != record.id and row.parse_status != "failed"
    ]
    capture = await txn.pool.get_capture(capture_id)
    if capture is not None:
        prior = await txn.pool.latest_parse_for_document(
            capture.document_id, exclude_capture_id=capture_id
        )
        if prior is not None and all(
            row.id != prior.id for row in candidates
        ):
            candidates.append(prior)

    for prior in candidates:
        prior_artifact = _artifact_from_record(prior)
        current_artifact = _artifact_from_record(record)
        # Direction is recency-based, not execution-order-based: with
        # concurrent workers the OLDER capture's parse can run last, and
        # from→to must still read old version → new version (consumers
        # and field_changes direction depend on it).
        prior_capture = await txn.pool.get_capture(prior.capture_id)
        prior_key = (
            prior_capture.fetched_at if prior_capture is not None else None,
            prior.parsed_at,
            prior.id,
        )
        current_key = (
            capture.fetched_at if capture is not None else None,
            record.parsed_at,
            record.id,
        )
        if prior_key <= current_key:
            from_artifact, to_artifact = prior_artifact, current_artifact
            from_id, to_id = prior.id, record.id
        else:
            from_artifact, to_artifact = current_artifact, prior_artifact
            from_id, to_id = record.id, prior.id
        result = diff_blocks(
            from_artifact, to_artifact, BLOCK_DIFF_ALGORITHM
        )
        await txn.pool.insert_document_diff(
            DocumentDiffRecord(
                owner_id=ctx.job.owner_id,
                from_parse_id=from_id,
                to_parse_id=to_id,
                diff_algorithm_version=result.algorithm_version,
                kind=result.kind,
                changed_blocks=list(result.changed_blocks),
                field_changes=list(result.field_changes),
            )
        )


def _parser_label(parser_version_id: UUID) -> str:
    """Stable comparison label for one parser_versions row. Used for BOTH
    the freshly parsed artifact and rehydrated prior parses, so
    same-parser diffs compare equal versions (PAR-01 classification
    depends on version identity, not just text equality)."""
    return f"parser:{parser_version_id}"


def _artifact_from_record(record: ParsedArtifactRecord):
    """Rehydrate a comparison artifact from the stored row (parser label
    keyed by the parser_versions id — enough identity for diff
    classification)."""
    return ParsedArtifactDTO(
        capture_id=record.capture_id,
        parser_version=_parser_label(record.parser_version_id),
        metadata=dict(record.artifact_metadata),
        blocks=[Block(**block) for block in record.blocks],
        coverage=dict(record.coverage),
        parse_status=record.parse_status,
        quality_flags=list(record.quality_flags),
        text_hash=record.text_hash,
    )
