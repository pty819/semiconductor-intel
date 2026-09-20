"""Ingestion DTOs (spec 14 §2 Source与parser DTO, 02 §6 protocols).

Service-internal contracts for the discovery/fetch pipeline. The design
pack's ``contracts/models.py`` stays a verbatim API-DTO copy, so the
ingestion shapes live here next to their consumers:

- :class:`FeedPlan` — one enumerated entrance: no topic keywords, no
  secrets (secrets resolve only inside the fetch gateway).
- :class:`DiscoveryPage` — one adapter step: items plus the cursor
  protocol. "No next_cursor and not exhausted" means partial (02 §6).
- :class:`FetchRequest` / :class:`CaptureResult` — the fetch boundary:
  header allowlist, size/deadline limits, no persistence (the workflow
  commits captures in its own transaction).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Adapter kinds (owner_feeds.adapter_type values, spec 03 §2).
ADAPTER_KINDS = ("rss", "atom", "api", "html_list", "sitemap", "page_monitor")

#: CaptureResult.outcome vocabulary (spec 14 §2 + Task 7 brief).
CaptureOutcome = Literal[
    "captured",
    "no_change",
    "error",
    "access_denied",
    "browser_unavailable",
]

#: How a DiscoveryPage relates to the overlap window (spec 04 §3: 到达已知
#: 边界后继续一个 72h 重叠窗口).
WindowCoverage = Literal["within_window", "beyond_window", "unknown"]


class IngestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoverLimits(IngestModel):
    """Per-run enumeration budget (FeedPlan.limits)."""

    max_pages: int = Field(default=10, ge=1)
    max_items: int = Field(default=500, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)


class FeedPlan(IngestModel):
    """What one adapter run needs (spec 14 §2 FeedPlan).

    ``config`` is the adapter config JSON *already validated* by
    :func:`intel.sources.adapters.validate_adapter_config` — no arbitrary
    code, only the per-kind allowlisted keys.
    """

    feed_id: UUID
    owner_id: UUID
    seed: str
    adapter: str
    config_version: int = Field(default=1, ge=1)
    config: dict[str, Any] = Field(default_factory=dict)
    cursor: str | None = None
    #: Overlap window for date-based feeds (spec 04 §3, default 72h).
    overlap_hours: int = Field(default=72, ge=0)
    limits: DiscoverLimits = Field(default_factory=DiscoverLimits)


class DiscoveredItem(IngestModel):
    """One enumerated candidate (spec 14 §2 DiscoveryPage items)."""

    url: str
    title_hint: str | None = None
    date_hint: datetime | None = None
    provider_id: str | None = None


class DiscoveryPage(IngestModel):
    """One adapter step's result plus the cursor protocol (02 §6).

    ``next_cursor`` set → more pages known; ``exhausted`` → the entrance's
    reachable history ended. Neither → partial (the caller records a
    continuation, never truncates to "complete").
    """

    items: list[DiscoveredItem] = Field(default_factory=list)
    next_cursor: str | None = None
    exhausted: bool = False
    window_coverage: WindowCoverage = "unknown"
    warnings: list[str] = Field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.next_cursor is None and not self.exhausted


class FetchRequest(IngestModel):
    """The fetch boundary contract (spec 14 §2 FetchRequest).

    ``conditional_headers`` carries only If-None-Match / If-Modified-Since
    derived from the prior capture; the fetcher never forwards arbitrary
    caller headers to the wire (no host/header injection).
    """

    item_id: UUID
    url: str
    conditional_headers: dict[str, str] = Field(default_factory=dict)
    visibility_scope_key: str = "public"
    max_bytes: int = Field(default=10_000_000, ge=1)
    deadline_at: datetime


#: Response headers kept as business evidence (spec 04 §4: header
#: allowlist; cookie/Authorization never saved).
HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "content-disposition",
        "etag",
        "last-modified",
        "date",
        "location",
        "retry-after",
        "server",
    }
)

#: Conditional headers a FetchRequest may carry; everything else is
#: rejected before the request leaves the process.
CONDITIONAL_HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {"if-none-match", "if-modified-since"}
)

#: Redirect cap for the static fetcher (Task 7 brief: 重定向上限 5).
MAX_REDIRECTS = 5


class CaptureResult(IngestModel):
    """Fetch outcome WITHOUT persistence (spec 14 §2 CaptureResult).

    ``body`` travels to the workflow, which writes the blob and mints
    ``blob_ref`` in its commit transaction; a ``no_change`` result carries
    no body and expects the caller to bind the prior capture (ING-04).
    """

    outcome: CaptureOutcome
    status: int | None = None
    final_url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes | None = None
    media_type: str | None = None
    blob_ref: str | None = None
    hash: str | None = None
    captured_at: datetime
    access_flags: list[str] = Field(default_factory=list)
    error_code: str | None = None
    retry_after: float | None = None
