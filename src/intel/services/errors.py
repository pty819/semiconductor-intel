"""Service-layer error vocabulary shared by services and the HTTP layer.

Every error carries a stable ``code`` (docs/08-api.md §6) and the HTTP status
the API renders it with, so ``api/errors.py`` only has to format the envelope
— the mapping lives in exactly one place, next to the semantics.

Codes outside the 08 §6 list (``already_exists``, ``rate_limited``,
``parser_unavailable``, ``internal_error``) are documented extensions: the
binding vocabulary has no entry for those business cases (duplicate active
name, login rate limiting, no published parser for an adapter, unhandled
server error). Task 15's contract pass should ratify or rename them.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base: a domain failure rendered as an ErrorEnvelope by the API."""

    code = "internal_error"
    http_status = 500

    def __init__(
        self, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.headers: dict[str, str] = {}


class Unauthenticated(ServiceError):
    code = "unauthenticated"
    http_status = 401


class CsrfFailed(ServiceError):
    code = "csrf_failed"
    http_status = 403


class NotFound(ServiceError):
    code = "not_found"
    http_status = 404


class VersionConflict(ServiceError):
    """Optimistic-concurrency miss (08 §1: 409, carry current version)."""

    code = "version_conflict"
    http_status = 409

    def __init__(self, current_version: int) -> None:
        super().__init__(
            "the row was modified after the version you read",
            details={"current_version": current_version},
        )


class IdempotencyConflict(ServiceError):
    """Same Idempotency-Key, different request body (08 §1)."""

    code = "idempotency_conflict"
    http_status = 409


class InvalidStateTransition(ServiceError):
    code = "invalid_state_transition"
    http_status = 409

    def __init__(
        self, message: str, *, action: str, current: str
    ) -> None:
        super().__init__(message, details={"action": action, "current": current})


class AlreadyExists(ServiceError):
    """A uniqueness rule over live rows rejected the write (03 §2)."""

    code = "already_exists"
    http_status = 409


class ValidationFailed(ServiceError):
    """Semantic validation beyond what the DTO schema can express."""

    code = "validation_error"
    http_status = 422


class RateLimited(ServiceError):
    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.headers["Retry-After"] = str(retry_after_seconds)


class ParserUnavailable(ServiceError):
    """No published parser version exists for the adapter (503)."""

    code = "parser_unavailable"
    http_status = 503


class EventCursorExpiredError(ServiceError):
    """SSE Last-Event-ID predates the retained log (07 §7, 08 §6)."""

    code = "event_cursor_expired"
    http_status = 409


class ConversationTurnConflict(ServiceError):
    """CHAT-04: parent_message_id / in-flight turn rejected (409)."""

    code = "invalid_state_transition"
    http_status = 409
