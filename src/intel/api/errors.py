"""API error rendering: every failure becomes an ErrorEnvelope (spec 08 §1/§6).

The stable code and HTTP status already live on the ServiceError hierarchy in
``services/errors.py``; this module only formats
``{error: {code, message, request_id, details?}}`` and keeps details free of
stacks, SQL, secrets, and other users' object existence. Pydantic validation
failures become 422 ``validation_error`` (locations and messages only — the
rejected ``input`` values are deliberately dropped so credentials echoed in a
bad request never land in an error body).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from intel.contracts import ErrorDetail, ErrorEnvelope
from intel.services.errors import ServiceError, ValidationFailed

_HTTP_CODE_FALLBACK = {
    400: "validation_error",
    401: "unauthenticated",
    403: "csrf_failed",
    404: "not_found",
    405: "validation_error",
    409: "validation_error",
    422: "validation_error",
    429: "rate_limited",
    503: "model_unavailable",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or uuid4().hex


def envelope_response(
    request: Request,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=_request_id(request),
            details=details,
        )
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return envelope_response(
            request,
            exc.code,
            exc.message,
            exc.http_status,
            details=exc.details,
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        errors = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": str(err.get("msg", "")),
            }
            for err in exc.errors()
        ]
        first = errors[0] if errors else {"loc": [], "msg": "invalid request"}
        where = ".".join(first["loc"]) or "body"
        return envelope_response(
            request,
            ValidationFailed.code,
            f"{where}: {first['msg']}",
            ValidationFailed.http_status,
            details={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ):
        code = _HTTP_CODE_FALLBACK.get(exc.status_code, "validation_error")
        return envelope_response(
            request, code, str(exc.detail), exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # Last resort: no detail leaks (08 §1: details 不含 stack/SQL/secret).
        return envelope_response(
            request, "internal_error", "internal server error", 500
        )
