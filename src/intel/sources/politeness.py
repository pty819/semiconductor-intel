"""Per-domain politeness (spec 04 §2: 初始默认每域并发 2、请求间隔至少 2 秒).

``PolitenessGate.request(host)`` is an async context manager: at most
``per_domain_concurrency`` in-flight fetches per hostname, and at least
``min_interval_seconds`` between consecutive releases per hostname.
Stricter per-source constraints ride the feed config later; this is the
global default the spec pins.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic

DEFAULT_PER_DOMAIN_CONCURRENCY = 2
DEFAULT_MIN_INTERVAL_SECONDS = 2.0


class PolitenessGate:
    def __init__(
        self,
        *,
        per_domain_concurrency: int = DEFAULT_PER_DOMAIN_CONCURRENCY,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._concurrency = per_domain_concurrency
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self._concurrency)
        )
        self._last_release: dict[str, float] = {}

    @asynccontextmanager
    async def request(self, host: str) -> AsyncIterator[None]:
        """Yield while holding one politeness slot for ``host``."""
        host = (host or "").lower()
        semaphore = self._semaphores[host]
        await semaphore.acquire()
        try:
            last = self._last_release.get(host)
            if last is not None:
                wait = self._min_interval - (self._clock() - last)
                if wait > 0:
                    await self._sleep(wait)
            yield
        finally:
            self._last_release[host] = self._clock()
            semaphore.release()
