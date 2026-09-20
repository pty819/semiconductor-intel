"""Generation-run summary + viewer-link (08 §2 / 15 §3)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from intel.api.deps import IndustryScopeDep, get_generation_repo
from intel.contracts import GenerationRunView, GenerationViewerLink
from intel.services.errors import NotFound

router = APIRouter(
    prefix="/industries/{industry_id}/generation-runs", tags=["generation-runs"]
)

Repo = Annotated[object, Depends(get_generation_repo)]


@router.get("/{run_id}")
async def get_generation_run(
    industry_id: UUID, run_id: UUID, repo: Repo, _scope: IndustryScopeDep
) -> GenerationRunView:
    row = await repo.get_run(run_id)
    if row is None:
        raise NotFound("generation run not found")
    return GenerationRunView(
        id=row["id"],
        job_id=row["job_id"],
        attempt=int(row.get("attempt") or 1),
        step_key=str(row.get("step_key") or "unknown"),
        trace_state=row.get("trace_state") or "missing",
        model_route_display=row.get("model_route_display"),
        nooa_commit=str(row.get("nooa_commit") or ""),
        prompt_version=str(row.get("prompt_version") or ""),
        started_at=row["started_at"],
        ended_at=row.get("ended_at"),
        summary=list(row.get("summary") or []),
        evidence_validation=row.get("evidence_validation") or "not_applicable",
        viewer_available=False,
    )


@router.get("/{run_id}/viewer-link")
async def viewer_link(
    industry_id: UUID, run_id: UUID, repo: Repo, _scope: IndustryScopeDep
) -> GenerationViewerLink:
    row = await repo.get_run(run_id)
    if row is None:
        # Ordinary users never receive a viewer URL; missing and unauthorized
        # both look like an unavailable entry rather than a leaked token.
        return GenerationViewerLink(available=False, reason="not_authorized")
    return GenerationViewerLink(available=False, reason="not_authorized")
