"""Idempotency store for POST creation/dispatch endpoints (spec 08 §1, 03 §7).

UNIQUE(owner_id, route, key) in the table plus the upsert below give
store-and-replay semantics: same key + same request hash replays the first
complete response; same key + different body is 409 ``idempotency_conflict``.
Entries live 24 hours; after expiry the client must not assume replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from intel.db.models.jobs import ApiIdempotency
from intel.repositories.base import ScopedRepository

IDEMPOTENCY_TTL = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class IdempotencyRecord:
    owner_id: UUID
    route: str
    key: str
    request_hash: str
    response_status: int
    response_body: Any
    expires_at: datetime = field(default_factory=_utcnow)


@runtime_checkable
class IdempotencyRepository(Protocol):
    """Store and replay POST responses keyed by (owner, route, key)."""

    async def get(
        self, owner_id: UUID, route: str, key: str
    ) -> IdempotencyRecord | None: ...
    async def put(self, record: IdempotencyRecord) -> None: ...


class SqlAlchemyIdempotencyRepository(ScopedRepository):
    """IdempotencyRepository on one owner-scoped connection."""

    def __init__(self, conn, scope) -> None:
        super().__init__(conn, scope)
        self._owner = scope.owner_id

    async def get(
        self, owner_id: UUID, route: str, key: str
    ) -> IdempotencyRecord | None:
        await self._bind()
        stmt = select(ApiIdempotency).where(
            ApiIdempotency.owner_id == owner_id,
            ApiIdempotency.route == route,
            ApiIdempotency.key == key,
        )
        row = (await self._conn.execute(stmt)).scalars().one_or_none()
        if row is None:
            return None
        return IdempotencyRecord(
            owner_id=row.owner_id,
            route=row.route,
            key=row.key,
            request_hash=row.request_hash,
            response_status=row.response_status,
            response_body=row.response_body,
            expires_at=row.expires_at,
        )

    async def put(self, record: IdempotencyRecord) -> None:
        await self._bind()
        stmt = pg_insert(ApiIdempotency).values(
            id=uuid4(),
            owner_id=record.owner_id,
            route=record.route,
            key=record.key,
            request_hash=record.request_hash,
            response_status=record.response_status,
            response_body=record.response_body,
            expires_at=record.expires_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_api_idempotency_route_key",
            set_={
                "request_hash": record.request_hash,
                "response_status": record.response_status,
                "response_body": record.response_body,
                "expires_at": record.expires_at,
                "updated_at": _utcnow(),
            },
        )
        await self._conn.execute(stmt)
