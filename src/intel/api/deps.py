"""FastAPI dependencies: principal, CSRF, scope validation, repos, services.

Rules implemented here (spec 08 §1):

- ``owner_id`` 永远来自 session. The opaque cookie is resolved through
  IdentityService (which stores only peppered hashes).
- Non-GET commands require ``X-CSRF-Token``; the header is checked with
  ``IdentityService.hash_token`` (the single hashing implementation) against
  the session's stored ``csrf_hash``. Login is exempt but checks Origin.
- URL ``industry_id`` is ownership-checked up front by ``get_scope``:
  foreign or missing → 404 ``not_found``, never 403 — a 403 would leak that
  another user's object exists (不存在与无权访问统一 404).
- Repositories are constructed per request, bound to the session-derived
  IndustryScope; services are constructed over those repos. Unit tests
  override the repo/service providers with fakes.

Sub-dependencies are declared with ``Annotated[T, Depends(fn)]`` rather than
call-in-default; the ``get_*`` functions remain the stable override points
for tests and Task 6+ wiring.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.rls import set_app_role
from intel.repositories.base import IndustryScope
from intel.repositories.conversations import SqlAlchemyConversationRepository
from intel.repositories.documents import SqlAlchemyDocumentRepository
from intel.repositories.generation import SqlAlchemyGenerationRepository
from intel.repositories.idempotency import (
    IdempotencyRepository,
    SqlAlchemyIdempotencyRepository,
)
from intel.repositories.jobs import SqlAlchemyJobEventLog, SqlAlchemyJobsStore
from intel.repositories.knowledge import (
    KnowledgeReadRepository,
    SqlAlchemyKnowledgeRepository,
)
from intel.repositories.reports import SqlAlchemyReportRepository
from intel.repositories.reviews import SqlAlchemyReviewRepository
from intel.repositories.search import SqlAlchemySearchRepository
from intel.repositories.sources import (
    SourcesRepository,
    SqlAlchemySourcesRepository,
)
from intel.repositories.workspace import (
    SqlAlchemyWorkspaceRepository,
    WorkspaceRepository,
)
from intel.services.acquisition import JobEnqueuer
from intel.services.errors import CsrfFailed, NotFound, Unauthenticated
from intel.services.identity import (
    IdentityRepository,
    IdentityService,
    Principal,
    SqlAlchemyIdentityRepository,
)
from intel.services.jobs import JobService, JobServiceEnqueuer
from intel.services.sources import SourcesService
from intel.services.workspace import WorkspaceService

#: Session cookie name (HttpOnly/Secure/SameSite=Lax; set by the auth routes).
SESSION_COOKIE = "intel_session"


# --------------------------------------------------------------------------
# infrastructure
# --------------------------------------------------------------------------


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One connection + one transaction per request; commit on success.

    Unit tests override the repo/service providers instead, so this (and the
    engine behind it) never runs in unit tests.
    """
    async with request.app.state.engine.connect() as conn, conn.begin():
        await set_app_role(conn)
        yield conn


ConnDep = Annotated[AsyncConnection, Depends(get_conn)]


def get_settings(request: Request):
    return request.app.state.settings


def get_identity_service(request: Request) -> IdentityService:
    return request.app.state.identity_service


def get_enqueuer(request: Request, conn: ConnDep) -> JobEnqueuer:
    """The Task 6 queue: enqueue lands in the jobs table inside the
    request's transaction. ``app.state.enqueuer`` (dev/tests) still wins
    when set, so fakes and the in-memory enqueuer keep working."""
    override: JobEnqueuer | None = getattr(request.app.state, "enqueuer", None)
    if override is not None:
        return override
    return JobServiceEnqueuer(JobService(), conn)


def get_identity_repo(conn: ConnDep) -> IdentityRepository:
    return SqlAlchemyIdentityRepository(conn)


IdentityRepo = Annotated[IdentityRepository, Depends(get_identity_repo)]
IdentityServiceDep = Annotated[IdentityService, Depends(get_identity_service)]
Enqueuer = Annotated[JobEnqueuer, Depends(get_enqueuer)]

# --------------------------------------------------------------------------
# principal + CSRF
# --------------------------------------------------------------------------


def require_session_cookie(request: Request) -> str:
    """Cheap gate resolved before any storage dependency: requests without
    the session cookie 401 without opening a connection."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise Unauthenticated("session required")
    return cookie


SessionCookie = Annotated[str, Depends(require_session_cookie)]


async def get_principal(
    request: Request,
    cookie: SessionCookie,
    service: IdentityServiceDep,
    repo: IdentityRepo,
) -> Principal:
    """Resolve the session cookie to a Principal; 401 when absent/invalid.

    The stored CSRF hash rides along on ``request.state`` for
    ``require_csrf`` — one storage read, no plaintext secrets materialized.
    """
    auth = await service.validate_session_detail(repo, cookie)
    if auth is None:
        raise Unauthenticated("session invalid or expired")
    request.state.csrf_hash = auth.csrf_hash
    return auth.principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def require_csrf(request: Request, principal: CurrentPrincipal) -> None:
    """Non-GET commands must carry X-CSRF-Token matching the session."""
    header = request.headers.get("x-csrf-token", "")
    expected = getattr(request.state, "csrf_hash", "")
    service: IdentityService = request.app.state.identity_service
    hashed = service.hash_token(header) if header else ""
    if not header or not expected or not hmac.compare_digest(hashed, expected):
        raise CsrfFailed("missing or invalid X-CSRF-Token")


Csrf = Annotated[None, Depends(require_csrf)]


# --------------------------------------------------------------------------
# scopes + repositories
# --------------------------------------------------------------------------


def get_workspace_repo(
    principal: CurrentPrincipal, conn: ConnDep
) -> WorkspaceRepository:
    """Owner-scoped workspace repo (industries list/create, scope checks)."""
    return SqlAlchemyWorkspaceRepository(
        conn, IndustryScope(owner_id=principal.user_id)
    )


async def get_scope(
    industry_id: UUID,
    principal: CurrentPrincipal,
    repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
) -> IndustryScope:
    """Validate URL industry ownership before anything else touches data.

    404 for both missing and foreign industries — a 403 would confirm the
    object exists for someone else (08 §1, U04 个人隔离). The owner always
    comes from the session, never the URL.
    """
    if not await repo.industry_owned(industry_id):
        raise NotFound("industry not found")
    return IndustryScope(owner_id=principal.user_id, industry_id=industry_id)


IndustryScopeDep = Annotated[IndustryScope, Depends(get_scope)]


def get_industry_workspace_repo(
    scope: IndustryScopeDep, conn: ConnDep
) -> WorkspaceRepository:
    return SqlAlchemyWorkspaceRepository(conn, scope)


def get_sources_repo(principal: CurrentPrincipal, conn: ConnDep) -> SourcesRepository:
    """Owner-scoped sources repo (/feeds routes)."""
    return SqlAlchemySourcesRepository(conn, IndustryScope(owner_id=principal.user_id))


def get_industry_sources_repo(
    scope: IndustryScopeDep, conn: ConnDep
) -> SourcesRepository:
    return SqlAlchemySourcesRepository(conn, scope)


def get_knowledge_repo(
    scope: IndustryScopeDep, conn: ConnDep
) -> KnowledgeReadRepository:
    """Industry-scoped knowledge reads (timeline/events/evidence/entities)."""
    return SqlAlchemyKnowledgeRepository(conn, scope)


def get_conversation_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemyConversationRepository(conn, scope)


def get_review_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemyReviewRepository(conn, scope)


def get_report_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemyReportRepository(conn, scope)


def get_search_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemySearchRepository(conn, scope)


def get_coverage_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemySearchRepository(conn, scope)


def get_document_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemyDocumentRepository(conn, scope)


def get_generation_repo(scope: IndustryScopeDep, conn: ConnDep):
    return SqlAlchemyGenerationRepository(conn, scope)


def get_jobs_repo(principal: CurrentPrincipal, conn: ConnDep):
    return SqlAlchemyJobsStore(conn, IndustryScope(owner_id=principal.user_id))


def get_job_event_log(principal: CurrentPrincipal, conn: ConnDep):
    store = SqlAlchemyJobsStore(conn, IndustryScope(owner_id=principal.user_id))
    return SqlAlchemyJobEventLog(store)


def get_idempotency_repo(
    principal: CurrentPrincipal, conn: ConnDep
) -> IdempotencyRepository:
    return SqlAlchemyIdempotencyRepository(
        conn, IndustryScope(owner_id=principal.user_id)
    )


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------


def get_workspace_service(
    repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    enqueuer: Enqueuer,
) -> WorkspaceService:
    return WorkspaceService(repo, enqueuer)


def get_industry_workspace_service(
    repo: Annotated[WorkspaceRepository, Depends(get_industry_workspace_repo)],
    enqueuer: Enqueuer,
) -> WorkspaceService:
    return WorkspaceService(repo, enqueuer)


def get_sources_service(
    repo: Annotated[SourcesRepository, Depends(get_sources_repo)],
    enqueuer: Enqueuer,
) -> SourcesService:
    return SourcesService(repo, enqueuer)


def get_industry_sources_service(
    repo: Annotated[SourcesRepository, Depends(get_industry_sources_repo)],
    enqueuer: Enqueuer,
) -> SourcesService:
    return SourcesService(repo, enqueuer)


def cursor_signing_key(request: Request) -> str:
    """HMAC key for opaque list cursors (session pepper re-use)."""
    return request.app.state.settings.session_pepper


__all__ = [
    "SESSION_COOKIE",
    "ConnDep",
    "Csrf",
    "CurrentPrincipal",
    "Enqueuer",
    "IdentityRepo",
    "IdentityServiceDep",
    "IndustryScopeDep",
    "get_conn",
    "get_conversation_repo",
    "get_coverage_repo",
    "get_document_repo",
    "get_enqueuer",
    "get_generation_repo",
    "get_idempotency_repo",
    "get_identity_repo",
    "get_identity_service",
    "get_industry_sources_repo",
    "get_industry_sources_service",
    "get_industry_workspace_repo",
    "get_industry_workspace_service",
    "get_job_event_log",
    "get_jobs_repo",
    "get_principal",
    "get_report_repo",
    "get_review_repo",
    "get_scope",
    "get_search_repo",
    "get_settings",
    "get_sources_repo",
    "get_sources_service",
    "get_workspace_repo",
    "get_workspace_service",
    "require_csrf",
]
