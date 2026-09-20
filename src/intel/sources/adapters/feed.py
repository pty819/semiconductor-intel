"""Shared RSS/Atom enumeration (spec 04 §3).

Syndication feeds paginate via RFC 5005 ``rel="next"`` archive links.
The cursor is the resume position for the NEXT discover call:

- mid-run: ``{"next": <archive url>, "boundary_date": <oldest seen>}``
- run end: ``{"boundary_date": <oldest seen>}`` — the next poll restarts
  at the seed but keeps the boundary, which is what makes the overlap
  window work across polls: entries older than
  ``boundary_date - overlap_hours`` are history (already walked), while
  late arrivals inside the window are kept (ING-01 pagination + 04 §3
  72h 重叠窗口补迟到).

``exhausted`` means "no further archive page in this run"; a non-None
``next_cursor`` with ``exhausted=True`` is the cross-run boundary, not a
partial page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser

from intel.sources.adapters.cursor import decode_cursor, encode_cursor
from intel.sources.dto import DiscoveredItem, DiscoveryPage, FeedPlan
from intel.sources.pageclient import PageClient


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=UTC)
    return None


def _next_link(parsed: Any) -> str | None:
    for link in parsed.feed.get("links", []):
        if link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None


def _parse_boundary(state: dict[str, Any]) -> datetime | None:
    raw = state.get("boundary_date")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


class SyndicationAdapter:
    """One archive page per ``discover`` call; the handler loops."""

    kind = "rss"

    def __init__(self, client: PageClient) -> None:
        self._client = client

    async def discover(
        self, plan: FeedPlan, cursor: str | None
    ) -> DiscoveryPage:
        state = decode_cursor(cursor)
        next_url = state.get("next")
        page_url = next_url if isinstance(next_url, str) and next_url else plan.seed
        boundary = _parse_boundary(state)

        response = await self._client.get(page_url)
        parsed = feedparser.parse(response.body)

        items: list[DiscoveredItem] = []
        dates: list[datetime] = []
        dropped_old = 0
        for entry in parsed.entries:
            link = getattr(entry, "link", "") or ""
            if not link:
                continue
            date_hint = _entry_datetime(entry)
            if boundary is not None and date_hint is not None:
                cutoff = boundary - timedelta(hours=plan.overlap_hours)
                if date_hint < cutoff:
                    dropped_old += 1
                    continue
            items.append(
                DiscoveredItem(
                    url=link,
                    title_hint=getattr(entry, "title", None),
                    date_hint=date_hint,
                )
            )
            if date_hint is not None:
                dates.append(date_hint)

        warnings: list[str] = []
        if dropped_old:
            warnings.append(
                f"{dropped_old} entries older than the overlap window"
                f" ({plan.overlap_hours}h) skipped"
            )

        if dates:
            boundary = min(dates) if boundary is None else min(boundary, min(dates))

        archive_next = _next_link(parsed)
        next_cursor = None
        if archive_next:
            payload: dict[str, Any] = {"next": archive_next}
            if boundary is not None:
                payload["boundary_date"] = boundary.isoformat()
            next_cursor = encode_cursor(payload)
        elif boundary is not None:
            # Run end: keep the boundary so the next poll overlaps 72h.
            next_cursor = encode_cursor({"boundary_date": boundary.isoformat()})

        window_coverage = "unknown"
        if boundary is not None and dates:
            window_coverage = "within_window"
        elif boundary is not None and dropped_old and not items:
            window_coverage = "beyond_window"

        return DiscoveryPage(
            items=items,
            next_cursor=next_cursor,
            exhausted=not archive_next,
            window_coverage=window_coverage,
            warnings=warnings,
        )
