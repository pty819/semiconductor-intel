"""Adapter registry: config validation, cursor codec, factory (14 §2).

``validate_adapter_config`` is the gate spec 14 §2 demands: config JSON
is checked against a per-kind allowlist of keys and shapes — no
arbitrary code, no URL template parameters outside the whitelist. The
cursor protocol is an opaque urlsafe-base64 JSON blob; every adapter
owns its own payload shape and treats it as untrusted input on decode.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from intel.services.errors import ValidationFailed
from intel.sources.adapters.api_list import ApiListAdapter
from intel.sources.adapters.atom import AtomAdapter
from intel.sources.adapters.cursor import decode_cursor, encode_cursor
from intel.sources.adapters.html_list import HtmlListAdapter
from intel.sources.adapters.page_monitor import PageMonitorAdapter
from intel.sources.adapters.rss import RssAdapter
from intel.sources.adapters.sitemap import SitemapAdapter
from intel.sources.dto import DiscoveryPage, FeedPlan
from intel.sources.pageclient import PageClient

#: URL-template placeholders any adapter endpoint may use (14 §2 白名单).
TEMPLATE_PARAM_WHITELIST: frozenset[str] = frozenset({"page"})

_TEMPLATE_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: Per-kind config keys; anything else is rejected.
_ADAPTER_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "rss": frozenset({"etag"}),
    "atom": frozenset({"etag"}),
    "api": frozenset(
        {
            "endpoint",
            "items_path",
            "url_field",
            "title_field",
            "date_field",
            "provider_id_field",
            "page_param",
            "page_start",
            "next_url_field",
        }
    ),
    "html_list": frozenset(
        {
            "list_selector",
            "link_selector",
            "title_selector",
            "date_selector",
            "base_url",
            "pagination",
        }
    ),
    "sitemap": frozenset({"allowed_paths"}),
    "page_monitor": frozenset({"etag", "selector"}),
}

_PAGINATION_MODES = frozenset({"next_link", "page_param"})


def _require_str(config: Mapping[str, Any], key: str, kind: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationFailed(
            f"adapter {kind!r} config requires string {key!r} (spec 14 §2)"
        )
    return value


def _check_endpoint_template(endpoint: str, kind: str) -> None:
    if not endpoint.startswith(("http://", "https://")):
        raise ValidationFailed(f"{kind} endpoint must be an absolute http(s) URL")
    for match in _TEMPLATE_RE.finditer(endpoint):
        if match.group(1) not in TEMPLATE_PARAM_WHITELIST:
            raise ValidationFailed(
                f"endpoint template parameter {match.group(0)!r} is not on"
                f" the whitelist {sorted(TEMPLATE_PARAM_WHITELIST)}"
                " (spec 14 §2 URL模板参数白名单)"
            )


def validate_adapter_config(kind: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one adapter config; returns a plain dict copy.

    Unknown kinds, unknown keys, or wrong shapes raise
    :class:`~intel.services.errors.ValidationFailed` — config JSON never
    reaches an adapter unvalidated.
    """
    allowed = _ADAPTER_CONFIG_KEYS.get(kind)
    if allowed is None:
        raise ValidationFailed(f"unknown adapter kind {kind!r} (spec 14 §2)")
    if not isinstance(config, Mapping):
        raise ValidationFailed("adapter config must be a JSON object")
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValidationFailed(
            f"adapter {kind!r} config keys {unknown} are not allowed;"
            f" permitted: {sorted(allowed)} (spec 14 §2 不接受任意代码)"
        )
    checked = dict(config)

    if kind in ("rss", "atom", "page_monitor"):
        pass  # seed URL is plan.seed; no required keys
    elif kind == "api":
        _check_endpoint_template(_require_str(config, "endpoint", kind), kind)
        _require_str(config, "items_path", kind)
        _require_str(config, "url_field", kind)
        for key in ("title_field", "date_field", "provider_id_field", "page_param"):
            if key in config and not isinstance(config[key], str):
                raise ValidationFailed(f"{kind} config {key!r} must be a string")
        if "page_start" in config and not isinstance(config["page_start"], int):
            raise ValidationFailed(f"{kind} config 'page_start' must be an integer")
        if "next_url_field" in config and not isinstance(config["next_url_field"], str):
            raise ValidationFailed(f"{kind} config 'next_url_field' must be a string")
    elif kind == "html_list":
        _require_str(config, "list_selector", kind)
        _require_str(config, "link_selector", kind)
        if "base_url" in config:
            base = config["base_url"]
            if not isinstance(base, str) or not base.startswith(("http://", "https://")):
                raise ValidationFailed("html_list 'base_url' must be absolute http(s)")
        pagination = config.get("pagination", {})
        if not isinstance(pagination, Mapping):
            raise ValidationFailed("html_list 'pagination' must be an object")
        unknown_p = sorted(set(pagination) - {"mode", "next_selector", "page_param"})
        if unknown_p:
            raise ValidationFailed(
                f"html_list pagination keys {unknown_p} are not allowed"
            )
        mode = pagination.get("mode", "next_link")
        if mode not in _PAGINATION_MODES:
            raise ValidationFailed(
                f"html_list pagination mode {mode!r} must be one of"
                f" {sorted(_PAGINATION_MODES)}"
            )
        if mode == "next_link" and "next_selector" not in pagination:
            raise ValidationFailed(
                "html_list pagination mode 'next_link' requires 'next_selector'"
            )
        if mode == "page_param":
            if "page_param" not in pagination:
                raise ValidationFailed(
                    "html_list pagination mode 'page_param' requires 'page_param'"
                )
            _check_endpoint_template(str(config.get("base_url", "")), kind)
    elif kind == "sitemap":
        paths = config.get("allowed_paths")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p.startswith("/") for p in paths)
        ):
            raise ValidationFailed(
                "sitemap config requires non-empty 'allowed_paths' of"
                " absolute path prefixes (spec 14 §2 入口及允许路径)"
            )
    return checked


class DiscoveryAdapter(Protocol):
    """The adapter protocol (spec 02 §6)."""

    async def discover(
        self, plan: FeedPlan, cursor: str | None
    ) -> DiscoveryPage: ...


def make_adapter(kind: str, client: PageClient) -> DiscoveryAdapter:
    """Build one adapter over a page client."""
    if kind == "rss":
        return RssAdapter(client)
    if kind == "atom":
        return AtomAdapter(client)
    if kind == "api":
        return ApiListAdapter(client)
    if kind == "html_list":
        return HtmlListAdapter(client)
    if kind == "sitemap":
        return SitemapAdapter(client)
    if kind == "page_monitor":
        return PageMonitorAdapter(client)
    raise ValidationFailed(f"unknown adapter kind {kind!r}")


__all__ = [
    "TEMPLATE_PARAM_WHITELIST",
    "DiscoveryAdapter",
    "decode_cursor",
    "encode_cursor",
    "make_adapter",
    "validate_adapter_config",
]
