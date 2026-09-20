"""Conversation + message routes (spec 08 §2, 16 §6/§7)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    CurrentPrincipal,
    Enqueuer,
    IndustryScopeDep,
    cursor_signing_key,
    get_conversation_repo,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import (
    ConversationCreate,
    ConversationView,
    MessageCreate,
    MessageView,
)
from intel.services.errors import ConversationTurnConflict, NotFound
from intel.services.research import ParentMismatch

router = APIRouter(
    prefix="/industries/{industry_id}/conversations", tags=["conversations"]
)

Repo = Annotated[object, Depends(get_conversation_repo)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


@router.get("")
async def list_conversations(
    industry_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    _scope: IndustryScopeDep,
) -> Page[ConversationView]:
    items = await repo.list_conversations()
    return paginate(items, params, key=cursor_signing_key(request))


@router.post("", status_code=201)
async def create_conversation(
    industry_id: UUID,
    body: ConversationCreate,
    guard: Guard,
    repo: Repo,
    _scope: IndustryScopeDep,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    view = await repo.create_conversation(body.title)
    payload = view.model_dump(mode="json")
    await guard.store(201, payload)
    return JSONResponse(payload, status_code=201)


@router.get("/{conversation_id}/messages")
async def list_messages(
    industry_id: UUID,
    conversation_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    _scope: IndustryScopeDep,
) -> Page[MessageView]:
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
        raise NotFound("conversation not found")
    items = await repo.list_messages(conversation_id)
    return paginate(items, params, key=cursor_signing_key(request))


@router.post("/{conversation_id}/messages", status_code=202)
async def post_message(
    industry_id: UUID,
    conversation_id: UUID,
    body: MessageCreate,
    guard: Guard,
    repo: Repo,
    enqueuer: Enqueuer,
    scope: IndustryScopeDep,
    principal: CurrentPrincipal,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    conversation = await repo.get_conversation(conversation_id)
    if conversation is None:
        raise NotFound("conversation not found")
    try:
        message_id = await repo.begin_turn(
            conversation_id,
            text=body.text,
            parent_message_id=body.parent_message_id,
            mode=body.mode,
            as_of=body.as_of,
            topic_ids=list(body.topic_ids),
        )
    except ParentMismatch as exc:
        raise ConversationTurnConflict(str(exc)) from exc
    online = body.mode == "online"
    if online:
        kind = "investigate"
        payload = {
            "industry": str(industry_id),
            "message_id": str(message_id),
            "request_version": "1",
            "conversation_id": str(conversation_id),
            "question": body.text,
            "online": True,
            "actor_id": str(principal.user_id),
        }
    else:
        kind = "archive_answer"
        payload = {
            "industry": str(industry_id),
            "message_id": str(message_id),
            "conversation_id": str(conversation_id),
            "question": body.text,
            "online": False,
            "actor_id": str(principal.user_id),
        }
    accepted = await enqueuer.enqueue(
        scope, kind=kind, payload=payload, idempotency_key=guard.key
    )
    body_out = accepted.model_dump(mode="json")
    await guard.store(202, body_out)
    return JSONResponse(body_out, status_code=202)
