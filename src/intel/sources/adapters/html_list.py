"""HTML list-page adapter (spec 14 §2: 列表 selector + 链接 selector +
分页规则; spec 04 §3 ING-03 overlap scan).

List pages shift when new items push older ones down or off the page —
there is no stable cursor. The strategy (04 §3 没有稳定游标时重叠扫描和
按 URL 去重): the cursor records the previous run's front-page URLs as the
seen boundary; the walk stops on the page that contains a boundary URL,
re-emitting that page so the handler's canonical-URL upsert dedups the
overlap while catching items inserted mid-list during enumeration. The
final cursor carries THIS run's front-page URLs as the next boundary.

``exhausted`` means "this entrance's reachable list was walked to the
boundary in this run"; a non-None ``next_cursor`` with ``exhausted=True``
is the cross-run boundary, not a partial page.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from intel.sources.adapters.cursor import decode_cursor, encode_cursor
from intel.sources.dto import DiscoveredItem, DiscoveryPage, FeedPlan
from intel.sources.pageclient import PageClient

#: How many front-page URLs form the seen boundary.
_BOUNDARY_SIZE = 20


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


class HtmlListAdapter:
    kind = "html_list"

    def __init__(self, client: PageClient) -> None:
        self._client = client

    async def discover(
        self, plan: FeedPlan, cursor: str | None
    ) -> DiscoveryPage:
        config = plan.config
        state = decode_cursor(cursor)
        stop_boundary = {
            url for url in state.get("boundary", []) if isinstance(url, str)
        }
        page_url = state.get("page_url")
        mid_run = isinstance(page_url, str) and bool(page_url)
        if not mid_run:
            page_url = plan.seed

        response = await self._client.get(page_url)
        soup = BeautifulSoup(response.body, "lxml")
        base = urljoin(str(page_url), str(config.get("base_url", "")) or page_url)

        items: list[DiscoveredItem] = []
        page_urls: list[str] = []
        hit_boundary = False
        for node in soup.select(str(config["list_selector"])):
            link = node.select_one(str(config["link_selector"]))
            if link is None or not link.get("href"):
                continue
            absolute = urljoin(base, link["href"])
            stripped = _strip_fragment(absolute)
            page_urls.append(stripped)
            if stop_boundary and stripped in stop_boundary:
                hit_boundary = True
            title = link.get_text(strip=True)
            if config.get("title_selector"):
                title_node = node.select_one(str(config["title_selector"]))
                if title_node is not None:
                    title = title_node.get_text(strip=True) or title
            date_hint = None
            if config.get("date_selector"):
                date_node = node.select_one(str(config["date_selector"]))
                if date_node is not None:
                    date_hint = _parse_date(
                        date_node.get("datetime") or date_node.get_text()
                    )
            items.append(
                DiscoveredItem(
                    url=absolute, title_hint=title or None, date_hint=date_hint
                )
            )

        # This run's frontier: the front page's newest URLs (recorded on
        # the run's first page, carried through the walk).
        run_top = state.get("boundary_top")
        if not mid_run or not isinstance(run_top, list) or not run_top:
            run_top = page_urls[:_BOUNDARY_SIZE]

        pagination = dict(config.get("pagination", {}))
        mode = pagination.get("mode", "next_link")
        next_page: str | None = None
        if not hit_boundary:
            if mode == "next_link":
                next_node = soup.select_one(str(pagination["next_selector"]))
                if next_node is not None and next_node.get("href"):
                    candidate = urljoin(str(page_url), next_node["href"])
                    if candidate.rstrip("/") != str(page_url).rstrip("/"):
                        next_page = candidate
            elif mode == "page_param":
                next_page = self._advance_page_param(str(page_url), pagination)

        warnings: list[str] = []
        if hit_boundary:
            warnings.append(
                "seen-URL boundary reached: overlap page re-emitted for dedup"
            )

        next_cursor = None
        if next_page:
            payload: dict[str, Any] = {
                "page_url": next_page,
                "boundary": sorted(stop_boundary),
                "boundary_top": list(run_top),
            }
            next_cursor = encode_cursor(payload)
        else:
            # Run end: this run's front page becomes the next boundary.
            next_cursor = encode_cursor({"boundary": sorted(run_top)})

        return DiscoveryPage(
            items=items,
            next_cursor=next_cursor,
            exhausted=next_page is None,
            window_coverage="unknown",
            warnings=warnings,
        )

    @staticmethod
    def _advance_page_param(
        page_url: str, pagination: dict[str, Any]
    ) -> str | None:
        current = urlsplit(page_url)
        query = current.query
        param = str(pagination["page_param"])
        marker = f"{param}="
        if marker in query:
            head, _, tail = query.partition(marker)
            old_value = tail.split("&", 1)[0]
            digits = old_value if old_value.isdigit() else "1"
            rest = tail.split("&", 1)[1] if "&" in tail else ""
            query = f"{head}{marker}{int(digits) + 1}" + (f"&{rest}" if rest else "")
        else:
            query = f"{query}&{marker}=2" if query else f"{marker}=2"
        candidate = urlunsplit(
            (current.scheme, current.netloc, current.path, query, "")
        )
        return None if candidate == page_url else candidate
