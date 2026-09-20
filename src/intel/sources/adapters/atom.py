"""Atom adapter (spec 14 §2: atom 需 feed URL — plan.seed)."""

from __future__ import annotations

from intel.sources.adapters.feed import SyndicationAdapter


class AtomAdapter(SyndicationAdapter):
    kind = "atom"
