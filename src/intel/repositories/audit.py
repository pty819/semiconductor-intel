"""Audit writer port (spec 07 §6 落点, 03 §6 audit_log).

The first real anchor: scope/auth violations during fetch (Task 7) must
write an ``audit_log`` row in the SAME short transaction as the failure
evidence. The port is deliberately tiny — an action name, an actor (the
job), a target, and a details dict. Rows are append-only and carry no
secrets (只记录必要差异，不记录秘密).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.conversation import AuditLog
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded audit event (in-memory shape mirrors the row)."""

    action: str
    actor_type: str
    actor_id: UUID
    target_type: str
    target_id: UUID
    owner_id: UUID
    industry_id: UUID | None = None
    details: dict = field(default_factory=dict)
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditWriter(Protocol):
    async def write(
        self,
        *,
        action: str,
        actor_type: str,
        actor_id: UUID,
        target_type: str,
        target_id: UUID,
        details: Mapping[str, object] | None = None,
    ) -> None: ...


class InMemoryAuditWriter:
    """Test double: collects entries for assertions."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._owner_id: UUID | None = None

    def bind(self, scope: IndustryScope) -> InMemoryAuditWriter:
        """Scopes subsequent writes (mirrors the SQL bind)."""
        writer = InMemoryAuditWriter.__new__(InMemoryAuditWriter)
        writer.entries = self.entries
        writer._owner_id = scope.owner_id
        return writer

    async def write(
        self,
        *,
        action: str,
        actor_type: str,
        actor_id: UUID,
        target_type: str,
        target_id: UUID,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self._owner_id is None:
            raise RuntimeError("audit writer used outside a bound scope")
        self.entries.append(
            AuditEntry(
                action=action,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type=target_type,
                target_id=target_id,
                owner_id=self._owner_id,
                details=dict(details or {}),
            )
        )


class SqlAlchemyAuditWriter:
    """Writes audit_log rows on the caller's connection/transaction."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        self._conn = conn
        self._scope = scope

    async def write(
        self,
        *,
        action: str,
        actor_type: str,
        actor_id: UUID,
        target_type: str,
        target_id: UUID,
        details: Mapping[str, object] | None = None,
    ) -> None:
        await set_scope(self._conn, self._scope.owner_id, self._scope.industry_id)
        await require_owner_guc(self._conn)
        await self._conn.execute(
            insert(AuditLog).values(
                id=uuid4(),
                owner_id=self._scope.owner_id,
                industry_id=self._scope.industry_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=dict(details or {}),
            )
        )
