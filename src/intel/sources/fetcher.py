"""Static HTTP fetcher (spec 04 §4, 14 §2 FetchRequest/CaptureResult).

``HttpFetcher.fetch`` performs ONE conditional GET with:

- **SSRF** — pre-flight ``UrlGuard.validate``, per-hop re-validation on
  every manual redirect (cap 5), and a pinned connection pool when the
  fetcher builds its own client (SEC-02).
- **Conditional requests** — If-None-Match / If-Modified-Since from the
  prior capture only; a 304 returns ``no_change`` for the caller to bind
  the existing capture (ING-04).
- **Limits** — ``max_bytes`` enforced against Content-Length up front and
  against the decompressed body after read (httpx decompresses while
  reading, so the accumulated size IS the decompressed size); the whole
  exchange runs under the request deadline.
- **Outcomes** — captured / no_change / error / access_denied /
  browser_unavailable, with header-allowlist capture, sha256 content
  hash, and login-redirect / JS-required heuristics. The fetcher NEVER
  persists; the workflow's commit transaction owns persistence.

Known residual gap (documented): the body is read fully before the size
check when Content-Length is absent or lying; a hostile server can push
``max_bytes`` plus buffering into memory before the cap aborts. A true
streaming abort needs a transport-level reader hook; recorded in the
Task 7 report as an accepted offline-environment limitation.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Protocol
from urllib.parse import urljoin

import httpx

from intel.sources.dto import (
    CONDITIONAL_HEADER_ALLOWLIST,
    HEADER_ALLOWLIST,
    MAX_REDIRECTS,
    CaptureResult,
    FetchRequest,
)
from intel.sources.ssrf import UrlGuard, guarded_async_client

_USER_AGENT = "semiconductor-intel/0.1 (+polite polling)"
#: Empty-body HTML below this much visible text is a JS shell.
_JS_TEXT_FLOOR = 200

#: Redirect targets that look like an authentication wall.
_LOGIN_PATH_RE = re.compile(
    r"(login|log-in|signin|sign-in|logon|sso|oauth|authenticate|session/new)",
    re.IGNORECASE,
)
_LOGIN_HOST_RE = re.compile(
    r"^(accounts?|login|signin|auth|sso|passport)\.", re.IGNORECASE
)


class BrowserUnavailable(Exception):
    """No playwright runtime; JS-required pages cannot be rendered."""


class BrowserRenderer(Protocol):
    """The fetcher's fallback branch (sources/browser.py implements it)."""

    async def render(self, request: FetchRequest) -> CaptureResult: ...


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    now = now if now is not None else datetime.now(UTC)
    return max((when - now).total_seconds(), 0.0)


def format_http_date(value: datetime) -> str:
    return format_datetime(value, usegmt=True)


def _looks_like_login(url: str) -> bool:
    parts = httpx.URL(url)
    return bool(_LOGIN_HOST_RE.match(parts.host or "")) or bool(
        _LOGIN_PATH_RE.search(parts.path or "")
    )


def _looks_js_required(media_type: str, body: bytes) -> bool:
    """Heuristic: an HTML shell whose visible text is empty but that
    carries scripts needs a renderer (spec 04 §4 确定性浏览器导航)."""
    if not media_type.startswith("text/html"):
        return False
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(body)
    except Exception:  # noqa: BLE001 - unparseable markup is not JS proof
        return False
    text = tree.text_content().strip()
    return len(text) < _JS_TEXT_FLOOR and len(tree.findall(".//script")) > 0


class HttpFetcher:
    """Fetcher over httpx; owns no persistence and no queue semantics."""

    def __init__(
        self,
        *,
        guard: UrlGuard,
        client: httpx.AsyncClient | None = None,
        browser: BrowserRenderer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._guard = guard
        self._client = client
        self._owns_client = client is None
        self._browser = browser
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, request: FetchRequest) -> CaptureResult:
        self._validate_conditional_headers(request)
        await self._guard.validate(request.url)  # pre-flight (SEC-02)
        client = self._ensure_client()
        remaining = (request.deadline_at - self._clock()).total_seconds()
        try:
            async with asyncio.timeout(max(remaining, 0.001)):
                return await self._exchange(client, request)
        except TimeoutError:
            return self._error(request, status=None, error_code="timeout")
        except httpx.TimeoutException:
            return self._error(request, status=None, error_code="timeout")
        except httpx.TransportError as exc:
            return self._error(
                request, status=None, error_code="network", message=exc
            )

    # -- internals -------------------------------------------------------------

    async def _exchange(
        self, client: httpx.AsyncClient, request: FetchRequest
    ) -> CaptureResult:
        url = request.url
        login_hop = False
        for _hop in range(MAX_REDIRECTS + 1):
            headers = {"user-agent": _USER_AGENT, "accept": "*/*"}
            headers.update(request.conditional_headers)
            response = await client.get(url, headers=headers)
            location = response.headers.get("location")
            if response.is_redirect and location:
                target = urljoin(str(response.url), location)
                if _looks_like_login(target):
                    login_hop = True
                await self._guard.validate(target)  # per-hop re-check (SEC-02)
                url = target
                continue
            if _hop == MAX_REDIRECTS:
                return self._error(
                    request, status=response.status_code,
                    error_code="redirect_limit",
                )
            return await self._terminal(request, response, url, login_hop)
        return self._error(request, status=None, error_code="redirect_limit")

    async def _terminal(
        self,
        request: FetchRequest,
        response: httpx.Response,
        final_url: str,
        login_hop: bool,
    ) -> CaptureResult:
        now = self._clock()
        status = response.status_code
        allowlist = {
            name: value
            for name, value in response.headers.items()
            if name.lower() in HEADER_ALLOWLIST
        }

        if status == 304:
            return CaptureResult(
                outcome="no_change",
                status=304,
                final_url=str(response.url),
                headers=allowlist,
                captured_at=now,
            )

        if status == 429:
            return CaptureResult(
                outcome="error",
                status=429,
                final_url=str(response.url),
                headers=allowlist,
                captured_at=now,
                error_code="http_429",
                retry_after=parse_retry_after(
                    response.headers.get("retry-after"), now=now
                ),
            )

        if status in (401, 403) or (login_hop and status == 200):
            return CaptureResult(
                outcome="access_denied",
                status=status,
                final_url=str(response.url),
                headers=allowlist,
                captured_at=now,
                access_flags=(["auth"] if status in (401, 403) else ["login_redirect"]),
                error_code=f"http_{status}" if status in (401, 403) else "login_redirect",
            )

        if status >= 400:
            return CaptureResult(
                outcome="error",
                status=status,
                final_url=str(response.url),
                headers=allowlist,
                captured_at=now,
                error_code=f"http_{status}",
            )

        # 2xx — enforce the size cap before and after reading.
        content_length = response.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > request.max_bytes
        ):
                return self._error(
                    request, status=status, error_code="body_too_large"
                )
        body = response.read()
        if len(body) > request.max_bytes:
            return self._error(request, status=status, error_code="body_too_large")

        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        digest = hashlib.sha256(body).hexdigest()

        if _looks_js_required(media_type, body):
            if self._browser is None:
                return CaptureResult(
                    outcome="browser_unavailable",
                    status=status,
                    final_url=str(response.url),
                    headers=allowlist,
                    captured_at=now,
                    access_flags=["js_required"],
                    error_code="browser_unavailable",
                )
            try:
                rendered = await self._browser.render(request)
            except BrowserUnavailable:
                return CaptureResult(
                    outcome="browser_unavailable",
                    status=status,
                    final_url=str(response.url),
                    headers=allowlist,
                    captured_at=now,
                    access_flags=["js_required"],
                    error_code="browser_unavailable",
                )
            # The rendered DOM is the evidence; the static shell's
            # headers/URL stay as transport metadata.
            rendered.access_flags = sorted(
                {"js_required", "browser_rendered", *rendered.access_flags}
            )
            return rendered

        return CaptureResult(
            outcome="captured",
            status=status,
            final_url=str(response.url),
            headers=allowlist,
            body=body,
            media_type=media_type,
            hash=digest,
            captured_at=now,
        )

    def _error(
        self,
        request: FetchRequest,
        *,
        status: int | None,
        error_code: str,
        message: Exception | None = None,
    ) -> CaptureResult:
        return CaptureResult(
            outcome="error",
            status=status,
            final_url=request.url,
            captured_at=self._clock(),
            error_code=error_code,
        )

    def _validate_conditional_headers(self, request: FetchRequest) -> None:
        for name in request.conditional_headers:
            if name.lower() not in CONDITIONAL_HEADER_ALLOWLIST:
                raise ValueError(
                    f"conditional header {name!r} is not allowed on a"
                    " FetchRequest (spec 14 §2 header whitelist)"
                )

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        # Construction fails loudly when the pin cannot be installed —
        # never a silently unguarded pool (Finding 2).
        self._client = guarded_async_client(self._guard, timeout=60.0)
        return self._client
