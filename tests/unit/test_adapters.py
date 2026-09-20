"""Adapter unit tests over embedded fixtures (ING-01, 04 §3 cursor +
overlap window, 14 §2 config validation). No network: a fake page client
serves RSS/Atom XML, JSON API payloads, HTML list pages and sitemap XML.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from intel.services.errors import ValidationFailed
from intel.sources.adapters import (
    decode_cursor,
    encode_cursor,
    make_adapter,
    validate_adapter_config,
)
from intel.sources.adapters.cursor import decode_cursor as decode
from intel.sources.adapters.cursor import encode_cursor as encode
from intel.sources.dto import DiscoverLimits, FeedPlan
from intel.sources.pageclient import PageFetchError, PageResponse

OWNER = "00000000-0000-0000-0000-000000000001"
FEED = "00000000-0000-0000-0000-000000000002"
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SEED = "https://news.example.com/feed.xml"
PAGE2 = "https://news.example.com/feed.xml?page=2"


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


def rss_page(links: list[str], next_url: str | None, hours_ago: int) -> PageResponse:
    items = []
    for n, link in enumerate(links):
        published = BASE - timedelta(hours=hours_ago - n)
        items.append(
            f"<item><title>Item {hours_ago - n}</title>"
            f"<link>{link}</link>"
            f"<pubDate>{format_datetime(published)}</pubDate></item>"
        )
    next_tag = (
        f'<atom:link rel="next" href="{next_url}"/>' if next_url else ""
    )
    body = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel><title>Etching News</title>"
        f"<link>https://news.example.com/</link>{next_tag}"
        + "".join(items)
        + "</channel></rss>"
    ).encode()
    return PageResponse(
        status=200,
        url=SEED,
        headers={"content-type": "application/rss+xml"},
        body=body,
        media_type="application/rss+xml",
    )


def plan(**kwargs) -> FeedPlan:
    defaults: dict = {
        "feed_id": FEED,
        "owner_id": OWNER,
        "seed": SEED,
        "adapter": "rss",
        "limits": DiscoverLimits(max_pages=5, max_items=100),
    }
    defaults.update(kwargs)
    return FeedPlan(**defaults)


def url(n: int) -> str:
    return f"https://news.example.com/a/{n}"


# -- ING-01: the 51st item beyond the front page is enumerated -------------------


async def test_rss_pages_beyond_the_front_page_ing01() -> None:
    front = rss_page([url(i) for i in range(50)], PAGE2, hours_ago=49)
    archive = rss_page([url(50 + i) for i in range(5)], None, hours_ago=54)
    client = FakePageClient({SEED: front, PAGE2: archive})
    adapter = make_adapter("rss", client)

    page1 = await adapter.discover(plan(), None)
    assert len(page1.items) == 50
    assert page1.next_cursor is not None
    assert not page1.exhausted
    assert page1.items[0].url == url(0)
    assert page1.items[0].date_hint is not None

    page2 = await adapter.discover(plan(), page1.next_cursor)
    # ING-01: items 51..55 live past the front page and are enumerated.
    assert [i.url for i in page2.items] == [url(50 + i) for i in range(5)]
    assert page2.exhausted
    assert page2.next_cursor is not None  # boundary for the next poll
    state = decode(page2.next_cursor)
    assert "boundary_date" in state and "next" not in state


async def test_rss_overlap_window_keeps_late_arrivals() -> None:
    # Boundary from a previous run: 72h before BASE-40h is BASE-112h.
    cursor = encode({"boundary_date": (BASE - timedelta(hours=40)).isoformat()})
    late = rss_page([url(41), url(42)], None, hours_ago=50)
    # Both entries sit inside the 72h overlap window (>= BASE-112h).
    client = FakePageClient({SEED: late})
    adapter = make_adapter("rss", client)
    page = await adapter.discover(plan(overlap_hours=72), cursor)
    assert [i.url for i in page.items] == [url(41), url(42)]
    assert page.window_coverage == "within_window"


async def test_rss_overlap_window_drops_history() -> None:
    cursor = encode({"boundary_date": (BASE - timedelta(hours=10)).isoformat()})
    history = rss_page([url(0), url(1)], None, hours_ago=200)
    client = FakePageClient({SEED: history})
    adapter = make_adapter("rss", client)
    page = await adapter.discover(plan(overlap_hours=72), cursor)
    assert page.items == []
    assert page.warnings and "overlap window" in page.warnings[0]
    assert page.window_coverage == "beyond_window"


async def test_rss_continues_from_cursor_archive_url() -> None:
    front = rss_page([url(0)], PAGE2, hours_ago=1)
    archive = rss_page([url(1)], None, hours_ago=2)
    client = FakePageClient({SEED: front, PAGE2: archive})
    adapter = make_adapter("rss", client)
    cursor = encode({"next": PAGE2, "boundary_date": (BASE - timedelta(hours=1)).isoformat()})
    page = await adapter.discover(plan(), cursor)
    assert client.hits == [PAGE2]
    assert [i.url for i in page.items] == [url(1)]


# -- Atom ------------------------------------------------------------------------


async def test_atom_enumerates_entries() -> None:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Atom Feed</title>"
        f'<link rel="self" href="{SEED}"/>'
        "<entry><title>Alpha</title>"
        f'<link href="{url(1)}"/>'
        "<id>tag:example.com,2026:1</id>"
        "<updated>2026-09-20T10:00:00Z</updated></entry>"
        "</feed>"
    ).encode()
    client = FakePageClient(
        {SEED: PageResponse(200, SEED, {}, body, "application/atom+xml")}
    )
    adapter = make_adapter("atom", client)
    page = await adapter.discover(plan(adapter="atom"), None)
    assert [i.url for i in page.items] == [url(1)]
    assert page.items[0].title_hint == "Alpha"
    assert page.items[0].date_hint == datetime(2026, 9, 20, 10, tzinfo=UTC)
    assert page.exhausted


# -- api_list ---------------------------------------------------------------------


def api_payload(items: list[dict], next_url: str | None = None) -> PageResponse:
    body = json.dumps({"data": {"items": items, "next": next_url}}).encode()
    return PageResponse(
        200, "https://api.example.com/items", {}, body, "application/json"
    )


async def test_api_list_numeric_pagination() -> None:
    page1 = api_payload(
        [{"url": url(i), "title": f"T{i}", "published": BASE.isoformat(),
          "id": f"p{i}"} for i in range(3)]
    )
    page2 = api_payload([])
    client = FakePageClient(
        {
            "https://api.example.com/items?page=1": page1,
            "https://api.example.com/items?page=2": page2,
        }
    )
    adapter = make_adapter("api", client)
    api_plan = plan(
        adapter="api",
        seed="https://api.example.com/items?page=1",
        config={
            "endpoint": "https://api.example.com/items?page={page}",
            "items_path": "data.items",
            "url_field": "url",
            "title_field": "title",
            "date_field": "published",
            "provider_id_field": "id",
        },
    )
    first = await adapter.discover(api_plan, None)
    assert [i.url for i in first.items] == [url(0), url(1), url(2)]
    assert first.items[0].provider_id == "p0"
    assert first.next_cursor is not None
    second = await adapter.discover(api_plan, first.next_cursor)
    assert second.items == []
    assert second.exhausted


async def test_api_list_next_url_field() -> None:
    page1 = api_payload([{"url": url(0)}], next_url="https://api.example.com/items?cursor=abc")
    page2 = api_payload([])
    client = FakePageClient(
        {
            "https://api.example.com/items?page=1": page1,
            "https://api.example.com/items?cursor=abc": page2,
        }
    )
    adapter = make_adapter("api", client)
    api_plan = plan(
        adapter="api",
        seed="https://api.example.com/items?page=1",
        config={
            "endpoint": "https://api.example.com/items?page={page}",
            "items_path": "data.items",
            "url_field": "url",
            "next_url_field": "data.next",
        },
    )
    first = await adapter.discover(api_plan, None)
    assert decode(first.next_cursor) == {
        "next_url": "https://api.example.com/items?cursor=abc"
    }
    second = await adapter.discover(api_plan, first.next_cursor)
    assert client.hits[-1] == "https://api.example.com/items?cursor=abc"
    assert second.exhausted


async def test_api_page_fetch_error_propagates() -> None:
    client = FakePageClient(
        {"https://api.example.com/items?page=1": PageFetchError(503, url="x")}
    )
    adapter = make_adapter("api", client)
    api_plan = plan(
        adapter="api",
        seed="https://api.example.com/items?page=1",
        config={
            "endpoint": "https://api.example.com/items?page={page}",
            "items_path": "data.items",
            "url_field": "url",
        },
    )
    with pytest.raises(PageFetchError):
        await adapter.discover(api_plan, None)


# -- html_list (ING-03 shape) -------------------------------------------------------


def html_list_page(urls: list[str], next_href: str | None = None) -> PageResponse:
    lis = "".join(
        f'<li><a href="{u}">Item {u.rsplit("/", 1)[-1]}</a></li>' for u in urls
    )
    next_link = f'<a class="next" href="{next_href}">next</a>' if next_href else ""
    body = (
        "<html><body><ul class='news'>" + lis + "</ul>" + next_link + "</body></html>"
    ).encode()
    return PageResponse(200, "https://portal.example.com/list", {}, body, "text/html")


HTML_CONFIG = {
    "list_selector": "ul.news li",
    "link_selector": "a",
    "pagination": {"mode": "next_link", "next_selector": "a.next"},
}


async def test_html_list_walks_pages_and_records_boundary() -> None:
    listing = "https://portal.example.com/list"
    page1 = html_list_page([url(i) for i in range(1, 11)], listing + "?p=2")
    page2 = html_list_page([url(i) for i in range(11, 21)])
    client = FakePageClient({listing: page1, listing + "?p=2": page2})
    adapter = make_adapter("html_list", client)
    html_plan = plan(
        adapter="html_list",
        seed=listing,
        config=dict(HTML_CONFIG),
        limits=DiscoverLimits(max_pages=5, max_items=50),
    )
    first = await adapter.discover(html_plan, None)
    assert len(first.items) == 10
    second = await adapter.discover(html_plan, first.next_cursor)
    assert [i.url for i in second.items] == [url(i) for i in range(11, 21)]
    assert second.exhausted
    final = decode(second.next_cursor)
    assert set(final["boundary"]) == {url(i) for i in range(1, 11)}


async def test_html_list_stops_at_seen_boundary_ing03() -> None:
    """Concurrent insertion shifts the list; the overlap scan re-emits the
    front page and stops at the previous run's boundary."""
    listing = "https://portal.example.com/list"
    # Previous run saw front page items 1..10 (boundary), walked to 20.
    boundary_cursor = encode({"boundary": [url(i) for i in range(1, 11)]})
    # A new item 0 was inserted; item 10 slid to page 2.
    shifted1 = html_list_page([url(0)] + [url(i) for i in range(1, 10)], listing + "?p=2")
    client = FakePageClient({listing: shifted1, listing + "?p=2": html_list_page([])})
    adapter = make_adapter("html_list", client)
    html_plan = plan(adapter="html_list", seed=listing, config=dict(HTML_CONFIG))
    page = await adapter.discover(html_plan, boundary_cursor)
    # The re-emitted overlap (1..9) plus the late arrival (0).
    assert [i.url for i in page.items] == [url(0)] + [url(i) for i in range(1, 10)]
    assert page.exhausted  # boundary reached: no archive re-walk
    assert any("boundary" in w for w in page.warnings)
    # The next run's boundary is this run's front page.
    assert set(decode(page.next_cursor)["boundary"]) == {
        url(0), *[url(i) for i in range(1, 10)]
    }


# -- sitemap ------------------------------------------------------------------------


async def test_sitemap_filters_allowed_paths_and_chunks() -> None:
    locs = [
        f"https://docs.example.com/{section}/p{i}"
        for section in ("etch", "etch", "pvd")
        for i in range(3)
    ]
    body = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
        + "</urlset>"
    ).encode()
    client = FakePageClient(
        {
            "https://docs.example.com/sitemap.xml": PageResponse(
                200, "https://docs.example.com/sitemap.xml", {}, body, "text/xml"
            )
        }
    )
    adapter = make_adapter("sitemap", client)
    sitemap_plan = plan(
        adapter="sitemap",
        seed="https://docs.example.com/sitemap.xml",
        config={"allowed_paths": ["/etch"]},
        limits=DiscoverLimits(max_pages=5, max_items=3),
    )
    first = await adapter.discover(sitemap_plan, None)
    assert [i.url for i in first.items] == [
        "https://docs.example.com/etch/p0",
        "https://docs.example.com/etch/p1",
        "https://docs.example.com/etch/p2",
    ]
    assert decode(first.next_cursor) == {"offset": 3}
    second = await adapter.discover(sitemap_plan, first.next_cursor)
    # Second chunk: the duplicated etch trio; then the allowed set is
    # exhausted (pvd entries were filtered out entirely).
    assert [i.url for i in second.items] == [
        "https://docs.example.com/etch/p0",
        "https://docs.example.com/etch/p1",
        "https://docs.example.com/etch/p2",
    ]
    assert second.next_cursor is None
    assert second.exhausted


# -- page_monitor --------------------------------------------------------------------


async def test_page_monitor_reemits_the_fixed_url() -> None:
    client = FakePageClient({})
    adapter = make_adapter("page_monitor", client)
    page = await adapter.discover(
        plan(adapter="page_monitor", seed="https://vendor.example.com/product"), None
    )
    assert [i.url for i in page.items] == ["https://vendor.example.com/product"]
    assert page.exhausted
    assert page.next_cursor is None
    assert client.hits == []  # discovery never fetches for page_monitor


# -- config validation (14 §2) ---------------------------------------------------------


def test_config_validation_requires_per_kind_fields() -> None:
    with pytest.raises(ValidationFailed):
        validate_adapter_config("api", {})  # missing endpoint etc.
    with pytest.raises(ValidationFailed):
        validate_adapter_config("html_list", {"list_selector": "ul"})
    with pytest.raises(ValidationFailed):
        validate_adapter_config("sitemap", {})
    with pytest.raises(ValidationFailed):
        validate_adapter_config("bogus", {})


def test_config_validation_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationFailed):
        validate_adapter_config("rss", {"fetch_hook": "rm -rf /"})
    with pytest.raises(ValidationFailed):
        validate_adapter_config(
            "page_monitor", {"eval": "javascript:alert(1)"}
        )


def test_config_validation_rejects_non_whitelisted_template_params() -> None:
    with pytest.raises(ValidationFailed):
        validate_adapter_config(
            "api",
            {
                "endpoint": "https://api.example.com/items?token={secret}",
                "items_path": "data.items",
                "url_field": "url",
            },
        )
    # {page} is whitelisted.
    validate_adapter_config(
        "api",
        {
            "endpoint": "https://api.example.com/items?page={page}",
            "items_path": "data.items",
            "url_field": "url",
        },
    )


def test_cursor_codec_is_opaque_and_tolerant() -> None:
    cursor = encode_cursor({"page": 3})
    assert decode_cursor(cursor) == {"page": 3}
    assert decode_cursor("!!!not-base64!!!") == {}
    assert decode_cursor(None) == {}


# -- untrusted-XML hardening (fix round 1: billion-laughs defense) ------------------


async def test_sitemap_entity_expansion_is_neutralized() -> None:
    """A DTD full of nested entities must not expand: with
    resolve_entities=False the <loc> text stays unexpanded (None) and the
    entry is dropped instead of ballooning memory."""
    laughing = (
        b"<?xml version='1.0'?>\n"
        b"<!DOCTYPE urlset [\n"
        b'  <!ENTITY a "0123456789abcdefghijklmnopqrstuvwxyz0123456789">\n'
        b'  <!ENTITY a1 "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
        b'  <!ENTITY a2 "&a1;&a1;&a1;&a1;&a1;&a1;&a1;&a1;&a1;&a1;">\n'
        b'  <!ENTITY a3 "&a2;&a2;&a2;&a2;&a2;&a2;&a2;&a2;&a2;&a2;">\n'
        b"]>\n"
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://docs.example.com/etch/&a3;</loc></url>"
        b"</urlset>"
    )
    seed_url = "https://docs.example.com/sitemap.xml"
    client = FakePageClient(
        {seed_url: PageResponse(200, seed_url, {}, laughing, "text/xml")}
    )
    adapter = make_adapter("sitemap", client)
    sitemap_plan = plan(
        adapter="sitemap",
        seed=seed_url,
        config={"allowed_paths": ["/etch"]},
    )
    page = await adapter.discover(sitemap_plan, None)
    # No expansion: the entity stays an unresolved reference node, so
    # <loc> text is only the segment before it -- nothing anywhere in
    # the result carries the expanded payload, and memory stayed flat.
    assert [i.url for i in page.items] == ["https://docs.example.com/etch/"]
    expanded = b"0123456789abcdefghijklmnopqrstuvwxyz" * 10
    assert expanded not in str(page).encode()
    assert all(expanded not in i.url.encode() for i in page.items)
