"""Browser fallback tests: the deterministic navigation script against a
fake page object, plus the renderer itself (skipped when playwright
browsers are not installed — the offline default).
"""

from __future__ import annotations

import pytest

from intel.sources.browser import BrowserRenderer, deterministic_navigation
from intel.sources.dto import FetchRequest

T0_ISO = "2026-09-20T12:00:00+00:00"


class FakePage:
    """Page-like object recording the fixed navigation steps."""

    def __init__(self, html: str, final_url: str) -> None:
        self.html = html
        self.url = final_url
        self.calls: list[tuple] = []

    async def goto(self, url, timeout=None, wait_until=None):
        self.calls.append(("goto", url, timeout, wait_until))

    async def wait_for(self, selector, timeout=None):
        self.calls.append(("wait_for", selector, timeout))

    async def content(self):
        self.calls.append(("content",))
        return self.html


def make_request() -> FetchRequest:
    return FetchRequest(
        item_id="00000000-0000-0000-0000-0000000000aa",
        url="https://portal.example.com/products/x",
        deadline_at=T0_ISO,
    )


async def test_deterministic_navigation_script_shape() -> None:
    page = FakePage("<html>rendered</html>", "https://portal.example.com/products/x")
    final_url, body = await deterministic_navigation(
        page,
        "https://portal.example.com/products/x",
        selector="#root",
        nav_timeout_ms=5000,
        wait_timeout_ms=1500,
    )
    assert final_url == "https://portal.example.com/products/x"
    assert body == b"<html>rendered</html>"
    # goto → wait_for(selector) → content, in that fixed order.
    assert page.calls[0][0] == "goto"
    assert page.calls[0][1] == "https://portal.example.com/products/x"
    assert page.calls[0][2] == 5000
    assert page.calls[0][3] == "domcontentloaded"
    assert page.calls[1] == ("wait_for", "#root", 1500)
    assert page.calls[2] == ("content",)


async def test_browser_renderer_requires_playwright_browsers() -> None:
    """The renderer path needs a real chromium; the offline environment
    does not run `playwright install`, so this exercises the graceful
    skip. The import-guard/browser_unavailable behavior is covered by the
    fetcher tests via a stub browser."""
    pytest.importorskip("playwright.async_api")
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - missing browsers, sandbox...
        pytest.skip(f"playwright browsers unavailable: {exc}")

    renderer = BrowserRenderer(selector="#root")
    result = await renderer.render(make_request())
    assert result.outcome == "captured"
    assert "js_required" in result.access_flags
    assert result.hash is not None
