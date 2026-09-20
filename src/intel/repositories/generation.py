"""Generation-run reads (08 §2 / 15 §3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.generation import GenerationRun
from intel.db.rls import require_owner_guc, set_scope
from intel.repositories.base import IndustryScope, ScopedRepository


class GenerationRepository:
    """Protocol surface the generation-run routes consume."""


class SqlAlchemyGenerationRepository(ScopedRepository, GenerationRepository):
    def __init__(self, conn: AsyncConnection, scope: IndustryScope) -> None:
        super().__init__(conn, scope)

    async def _bind(self) -> UUID:
        industry_id = self.scope.require_industry_id()
        await set_scope(self.conn, self.owner_id, industry_id)
        await require_owner_guc(self.conn)
        return industry_id

    async def get_run(self, run_id: UUID) -> dict[str, Any] | None:
        industry_id = await self._bind()
        row = (
            (
                await self.conn.execute(
                    select(GenerationRun).where(
                        GenerationRun.id == run_id,
                        GenerationRun.owner_id == self.owner_id,
                        GenerationRun.industry_id == industry_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return {
            "id": row.id,
            "job_id": row.job_id,
            "attempt": row.attempt,
            "step_key": row.step_key,
            "trace_state": row.trace_state,
            "model_route_display": row.model_route_display,
            "nooa_commit": row.nooa_commit,
            "prompt_version": row.prompt_version,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
            "summary": [],
            "evidence_validation": "not_applicable",
            "viewer_available": False,
        }
