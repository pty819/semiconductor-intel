"""Feed routes: /feeds CRUD + poll + runs (spec 08 §2).

Owner-level entrances (本人的公共采集入口配置). PATCH changes
interval/user_enabled/published parser selection — credentials are never
accepted in PATCH bodies (FeedPatch carries only a credential-free field
set; the DTO is the contract, 08 §2 拒绝凭据明文). Poll dispatches a job
(202), never fetches inline and never filters by topic.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    CurrentPrincipal,
    cursor_signing_key,
    get_sources_service,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import (
    FeedCreate,
    FeedPatch,
    FeedView,
    PollRequest,
    SourceRunView,
)
from intel.repositories.base import IndustryScope
from intel.services.sources import SourcesService

router = APIRouter(prefix="/feeds", tags=["feeds"])

Service = Annotated[SourcesService, Depends(get_sources_service)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


@router.get("")
async def list_feeds(
    request: Request,
    params: Annotated[PageParams, Depends()],
    service: Service,
) -> Page[FeedView]:
    views = await service.list_feeds()
    return paginate(views, params, key=cursor_signing_key(request))


@router.post("", status_code=201)
async def create_feed(
    body: FeedCreate,
    guard: Guard,
    service: Service,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    view = await service.create_feed(body)
    payload = view.model_dump(mode="json")
    await guard.store(201, payload)
    return JSONResponse(payload, status_code=201)


@router.get("/{feed_id}")
async def get_feed(
    feed_id: UUID,
    service: Service,
) -> FeedView:
    return await service.get_feed(feed_id)


@router.patch("/{feed_id}")
async def patch_feed(
    feed_id: UUID,
    body: FeedPatch,
    service: Service,
    _: Csrf,
) -> FeedView:
    return await service.patch_feed(feed_id, body)


@router.post("/{feed_id}/poll", status_code=202)
async def poll_feed(
    feed_id: UUID,
    body: PollRequest,
    principal: CurrentPrincipal,
    guard: Guard,
    service: Service,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    accepted = await service.poll(
        feed_id,
        body,
        IndustryScope(owner_id=principal.user_id),
        idempotency_key=guard.key,
    )
    payload = accepted.model_dump(mode="json")
    await guard.store(202, payload)
    return JSONResponse(payload, status_code=202)


@router.get("/{feed_id}/runs")
async def list_runs(
    feed_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    service: Service,
) -> Page[SourceRunView]:
    views = await service.list_runs(feed_id)
    return paginate(views, params, key=cursor_signing_key(request))
