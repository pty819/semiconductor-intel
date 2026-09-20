"""Cancel route must call JobsStore.request_cancel(*, at=) (real signature)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from intel.api.routes.jobs import request_job_cancel
from intel.repositories.jobs import (
    CANCELLABLE_STATES,
    InMemoryJobsDatabase,
    InMemoryJobsStore,
    JobRecord,
)
from intel.services.errors import InvalidStateTransition

OWNER = uuid4()


def _job(*, state: str) -> JobRecord:
    return JobRecord(
        owner_id=OWNER,
        kind="archive_answer",
        idempotency_key=f"k-{uuid4()}",
        state=state,
        input={"q": "1"},
    )


class TestRequestJobCancel:
    async def test_running_job_sets_cancel_requested_at_via_keyword(self) -> None:
        db = InMemoryJobsDatabase()
        store = InMemoryJobsStore(db)
        job = _job(state="running")
        assert await store.insert_job(job) is not None
        at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        await request_job_cancel(store, job, now=at)
        fresh = await store.get_job(job.id)
        assert fresh is not None
        assert fresh.cancel_requested_at == at

    async def test_terminal_job_is_invalid_state_not_typeerror(self) -> None:
        db = InMemoryJobsDatabase()
        store = InMemoryJobsStore(db)
        job = _job(state="succeeded")
        assert await store.insert_job(job) is not None
        with pytest.raises(InvalidStateTransition):
            await request_job_cancel(store, job)
        fresh = await store.get_job(job.id)
        assert fresh is not None
        assert fresh.cancel_requested_at is None

    def test_cancellable_states_match_store_predicate(self) -> None:
        assert "running" in CANCELLABLE_STATES
        assert "succeeded" not in CANCELLABLE_STATES
