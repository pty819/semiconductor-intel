"""Timeline reading: as_of visibility, interval intersection, stable cursor (05 §5).

The four-tier time model (03) makes "what did we know at T" a query over
RECORD versions, not a snapshot table:

- ``select_revision`` picks the latest revision whose ``recorded_at`` ≤
  ``as_of`` — TIM-03: after a correction the OLD revision's text is still
  what an earlier as_of view shows (corrections are new versions, never
  rewrites);
- ``visible_at`` requires the record's discovery time ≤ ``as_of`` — TIM-02:
  今天发现的去年事件在 as_of=上月 不可见 (late discovery stays out of
  historical views; the current timeline includes it with the 迟到标记);
- ``intersects`` does half-open [start, end) interval intersection against
  the query window — TIM-01: a month-precision event covers its whole
  month, so any window overlapping that month matches;
- unknown dates never match ranged queries — they live in the separate
  “日期未知” group (user-hideable), and ``effective_sort_key`` orders them
  last instead of laundering ``discovered_at`` as ``occurred_at``;
- pagination walks ``(effective_sort_key, event_id)`` — the stable cursor.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from intel.contracts.models import TimeValue

__all__ = [
    "decode_cursor",
    "effective_sort_key",
    "encode_cursor",
    "intersects",
    "page_events",
    "select_revision",
    "visible_at",
]


def effective_sort_key(tv: TimeValue) -> tuple[int, str]:
    """Ordering key: known dates ascending, then the separate unknown group.

    The first component isolates unknown dates (group 1) after every
    known date (group 0) — they are a GROUP, not dates, and must never
    sort as their discovery time.
    """
    if tv.precision == "unknown":
        return (1, "")
    return (0, tv.start.isoformat() if tv.start else "")


def intersects(tv: TimeValue, window_from: datetime, window_to: datetime) -> bool:
    """Half-open interval intersection of one TimeValue with a window.

    instant/day/month/year/range all carry a [start, end) span (instant
    treated as the point interval [start, start] tested for containment);
    unknown never matches a ranged filter.
    """
    if tv.precision == "unknown":
        return False
    start = tv.start
    if start is None:  # pragma: no cover - TimeValue validator forbids
        return False
    if tv.precision == "instant":
        return window_from <= start < window_to
    end = tv.end if tv.end is not None else start
    return start < window_to and end > window_from


def visible_at(discovered_at: datetime, as_of: datetime) -> bool:
    """TIM-02: discovery time gates historical views."""
    return discovered_at <= as_of


@dataclass(frozen=True, slots=True)
class RevisionLike:
    """The three fields revision selection needs (any row duck-types)."""

    id: UUID
    recorded_at: datetime
    text: str


def select_revision(
    revisions: Iterable[RevisionLike], as_of: datetime
) -> RevisionLike | None:
    """Latest revision recorded at-or-before as_of (TIM-03: old text stays)."""
    eligible = [revision for revision in revisions if revision.recorded_at <= as_of]
    if not eligible:
        return None
    return max(eligible, key=lambda revision: (revision.recorded_at, revision.id))


def encode_cursor(sort_key: tuple[int, str], event_id: UUID) -> str:
    """Opaque cursor for ``(effective_sort_key, event_id)`` pagination."""
    payload = {"group": sort_key[0], "start": sort_key[1], "id": str(event_id)}
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode("ascii")
    )


def decode_cursor(cursor: str) -> tuple[tuple[int, str], UUID]:
    padding = "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("ascii"))
    return (int(payload["group"]), str(payload["start"])), UUID(payload["id"])


def page_events(
    entries: Sequence[tuple[tuple[int, str], UUID, Any]],
    *,
    cursor: tuple[tuple[int, str], UUID] | None,
    limit: int,
) -> tuple[list[Any], str | None]:
    """Stable pagination over pre-sorted ``(sort_key, event_id, payload)``.

    The caller sorts once (deterministic order); the cursor resumes
    strictly AFTER its ``(sort_key, event_id)`` — equal keys break on the
    event id, so re-pagination never skips or repeats.
    """
    ordered = sorted(entries, key=lambda entry: (entry[0], str(entry[1])))
    if cursor is not None:
        position = (cursor[0], str(cursor[1]))
        ordered = [entry for entry in ordered if ((entry[0], str(entry[1])) > position)]
    page = ordered[:limit]
    next_cursor = (
        encode_cursor(page[-1][0], page[-1][1])
        if len(ordered) > limit and page
        else None
    )
    return [entry[2] for entry in page], next_cursor
