"""Domain pure functions (no I/O, no persistence)."""

from intel.domain.time import time_value_bounds
from intel.domain.urlnorm import normalize_url

__all__ = ["normalize_url", "time_value_bounds"]
