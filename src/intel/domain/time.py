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
"""

from __future__ import annotations

from datetime import UTC, datetime

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
