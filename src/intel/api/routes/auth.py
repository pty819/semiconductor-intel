"""Auth routes: /auth/login, /auth/logout, /auth/me (spec 08 §1/§2, 10 §2).

Login checks Origin (CSRF is impossible before a session exists), rotates a
fresh session (防会话固定), sets the opaque cookie with
HttpOnly/Secure/SameSite=Lax and returns the CSRF token exactly once — only
its peppered hash is stored, so /auth/me cannot resurrect it. Logout revokes
the session; accounts themselves are CLI-only (01 §5), never HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from intel.api.deps import (
    SESSION_COOKIE,
    get_identity_repo,
    get_identity_service,
    get_principal,
    require_csrf,
)
from intel.contracts import Ack, LoginRequest, UserView
from intel.services.errors import CsrfFailed, RateLimited, Unauthenticated
from intel.services.identity import (
    SESSION_TTL,
    IdentityRepository,
    IdentityService,
    LoginRateLimited,
    Principal,
)

router = APIRouter(prefix="/auth", tags=["auth"])

IdentityRepo = Annotated[IdentityRepository, Depends(get_identity_repo)]
IdentityServiceDep = Annotated[IdentityService, Depends(get_identity_service)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Csrf = Annotated[None, Depends(require_csrf)]


def _check_origin(request: Request) -> None:
    """Login's CSRF substitute: when Origin is present it must match Host."""
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc != host:
        raise CsrfFailed("cross-origin login refused")


def _session_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(
    request: Request,
    body: LoginRequest,
    service: IdentityServiceDep,
    repo: IdentityRepo,
) -> Response:
    _check_origin(request)
    try:
        principal = await service.authenticate(repo, body.login, body.password)
    except LoginRateLimited as exc:
        wait = int((exc.retry_after - datetime.now(UTC)).total_seconds()) + 1
        raise RateLimited(
            "too many failed logins; try again later",
            retry_after_seconds=max(wait, 1),
        ) from exc
    if principal is None:
        raise Unauthenticated("login failed")
    tokens = await service.issue_session(repo, principal)
    user = await repo.get_user(principal.user_id)
    if user is None:  # pragma: no cover — issue_session already loaded it
        raise Unauthenticated("login failed")
    view = UserView(
        id=user.id,
        login=user.login,
        timezone=user.timezone,
        csrf_token=tokens.csrf_token,
    )
    response = JSONResponse(view.model_dump(mode="json"), status_code=200)
    _session_cookie(response, tokens.cookie_value)
    return response


@router.post("/logout")
async def logout(
    principal: CurrentPrincipal,
    service: IdentityServiceDep,
    repo: IdentityRepo,
    _: Csrf,
) -> Response:
    await service.logout(repo, principal)
    response = JSONResponse(Ack(ok=True).model_dump(mode="json"), status_code=200)
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax"
    )
    return response


@router.get("/me")
async def me(
    principal: CurrentPrincipal,
    repo: IdentityRepo,
) -> UserView:
    """Current settings. csrf_token is "" here: the plaintext token is
    handed out once at login; storage keeps only its hash."""
    user = await repo.get_user(principal.user_id)
    if user is None:
        raise Unauthenticated("session invalid")
    return UserView(
        id=user.id, login=user.login, timezone=user.timezone, csrf_token=""
    )
