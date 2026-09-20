"""Unit tests: apply_review workflow wraps ReviewService.decide."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService
from intel.services.reviews import (
    ReviewDecisionKind,
    ReviewRecord,
    ReviewService,
)
from intel.workers.runner import JobRunner
from intel.workflows.review import ReviewWiring, make_review_handler

OWNER = uuid4()
INDUSTRY = uuid4()
ACTOR = uuid4()


class InMemoryReviewStore:
    def __init__(self) -> None:
        self.reviews: dict = {}
        self.versions: dict = {}
        self.audit: list[dict] = []
        self.applied: list[UUID] = []
        self.compensated: list[UUID] = []

    async def get_review(self, review_id):
        return self.reviews.get(review_id)

    async def row_versions(self, refs):
        return {ref: self.versions.get(ref, 1) for ref in refs}

    async def save_review(self, review):
        self.reviews[review.id] = review

    async def append_audit(self, entry):
        self.audit.append(entry)

    async def apply_decision(self, review):
        self.applied.append(review.id)
        return {"applied": str(review.id)}

    async def compensate(self, review):
        self.compensated.append(review.id)


def _store_with_review():
    store = InMemoryReviewStore()
    review = ReviewRecord(
        id=uuid4(), object_type="event_merge", payload={"canonical": "a"}
    )
    event_id = uuid4()
    store.reviews[review.id] = review
    store.versions[("events", event_id)] = 3
    return store, review, event_id


async def _run(store, payload):
    db = InMemoryJobsDatabase()
    service = JobService(clock=lambda: datetime.now(UTC), rng=random.Random(1))

    @asynccontextmanager
    async def open_jobs(scope):
        yield InMemoryJobsStore(db)

    @asynccontextmanager
    async def open_review(scope):
        yield store

    runner = JobRunner(service, open_jobs)
    runner.register(
        "apply_review",
        make_review_handler(ReviewWiring(open_store=open_review)),
    )
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(OWNER, INDUSTRY),
        kind="apply_review",
        payload=payload,
        idempotency_key=(
            f"apply_review:{payload['industry']}/{payload['review_id']}"
            f"/{payload['decision_version']}"
        ),
    )
    return await runner.run_once()


class TestApplyReview:
    async def test_approve_uses_expected_versions_from_payload(self):
        store, review, event_id = _store_with_review()
        result = await _run(
            store,
            {
                "industry": str(INDUSTRY),
                "review_id": str(review.id),
                "decision_version": "3",
                "action": "approve",
                "actor_id": str(ACTOR),
                "expected_versions": {f"events:{event_id}": 3},
            },
        )
        assert result.state == "succeeded"
        assert result.progress["status"] == "approved"
        assert result.progress["applied"] is True
        assert store.applied == [review.id]
        assert isinstance(result.input["review_id"], str)

    async def test_stale_expected_versions_fail_the_job(self):
        store, review, event_id = _store_with_review()
        result = await _run(
            store,
            {
                "industry": str(INDUSTRY),
                "review_id": str(review.id),
                "decision_version": "2",
                "action": "approve",
                "actor_id": str(ACTOR),
                "expected_versions": {f"events:{event_id}": 2},
            },
        )
        assert result.state == "failed"
        assert result.error["code"] == "version_conflict"
        assert store.applied == []

    async def test_idempotent_re_delivery(self):
        store, review, event_id = _store_with_review()
        payload = {
            "industry": str(INDUSTRY),
            "review_id": str(review.id),
            "decision_version": "3",
            "action": "approve",
            "actor_id": str(ACTOR),
            "expected_versions": {f"events:{event_id}": 3},
        }
        first = await _run(store, payload)
        assert first.progress["applied"] is True
        # Same decided review, new job (at-least-once).
        payload = {**payload, "decision_version": "3-retry"}
        second = await _run(store, payload)
        assert second.state == "succeeded"
        assert second.progress["applied"] is False
        assert len(store.applied) == 1

    async def test_undo_compensates(self):
        store, review, event_id = _store_with_review()
        await ReviewService(store).decide(
            review_id=review.id,
            actor=ACTOR,
            decision=ReviewDecisionKind.APPROVE,
            expected_versions={("events", event_id): 3},
        )
        result = await _run(
            store,
            {
                "industry": str(INDUSTRY),
                "review_id": str(review.id),
                "decision_version": "undo-1",
                "action": "undo",
                "actor_id": str(ACTOR),
                "expected_versions": {f"events:{event_id}": 3},
            },
        )
        assert result.state == "succeeded"
        assert result.progress["status"] == "pending"
        assert store.compensated == [review.id]
