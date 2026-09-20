"""Industry routes: /industries CRUD + lifecycle (spec 08 §2 table rows).

POST create and POST lifecycle carry the required Idempotency-Key
(store-and-replay); PATCH carries expected_version and appends a revision
when revision content changes; lifecycle enforces the 01 §5 state machine.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    cursor_signing_key,
    get_workspace_service,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import (
    IndustryCreate,
    IndustryPatch,
    IndustryView,
    LifecycleCommand,
)
from intel.services.workspace import WorkspaceService

router = APIRouter(prefix="/industries", tags=["industries"])

Service = Annotated[WorkspaceService, Depends(get_workspace_service)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


@router.get("")
async def list_industries(
    request: Request,
    params: Annotated[PageParams, Depends()],
    service: Service,
) -> Page[IndustryView]:
    views = await service.list_industries()
    return paginate(views, params, key=cursor_signing_key(request))


@router.post("", status_code=201)
async def create_industry(
    body: IndustryCreate,
    guard: Guard,
    service: Service,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    view = await service.create_industry(body)
    payload = view.model_dump(mode="json")
    await guard.store(201, payload)
    return JSONResponse(payload, status_code=201)


@router.get("/{industry_id}")
async def get_industry(
    industry_id: UUID,
    service: Service,
) -> IndustryView:
    return await service.get_industry(industry_id)


@router.patch("/{industry_id}")
async def patch_industry(
    industry_id: UUID,
    body: IndustryPatch,
    service: Service,
    _: Csrf,
) -> IndustryView:
    return await service.patch_industry(industry_id, body)


@router.post("/{industry_id}/lifecycle")
async def lifecycle(
    industry_id: UUID,
    body: LifecycleCommand,
    guard: Guard,
    service: Service,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    view = await service.lifecycle(industry_id, body)
    payload = view.model_dump(mode="json")
    await guard.store(200, payload)
    return JSONResponse(payload, status_code=200)
