"""SSE plumbing for job progress streams (spec 07 §7, 08 §2; Task 14).

Semantics:

- **Replay by Last-Event-ID**: a reconnect sends the ``id:`` of the last
  event it saw; the stream replays everything after it from the durable
  job_events log (seq numbers), then continues live.
- **event_cursor_expired (409-class)**: when the client's Last-Event-ID
  predates the log's retention floor (events pruned), the handshake
  rejects instead of silently skipping — the client refetches state.
- **Heartbeat comments**: ``: ping`` lines every ``heartbeat_seconds``
  keep intermediaries from idling the connection out; comments carry no
  event data.
- The generator never holds a database cursor: it polls the event log in
  bounded batches (the jobs table is the durability boundary).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import Request
from fastapi.responses import StreamingResponse

__all__ = [
    "HEARTBEAT_SECONDS",
    "EventCursorExpired",
    "JobEventLog",
    "sse_stream",
]

#: Keepalive comment cadence (07 §7).
HEARTBEAT_SECONDS = 15.0

#: Retention floor: how far back replay is guaranteed (07 §7 retention).
REPLAY_WINDOW_SECONDS = 24 * 3600


class EventCursorExpired(Exception):
    """Last-Event-ID older than the replayable window (409-class)."""

    code = "event_cursor_expired"


class JobEventLog(Protocol):
    """What the SSE stream needs from durable storage."""

    async def events_after(
        self, job_id: UUID, after_seq: int, *, limit: int
    ) -> list[dict[str, Any]]:
        """Rising-seq events; each carries ``seq``, ``kind``, ``payload``."""

    async def earliest_seq(self, job_id: UUID) -> int | None: ...

    async def job_is_active(self, job_id: UUID) -> bool: ...


@dataclass(slots=True)
class SseEvent:
    seq: int
    kind: str
    payload: dict[str, Any]

    def render(self) -> str:
        import json

        lines = [
            f"id: {self.seq}",
            f"event: {self.kind}",
            f"data: {json.dumps(self.payload, ensure_ascii=False)}",
        ]
        return "\n".join(lines) + "\n\n"


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("Last-Event-ID")
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


async def sse_stream(
    request: Request,
    *,
    job_id: UUID,
    log: JobEventLog,
    open_log: Callable[[], AbstractAsyncContextManager[JobEventLog]] | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    poll_seconds: float = 1.0,
    batch_limit: int = 100,
) -> StreamingResponse:
    """One job's event stream with replay + heartbeat + clean completion.

    ``log`` serves the synchronous handshake (cursor expiry check) on the
    caller's — request-scoped — transaction. ``open_log`` opens ONE
    SHORT-LIVED transaction per poll batch: FastAPI closes yield-
    dependencies only after the response finishes streaming, so polling
    through the request transaction would pin one connection
    idle-in-transaction for the stream's whole lifetime. Production must
    pass ``open_log``; the default reuses ``log`` (the in-memory fakes'
    single-object contract).
    """
    if open_log is None:

        @asynccontextmanager
        async def _reuse_log() -> AsyncIterator[JobEventLog]:
            yield log

        open_log = _reuse_log

    cursor = _last_event_id(request)
    if cursor is not None:
        earliest = await log.earliest_seq(job_id)
        if earliest is not None and cursor + 1 < earliest:
            # The requested resume point predates the retained log —
            # refuse rather than silently skip events.
            raise EventCursorExpired(
                f"Last-Event-ID {cursor} predates retained log (earliest {earliest})"
            )

    async def generate() -> AsyncIterator[str]:
        last_beat = datetime.now(UTC)
        current = cursor if cursor is not None else 0
        while True:
            if await request.is_disconnected():
                return
            # One short transaction per batch: no connection is held
            # open across the sleep below.
            async with open_log() as poll_log:
                batch = await poll_log.events_after(job_id, current, limit=batch_limit)
                active = await poll_log.job_is_active(job_id)
            for event in batch:
                rendered = SseEvent(
                    seq=int(event["seq"]),
                    kind=str(event.get("kind", "progress")),
                    payload=dict(event.get("payload") or {}),
                )
                current = rendered.seq
                yield rendered.render()
            if not active and not batch:
                # Terminal: nothing more will land; the final batch (if
                # any) has already been yielded above.
                yield "event: stream_end\ndata: {}\n\n"
                return
            now = datetime.now(UTC)
            if (now - last_beat).total_seconds() >= heartbeat_seconds:
                yield ": ping\n\n"
                last_beat = now
            await asyncio.sleep(poll_seconds)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
