"""Browser fallback tests: the deterministic navigation script against a
fake page object, the SSRF request-interception route (redirect hops,
subresources, non-http schemes, fail-closed install), plus the renderer
itself (skipped when playwright browsers are not installed — the offline
default).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from intel.sources.browser import (
    BrowserRenderer,
    deterministic_navigation,
    install_guard_interception,
)
from intel.sources.dto import FetchRequest
from intel.sources.fetcher import BrowserUnavailable
from intel.sources.ssrf import UrlBlockedError, UrlGuard

T0_ISO = "2026-09-20T12:00:00+00:00"
MAIN = "https://portal.example.com/products/x"
META = "http://169.254.169.254/latest/meta-data/"


class StaticResolver:
    async def resolve(self, host: str) -> list[str]:
        return {"internal.corp": ["10.0.0.1"]}.get(host, ["93.184.216.34"])


def make_guard() -> UrlGuard:
    return UrlGuard(resolver=StaticResolver())


def make_request(url: str = MAIN) -> FetchRequest:
    return FetchRequest(
        item_id="00000000-0000-0000-0000-0000000000aa",
        url=url,
        deadline_at=T0_ISO,
    )


# -- fake playwright plumbing ------------------------------------------------------


class FakeRoute:
    def __init__(self, url: str, sink: ScriptedPage) -> None:
        self.request = SimpleNamespace(url=url)
        self._sink = sink
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True
        self._sink.aborted.append(self.request.url)

    async def continue_(self) -> None:
        self.continued = True
        self._sink.allowed.append(self.request.url)


class ScriptedPage:
    """Fake playwright page: ``goto`` drives the scripted request flow
    through the installed interception handlers, mimicking Chromium —
    redirect hops replace the navigation (abort fails goto), subresource
    aborts degrade the page but not the navigation."""

    def __init__(
        self,
        script: dict[str, list[tuple[str, str]]],
        html: str = "<html>rendered</html>",
        final_url: str = MAIN,
    ) -> None:
        self._script = script
        self._html = html
        self.url = final_url
        self.aborted: list[str] = []
        self.allowed: list[str] = []
        self.calls: list[tuple] = []
        self._handlers: list = []

    async def route(self, pattern, handler) -> None:
        self.calls.append(("route", pattern))
        self._handlers.append(handler)

    async def _dispatch(self, url: str) -> FakeRoute:
        route = FakeRoute(url, self)
        for handler in list(self._handlers):
            await handler(route)
        return route

    async def goto(self, url, timeout=None, wait_until=None) -> None:
        self.calls.append(("goto", url, timeout, wait_until))
        main = await self._dispatch(url)
        if main.aborted:
            raise RuntimeError("net::ERR_FAILED")
        for kind, follow in self._script.get(url, []):
            route = await self._dispatch(follow)
            if kind == "redirect" and route.aborted:
                raise RuntimeError("net::ERR_FAILED")

    async def wait_for(self, selector, timeout=None) -> None:
        self.calls.append(("wait_for", selector, timeout))

    async def content(self) -> str:
        self.calls.append(("content",))
        return self._html


# -- deterministic navigation script shape ------------------------------------------


async def test_deterministic_navigation_script_shape() -> None:
    page = ScriptedPage({}, final_url=MAIN)
    log = await install_guard_interception(page, make_guard())
    final_url, body = await deterministic_navigation(
        page,
        MAIN,
        selector="#root",
        nav_timeout_ms=5000,
        wait_timeout_ms=1500,
    )
    assert final_url == MAIN
    assert body == b"<html>rendered</html>"
    # route("**/*") first, then the fixed goto → wait_for → content.
    assert page.calls[0] == ("route", "**/*")
    goto = page.calls[1]
    assert goto[0] == "goto" and goto[1] == MAIN and goto[2] == 5000
    assert goto[3] == "domcontentloaded"
    assert page.calls[2] == ("wait_for", "#root", 1500)
    assert page.calls[3] == ("content",)
    assert log.allowed == [MAIN]


# -- Finding 1: interception re-validates every browser request ----------------------


async def test_interception_aborts_redirect_hop_to_metadata() -> None:
    page = ScriptedPage(
        {MAIN: [("redirect", META)]}  # Chromium follows the 30x itself
    )
    log = await install_guard_interception(page, make_guard())
    with pytest.raises(RuntimeError, match="net::ERR_FAILED"):
        await deterministic_navigation(page, MAIN)
    assert page.allowed == [MAIN]  # hop 1 validated and continued
    assert page.aborted == [META]  # hop 2 guard-aborted
    assert log.aborted == [META]


async def test_interception_aborts_redirect_hop_resolving_private() -> None:
    page = ScriptedPage(
        {MAIN: [("redirect", "https://internal.corp/admin")]}
    )
    await install_guard_interception(page, make_guard())
    with pytest.raises(RuntimeError, match="net::ERR_FAILED"):
        await deterministic_navigation(page, MAIN)
    assert page.aborted == ["https://internal.corp/admin"]


async def test_interception_aborts_metadata_and_file_subresources() -> None:
    page = ScriptedPage(
        {
            MAIN: [
                ("subresource", META),
                ("subresource", "file:///etc/passwd"),
                ("subresource", "data:text/html,hello"),
                ("subresource", "https://cdn.example.com/app.js"),
            ]
        }
    )
    log = await install_guard_interception(page, make_guard())
    final_url, body = await deterministic_navigation(page, MAIN)
    # Navigation completes; only conforming subresources loaded.
    assert final_url == MAIN
    assert body == b"<html>rendered</html>"
    assert page.allowed == [MAIN, "https://cdn.example.com/app.js"]
    assert set(page.aborted) == {META, "file:///etc/passwd", "data:text/html,hello"}
    assert set(log.aborted) == set(page.aborted)


async def test_interception_fail_closed_when_route_api_missing() -> None:
    class NoRoutePage:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def goto(self, url, timeout=None, wait_until=None):
            self.calls.append(("goto", url))

        async def wait_for(self, selector, timeout=None):
            self.calls.append(("wait_for", selector))

        async def content(self):
            self.calls.append(("content",))
            return "<html>x</html>"

    page = NoRoutePage()
    with pytest.raises(BrowserUnavailable, match="interception"):
        await install_guard_interception(page, make_guard())
    # Fail closed: navigation never happens unguarded.
    assert page.calls == []


async def test_interception_fail_closed_when_route_install_raises() -> None:
    class BrokenRoutePage(ScriptedPage):
        async def route(self, pattern, handler):
            raise RuntimeError("context closed")

    page = BrokenRoutePage({})
    with pytest.raises(BrowserUnavailable, match="could not be installed"):
        await install_guard_interception(page, make_guard())
    assert page.calls == []  # no goto either


async def test_renderer_requires_guard_fail_closed() -> None:
    renderer = BrowserRenderer(selector="#root", guard=None)
    with pytest.raises(BrowserUnavailable, match="UrlGuard"):
        await renderer.render(make_request())


# -- renderer happy path (needs real browsers) ---------------------------------------


async def test_browser_renderer_requires_playwright_browsers() -> None:
    """The renderer path needs a real chromium; the offline environment
    does not run `playwright install`, so this exercises the graceful
    skip. Import-guard/browser_unavailable behavior is covered by the
    fetcher tests via a stub browser."""
    pytest.importorskip("playwright.async_api")
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - missing browsers, sandbox...
        pytest.skip(f"playwright browsers unavailable: {exc}")

    renderer = BrowserRenderer(selector="#root", guard=make_guard())
    result = await renderer.render(make_request())
    assert result.outcome == "captured"
    assert "js_required" in result.access_flags
    assert result.hash is not None


async def test_guard_blocks_main_navigation_url_pre_flight() -> None:
    """The render entry validates the request URL before any browser
    launch; blocked targets surface as UrlBlockedError (mapped by the
    fetch handler to scope_violation + audit)."""
    renderer = BrowserRenderer(selector="#root", guard=make_guard())
    with pytest.raises(UrlBlockedError, match="private"):
        await renderer.render(make_request("https://internal.corp/report"))
