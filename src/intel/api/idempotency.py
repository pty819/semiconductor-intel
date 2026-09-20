"""Store-and-replay for Idempotency-Key POSTs (spec 08 §1, 03 §7).

The guard hashes the raw request body, looks the (owner, route, key) up in
the api_idempotency store and either replays the first complete response,
raises 409 ``idempotency_conflict`` on a hash mismatch, or lets the route
run and stores its response for next time. Entries expire after 24h; an
expired record is treated as absent (超过保留期客户端不得假设继续幂等).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from intel.api.deps import get_idempotency_repo, get_principal
from intel.repositories.idempotency import (
    IDEMPOTENCY_TTL,
    IdempotencyRecord,
    IdempotencyRepository,
)
from intel.services.errors import IdempotencyConflict
from intel.services.identity import Principal


def _utcnow() -> datetime:
    return datetime.now(UTC)


def request_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


async def guard_factory(
    request: Request,
    repo: IdempotencyRepository,
    owner_id: UUID,
    idempotency_key: str,
) -> IdempotencyGuard:
    """Plain factory used by the dependency above (and tests)."""
    guard = IdempotencyGuard(
        repo,
        owner_id=owner_id,
        route=request.url.path,
        key=idempotency_key,
        body_hash=request_hash(await request.body()),
    )
    await guard.lookup()
    return guard


class IdempotencyGuard:
    """Per-request helper: replay lookup up front, store after the work."""

    def __init__(
        self,
        repo: IdempotencyRepository,
        *,
        owner_id: UUID,
        route: str,
        key: str,
        body_hash: str,
    ) -> None:
        self._repo = repo
        self._owner_id = owner_id
        self._route = route
        self._key = key
        self._body_hash = body_hash
        self.replayed: tuple[int, Any] | None = None

    @property
    def key(self) -> str:
        """The request's Idempotency-Key — routes forward it to job dispatch
        so a retried POST maps to the same enqueued job (03 §7)."""
        return self._key

    async def lookup(self) -> None:
        record = await self._repo.get(self._owner_id, self._route, self._key)
        if record is None or record.expires_at <= _utcnow():
            return
        if record.request_hash != self._body_hash:
            raise IdempotencyConflict(
                "Idempotency-Key was already used with a different request body"
            )
        self.replayed = (record.response_status, record.response_body)

    async def store(self, status_code: int, body: Any) -> None:
        await self._repo.put(
            IdempotencyRecord(
                owner_id=self._owner_id,
                route=self._route,
                key=self._key,
                request_hash=self._body_hash,
                response_status=status_code,
                response_body=body,
                expires_at=_utcnow() + IDEMPOTENCY_TTL,
            )
        )

    def replay_response(self) -> Response:
        assert self.replayed is not None
        status, body = self.replayed
        return JSONResponse(
            body, status_code=status, headers={"Idempotency-Replayed": "true"}
        )


async def idempotency(
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    repo: Annotated[IdempotencyRepository, Depends(get_idempotency_repo)],
    key: Annotated[str, Header(alias="Idempotency-Key")],
) -> IdempotencyGuard:
    """Route dependency: required Idempotency-Key on creation/dispatch POSTs
    (08 §1 and the openapi contract); owner comes from the session."""
    return await guard_factory(request, repo, principal.user_id, key)
