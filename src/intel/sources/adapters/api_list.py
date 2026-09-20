"""JSON API list adapter (spec 14 §2: 固定 endpoint + 分页字段映射).

Config (validated in :mod:`intel.sources.adapters`): a fixed endpoint
whose URL template may only use the whitelisted ``{page}`` placeholder,
a dot-path to the items array, per-item field mappings, and either
numeric paging (``page_param``/``page_start``) or a response-driven
next URL (``next_url_field``). The cursor is the page number (or next
URL); ID-first ordering is the API's own — we enumerate pages until an
empty page (or the budget) ends the run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from intel.sources.adapters.cursor import decode_cursor, encode_cursor
from intel.sources.dto import DiscoveredItem, DiscoveryPage, FeedPlan
from intel.sources.pageclient import PageClient


def _walk(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        # Unix seconds (milliseconds would be absurd for publish dates).
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


class ApiListAdapter:
    kind = "api"

    def __init__(self, client: PageClient) -> None:
        self._client = client

    async def discover(
        self, plan: FeedPlan, cursor: str | None
    ) -> DiscoveryPage:
        config = plan.config
        state = decode_cursor(cursor)
        page_start = int(config.get("page_start", 1))
        page = state.get("page", page_start)
        if not isinstance(page, int):
            page = page_start
        next_url = state.get("next_url")
        if not isinstance(next_url, str):
            next_url = None

        if next_url:
            url = next_url
        else:
            url = str(config["endpoint"]).format(page=page)

        response = await self._client.get(url)
        try:
            data = json.loads(response.body)
        except ValueError as exc:
            raise ValueError(f"API endpoint returned non-JSON body: {url}") from exc

        rows = _walk(data, str(config["items_path"]))
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise TypeError(
                f"items_path {config['items_path']!r} did not resolve to a list"
            )

        url_field = str(config["url_field"])
        items: list[DiscoveredItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            link = row.get(url_field)
            if not isinstance(link, str) or not link:
                continue
            provider_id = row.get(str(config["provider_id_field"])) \
                if config.get("provider_id_field") else None
            items.append(
                DiscoveredItem(
                    url=link,
                    title_hint=row.get(str(config["title_field"]))
                    if config.get("title_field") else None,
                    date_hint=_parse_date(
                        row.get(str(config["date_field"]))
                        if config.get("date_field") else None
                    ),
                    provider_id=str(provider_id) if provider_id is not None else None,
                )
            )

        warnings: list[str] = []
        next_cursor = None
        response_next = None
        if config.get("next_url_field"):
            response_next = _walk(data, str(config["next_url_field"]))
        if isinstance(response_next, str) and response_next:
            next_cursor = encode_cursor({"next_url": response_next})
        elif items:
            next_cursor = encode_cursor({"page": page + 1})
        if not items and not response_next:
            warnings.append("empty page: enumeration reached the API end")

        return DiscoveryPage(
            items=items,
            next_cursor=next_cursor,
            exhausted=next_cursor is None,
            window_coverage="unknown",
            warnings=warnings,
        )
