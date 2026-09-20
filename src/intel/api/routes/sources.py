"""Source catalog + industry subscription routes (spec 08 §2).

GET /source-templates serves the global static catalog with no user state.
/industries/{id}/sources manages subscriptions binding an owner feed into
one industry (default all_public routing pool); PATCH toggles the
subscription without touching archived material.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    cursor_signing_key,
    get_industry_sources_service,
    get_sources_service,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import (
    SourceSubscriptionCreate,
    SourceSubscriptionPatch,
    SourceSubscriptionView,
    SourceTemplate,
)
from intel.services.sources import SourcesService

templates_router = APIRouter(tags=["source-templates"])
sources_router = APIRouter(
    prefix="/industries/{industry_id}/sources", tags=["sources"]
)

OwnerService = Annotated[SourcesService, Depends(get_sources_service)]
IndustryService = Annotated[SourcesService, Depends(get_industry_sources_service)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


@templates_router.get("/source-templates")
async def list_templates(
    request: Request,
    params: Annotated[PageParams, Depends()],
    service: OwnerService,
) -> Page[SourceTemplate]:
    templates = await service.list_templates()
    return paginate(templates, params, key=cursor_signing_key(request))


@sources_router.get("")
async def list_subscriptions(
    industry_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    service: IndustryService,
) -> Page[SourceSubscriptionView]:
    views = await service.list_subscriptions(industry_id)
    return paginate(views, params, key=cursor_signing_key(request))


@sources_router.post("", status_code=201)
async def create_subscription(
    industry_id: UUID,
    body: SourceSubscriptionCreate,
    guard: Guard,
    service: IndustryService,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    view = await service.create_subscription(industry_id, body)
    payload = view.model_dump(mode="json")
    await guard.store(201, payload)
    return JSONResponse(payload, status_code=201)


@sources_router.patch("/{subscription_id}")
async def patch_subscription(
    industry_id: UUID,
    subscription_id: UUID,
    body: SourceSubscriptionPatch,
    service: IndustryService,
    _: Csrf,
) -> SourceSubscriptionView:
    return await service.patch_subscription(industry_id, subscription_id, body)
