"""Fetcher unit tests over httpx.MockTransport — no network (ING-04
three-state conditional flow, header allowlist, redirect re-validation,
size/redirect caps, 429/401/login outcomes, JS-required fallback).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from intel.sources.dto import CaptureResult, FetchRequest
from intel.sources.fetcher import BrowserUnavailable, HttpFetcher
from intel.sources.ssrf import UrlBlockedError, UrlGuard

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
GOOD = "https://news.example.com/a/1"
META = "http://169.254.169.254/latest/meta-data/"


class StaticResolver:
    async def resolve(self, host: str) -> list[str]:
        return ["93.184.216.34"]


def make_fetcher(
    handler, *, browser=None
) -> HttpFetcher:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return HttpFetcher(
        guard=UrlGuard(resolver=StaticResolver()),
        client=client,
        browser=browser,
        clock=lambda: T0,
    )


def request(url: str = GOOD, **kwargs) -> FetchRequest:
    defaults: dict = {
        "item_id": "00000000-0000-0000-0000-0000000000aa",
        "url": url,
        "deadline_at": T0 + timedelta(seconds=30),
    }
    defaults.update(kwargs)
    return FetchRequest(**defaults)


def ok(body: bytes, etag: str | None = None, content_type: str = "text/html"):
    headers = {"content-type": content_type}
    if etag:
        headers["etag"] = etag
    return httpx.Response(200, headers=headers, content=body)


async def test_conditional_get_304_returns_no_change() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return ok(b"<html><body>hello</body></html>", etag='"v1"')

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request(conditional_headers={"If-None-Match": '"v1"'}))
    assert result.outcome == "no_change"
    assert result.status == 304
    assert seen[0]["if-none-match"] == '"v1"'


async def test_changed_200_returns_captured_with_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok(b"<html><body>new body</body></html>", etag='"v2"')

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request())
    assert result.outcome == "captured"
    assert result.hash == hashlib.sha256(b"<html><body>new body</body></html>").hexdigest()
    assert result.headers["etag"] == '"v2"'
    assert result.media_type == "text/html"


async def test_header_allowlist_drops_cookies_and_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "etag": '"v1"',
                "set-cookie": "session=secret",
                "www-authenticate": "Bearer",
                "x-custom": "dropped",
            },
            content=b"%PDF-1.4 fake",
        )

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request())
    from intel.sources.dto import HEADER_ALLOWLIST

    assert set(result.headers) <= HEADER_ALLOWLIST
    assert set(result.headers) >= {"content-type", "etag"}
    assert "session" not in str(result.headers)


async def test_conditional_header_allowlist_is_enforced() -> None:
    fetcher = make_fetcher(lambda req: ok(b"x"))
    with pytest.raises(ValueError):
        await fetcher.fetch(
            request(conditional_headers={"X-Evil": "1", "Cookie": "a=b"})
        )


async def test_redirect_hops_followed_to_final_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a/1":
            return httpx.Response(302, headers={"location": "/a/2"})
        if request.url.path == "/a/2":
            return httpx.Response(301, headers={"location": "/a/3"})
        return ok(b"<html><body>final</body></html>")

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request())
    assert result.outcome == "captured"
    assert result.final_url == "https://news.example.com/a/3"


async def test_redirect_to_metadata_endpoint_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": META})

    fetcher = make_fetcher(handler)
    with pytest.raises(UrlBlockedError) as excinfo:
        await fetcher.fetch(request())
    assert excinfo.value.reason == "link_local"


async def test_redirect_cap_of_five() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Every hop redirects again — never settles.
        n = request.url.params.get("hop", "0")
        return httpx.Response(
            302, headers={"location": f"https://news.example.com/loop?hop={n}"}
        )

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request("https://news.example.com/loop?hop=0"))
    assert result.outcome == "error"
    assert result.error_code == "redirect_limit"


async def test_body_too_large_rejected_upfront() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "content-length": "999999"},
            content=b"%",
        )

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request(max_bytes=100))
    assert result.outcome == "error"
    assert result.error_code == "body_too_large"


async def test_body_too_large_enforced_after_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Lying or absent Content-Length: the decompressed cap still fires.
        return httpx.Response(200, content=b"x" * 500, headers={"content-type": "text/html"})

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request(max_bytes=100))
    assert result.outcome == "error"
    assert result.error_code == "body_too_large"


async def test_429_reports_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "120"})

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request())
    assert result.outcome == "error"
    assert result.error_code == "http_429"
    assert result.retry_after == 120.0


async def test_403_is_access_denied() -> None:
    fetcher = make_fetcher(lambda req: httpx.Response(403))
    result = await fetcher.fetch(request())
    assert result.outcome == "access_denied"
    assert result.status == 403
    assert "auth" in result.access_flags


async def test_redirect_to_login_page_is_access_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts." in request.url.host:
            return ok(b"<html><body>please sign in</body></html>")
        return httpx.Response(
            302, headers={"location": "https://accounts.example.com/login?next=x"}
        )

    fetcher = make_fetcher(handler)
    result = await fetcher.fetch(request("https://paywalled.example.com/report"))
    assert result.outcome == "access_denied"
    assert "login_redirect" in result.access_flags


# -- JS-required / browser fallback -------------------------------------------


class StubBrowser:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[FetchRequest] = []
        self.fail = fail

    async def render(self, request: FetchRequest) -> CaptureResult:
        self.calls.append(request)
        if self.fail:
            raise BrowserUnavailable("no playwright")
        return CaptureResult(
            outcome="captured",
            status=200,
            final_url=request.url,
            body=b"<html><body>rendered</body></html>",
            media_type="text/html",
            hash=hashlib.sha256(b"<html><body>rendered</body></html>").hexdigest(),
            captured_at=T0,
            access_flags=[],
        )


JS_SHELL = (
    b"<html><head><script>fetch('/api/boot').then(boot)</script></head>"
    b"<body><div id='root'></div></body></html>"
)


async def test_js_shell_without_browser_is_browser_unavailable() -> None:
    fetcher = make_fetcher(lambda req: ok(JS_SHELL))
    result = await fetcher.fetch(request())
    assert result.outcome == "browser_unavailable"
    assert "js_required" in result.access_flags
    # Never archived as a captured empty shell.
    assert result.body is None


async def test_js_shell_with_browser_renders_dom() -> None:
    browser = StubBrowser()
    fetcher = make_fetcher(lambda req: ok(JS_SHELL), browser=browser)
    result = await fetcher.fetch(request())
    assert result.outcome == "captured"
    assert result.body == b"<html><body>rendered</body></html>"
    assert {"js_required", "browser_rendered"} <= set(result.access_flags)
    assert browser.calls[0].url == GOOD


async def test_js_shell_browser_missing_degrades_distinctly() -> None:
    browser = StubBrowser(fail=True)
    fetcher = make_fetcher(lambda req: ok(JS_SHELL), browser=browser)
    result = await fetcher.fetch(request())
    assert result.outcome == "browser_unavailable"
    assert result.error_code == "browser_unavailable"


async def test_plain_html_never_triggers_browser() -> None:
    browser = StubBrowser()
    fetcher = make_fetcher(
        lambda req: ok(b"<html><body>" + b"paragraph. " * 100 + b"</body></html>"),
        browser=browser,
    )
    result = await fetcher.fetch(request())
    assert result.outcome == "captured"
    assert browser.calls == []


# -- HttpPageClient: untrusted-page caps and hop re-validation (fix round 1) -------


def make_page_client(handler, *, max_bytes: int = 10_000_000):
    from intel.sources.pageclient import HttpPageClient

    return HttpPageClient(
        guard=UrlGuard(resolver=StaticResolver()),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
        max_bytes=max_bytes,
    )


async def test_page_client_returns_page_within_cap() -> None:
    from intel.sources.pageclient import HttpPageClient  # noqa: F401

    client = make_page_client(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>x</html>"
        )
    )
    response = await client.get(GOOD)
    assert response.status == 200
    assert response.body == b"<html>x</html>"


async def test_page_client_rejects_oversized_body_upfront() -> None:
    from intel.sources.pageclient import PageBodyTooLarge

    client = make_page_client(
        lambda req: httpx.Response(
            200,
            headers={"content-type": "text/xml", "content-length": "999999"},
            content=b"<x/>",
        ),
        max_bytes=100,
    )
    with pytest.raises(PageBodyTooLarge):
        await client.get(GOOD)


async def test_page_client_rejects_oversized_body_after_read() -> None:
    from intel.sources.pageclient import PageBodyTooLarge

    client = make_page_client(
        lambda req: httpx.Response(  # lying/absent Content-Length
            200, headers={"content-type": "text/xml"}, content=b"x" * 500
        ),
        max_bytes=100,
    )
    with pytest.raises(PageBodyTooLarge):
        await client.get(GOOD)


async def test_page_client_revalidates_each_redirect_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/"}
        )

    client = make_page_client(handler)
    with pytest.raises(UrlBlockedError) as excinfo:
        await client.get(GOOD)
    assert excinfo.value.reason == "link_local"
