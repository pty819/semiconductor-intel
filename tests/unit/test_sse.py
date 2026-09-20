"""Unit tests: SSE replay / cursor expiry / heartbeat (07 §7; Task 14).

Offline over an in-memory JobEventLog: replay resumes strictly after the
Last-Event-ID; an id predating the retained window raises
event_cursor_expired (never a silent skip); heartbeats are comments; the
stream ends when the job settles.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest

from intel.api.sse import (
    HEARTBEAT_SECONDS,
    EventCursorExpired,
    SseEvent,
    sse_stream,
)

JOB = uuid4()


class FakeRequest:
    def __init__(self, last_event_id: str | None) -> None:
        self.headers = {"Last-Event-ID": last_event_id} if last_event_id else {}

    async def is_disconnected(self) -> bool:
        return False


class FakeLog:
    def __init__(
        self, events: list[dict[str, Any]], *, active: bool = True, earliest: int = 1
    ) -> None:
        self.events = events
        self.active = active
        self._earliest = earliest
        self.queries: list[int] = []

    async def events_after(self, job_id, after_seq, *, limit) -> list[dict]:
        self.queries.append(after_seq)
        return [e for e in self.events if e["seq"] > after_seq][:limit]

    async def earliest_seq(self, job_id) -> int:
        return self._earliest

    async def job_is_active(self, job_id) -> bool:
        return self.active


EVENTS = [
    {"seq": 3, "kind": "progress", "payload": {"phase": "retrieving"}},
    {"seq": 4, "kind": "progress", "payload": {"phase": "extracting"}},
    {"seq": 5, "kind": "completed", "payload": {}},
]


class TestReplay:
    async def test_reconnect_resumes_after_last_event_id(self):
        log = FakeLog(EVENTS, active=False)
        response = await sse_stream(
            FakeRequest("4"), job_id=JOB, log=log, poll_seconds=0
        )
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= 3:
                break
        body = "".join(chunks)
        assert "id: 5" in body and "event: completed" in body
        assert "id: 4" not in body and "id: 3" not in body  # strictly after
        assert log.queries[0] == 4

    async def test_fresh_connect_replays_everything(self):
        log = FakeLog(EVENTS, active=False)
        response = await sse_stream(
            FakeRequest(None), job_id=JOB, log=log, poll_seconds=0
        )
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= 4:
                break
        body = "".join(chunks)
        assert "id: 3" in body and "id: 5" in body

    async def test_expired_cursor_rejected_not_skipped(self):
        log = FakeLog(EVENTS, earliest=3)
        with pytest.raises(EventCursorExpired) as caught:
            await sse_stream(FakeRequest("1"), job_id=JOB, log=log)
        assert caught.value.code == "event_cursor_expired"


class TestHeartbeatAndEnd:
    async def test_heartbeat_comment_keeps_connection_alive(self):
        log = FakeLog([], active=True)
        response = await sse_stream(
            FakeRequest(None),
            job_id=JOB,
            log=log,
            heartbeat_seconds=0.01,
            poll_seconds=0.01,
        )
        saw_beat = False
        task = asyncio.ensure_future(collect(response, 6))
        chunks = await asyncio.wait_for(task, timeout=2)
        for chunk in chunks:
            if chunk.strip() == ": ping":
                saw_beat = True
        assert saw_beat
        assert HEARTBEAT_SECONDS > 0

    async def test_stream_ends_with_stream_end_when_settled(self):
        log = FakeLog(EVENTS, active=False)
        response = await sse_stream(
            FakeRequest(None), job_id=JOB, log=log, poll_seconds=0.001
        )
        chunks = await collect(response, 10)
        body = "".join(chunks)
        assert "event: stream_end" in body


class TestPollTransactions:
    async def test_each_poll_batch_opens_its_own_log(self):
        """The poll loop must not ride the request transaction: every
        batch opens (and closes) its own short-lived log context."""
        from contextlib import asynccontextmanager

        log = FakeLog(EVENTS, active=True)
        opened: list[int] = []

        @asynccontextmanager
        async def open_log():
            opened.append(1)
            yield log

        response = await sse_stream(
            FakeRequest(None),
            job_id=JOB,
            log=log,
            open_log=open_log,
            poll_seconds=0,
            batch_limit=2,  # force a second poll for the third event
        )
        chunks = await collect(response, 3)
        assert len(chunks) == 3
        assert len(opened) >= 2  # one fresh context per poll batch


async def collect(response, max_chunks: int) -> list[str]:
    out: list[str] = []
    async for chunk in response.body_iterator:
        out.append(chunk if isinstance(chunk, str) else chunk.decode())
        if len(out) >= max_chunks:
            break
    return out


class TestSseEventRender:
    def test_render_shape_and_utf8_payload(self):
        event = SseEvent(seq=7, kind="progress", payload={"phase": "检索"})
        text = event.render()
        assert text.startswith("id: 7\nevent: progress\ndata: ")
        assert json.loads(text.split("data: ", 1)[1].strip())["phase"] == "检索"
        assert text.endswith("\n\n")
