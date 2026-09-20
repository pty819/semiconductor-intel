"""Scoped-token tool gateway for the investigation agent (spec 14 §1, D16).

The runner signs a short-lived scoped token when it starts an
InvestigationAgent job; the token declares owner/industry/job/actions/
expiry and nothing else — the worker process never holds database
credentials (14 §1: 数据库credential不传入NOOA worker). Every gateway tool
re-verifies, per call:

1. the HMAC signature (token not forged or truncated);
2. the expiry (short TTL; a leaked token dies on its own);
3. the action grant (a token issued for archive reading cannot fetch
   public pages);
4. that the job is still active — a cancelled job or a lost lease
   revokes capability immediately (job取消后撤销能力).

Fail-closed everywhere: :class:`GatewayRejected` propagates out of the
tool into the strategy and fails the job; there is no read-only
degradation path.

The five tools (14 §1 / doc 06 §4) delegate to seams — async callables
injected by the composition root. Tasks 12/14 wire the real seams
(repositories, SSRF-guarded fetch client); this module owns the guard
and the shape.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

ACTION_SEARCH_ARCHIVE = "search_archive"
ACTION_READ_EVIDENCE = "read_evidence"
ACTION_READ_DOCUMENT_BLOCKS = "read_document_blocks"
ACTION_FETCH_PUBLIC = "fetch_public"
ACTION_SEARCH_WEB = "search_web"

GATEWAY_ACTIONS = frozenset(
    {
        ACTION_SEARCH_ARCHIVE,
        ACTION_READ_EVIDENCE,
        ACTION_READ_DOCUMENT_BLOCKS,
        ACTION_FETCH_PUBLIC,
        ACTION_SEARCH_WEB,
    }
)

#: Seams: async callables the composition root injects. All run in the
#: parent process — 网络工具必须在 gateway 检查 scope/SSRF/timeout (doc 06
#: §6); the worker-side network ban is not the network limit. Each seam
#: receives the verified token payload so it can bind scope without ever
#: seeing the signing secret.
SearchArchiveFn = Callable[..., Awaitable[list[dict[str, Any]]]]
ReadEvidenceFn = Callable[..., Awaitable[dict[str, Any]]]
ReadDocumentBlocksFn = Callable[..., Awaitable[dict[str, Any]]]
FetchPublicFn = Callable[..., Awaitable[dict[str, Any]]]
SearchWebFn = Callable[..., Awaitable[list[dict[str, Any]]]]
JobActiveCheck = Callable[[UUID], Awaitable[bool]]


class GatewayRejected(Exception):
    """A gateway call was refused — fail closed, never degrade."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ScopedTokenPayload:
    owner_id: UUID
    industry_id: UUID | None
    job_id: UUID
    actions: frozenset[str]
    expires_at: datetime


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_scoped_token(
    secret: str,
    *,
    owner_id: UUID,
    industry_id: UUID | None,
    job_id: UUID,
    actions: frozenset[str] | set[str],
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """Sign ``base64url(payload).hmac_sha256`` — no external dependency.

    The secret comes from Settings.gateway_secret (env-injected). Keep the
    payload minimal: it travels inside the job process, but minimal scope
    keeps a leaked token cheap.
    """
    if not secret:
        raise GatewayRejected(
            "gateway_unconfigured", "gateway secret is empty (fail closed)"
        )
    unknown = set(actions) - GATEWAY_ACTIONS
    if unknown:
        raise GatewayRejected(
            "unknown_actions", f"not gateway actions: {sorted(unknown)}"
        )
    issued_at = now or datetime.now(UTC)
    payload = {
        "owner_id": str(owner_id),
        "industry_id": str(industry_id) if industry_id else None,
        "job_id": str(job_id),
        "actions": sorted(actions),
        "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


def verify_scoped_token(
    secret: str, token: str, *, now: datetime | None = None
) -> ScopedTokenPayload:
    """Verify signature + expiry; raise :class:`GatewayRejected` otherwise."""
    if not secret:
        raise GatewayRejected(
            "gateway_unconfigured", "gateway secret is empty (fail closed)"
        )
    try:
        body, signature_text = token.split(".", 1)
        signature = _b64url_decode(signature_text)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("signature mismatch")
        payload = json.loads(_b64url_decode(body))
        if not isinstance(payload, dict):
            raise TypeError("payload not an object")
        parsed = ScopedTokenPayload(
            owner_id=UUID(payload["owner_id"]),
            industry_id=(
                UUID(payload["industry_id"]) if payload["industry_id"] else None
            ),
            job_id=UUID(payload["job_id"]),
            actions=frozenset(payload["actions"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise GatewayRejected("bad_token", "malformed or forged token") from exc
    if (now or datetime.now(UTC)) >= parsed.expires_at:
        raise GatewayRejected("token_expired", "scoped token past expiry")
    return parsed


class ToolGateway:
    """The five scoped tools bound to one job's token.

    Constructed by the runner per job. Seams left as ``None`` reject calls
    with ``tool_unavailable`` — an explicitly online-only tool (fetch_public,
    search_web) stays unavailable until the composition root wires it (16
    §2: 显式在线模式才提供).
    """

    def __init__(
        self,
        *,
        token: str,
        secret: str,
        is_job_active: JobActiveCheck,
        search_archive_fn: SearchArchiveFn | None = None,
        read_evidence_fn: ReadEvidenceFn | None = None,
        read_document_blocks_fn: ReadDocumentBlocksFn | None = None,
        fetch_public_fn: FetchPublicFn | None = None,
        search_web_fn: SearchWebFn | None = None,
    ) -> None:
        # Construction-time verification: a bad token never yields a
        # gateway at all. The raw token is kept for per-call re-verification
        # (expiry passes as the clock moves).
        self._payload = verify_scoped_token(secret, token)
        self._token = token
        self._secret = secret
        self._is_job_active = is_job_active
        self._search_archive_fn = search_archive_fn
        self._read_evidence_fn = read_evidence_fn
        self._read_document_blocks_fn = read_document_blocks_fn
        self._fetch_public_fn = fetch_public_fn
        self._search_web_fn = search_web_fn

    @property
    def payload(self) -> ScopedTokenPayload:
        return self._payload

    async def _check(self, action: str) -> ScopedTokenPayload:
        """Per-call re-verification (14 §1: token复验 + job仍active)."""
        payload = verify_scoped_token(self._secret, self._token)
        if action not in payload.actions:
            raise GatewayRejected(
                "action_not_granted",
                f"token does not grant {action!r}",
            )
        if not await self._is_job_active(payload.job_id):
            raise GatewayRejected(
                "job_inactive", "job cancelled or lease lost; tools revoked"
            )
        return payload

    # -- the five tools ------------------------------------------------------

    async def search_archive(
        self, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        payload = await self._check(ACTION_SEARCH_ARCHIVE)
        if self._search_archive_fn is None:
            raise GatewayRejected("tool_unavailable", "search_archive seam not wired")
        return await self._search_archive_fn(payload, query=query, limit=limit)

    async def read_evidence(self, evidence_id: str) -> dict[str, Any]:
        payload = await self._check(ACTION_READ_EVIDENCE)
        if self._read_evidence_fn is None:
            raise GatewayRejected("tool_unavailable", "read_evidence seam not wired")
        return await self._read_evidence_fn(payload, evidence_id=evidence_id)

    async def read_document_blocks(
        self, document_id: str, block_ids: list[str] | None = None
    ) -> dict[str, Any]:
        payload = await self._check(ACTION_READ_DOCUMENT_BLOCKS)
        if self._read_document_blocks_fn is None:
            raise GatewayRejected(
                "tool_unavailable", "read_document_blocks seam not wired"
            )
        return await self._read_document_blocks_fn(
            payload, document_id=document_id, block_ids=block_ids
        )

    async def fetch_public(self, url: str) -> dict[str, Any]:
        payload = await self._check(ACTION_FETCH_PUBLIC)
        if self._fetch_public_fn is None:
            raise GatewayRejected(
                "tool_unavailable",
                "fetch_public seam not wired (online mode off?)",
            )
        return await self._fetch_public_fn(payload, url=url)

    async def search_web(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        payload = await self._check(ACTION_SEARCH_WEB)
        if self._search_web_fn is None:
            raise GatewayRejected(
                "tool_unavailable",
                "search_web seam not wired (online mode off?)",
            )
        return await self._search_web_fn(payload, query=query, limit=limit)


__all__ = [
    "ACTION_FETCH_PUBLIC",
    "ACTION_READ_DOCUMENT_BLOCKS",
    "ACTION_READ_EVIDENCE",
    "ACTION_SEARCH_ARCHIVE",
    "ACTION_SEARCH_WEB",
    "GATEWAY_ACTIONS",
    "GatewayRejected",
    "JobActiveCheck",
    "ScopedTokenPayload",
    "ToolGateway",
    "sign_scoped_token",
    "verify_scoped_token",
]
