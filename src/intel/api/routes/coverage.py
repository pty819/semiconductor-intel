"""Coverage route (08 §2: 输入范围与未完成/失败缺口)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from intel.api.deps import IndustryScopeDep, get_coverage_repo
from intel.contracts import Coverage

router = APIRouter(prefix="/industries/{industry_id}", tags=["coverage"])

Repo = Annotated[object, Depends(get_coverage_repo)]


@router.get("/coverage")
async def get_coverage(
    industry_id: UUID, repo: Repo, _scope: IndustryScopeDep
) -> Coverage:
    row = await repo.coverage()
    return Coverage(
        status=row.get("status") or "unknown",
        expected=row.get("expected"),
        processed=int(row.get("processed") or 0),
        failed=int(row.get("failed") or 0),
        gaps=list(row.get("gaps") or []),
        watermark=row.get("watermark"),
    )
