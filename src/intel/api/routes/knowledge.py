"""Knowledge read routes: timeline / events / evidence / entities / watches
/ evolutions (spec 08 §3; Task 13).

All routes are industry-scoped through the session-derived scope (deps
404s foreign industries). The timeline route exposes the 05 §5 query
surface verbatim — as_of, window, filters, the stable cursor, and the
user-hideable unknown-date group; EventCard join-dependent fields
(interpretations, citations, generation_refs) render as empty v1 lists
pending the Task-14 generation wiring (ledgered deviation).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from intel.api.deps import (
    cursor_signing_key,
    get_knowledge_repo,
    get_scope,
)
from intel.api.pagination import Page, PageParams, paginate
from intel.repositories.base import IndustryScope
from intel.repositories.knowledge import KnowledgeReadRepository
from intel.services.errors import NotFound

timeline_router = APIRouter(prefix="/industries/{industry_id}", tags=["timeline"])
entities_router = APIRouter(
    prefix="/industries/{industry_id}/entities", tags=["entities"]
)
watches_router = APIRouter(prefix="/industries/{industry_id}/watches", tags=["watches"])
evidence_router = APIRouter(prefix="/industries/{industry_id}", tags=["evidence"])
evolutions_router = APIRouter(
    prefix="/industries/{industry_id}/topics/{topic_id}/evolution",
    tags=["evolution"],
)

Repo = Annotated[KnowledgeReadRepository, Depends(get_knowledge_repo)]
Scope = Annotated[IndustryScope, Depends(get_scope)]


@timeline_router.get("/timeline")
async def read_timeline(
    industry_id: UUID,
    request: Request,
    repo: Repo,
    params: Annotated[PageParams, Depends()],
    topic_ids: Annotated[list[UUID] | None, Query()] = None,
    event_types: Annotated[list[str] | None, Query()] = None,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    as_of: datetime | None = None,
    include_unknown: bool = True,
    cursor: str | None = None,
    sort: Literal["occurred", "discovered"] = "occurred",
) -> dict[str, Any]:
    """05 §5 timeline page: as_of view, window intersection, stable cursor."""
    cards, next_cursor = await repo.list_event_cards(
        topic_ids=topic_ids,
        event_types=event_types,
        window_from=window_from,
        window_to=window_to,
        as_of=as_of,
        include_unknown=include_unknown,
        cursor=cursor,
        limit=params.limit,
        sort=sort,
    )
    return {
        "items": cards,
        "next_cursor": next_cursor,
        "page": {"limit": params.limit},
    }


@timeline_router.get("/events/{event_id}")
async def read_event(industry_id: UUID, event_id: UUID, repo: Repo) -> dict[str, Any]:
    card = await repo.get_event_card(event_id)
    if card is None:
        raise NotFound("event not found")
    return {"event": card}


@evidence_router.get("/claims/{claim_revision_id}/evidence")
async def list_claim_evidence(
    industry_id: UUID,
    request: Request,
    claim_revision_id: UUID,
    repo: Repo,
    params: Annotated[PageParams, Depends()],
) -> Page[dict[str, Any]]:
    evidence = await repo.list_evidence_for_claim(claim_revision_id)
    return paginate(evidence, params, key=cursor_signing_key(request))


@entities_router.get("")
async def list_entities(
    industry_id: UUID,
    request: Request,
    repo: Repo,
    params: Annotated[PageParams, Depends()],
    kind: str | None = None,
) -> Page[dict[str, Any]]:
    entities = await repo.list_entities(kind=kind)
    return paginate(entities, params, key=cursor_signing_key(request))


@entities_router.post("")
async def create_entity(
    industry_id: UUID,
    repo: Repo,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return await repo.create_entity(
        kind=str(payload["kind"]),
        canonical_name=str(payload["canonical_name"]),
        identifiers=payload.get("identifiers"),
    )


@watches_router.get("")
async def list_watches(
    industry_id: UUID,
    request: Request,
    repo: Repo,
    params: Annotated[PageParams, Depends()],
) -> Page[dict[str, Any]]:
    watches = await repo.list_watches()
    return paginate(watches, params, key=cursor_signing_key(request))


@watches_router.post("")
async def create_watch(
    industry_id: UUID, repo: Repo, payload: dict[str, Any]
) -> dict[str, Any]:
    return await repo.create_watch(
        title=str(payload["title"]), question=str(payload["question"])
    )


@watches_router.patch("/{watch_id}")
async def patch_watch(
    industry_id: UUID, watch_id: UUID, repo: Repo, payload: dict[str, Any]
) -> dict[str, Any]:
    await repo.set_watch_status(watch_id, status=str(payload["status"]))
    return {"id": watch_id, "status": payload["status"]}


@evolutions_router.get("")
async def list_evolutions(
    industry_id: UUID,
    topic_id: UUID,
    request: Request,
    repo: Repo,
    params: Annotated[PageParams, Depends()],
) -> Page[dict[str, Any]]:
    evolutions = await repo.list_evolutions(topic_id=topic_id)
    return paginate(evolutions, params, key=cursor_signing_key(request))


@evolutions_router.get("/{evolution_id}")
async def read_evolution(
    industry_id: UUID, topic_id: UUID, evolution_id: UUID, repo: Repo
) -> dict[str, Any]:
    evolution = await repo.get_evolution(evolution_id)
    if evolution is None:
        raise NotFound("evolution not found")
    return evolution
