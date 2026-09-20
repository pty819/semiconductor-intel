"""RSS 2.0 adapter (spec 14 §2: rss 需 feed URL — plan.seed)."""

from __future__ import annotations

from intel.sources.adapters.feed import SyndicationAdapter


class RssAdapter(SyndicationAdapter):
    kind = "rss"
