"""Sitemap adapter (spec 14 §2: 入口及允许路径).

Sitemaps are URL inventories without dates, so the cursor is a plain
offset into the (allowed subset of) ``<url><loc>`` entries — chunked by
``limits.max_items`` so one discover call is bounded. ``sitemapindex``
documents are not recursed in this version (warning + partial), keeping
the first version honest about coverage rather than silently skipping
child maps.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from lxml import etree

from intel.sources.adapters.cursor import decode_cursor, encode_cursor
from intel.sources.dto import DiscoveredItem, DiscoveryPage, FeedPlan
from intel.sources.pageclient import PageClient

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _hardened_parser() -> etree.XMLParser:
    """Parser settings for UNTRUSTED XML (billion-laughs defense): no
    entity resolution (a DTD full of nested entities yields unexpanded
    reference nodes instead of a memory bomb), no network DTD fetch, no
    huge-tree relaxation. A fresh instance per parse — lxml parsers are
    not thread-safe to share."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        load_dtd=False,
        dtd_validation=False,
    )


def _path_matches(loc: str, prefixes: list[str]) -> bool:
    """Allowed paths are absolute path prefixes (``/etch/``); they match
    a ``<loc>`` by its URL path (an absolute-URL prefix also matches)."""
    path = urlsplit(loc).path or "/"
    return any(loc.startswith(p) or path.startswith(p) for p in prefixes)


class SitemapAdapter:
    kind = "sitemap"

    def __init__(self, client: PageClient) -> None:
        self._client = client

    async def discover(
        self, plan: FeedPlan, cursor: str | None
    ) -> DiscoveryPage:
        config = plan.config
        state = decode_cursor(cursor)
        offset = state.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            offset = 0

        response = await self._client.get(plan.seed)
        warnings: list[str] = []
        try:
            root = etree.fromstring(response.body, parser=_hardened_parser())
        except etree.XMLSyntaxError as exc:
            raise ValueError(f"sitemap is not well-formed XML: {plan.seed}") from exc

        localname = etree.QName(root).localname
        if localname == "sitemapindex":
            warnings.append(
                "sitemapindex documents are not recursed yet: child maps"
                " need explicit feed entries (coverage marked partial)"
            )
            return DiscoveryPage(
                items=[],
                next_cursor=None,
                exhausted=False,  # partial: known-unwalked territory
                window_coverage="unknown",
                warnings=warnings,
            )

        allowed = [str(p) for p in config["allowed_paths"]]
        locs = [
            loc.text.strip()
            for loc in root.findall("sm:url/sm:loc", _NS)
            if loc.text and _path_matches(loc.text.strip(), allowed)
        ]
        page = locs[offset : offset + plan.limits.max_items]
        next_offset = offset + len(page)

        next_cursor = None
        if next_offset < len(locs):
            next_cursor = encode_cursor({"offset": next_offset})

        return DiscoveryPage(
            items=[DiscoveredItem(url=url) for url in page],
            next_cursor=next_cursor,
            exhausted=next_cursor is None,
            window_coverage="unknown",
            warnings=warnings,
        )
