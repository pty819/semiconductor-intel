"""Absolute time-bound semantics for TimeValue (spec 03 §5).

Rules implemented here:
- Intervals are uniformly half-open ``[start, end)``.
- A day is local midnight to the next local midnight; a TimeValue stores the
  bounds as aware datetimes, and this module is the single place that turns
  them into absolute UTC bounds.
- An unknown time has both bounds ``None`` — it must never contribute
  fabricated bounds.
- Month/year precision covers the whole period; no specific day is invented
  (that is a construction rule for whoever builds the TimeValue; this
  function only projects the stored interval).

Structural validity (known → start present; instant → start only; others →
nonempty half-open interval) is enforced by TimeValue's own validators.

``parse_occurrence_time`` is the deterministic parser the event-build
workflow runs on the model's proposed time expression (model proposes, code
validates — 05 §5 四类时间): unparseable text downgrades to ``unknown``,
never raises, and never invents a day for month/year precision.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from intel.contracts.models import TimeValue


def time_value_bounds(tv: TimeValue) -> tuple[datetime | None, datetime | None]:
    """Return the absolute bounds of *tv*, normalized to UTC.

    - ``unknown``  → ``(None, None)``.
    - ``instant``  → ``(start, start)``: a point is the degenerate empty
      half-open interval ``[t, t)``. Callers matching points against a
      window ``[a, b)`` must test ``a <= t < b`` directly, not interval
      overlap.
    - everything else → the stored ``[start, end)`` half-open interval.

    Datetimes are returned with ``tzinfo`` UTC; absolute values are unchanged
    (``astimezone`` only re-renders the offset).
    """
    if tv.precision == "unknown":
        return (None, None)
    if tv.start is None:  # pragma: no cover - rejected by TimeValue validators
        raise ValueError("known TimeValue requires start")
    start = tv.start.astimezone(UTC)
    if tv.precision == "instant":
        return (start, start)
    if tv.end is None:  # model_construct can bypass the DTO validators
        raise ValueError(
            f"TimeValue with precision={tv.precision!r} requires a non-None end"
        )
    return (start, tv.end.astimezone(UTC))


def occurred_span(tv: TimeValue) -> tuple[datetime | None, datetime | None]:
    """``(occurred_start, occurred_end)`` exactly as the event_revisions ORM
    defines its projection columns (spec 03 §8):

    - ``unknown`` → ``(None, None)`` — no fabricated bounds;
    - ``instant`` → ``(start, None)`` — NULL end, range queries compare by
      ``occurred_start`` alone (``coalesce(occurred_end, occurred_start)``);
    - everything else → the stored half-open ``[start, end)`` interval.
    """
    if tv.precision == "unknown":
        return (None, None)
    if tv.start is None:  # pragma: no cover - rejected by TimeValue validators
        raise ValueError("known TimeValue requires start")
    start = tv.start.astimezone(UTC)
    if tv.precision == "instant":
        return (start, None)
    if tv.end is None:  # pragma: no cover - rejected by TimeValue validators
        raise ValueError(f"TimeValue precision={tv.precision!r} requires end")
    return (start, tv.end.astimezone(UTC))


# Date shapes the deterministic parser accepts (03 §5): an ISO or 中文
# calendar date embedded anywhere in the proposed expression. Datetimes
# with a time component parse as instants; bare dates carry day/month/year
# precision covering the whole period (no specific hour is invented).
_ISO_DATETIME = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ]"
    r"(?P<h>\d{2}):(?P<mi>\d{2})(?::\d{2})?(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)
_ISO_DATE = re.compile(r"(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})")
_ISO_MONTH = re.compile(r"(?P<y>\d{4})-(?P<mo>\d{1,2})")
_ISO_YEAR = re.compile(r"(?P<y>\d{4})(?!\d|-\d)")
_CN_DATETIME = re.compile(
    r"(?P<y>\d{4})年(?P<mo>\d{1,2})月(?P<d>\d{1,2})日\s*"
    r"(?P<h>\d{1,2})时(?::?(?P<mi>\d{1,2})分)?"
)
_CN_DATE = re.compile(r"(?P<y>\d{4})年(?P<mo>\d{1,2})月(?P<d>\d{1,2})日")
_CN_MONTH = re.compile(r"(?P<y>\d{4})年(?P<mo>\d{1,2})月")
_CN_YEAR = re.compile(r"(?P<y>\d{4})年")


def _tz(offset_text: str | None) -> timezone | None:
    if offset_text is None:
        return None
    if offset_text == "Z":
        return UTC
    text = offset_text.replace(":", "")
    sign = -1 if text[0] == "-" else 1
    hours, minutes = int(text[1:3]), int(text[3:5])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _day(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, day, tzinfo=UTC)
    return (start, start + timedelta(days=1))


def _month_of(year: int, month: int) -> tuple[datetime, datetime]:
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return (datetime(year, month, 1, tzinfo=UTC), end)


def _year_of(year: int) -> tuple[datetime, datetime]:
    return (
        datetime(year, 1, 1, tzinfo=UTC),
        datetime(year + 1, 1, 1, tzinfo=UTC),
    )


def _build(
    precision: str, start: datetime, end: datetime | None, original: str
) -> TimeValue:
    return TimeValue(
        start=start,
        end=end,
        precision=precision,  # type: ignore[arg-type]
        timezone="UTC",
        original_text=original,
        basis="explicit",
    )


def parse_occurrence_time(text: str) -> TimeValue:
    """Deterministically parse a proposed time expression into a TimeValue.

    The model proposes the expression verbatim (``EventProposal.occurred``);
    this code decides what (if anything) it means — first match wins, wider
    shapes before narrower ones. Anything unparseable (including empty
    text) downgrades to ``precision="unknown"``; this function never
    raises and never invents a specific day for month/year precision
    (03 §5).
    """
    raw = (text or "").strip()
    if not raw:
        return TimeValue(precision="unknown")

    try:
        match = _ISO_DATETIME.search(raw) or _CN_DATETIME.search(raw)
        if match is not None:
            start = datetime(
                int(match["y"]),
                int(match["mo"]),
                int(match["d"]),
                int(match["h"]),
                int(match["mi"] or 0),
                tzinfo=(_tz(match.groupdict().get("tz")) or UTC),
            ).astimezone(UTC)
            return _build("instant", start, None, match.group(0))

        match = _ISO_DATE.search(raw) or _CN_DATE.search(raw)
        if match is not None:
            start, end = _day(int(match["y"]), int(match["mo"]), int(match["d"]))
            return _build("day", start, end, match.group(0))

        match = _ISO_MONTH.search(raw) or _CN_MONTH.search(raw)
        if match is not None:
            start, end = _month_of(int(match["y"]), int(match["mo"]))
            return _build("month", start, end, match.group(0))

        match = _CN_YEAR.search(raw) or _ISO_YEAR.search(raw)
        if match is not None:
            start, end = _year_of(int(match["y"]))
            return _build("year", start, end, match.group(0))
    except ValueError:
        # e.g. 2026年13月 — structurally matched, semantically invalid:
        # unknown, never a crash (03 §5 downgrade rule).
        return TimeValue(precision="unknown")
    return TimeValue(precision="unknown")
