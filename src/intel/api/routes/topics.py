"""Topic routes under /industries/{industry_id}/topics (spec 08 §2).

Every route inherits ``get_scope``'s ownership check via the industry-scoped
service dependency (foreign industry → 404 before any topic work). PATCH
appends a topic revision; replay dispatches a local re-classification job
(U05: 本地重归类, no re-fetch, no topic web search).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    IndustryScopeDep,
    cursor_signing_key,
    get_industry_workspace_service,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import TopicCreate, TopicPatch, TopicView, WindowRequest
from intel.services.workspace import WorkspaceService

router = APIRouter(prefix="/industries/{industry_id}/topics", tags=["topics"])

Service = Annotated[WorkspaceService, Depends(get_industry_workspace_service)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


@router.get("")
async def list_topics(
    industry_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    service: Service,
) -> Page[TopicView]:
    views = await service.list_topics(industry_id)
    return paginate(views, params, key=cursor_signing_key(request))


@router.post("", status_code=201)
async def create_topic(
    industry_id: UUID,
    body: TopicCreate,
    guard: Guard,
    service: Service,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    view = await service.create_topic(industry_id, body)
    payload = view.model_dump(mode="json")
    await guard.store(201, payload)
    return JSONResponse(payload, status_code=201)


@router.get("/{topic_id}")
async def get_topic(
    industry_id: UUID,
    topic_id: UUID,
    service: Service,
) -> TopicView:
    return await service.get_topic(industry_id, topic_id)


@router.patch("/{topic_id}")
async def patch_topic(
    industry_id: UUID,
    topic_id: UUID,
    body: TopicPatch,
    service: Service,
    _: Csrf,
) -> TopicView:
    return await service.patch_topic(industry_id, topic_id, body)


@router.post("/{topic_id}/replay", status_code=202)
async def replay_topic(
    industry_id: UUID,
    topic_id: UUID,
    body: WindowRequest,
    scope: IndustryScopeDep,
    guard: Guard,
    service: Service,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    accepted = await service.replay_topic(
        industry_id,
        topic_id,
        body,
        scope=scope,
        idempotency_key=guard.key,
    )
    payload = accepted.model_dump(mode="json")
    await guard.store(202, payload)
    return JSONResponse(payload, status_code=202)
