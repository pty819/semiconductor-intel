"""Deterministic browser rendering fallback (spec 04 §4, SEC-02).

The navigation script is FIXED — ``goto`` → ``wait_for(selector)`` →
take the DOM — and lives in :func:`deterministic_navigation` so it is
testable against any page-like object without playwright installed
(browser scripts only ever come from published code; no user-supplied
evaluation strings, spec 14 §2).

Every request the browser issues — the initial navigation, each
Chromium-followed 30x redirect hop, meta refresh, JS navigations, and
all subresources — passes through the interception route installed by
:func:`install_guard_interception`, which runs ``guard.validate`` on the
URL and aborts non-conforming requests (private/loopback/link-local
targets, non-http(s) schemes such as ``file:``/``data:``/``about:``).
If the interception API is missing or unusable, the renderer refuses to
navigate at all (:class:`BrowserUnavailable`) — fail closed, never an
unguarded navigation.

``playwright`` is imported lazily inside :meth:`BrowserRenderer.render`;
when the runtime is absent the fetcher reports ``browser_unavailable``
— it NEVER silently falls back to the static shell for a JS-required
page. ``access_flags`` keep ``js_required`` so coverage metrics can
distinguish rendered shells, plus ``subresource_blocked`` when the guard
aborted anything during rendering.

Residual gap, documented honestly: interception validates every request
URL *before* the browser dials, but the browser resolves DNS on its own
— a resolver that alternates public/private answers can slip a private
dial through between validation and connect (no socket-level pinning;
the static fetcher's :class:`~intel.sources.ssrf.GuardedNetworkBackend`
does not apply here). Closing it needs a mandatory proxy or Chromium
``--host-resolver-rules`` pinning in the composition root.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from intel.sources.dto import CaptureResult, FetchRequest
from intel.sources.fetcher import BrowserUnavailable
from intel.sources.ssrf import UrlBlockedError, UrlGuard

#: Default wait target: anything with a body renders past this.
DEFAULT_SELECTOR = "body"

#: Playwright interception catch-all pattern.
INTERCEPT_PATTERN = "**/*"


@dataclass(slots=True)
class InterceptionLog:
    """What the guard route saw (evidence for flags and tests)."""

    allowed: list[str] = field(default_factory=list)
    aborted: list[str] = field(default_factory=list)
    #: route.abort() itself failed (context already torn down etc.)
    abort_failures: int = 0


async def install_guard_interception(page: object, guard: UrlGuard) -> InterceptionLog:
    """Register a request-interception route validating EVERY request URL.

    Covers navigation, redirect hops, meta refresh, JS navigations and
    subresources (``**/*``). Raises :class:`BrowserUnavailable` when the
    route API is absent or the handler cannot be installed — the caller
    must not navigate unguarded (fail closed).
    """
    log = InterceptionLog()

    async def _handler(route) -> None:  # playwright Route
        url = route.request.url
        try:
            await guard.validate(url)
        except UrlBlockedError:
            log.aborted.append(url)
            try:
                await route.abort()
            except Exception:  # noqa: BLE001 - request dies either way
                log.abort_failures += 1
            return
        log.allowed.append(url)
        await route.continue_()

    route_api = getattr(page, "route", None)
    if not callable(route_api):
        raise BrowserUnavailable(
            "playwright page lacks request interception (route); refusing"
            " to navigate unguarded (SEC-02)"
        )
    try:
        await route_api(INTERCEPT_PATTERN, _handler)
    except Exception as exc:
        raise BrowserUnavailable(
            f"guard interception could not be installed: {exc}; refusing"
            " to navigate unguarded (SEC-02)"
        ) from exc
    return log


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
    """Renders JS-required pages through a real browser, deterministically
    and only behind the SSRF guard's request interception."""

    def __init__(
        self,
        *,
        selector: str = DEFAULT_SELECTOR,
        nav_timeout_ms: int = 30_000,
        wait_timeout_ms: int = 10_000,
        guard: UrlGuard | None = None,
    ) -> None:
        self._selector = selector
        self._nav_timeout_ms = nav_timeout_ms
        self._wait_timeout_ms = wait_timeout_ms
        self._guard = guard

    async def render(self, request: FetchRequest) -> CaptureResult:
        if self._guard is None:
            # Fail closed: no guard, no navigation.
            raise BrowserUnavailable(
                "BrowserRenderer requires a UrlGuard; refusing to navigate"
                " unguarded (SEC-02)"
            )
        guard = self._guard
        # Pre-flight before any browser launch: a blocked target never
        # starts Chromium; the fetch handler maps this to
        # scope_violation + audit (same contract as the static fetcher).
        await guard.validate(request.url)
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
                log = await install_guard_interception(page, guard)
                try:
                    final_url, body = await deterministic_navigation(
                        page,
                        request.url,
                        selector=self._selector,
                        nav_timeout_ms=self._nav_timeout_ms,
                        wait_timeout_ms=self._wait_timeout_ms,
                    )
                except Exception as exc:
                    if log.aborted:
                        # The guard aborted a request in this navigation's
                        # chain (main frame or a followed hop) — that is a
                        # scope violation, not a rendering accident.
                        raise UrlBlockedError(
                            "browser_guard_abort", log.aborted[-1]
                        ) from exc
                    raise
            finally:
                await browser.close()
        flags = ["js_required"]
        if log.aborted:
            flags.append("subresource_blocked")
        return CaptureResult(
            outcome="captured",
            status=200,
            final_url=final_url,
            headers={},
            body=body,
            media_type="text/html",
            hash=hashlib.sha256(body).hexdigest(),
            captured_at=datetime.now(UTC),
            access_flags=flags,
        )
