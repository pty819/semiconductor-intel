"""Ingest workflow handler tests (spec 04 §3: one transaction per page,
cursor protocol, ING-01/03/04; 07 §6 audit on scope/auth violations).

Offline: fake page clients serve fixtures, httpx.MockTransport serves
fetch responses, in-memory stores provide the one-transaction semantics.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import random
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from uuid import UUID, uuid4

import httpx

from intel.repositories.audit import InMemoryAuditWriter
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.repositories.pool import (
    BlobRecord,
    CaptureRecord,
    DiscoveryItemRecord,
    DocumentRecord,
    InMemoryPoolDatabase,
    InMemoryPoolStore,
    seed_feed,
)
from intel.services.jobs import JobService
from intel.sources.adapters.cursor import decode_cursor
from intel.sources.blobstore import MemoryObjectStore
from intel.sources.fetcher import HttpFetcher
from intel.sources.pageclient import PageResponse
from intel.sources.politeness import PolitenessGate
from intel.sources.ssrf import UrlGuard
from intel.workers.runner import JobRunner
from intel.workflows.ingest import IngestTxn, IngestWiring, register_ingest_handlers

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SEED = "https://news.example.com/feed.xml"
PAGE2 = "https://news.example.com/feed.xml?page=2"
OWNER = uuid4()


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class StaticResolver:
    async def resolve(self, host: str) -> list[str]:
        return ["93.184.216.34"]


class FakePageClient:
    def __init__(self, pages: Mapping[str, PageResponse | Exception]) -> None:
        self.pages = dict(pages)
        self.hits: list[str] = []

    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> PageResponse:
        self.hits.append(url)
        target = self.pages[url]
        if isinstance(target, Exception):
            raise target
        return target


class FlakyJobsStore:
    """Fails insert_job once armed — proves the page transaction rolls
    back candidates, jobs and cursor together."""

    def __init__(self, inner: InMemoryJobsStore) -> None:
        self._inner = inner
        self.fail_after: int | None = None
        self.inserted = 0

    async def insert_job(self, record):
        if self.fail_after is not None and self.inserted >= self.fail_after:
            raise RuntimeError("simulated job insert failure")
        self.inserted += 1
        return await self._inner.insert_job(record)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _jobs_snapshot(db: InMemoryJobsDatabase) -> dict:
    return {
        "jobs": copy.deepcopy(db.jobs),
        "idempotency": dict(db.idempotency),
        "steps": copy.deepcopy(db.steps),
        "events": {k: list(v) for k, v in db.events.items()},
    }


def _jobs_rollback(db: InMemoryJobsDatabase, snap: dict) -> None:
    db.jobs = snap["jobs"]
    db.idempotency = snap["idempotency"]
    db.steps = snap["steps"]
    db.events = snap["events"]


class Harness:
    def __init__(self, pages: dict | None = None) -> None:
        self.jobs_db = InMemoryJobsDatabase()
        self.pool_db = InMemoryPoolDatabase()
        self.audit = InMemoryAuditWriter()
        self.objects = MemoryObjectStore()
        self.clock = FakeClock()
        self.now = 0.0
        self.sleeps: list[float] = []
        self.flaky = FlakyJobsStore(InMemoryJobsStore(self.jobs_db))
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: self.http(req)),
            follow_redirects=False,
        )
        self.http_handler = lambda req: httpx.Response(
            200, headers={"content-type": "text/html"},
            content=b"<html><body>default</body></html>",
        )
        wiring = IngestWiring(
            open_ingest=self._open_ingest,
            page_client_factory=lambda: FakePageClient(pages or {}),
            fetcher_factory=lambda: HttpFetcher(
                guard=UrlGuard(resolver=StaticResolver()),
                client=self._client,
                clock=self.clock,
            ),
            object_store=self.objects,
            politeness=PolitenessGate(
                clock=lambda: self.now, sleep=self._fake_sleep
            ),
            clock=self.clock,
        )
        self.service = JobService(clock=self.clock, rng=random.Random(7))

        @asynccontextmanager
        async def open_store(
            scope: IndustryScope | None,
        ) -> AsyncIterator[InMemoryJobsStore]:
            yield InMemoryJobsStore(self.jobs_db)

        self.runner = JobRunner(self.service, open_store, clock=self.clock)
        register_ingest_handlers(self.runner, wiring)

    def http(self, request: httpx.Request) -> httpx.Response:
        return self.http_handler(request)

    async def _fake_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @asynccontextmanager
    async def _open_ingest(
        self, scope: IndustryScope
    ) -> AsyncIterator[IngestTxn]:
        pool_snap = self.pool_db.snapshot()
        jobs_snap = _jobs_snapshot(self.jobs_db)
        txn = IngestTxn(
            pool=InMemoryPoolStore(self.pool_db, scope),
            jobs=self.flaky if self.flaky.fail_after is not None else self.flaky._inner,
            audit=self.audit.bind(scope),
            object_store=self.objects,
        )
        try:
            yield txn
        except BaseException:
            self.pool_db.rollback(pool_snap)
            _jobs_rollback(self.jobs_db, jobs_snap)
            raise

    async def enqueue(self, kind: str, payload: dict, key: str | None = None):
        return await self.service.enqueue(
            InMemoryJobsStore(self.jobs_db),
            IndustryScope(owner_id=OWNER),
            kind=kind,
            payload=payload,
            idempotency_key=key or f"{kind}-{uuid4().hex[:8]}",
        )

    async def drain(self, limit: int = 500) -> int:
        ran = 0
        while await self.runner.run_once() is not None:
            ran += 1
            assert ran < limit, "drain did not terminate"
        return ran

    def jobs_of_kind(self, kind: str) -> list[dict]:
        return [j for j in self.jobs_db.jobs.values() if j["kind"] == kind]

    def feed_cursor(self, feed_id: UUID) -> str | None:
        return self.pool_db.feeds[feed_id].get("discovery_cursor")


# -- fixtures -------------------------------------------------------------------


def rss_xml(links: list[str], next_url: str | None, hours_ago: int) -> bytes:
    items = []
    for n, link in enumerate(links):
        published = T0 - timedelta(hours=hours_ago - n)
        items.append(
            f"<item><title>Item {hours_ago - n}</title><link>{link}</link>"
            f"<pubDate>{format_datetime(published)}</pubDate></item>"
        )
    next_tag = f'<atom:link rel="next" href="{next_url}"/>' if next_url else ""
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        f"<channel><title>News</title>{next_tag}"
        + "".join(items)
        + "</channel></rss>"
    ).encode()


def page(url: str, body: bytes, media_type: str = "application/rss+xml"):
    return PageResponse(200, url, {}, body, media_type)


def url(n: int) -> str:
    return f"https://news.example.com/a/{n}"


def two_page_rss_pages() -> dict:
    return {
        SEED: page(SEED, rss_xml([url(i) for i in range(50)], PAGE2, 49)),
        PAGE2: page(PAGE2, rss_xml([url(50 + i) for i in range(5)], None, 54)),
    }


# -- ING-01: discovery enumerates past the front page -----------------------------


async def test_discover_enumerates_51st_item_ing01() -> None:
    harness = Harness(two_page_rss_pages())
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER, seed_url=SEED)
    await harness.enqueue(
        "discover",
        {"feed_id": str(feed_id), "schedule_slot": "2026-09-20T12:00"},
    )
    await harness.drain()

    # ING-01: all 55 items — including the 51st+ from page 2 — enumerated.
    assert len(harness.pool_db.items) == 55
    # One fetch job per new item, spawned in the same transaction.
    assert len(harness.jobs_of_kind("fetch")) == 55
    # The cursor advanced to the cross-run boundary (date only, no page).
    cursor = harness.feed_cursor(feed_id)
    state = decode_cursor(cursor)
    assert "boundary_date" in state and "next" not in state
    # The discover job finished with honest progress.
    discover_jobs = harness.jobs_of_kind("discover")
    assert discover_jobs[0]["state"] == "succeeded"
    assert discover_jobs[0]["progress"]["new_items"] == 55
    assert discover_jobs[0]["progress"]["pages"] == 2
    # Source run recorded.
    runs = list(harness.pool_db.runs.values())
    assert runs and runs[0]["outcome"] == "success"
    assert runs[0]["discovered_count"] == 55


async def test_discover_normalizes_and_dedups_candidates() -> None:
    links = [
        "https://news.example.com/a/1?utm_source=feed&utm_medium=rss",
        "https://NEWS.example.com:443/a/1#section",
        "https://news.example.com/a/2",
    ]
    harness = Harness({SEED: page(SEED, rss_xml(links, None, 2))})
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER, seed_url=SEED)
    await harness.enqueue("discover", {"feed_id": str(feed_id)})
    await harness.drain()

    canonicals = sorted(i["canonical_url"] for i in harness.pool_db.items.values())
    # Tracking params / fragments / default port collapse to one identity.
    assert canonicals == [
        "https://news.example.com/a/1",
        "https://news.example.com/a/2",
    ]
    assert len(harness.jobs_of_kind("fetch")) == 2


async def test_discover_page_transaction_rolls_back_atomically() -> None:
    harness = Harness(two_page_rss_pages())
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    harness.flaky.fail_after = 0  # the first fetch-job insert explodes
    await harness.enqueue("discover", {"feed_id": str(feed_id)})
    await harness.drain()

    # 04 §3: 事务失败不推进游标 — no candidates, no fetch jobs, no cursor.
    assert harness.pool_db.items == {}
    assert harness.jobs_of_kind("fetch") == []
    assert len(harness.pool_db.runs) == 0
    assert harness.feed_cursor(feed_id) is None
    assert harness.pool_db.feeds[feed_id]["cursor_version"] == 1
    # The discover job itself failed (supervisor backstop).
    assert harness.jobs_of_kind("discover")[0]["state"] == "failed"


async def test_source_poll_kind_maps_to_discover_semantics() -> None:
    harness = Harness(two_page_rss_pages())
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER, seed_url=SEED)
    await harness.enqueue(
        "source_poll", {"feed_id": str(feed_id), "mode": "incremental"}
    )
    await harness.drain()
    poll = harness.jobs_of_kind("source_poll")[0]
    assert poll["state"] == "succeeded"
    assert poll["progress"]["new_items"] == 55


# -- ING-03: list shift + overlap scan dedup ----------------------------------------


def html_list_page(urls: list[str], next_href: str | None = None) -> PageResponse:
    lis = "".join(
        f"<li><a href='{u}'>Item {u.rsplit('/', 1)[-1]}</a></li>" for u in urls
    )
    nxt = f"<a class='next' href='{next_href}'>next</a>" if next_href else ""
    body = f"<html><body><ul class='news'>{lis}</ul>{nxt}</body></html>".encode()
    return PageResponse(200, "https://portal.example.com/list", {}, body, "text/html")


HTML_CONFIG = {
    "list_selector": "ul.news li",
    "link_selector": "a",
    "pagination": {"mode": "next_link", "next_selector": "a.next"},
}
LISTING = "https://portal.example.com/list"


async def test_html_list_concurrent_insertion_overlap_scan_ing03() -> None:
    pages = {
        LISTING: html_list_page([url(i) for i in range(1, 11)], LISTING + "?p=2"),
        LISTING + "?p=2": html_list_page([url(i) for i in range(11, 21)]),
    }
    harness = Harness(pages)
    feed_id = seed_feed(
        harness.pool_db,
        owner_id=OWNER,
        adapter_type="html_list",
        seed_url=LISTING,
        config=dict(HTML_CONFIG),
    )
    await harness.enqueue("discover", {"feed_id": str(feed_id)})
    await harness.drain()
    assert len(harness.pool_db.items) == 20
    assert len(harness.jobs_of_kind("fetch")) == 20

    # Between polls a new item (url 0) is inserted at the top; item 10
    # slides onto page 2. The page factory closes over the SAME dict, so
    # mutating it in place simulates the upstream change.
    shifted = {
        LISTING: html_list_page(
            [url(0)] + [url(i) for i in range(1, 10)], LISTING + "?p=2"
        ),
        LISTING + "?p=2": html_list_page([url(i) for i in range(10, 21)]),
    }
    pages.clear()
    pages.update(shifted)
    await harness.enqueue("discover", {"feed_id": str(feed_id)})
    await harness.drain()

    # ING-03: overlap scan picks the late item; dedup keeps identity stable.
    assert len(harness.pool_db.items) == 21
    assert len(harness.jobs_of_kind("fetch")) == 21  # 20 + exactly one new
    discovers = harness.jobs_of_kind("discover")
    assert discovers[-1]["progress"]["new_items"] == 1


# -- ING-04: conditional fetch three states ------------------------------------------


async def seed_prior_capture(
    harness: Harness, feed_id: UUID, body: bytes, etag: str
) -> UUID:
    store = InMemoryPoolStore(harness.pool_db, IndustryScope(owner_id=OWNER))
    item, _ = await store.upsert_discovery_item(
        DiscoveryItemRecord(
            owner_id=OWNER,
            feed_id=feed_id,
            discovered_url=url(1),
            canonical_url=url(1),
            origin_kind="rss",
        )
    )
    digest = hashlib.sha256(body).hexdigest()
    blob = await store.insert_blob(
        BlobRecord(
            owner_id=OWNER, object_key=f"raw/{digest[:2]}/{digest}",
            sha256=digest, media_type="text/html", byte_size=len(body),
        )
    )
    document = await store.insert_document(
        DocumentRecord(
            owner_id=OWNER,
            canonical_url=url(1),
            identity_namespace="url",
            identity_value=url(1),
            visibility_scope_key="public",
            origin_kind="rss",
        )
    )
    capture = await store.insert_capture(
        CaptureRecord(
            owner_id=OWNER,
            document_id=document.id,
            raw_blob_id=blob.id,
            response_status=200,
            effective_url=url(1),
            content_hash=digest,
            etag=etag,
            content_type="text/html",
        )
    )
    await store.set_current_capture(document.id, capture.id)
    await store.insert_origin(
        document_id=document.id, discovery_item_id=item.id, feed_id=feed_id
    )
    harness._seeded = {"item": item, "capture": capture, "document": document}
    return item.id


async def enqueue_fetch(harness: Harness, item_id: UUID) -> None:
    await harness.enqueue(
        "fetch", {"discovery_item_id": str(item_id), "url": url(1)}, key=None
    )


async def test_fetch_304_binds_existing_capture_ing04() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    body = b"<html><body>v1</body></html>"
    item_id = await seed_prior_capture(harness, feed_id, body, '"v1"')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("if-none-match") == '"v1"'
        return httpx.Response(304, headers={"etag": '"v1"'})

    harness.http_handler = handler
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    observations = list(harness.pool_db.observations.values())
    assert len(observations) == 1
    assert observations[0]["outcome"] == "no_change"
    assert observations[0]["status_code"] == 304
    # ING-04: the observation binds the existing capture; no new version.
    assert observations[0]["capture_id"] == harness._seeded["capture"].id
    assert len(harness.pool_db.captures) == 1
    job = harness.jobs_of_kind("fetch")[0]
    assert job["state"] == "succeeded"
    assert job["progress"]["outcome"] == "no_change"


async def test_fetch_changed_200_creates_new_capture_version() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    item_id = await seed_prior_capture(
        harness, feed_id, b"<html><body>v1</body></html>", '"v1"'
    )
    body2 = b"<html><body>v2 updated</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "etag": '"v2"'},
            content=body2,
        )

    harness.http_handler = handler
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    assert len(harness.pool_db.captures) == 2
    new_capture = next(
        c for c in harness.pool_db.captures.values() if c["etag"] == '"v2"'
    )
    assert new_capture["content_hash"] == hashlib.sha256(body2).hexdigest()
    assert new_capture["etag"] == '"v2"'
    document = harness.pool_db.documents[harness._seeded["document"].id]
    assert document["current_capture_id"] == new_capture["id"]
    # Blob bytes landed in the object store under the content key.
    assert new_capture["raw_blob_id"] in harness.pool_db.blobs
    job = harness.jobs_of_kind("fetch")[0]
    assert job["progress"]["outcome"] == "captured"
    item = harness.pool_db.items[item_id]
    assert item["state"] == "captured"


async def test_fetch_unchanged_200_binds_prior_capture_ing04() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    body = b"<html><body>same bytes</body></html>"
    item_id = await seed_prior_capture(harness, feed_id, body, '"v1"')

    def handler(request: httpx.Request) -> httpx.Response:
        # Server ignored the conditional and returned 200 with the SAME
        # bytes: hash equality must not mint a new capture version.
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=body
        )

    harness.http_handler = handler
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    assert len(harness.pool_db.captures) == 1
    observations = list(harness.pool_db.observations.values())
    assert observations[0]["outcome"] == "no_change"
    assert observations[0]["capture_id"] == harness._seeded["capture"].id


# -- failures, retries, audit ---------------------------------------------------------


async def test_fetch_429_honors_retry_after() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    item_id = await seed_prior_capture(
        harness, feed_id, b"<html><body>v1</body></html>", '"v1"'
    )
    harness.http_handler = lambda req: httpx.Response(
        429, headers={"retry-after": "120"}
    )
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    job = harness.jobs_of_kind("fetch")[0]
    assert job["state"] == "retry_wait"
    assert job["error"]["code"] == "transient"
    assert job["available_at"] == T0 + timedelta(seconds=120)
    observations = list(harness.pool_db.observations.values())
    assert observations[0]["outcome"] == "error"
    assert observations[0]["error_code"] == "http_429"


async def test_fetch_403_audits_and_fails_access_blocked() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    item_id = await seed_prior_capture(
        harness, feed_id, b"<html><body>v1</body></html>", '"v1"'
    )
    harness.http_handler = lambda req: httpx.Response(403)
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    job = harness.jobs_of_kind("fetch")[0]
    assert job["state"] == "failed"
    assert job["error"]["code"] == "access_blocked"
    # 07 §6 落点: the audit row rides the same transaction as the evidence.
    assert [e.action for e in harness.audit.entries] == ["fetch_access_denied"]
    entry = harness.audit.entries[0]
    assert entry.target_id == item_id
    assert entry.owner_id == OWNER
    observations = list(harness.pool_db.observations.values())
    assert observations[0]["outcome"] == "access_denied"
    assert harness.pool_db.items[item_id]["state"] == "access_denied"


async def test_fetch_ssrf_redirect_audits_scope_violation() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    item_id = await seed_prior_capture(
        harness, feed_id, b"<html><body>v1</body></html>", '"v1"'
    )
    harness.http_handler = lambda req: httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
    )
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    job = harness.jobs_of_kind("fetch")[0]
    assert job["state"] == "failed"
    assert job["error"]["code"] == "scope_violation"
    entry = harness.audit.entries[0]
    assert entry.action == "fetch_ssrf_blocked"
    assert entry.details["reason"] == "link_local"
    observations = list(harness.pool_db.observations.values())
    assert observations[0]["error_code"] == "ssrf_blocked"
    assert harness.pool_db.items[item_id]["state"] == "failed"


async def test_fetch_js_required_without_browser_is_partial() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    item_id = await seed_prior_capture(
        harness, feed_id, b"<html><body>v1</body></html>", '"v1"'
    )
    js_shell = (
        b"<html><head><script>boot()</script></head><body><div id='root'>"
        b"</div></body></html>"
    )
    harness.http_handler = lambda req: httpx.Response(
        200, headers={"content-type": "text/html"}, content=js_shell
    )
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    job = harness.jobs_of_kind("fetch")[0]
    assert job["state"] == "partial"
    assert job["error"]["gaps"] == ["browser_unavailable"]
    observations = list(harness.pool_db.observations.values())
    assert observations[0]["error_code"] == "browser_unavailable"
    assert harness.pool_db.items[item_id]["state"] == "pending_js"


# -- capture → parse handoff (Task 8, 04 §4) ----------------------------------------


async def test_captured_fetch_spawns_parse_job_for_new_capture() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    item_id = await seed_prior_capture(
        harness, feed_id, b"<html><body>v1</body></html>", '"v1"'
    )
    body = b"<html><body><main><p>fresh content</p></main></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=body
        )

    harness.http_handler = handler
    await enqueue_fetch(harness, item_id)
    await harness.drain()

    new_capture = next(
        c for c in harness.pool_db.captures.values()
        if c["content_hash"] == hashlib.sha256(body).hexdigest()
    )
    # The parse job for the new capture was spawned (same commit) and ran.
    parse_jobs = [
        j for j in harness.jobs_of_kind("parse")
        if j["input"]["capture_id"] == str(new_capture["id"])
    ]
    assert len(parse_jobs) == 1
    assert parse_jobs[0]["state"] == "succeeded"
    assert parse_jobs[0]["progress"]["parse_status"] in ("ok", "partial")
    # And the parse persisted its artifact bound to that capture.
    parses = [
        p for p in harness.pool_db.parses.values()
        if p["capture_id"] == new_capture["id"]
    ]
    assert len(parses) == 1


# -- politeness (04 §2) ----------------------------------------------------------------


async def test_politeness_gate_min_interval_per_domain() -> None:
    now = 0.0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        nonlocal now
        now += seconds

    gate = PolitenessGate(clock=lambda: now, sleep=fake_sleep)
    async with gate.request("news.example.com"):
        pass
    async with gate.request("news.example.com"):
        pass
    async with gate.request("other.example.com"):
        pass
    assert sleeps == [2.0]  # same domain spaced; different domain not


async def test_politeness_gate_concurrency_cap() -> None:
    now = 0.0

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    gate = PolitenessGate(clock=lambda: now, sleep=fake_sleep)
    active = 0
    peak = 0

    async def one() -> None:
        nonlocal active, peak
        async with gate.request("news.example.com"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*[one() for _ in range(5)])
    assert peak == 2


async def test_fetch_respects_min_interval_between_same_domain_items() -> None:
    harness = Harness()
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER)
    harness.http_handler = lambda req: httpx.Response(
        200, headers={"content-type": "text/html"},
        content=b"<html><body>polite</body></html>",
    )
    store = InMemoryPoolStore(harness.pool_db, IndustryScope(owner_id=OWNER))
    ids = []
    for n in (1, 2):
        item, _ = await store.upsert_discovery_item(
            DiscoveryItemRecord(
                owner_id=OWNER, feed_id=feed_id, discovered_url=url(n),
                canonical_url=url(n), origin_kind="rss",
            )
        )
        ids.append(item.id)
    for item_id in ids:
        await harness.enqueue("fetch", {"discovery_item_id": str(item_id)})
    await harness.drain()
    # The second same-domain fetch waited the 2s minimum interval.
    assert 2.0 in harness.sleeps


# -- page_monitor re-check cadence -----------------------------------------------------


async def test_page_monitor_refetches_each_poll_with_fresh_epoch() -> None:
    harness = Harness()
    feed_id = seed_feed(
        harness.pool_db,
        owner_id=OWNER,
        adapter_type="page_monitor",
        seed_url="https://vendor.example.com/product",
    )
    versions = iter((b"v1", b"v2"))
    harness.http_handler = lambda req: httpx.Response(
        200, headers={"content-type": "text/html"}, content=next(versions)
    )
    await harness.enqueue(
        "discover", {"feed_id": str(feed_id), "schedule_slot": "slot-1"}
    )
    await harness.drain()
    assert len(harness.pool_db.items) == 1
    assert len(harness.pool_db.captures) == 1

    # Second poll: same item, NEW refresh_epoch → refetch, new capture.
    await harness.enqueue(
        "discover", {"feed_id": str(feed_id), "schedule_slot": "slot-2"}
    )
    await harness.drain()
    assert len(harness.pool_db.items) == 1
    fetch_jobs = harness.jobs_of_kind("fetch")
    assert len(fetch_jobs) == 2
    assert len(harness.pool_db.captures) == 2
    observations = list(harness.pool_db.observations.values())
    assert [o["outcome"] for o in observations] == ["captured", "captured"]


# -- oversized discovery page fails closed (fix round 1) -----------------------------


async def test_discover_oversized_page_fails_without_retry() -> None:
    from intel.sources.pageclient import PageBodyTooLarge

    harness = Harness({SEED: PageBodyTooLarge(SEED, 100)})
    feed_id = seed_feed(harness.pool_db, owner_id=OWNER, seed_url=SEED)
    await harness.enqueue("discover", {"feed_id": str(feed_id)})
    await harness.drain()

    job = harness.jobs_of_kind("discover")[0]
    assert job["state"] == "failed"  # non-retryable, not retry_wait
    assert job["error"]["code"] == "page_body_too_large"
    assert harness.pool_db.items == {}  # nothing persisted
