"""Industry archive search (08 §2: 不联网)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import Csrf, IndustryScopeDep, cursor_signing_key, get_search_repo
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import PageParams, paginate
from intel.contracts import SearchHit, SearchRequest

router = APIRouter(prefix="/industries/{industry_id}", tags=["search"])

Repo = Annotated[object, Depends(get_search_repo)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


@router.post("/search")
async def search(
    industry_id: UUID,
    body: SearchRequest,
    request: Request,
    params: Annotated[PageParams, Depends()],
    guard: Guard,
    repo: Repo,
    _scope: IndustryScopeDep,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    hits = [
        SearchHit(
            object_id=row["object_id"],
            object_type=row["object_type"],
            title=row["title"],
            excerpt=row["excerpt"],
            channels=list(row.get("channels") or []),
            citation_ids=list(row.get("citation_ids") or []),
        )
        for row in await repo.search(
            body.query,
            topic_ids=body.topic_ids,
            entity_ids=body.entity_ids,
            as_of=body.as_of,
        )
    ]
    page = paginate(hits, params, key=cursor_signing_key(request))
    payload = page.model_dump(mode="json")
    await guard.store(200, payload)
    return JSONResponse(payload, status_code=200)
