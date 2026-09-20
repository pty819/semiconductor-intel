"""List pagination: opaque, signed offset cursors (spec 08 §1).

The cursor is base64("offset:mac") where mac = HMAC-SHA256(pepper, offset)
truncated — opaque to the client, unforgeable without the server secret.
v1 binds only the offset; binding scope/filter-hash/sort/as_of into the MAC
is a documented simplification (lists here filter server-side on every
request anyway, so a stale cursor can at worst re-read a window).
Page size defaults to 50, max 200 (不能越界返回).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class PageParams:
    """Dependency: cursor + page_size query parameters (08 §1).

    The cursor is decoded later, inside ``paginate``, where the signing key
    from app state is available.
    """

    def __init__(
        self,
        cursor: Annotated[str | None, Query()] = None,
        page_size: Annotated[
            int, Query(ge=1, le=MAX_PAGE_SIZE)
        ] = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.cursor = cursor
        self.page_size = page_size


class Page[T](BaseModel):
    """List response envelope: {items, next_cursor} (openapi *Page)."""

    items: list[T]
    next_cursor: str | None = None


def _mac(offset: int, key: str) -> str:
    return hmac.new(
        key.encode("utf-8"), str(offset).encode("ascii"), hashlib.sha256
    ).hexdigest()[:16]


def encode_cursor(offset: int, *, key: str) -> str:
    raw = f"{offset}:{_mac(offset, key)}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str | None, *, key: str) -> int:
    if not token:
        return 0
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        offset_text, _, mac = raw.partition(":")
        offset = int(offset_text)
    except (ValueError, UnicodeError):
        return 0
    if not hmac.compare_digest(mac, _mac(offset, key)):
        return 0
    return max(offset, 0)


def paginate[T](items: list[T], params: PageParams, *, key: str) -> Page[T]:
    offset = decode_cursor(params.cursor, key=key)
    window = items[offset : offset + params.page_size]
    has_more = offset + params.page_size < len(items)
    return Page(
        items=window,
        next_cursor=(
            encode_cursor(offset + params.page_size, key=key) if has_more else None
        ),
    )
