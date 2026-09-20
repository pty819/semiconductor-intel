"""Deterministic browser rendering fallback (spec 04 §4).

The navigation script is FIXED — ``goto`` → ``wait_for(selector)`` →
take the DOM — and lives in :func:`deterministic_navigation` so it is
testable against any page-like object without playwright installed
(browser scripts only ever come from published code; no user-supplied
evaluation strings, spec 14 §2).

``playwright`` is imported lazily inside :meth:`BrowserRenderer.render`;
when the runtime is absent the renderer raises
:class:`~intel.sources.fetcher.BrowserUnavailable` and the fetcher
reports ``browser_unavailable`` — it NEVER silently falls back to the
static shell for a JS-required page (that would archive an empty body as
a success). ``access_flags`` keep ``js_required`` so coverage metrics can
distinguish rendered shells.

Residual gap, documented honestly: playwright resolves DNS and dials on
its own; the guard validates the URL pre-navigation, but socket-level
pinning (the anti-rebinding property the static fetcher has via
:class:`~intel.sources.ssrf.GuardedNetworkBackend`) would need
CDP/launch-arg plumbing. Recorded in the Task 7 report.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from intel.sources.dto import CaptureResult, FetchRequest
from intel.sources.fetcher import BrowserUnavailable

#: Default wait target: anything with a body renders past this.
DEFAULT_SELECTOR = "body"


async def deterministic_navigation(
    page: object,
    url: str,
    *,
    selector: str = DEFAULT_SELECTOR,
    nav_timeout_ms: int = 30_000,
    wait_timeout_ms: int = 10_000,
) -> tuple[str, bytes]:
    """The fixed navigation script; ``page`` only needs
    ``goto``/``wait_for``/``content``/``url`` (playwright Page or a fake)."""
    await page.goto(url, timeout=nav_timeout_ms, wait_until="domcontentloaded")
    await page.wait_for(selector, timeout=wait_timeout_ms)
    html = await page.content()
    return str(getattr(page, "url", url)), html.encode("utf-8")


class BrowserRenderer:
    """Renders JS-required pages through a real browser, deterministically."""

    def __init__(
        self,
        *,
        selector: str = DEFAULT_SELECTOR,
        nav_timeout_ms: int = 30_000,
        wait_timeout_ms: int = 10_000,
        guard: object | None = None,
    ) -> None:
        self._selector = selector
        self._nav_timeout_ms = nav_timeout_ms
        self._wait_timeout_ms = wait_timeout_ms
        self._guard = guard

    async def render(self, request: FetchRequest) -> CaptureResult:
        if self._guard is not None:
            # Pre-navigation validation only; see the module docstring for
            # the pinning gap.
            await self._guard.validate(request.url)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - offline default
            raise BrowserUnavailable(
                "playwright is not installed; JS-required pages cannot be"
                " rendered"
            ) from exc
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                final_url, body = await deterministic_navigation(
                    page,
                    request.url,
                    selector=self._selector,
                    nav_timeout_ms=self._nav_timeout_ms,
                    wait_timeout_ms=self._wait_timeout_ms,
                )
            finally:
                await browser.close()
        return CaptureResult(
            outcome="captured",
            status=200,
            final_url=final_url,
            headers={},
            body=body,
            media_type="text/html",
            hash=hashlib.sha256(body).hexdigest(),
            captured_at=datetime.now(UTC),
            access_flags=["js_required"],
        )
