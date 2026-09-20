"""Industry document routes (08 §2)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from intel.api.deps import (
    Csrf,
    Enqueuer,
    IndustryScopeDep,
    cursor_signing_key,
    get_document_repo,
)
from intel.api.idempotency import IdempotencyGuard, idempotency
from intel.api.pagination import Page, PageParams, paginate
from intel.contracts import (
    DiffView,
    DocumentRevisionView,
    DocumentTopicCommand,
    DocumentView,
    ImportURL,
)
from intel.services.errors import NotFound

router = APIRouter(prefix="/industries/{industry_id}/documents", tags=["documents"])

Repo = Annotated[object, Depends(get_document_repo)]
Guard = Annotated[IdempotencyGuard, Depends(idempotency)]


def _view(row: dict) -> DocumentView:
    return DocumentView(
        id=row["id"],
        industry_id=row["industry_id"],
        row_version=int(row.get("row_version") or 1),
        title=str(row.get("title") or ""),
        source_url=str(row.get("source_url") or ""),
        current_parse_id=row.get("current_parse_id"),
        retrieval_scope=row.get("retrieval_scope") or "fulltext",
        parse_status=row.get("parse_status") or "pending",
        relevance=row.get("relevance") or "uncertain",
        quality_flags=list(row.get("quality_flags") or []),
        first_seen_at=row["first_seen_at"],
    )


@router.get("")
async def list_documents(
    industry_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    _scope: IndustryScopeDep,
) -> Page[DocumentView]:
    items = [_view(row) for row in await repo.list_documents()]
    return paginate(items, params, key=cursor_signing_key(request))


@router.get("/{document_id}")
async def get_document(
    industry_id: UUID, document_id: UUID, repo: Repo, _scope: IndustryScopeDep
) -> DocumentView:
    row = await repo.get_document(document_id)
    if row is None:
        raise NotFound("document not found")
    return _view(row)


@router.get("/{document_id}/revisions")
async def list_revisions(
    industry_id: UUID,
    document_id: UUID,
    request: Request,
    params: Annotated[PageParams, Depends()],
    repo: Repo,
    _scope: IndustryScopeDep,
) -> Page[DocumentRevisionView]:
    rows = await repo.list_revisions(document_id)
    items = [
        DocumentRevisionView(
            id=row["id"],
            document_id=row["document_id"],
            capture_id=row["capture_id"],
            parser_version_id=row["parser_version_id"],
            parsed_at=row["parsed_at"],
            coverage=row["coverage"],
        )
        for row in rows
    ]
    return paginate(items, params, key=cursor_signing_key(request))


@router.get("/{document_id}/diff")
async def document_diff(
    industry_id: UUID,
    document_id: UUID,
    repo: Repo,
    _scope: IndustryScopeDep,
    from_parse_id: UUID | None = None,
    to_parse_id: UUID | None = None,
) -> DiffView:
    row = await repo.get_diff(
        document_id, from_parse_id=from_parse_id, to_parse_id=to_parse_id
    )
    if row is None:
        raise NotFound("diff not found")
    return DiffView(
        from_parse_id=row["from_parse_id"],
        to_parse_id=row["to_parse_id"],
        kind=row["kind"],
        algorithm_version=row["algorithm_version"],
        changed_blocks=row["changed_blocks"],
        field_changes=row["field_changes"],
    )


@router.post("/import-url", status_code=202)
async def import_url(
    industry_id: UUID,
    body: ImportURL,
    guard: Guard,
    enqueuer: Enqueuer,
    scope: IndustryScopeDep,
    _: Csrf,
) -> JSONResponse:
    if guard.replayed is not None:
        return guard.replay_response()
    payload = {
        "url": body.url,
        "industry": str(industry_id),
        "discovery_item": str(uuid4()),
        "refresh_epoch": "import",
        "explicit_import": True,
    }
    accepted = await enqueuer.enqueue(
        scope, kind="fetch", payload=payload, idempotency_key=guard.key
    )
    body_out = accepted.model_dump(mode="json")
    await guard.store(202, body_out)
    return JSONResponse(body_out, status_code=202)


@router.post("/{document_id}/topic-decisions")
async def topic_decisions(
    industry_id: UUID,
    document_id: UUID,
    body: DocumentTopicCommand,
    repo: Repo,
    _scope: IndustryScopeDep,
    _: Csrf,
) -> dict:
    return await repo.apply_topic_decision(document_id, body)
