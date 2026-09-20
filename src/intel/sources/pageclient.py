"""Page fetching for discovery adapters (feeds, APIs, list pages, sitemaps).

The adapter-facing seam: a tiny async ``get(url)`` protocol so adapters
stay pure (fixtures drive unit tests) while production wraps an httpx
client behind the SSRF guard with manual redirect re-validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

import httpcore
import httpx

from intel.sources.ssrf import GuardedNetworkBackend, UrlGuard


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

    When ``client`` is omitted, a guarded client is built once: its
    httpcore connection pool dials only guard-validated addresses
    (:class:`GuardedNetworkBackend`), and every redirect hop is resolved
    manually through ``guard.validate`` again (SEC-02 每次重定向重校验).
    """

    def __init__(
        self,
        *,
        guard: UrlGuard,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._guard = guard
        self._client = client
        self._owns_client = client is None

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
            return PageResponse(
                status=response.status_code,
                url=str(response.url),
                headers=dict(response.headers),
                body=response.content,
                media_type=response.headers.get("content-type", "").split(";")[0].strip(),
            )
        raise PageFetchError(-1, url=current)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        transport = httpx.AsyncHTTPTransport()
        pool = getattr(transport, "_pool", None)
        if isinstance(pool, httpcore.AsyncConnectionPool):
            pool._network_backend = GuardedNetworkBackend(
                self._guard, delegate=pool._network_backend
            )
        self._client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, timeout=30.0
        )
        return self._client
