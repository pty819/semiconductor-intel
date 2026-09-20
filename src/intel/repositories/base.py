"""Scoped repositories: the base pattern every repo builds on (spec 03 §1, 10 §1).

A repository is bound at construction to one connection and one
:class:`IndustryScope`. Every query filters by the scope columns explicitly,
and every method binds the transaction-local RLS GUCs via
``db.rls.set_scope`` before touching the database — without the GUC, RLS
hides even the caller's own rows, and with it a pooled connection can never
leak one owner's scope into another's transaction.

Repository *protocols* (WorkspaceRepository, SourcesRepository,
IdempotencyRepository in their modules) describe what services consume;
the SqlAlchemy* classes here are the production adapters, and unit tests
drive the services with dict-backed fakes instead — the same split
``services/identity.py`` established.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.rls import require_owner_guc, set_scope


@dataclass(frozen=True, slots=True)
class IndustryScope:
    """The scope one repository (or job) acts within.

    ``industry_id`` is ``None`` for O-scope work (owner-wide: feeds, raw
    pool, owner industry list); I-scope work carries both ids. The API layer
    constructs scopes — ``owner_id`` always comes from the session (08 §1),
    never from the request body.
    """

    owner_id: UUID
    industry_id: UUID | None = None

    def require_industry_id(self) -> UUID:
        if self.industry_id is None:
            raise ValueError("this operation requires an industry scope")
        return self.industry_id


class ScopedRepository:
    """Base class for connection-scoped, scope-filtered repositories."""

    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        self._conn = conn
        self.scope = scope

    @property
    def conn(self) -> AsyncConnection:
        return self._conn

    @property
    def owner_id(self) -> UUID:
        return self.scope.owner_id

    async def _bind(self) -> None:
        """Bind this repository's scope to the transaction (RLS GUCs).

        ``set_scope`` writes transaction-local GUCs (parameterized);
        ``require_owner_guc`` reads the owner GUC back as the fail-closed
        guard (spec 10 §1: repositories call it on every write path — here
        it verifies the bind on every path, reads included, because RLS
        hides rows from reads without it too).
        """
        await set_scope(self._conn, self.scope.owner_id, self.scope.industry_id)
        await require_owner_guc(self._conn)

    async def _bind_industry(self) -> UUID:
        """Bind with the industry GUC and return the industry id."""
        industry_id = self.scope.require_industry_id()
        await set_scope(self._conn, self.scope.owner_id, industry_id)
        await require_owner_guc(self._conn)
        return industry_id
