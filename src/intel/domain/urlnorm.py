"""URL canonicalization for source URLs (spec 04 §3).

Normalization is deliberately minimal so that distinct URLs stay distinct:
strip the fragment, lowercase the host, drop scheme-default ports, and delete
ONLY known tracking parameters. Semantic parameters (language, version,
product, pagination, ids, ...) and their order are preserved byte-for-byte —
this is a deletion filter, not a re-encoding pass. Cross-domain canonical
link rewriting is out of scope here (it requires content verification).
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

#: Tracking parameters deleted exactly (case-insensitive match).
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",  # Meta
        "gclid",  # Google Ads
        "msclkid",  # Microsoft Advertising
        "twclid",  # X / Twitter
        "igshid",  # Instagram
        "yclid",  # Yandex
        "mc_cid",  # Mailchimp campaign
        "mc_eid",  # Mailchimp email
        "_ga",  # Google Analytics
        "_gl",  # Google Analytics linker
    }
)

#: Tracking parameter prefixes deleted (case-insensitive match).
TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_",)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def _filter_query(query: str) -> str:
    """Delete tracking parameters from a raw query string, preserving order.

    Surviving ``key=value`` segments are kept verbatim (no re-encoding, no
    sorting). Empty segments (``a=1&&b=2``) are dropped: they carry no
    semantic content. Keys are percent-decoded only for the whitelist match.
    """
    if not query:
        return ""
    kept: list[str] = []
    for segment in query.split("&"):
        if not segment:
            continue
        key = segment.split("=", 1)[0]
        if _is_tracking_param(unquote(key)):
            continue
        kept.append(segment)
    return "&".join(kept)


def _split_host_port(hostport: str) -> tuple[str, str, str]:
    """Split a host[:port] pair into (host, separator, port-string)."""
    if hostport.startswith("["):
        # IPv6 literal: the port, if any, follows the closing bracket.
        host, bracket, rest = hostport.partition("]")
        if not bracket:
            return hostport, "", ""
        if rest.startswith(":"):
            return host + "]", ":", rest[1:]
        return host + "]", "", ""
    return hostport.partition(":")


def _canonical_netloc(scheme: str, netloc: str) -> str:
    """Lowercase the host and drop scheme-default ports, keeping userinfo."""
    userinfo, _, hostport = netloc.rpartition("@")
    host, colon, port_str = _split_host_port(hostport)
    host = host.lower()
    if colon and not port_str:
        colon = ""  # dangling separator from an empty port ("host:")
    if colon and port_str:
        try:
            port = int(port_str)
        except ValueError as exc:
            raise ValueError(f"invalid port {port_str!r} in URL authority") from exc
        if _DEFAULT_PORTS.get(scheme) == port:
            colon, port_str = "", ""
    hostport = f"{host}:{port_str}" if colon else host
    return f"{userinfo}@{hostport}" if userinfo else hostport


def normalize_url(url: str) -> str:
    """Canonicalize *url* for storage/dedup (spec 04 §3).

    Applies, and only applies: surrounding-whitespace strip, fragment drop,
    host lowercasing, scheme-default port removal (``:80`` for http, ``:443``
    for https), and whitelist tracking-parameter deletion. Userinfo, path,
    query values, and parameter order pass through untouched. Idempotent.

    Raises:
        ValueError: if *url* is not an absolute http(s) URL, or its port is
            not a valid integer.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"expected an absolute http(s) URL, got {url!r}")
    netloc = _canonical_netloc(parts.scheme, parts.netloc)
    query = _filter_query(parts.query)
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
