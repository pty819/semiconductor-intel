"""Page fetching for discovery adapters (feeds, APIs, list pages, sitemaps).

The adapter-facing seam: a tiny async ``get(url)`` protocol so adapters
stay pure (fixtures drive unit tests) while production wraps an httpx
client behind the SSRF guard with manual redirect re-validation.

Untrusted-content limits (Task 7 fix round 1): bodies are capped at
``max_bytes`` (Content-Length pre-check + post-read length check, the
same documented post-read semantics as the fetcher) so a hostile feed
or sitemap cannot balloon memory during discovery, and XML consumers
parse with hardened parser settings (see adapters/sitemap.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

import httpx

from intel.sources.ssrf import UrlGuard, guarded_async_client

#: Discovery pages cap at the same default budget as fetch bodies.
DEFAULT_PAGE_MAX_BYTES = 10_000_000


@dataclass(frozen=True, slots=True)
class PageResponse:
    """One successfully fetched (2xx/3xx-followed) page."""

    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    media_type: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class PageFetchError(Exception):
    """A non-2xx page fetch; ``retry_after`` carries a parsed 429 hint."""

    def __init__(
        self,
        status: int,
        *,
        url: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"page fetch failed with HTTP {status}: {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after


class PageBodyTooLarge(PageFetchError):
    """The page body exceeded ``max_bytes`` (non-retryable: the same page
    will still be too large next attempt)."""

    def __init__(self, url: str, max_bytes: int) -> None:
        super().__init__(-1, url=url)
        self.max_bytes = max_bytes


class PageClient(Protocol):
    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> PageResponse: ...


#: Page-client redirect cap (each hop re-validated).
_PAGE_REDIRECT_CAP = 5


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class HttpPageClient:
    """Production :class:`PageClient` behind the :class:`UrlGuard`.

    When ``client`` is omitted, a guarded client is built once via
    :func:`~intel.sources.ssrf.guarded_async_client` (construction fails
    loudly when the pin cannot be installed); every redirect hop is
    resolved manually through ``guard.validate`` again (SEC-02 每次重定向
    重校验), and bodies are size-capped like fetch bodies.
    """

    def __init__(
        self,
        *,
        guard: UrlGuard,
        client: httpx.AsyncClient | None = None,
        max_bytes: int = DEFAULT_PAGE_MAX_BYTES,
    ) -> None:
        self._guard = guard
        self._client = client
        self._owns_client = client is None
        self._max_bytes = max_bytes

    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> PageResponse:
        await self._guard.validate(url)  # pre-flight (SEC-02)
        client = self._ensure_client()
        current = url
        for _hop in range(_PAGE_REDIRECT_CAP + 1):
            response = await client.get(
                current, headers=dict(headers) if headers else None
            )
            location = response.headers.get("location")
            if response.is_redirect and location:
                target = urljoin(str(response.url), location)
                await self._guard.validate(target)  # per-hop re-check
                current = target
                continue
            if response.status_code >= 400:
                raise PageFetchError(
                    response.status_code,
                    url=str(response.url),
                    retry_after=_parse_retry_after(
                        response.headers.get("retry-after")
                    ),
                )
            self._check_size(response)
            return PageResponse(
                status=response.status_code,
                url=str(response.url),
                headers=dict(response.headers),
                body=response.content,
                media_type=response.headers.get("content-type", "").split(";")[0].strip(),
            )
        raise PageFetchError(-1, url=current)

    def _check_size(self, response: httpx.Response) -> None:
        """Same documented post-read semantics as the fetcher: trust an
        honest Content-Length up front, always verify the read body."""
        content_length = response.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > self._max_bytes
        ):
            raise PageBodyTooLarge(str(response.url), self._max_bytes)
        if len(response.content) > self._max_bytes:
            raise PageBodyTooLarge(str(response.url), self._max_bytes)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        self._client = guarded_async_client(self._guard, timeout=30.0)
        return self._client
