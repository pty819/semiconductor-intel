"""Fixed page monitor adapter (spec 14 §2: page_monitor 需固定 URL).

Discovery always re-emits the monitored URL: the item exists after the
first poll, so the handler's page_monitor branch re-enqueues the fetch
with a fresh refresh_epoch each run (spec 14 §7 page_monitor 按自己的
频率). Change detection is capture-level (conditional GET / content
hash), not discovery-level.
"""

from __future__ import annotations

from intel.sources.dto import DiscoveredItem, DiscoveryPage, FeedPlan
from intel.sources.pageclient import PageClient


class PageMonitorAdapter:
    kind = "page_monitor"

    def __init__(self, client: PageClient) -> None:
        self._client = client

    async def discover(
        self, plan: FeedPlan, cursor: str | None
    ) -> DiscoveryPage:
        return DiscoveryPage(
            items=[DiscoveredItem(url=plan.seed)],
            next_cursor=None,
            exhausted=True,
            window_coverage="unknown",
        )
