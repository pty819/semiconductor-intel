"""Route registration + HTTP smoke for Task 14 remaining API (08 §2)."""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Depends
from fastapi.routing import APIRoute

from intel.api.app import create_app
from intel.api.deps import (
    get_conversation_repo,
    get_coverage_repo,
    get_document_repo,
    get_enqueuer,
    get_generation_repo,
    get_idempotency_repo,
    get_identity_repo,
    get_industry_workspace_repo,
    get_job_event_log,
    get_job_event_opener,
    get_jobs_repo,
    get_knowledge_repo,
    get_principal,
    get_report_repo,
    get_review_repo,
    get_search_repo,
    get_workspace_repo,
)
from intel.api.routes import all_routers
from intel.contracts import ConversationView, MessageView
from intel.repositories.idempotency import IdempotencyRecord
from intel.repositories.jobs import JobRecord
from intel.services.acquisition import InMemoryEnqueuer
from intel.services.identity import IdentityService, Principal
from intel.services.research import ParentMismatch

_API = importlib.util.spec_from_file_location(
    "task14_api_helpers", Path(__file__).with_name("test_api.py")
)
assert _API is not None and _API.loader is not None
_api = importlib.util.module_from_spec(_API)
_API.loader.exec_module(_api)
ALICE = _api.ALICE
BOB = _api.BOB
PASSWORD = _api.PASSWORD
BoundWorkspaceFake = _api.BoundWorkspaceFake
FakeIdentityRepo = _api.FakeIdentityRepo
add_user = _api.add_user
auth_headers = _api.auth_headers
create_industry = _api.create_industry
error_code = _api.error_code
login = _api.login

PEPPER = "task14-test-pepper"


class FakeIdempotencyRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple, IdempotencyRecord] = {}

    async def get(self, owner_id, route, key):
        return self.rows.get((owner_id, route, key))

    async def put(self, record: IdempotencyRecord) -> None:
        self.rows[(record.owner_id, record.route, record.key)] = record


class FakeConversationRepo:
    def __init__(self, owner_id: UUID) -> None:
        self.owner_id = owner_id
        self.conversations: dict[UUID, dict] = {}
        self.messages: dict[UUID, list[dict]] = {}

    async def list_conversations(self) -> list[ConversationView]:
        return [self._view(row) for row in self.conversations.values()]

    async def create_conversation(self, title: str) -> ConversationView:
        conversation_id = uuid4()
        row = {
            "id": conversation_id,
            "industry_id": getattr(self, "industry_id", None) or uuid4(),
            "title": title,
            "state_version": 1,
            "last_committed_message_id": None,
            "in_flight": False,
        }
        self.conversations[conversation_id] = row
        self.messages[conversation_id] = []
        return self._view(row)

    def bind_industry(self, industry_id: UUID) -> None:
        self.industry_id = industry_id
        for row in self.conversations.values():
            if row["industry_id"] is None:
                row["industry_id"] = industry_id

    async def get_conversation(self, conversation_id: UUID) -> ConversationView | None:
        row = self.conversations.get(conversation_id)
        return None if row is None else self._view(row)

    async def list_messages(self, conversation_id: UUID) -> list[MessageView]:
        return [
            MessageView(
                id=m["id"],
                parent_message_id=m["parent_message_id"],
                turn_index=m["turn_index"],
                role=m["role"],
                status=m["status"],
                blocks=[],
                citations=[],
                job_id=m.get("job_id"),
            )
            for m in self.messages.get(conversation_id, [])
        ]

    async def begin_turn(
        self,
        conversation_id: UUID,
        *,
        text: str,
        parent_message_id: UUID | None,
        mode: str,
        as_of,
        topic_ids,
    ) -> UUID:
        row = self.conversations[conversation_id]
        if row["in_flight"] or parent_message_id != row["last_committed_message_id"]:
            raise ParentMismatch("serial turn rejected")
        message_id = uuid4()
        turn_index = len(self.messages[conversation_id]) + 1
        self.messages[conversation_id].append(
            {
                "id": message_id,
                "parent_message_id": parent_message_id,
                "turn_index": turn_index,
                "role": "user",
                "status": "pending",
                "text": text,
                "mode": mode,
            }
        )
        row["in_flight"] = True
        return message_id

    def _view(self, row: dict) -> ConversationView:
        return ConversationView(
            id=row["id"],
            title=row["title"],
            industry_id=row["industry_id"] or uuid4(),
            state_version=row["state_version"],
            last_committed_message_id=row["last_committed_message_id"],
        )


class FakeReviewRepo:
    def __init__(self) -> None:
        self.items: dict[UUID, dict] = {}

    async def list_reviews(self, status: str | None = None) -> list[dict]:
        rows = list(self.items.values())
        if status:
            rows = [r for r in rows if r["status"] == status]
        return rows

    async def get_review(self, review_id: UUID) -> dict | None:
        return self.items.get(review_id)


class FakeReportRepo:
    def __init__(self) -> None:
        self.items: dict[UUID, dict] = {}

    async def list_reports(self) -> list[dict]:
        return list(self.items.values())

    async def get_report(self, report_id: UUID) -> dict | None:
        return self.items.get(report_id)

    async def create_placeholder(self, *, report_type: str, title: str) -> UUID:
        report_id = uuid4()
        self.items[report_id] = {
            "id": report_id,
            "revision_id": uuid4(),
            "title": title,
            "as_of": datetime.now(UTC),
            "blocks": [],
            "citations": [],
            "coverage": {
                "status": "pending",
                "processed": 0,
                "failed": 0,
                "gaps": [],
            },
            "stale": False,
            "type": report_type,
        }
        return report_id


class FakeSearchRepo:
    async def search(self, query: str, **kwargs) -> list[dict]:
        return []


class FakeCoverageRepo:
    async def coverage(self) -> dict:
        return {
            "status": "unknown",
            "processed": 0,
            "failed": 0,
            "gaps": [],
        }


class FakeDocumentRepo:
    def __init__(self) -> None:
        self.items: dict[UUID, dict] = {}

    async def list_documents(self) -> list[dict]:
        return list(self.items.values())

    async def get_document(self, document_id: UUID) -> dict | None:
        return self.items.get(document_id)

    async def list_revisions(self, document_id: UUID) -> list[dict]:
        return []

    async def get_diff(self, document_id: UUID, **kwargs) -> dict | None:
        return None

    async def apply_topic_decision(self, document_id: UUID, body) -> dict:
        return {"id": document_id, "ok": True}


class FakeGenerationRepo:
    def __init__(self) -> None:
        self.items: dict[UUID, dict] = {}

    async def get_run(self, run_id: UUID) -> dict | None:
        return self.items.get(run_id)


class FakeJobsRepo:
    def __init__(self, owner_id: UUID) -> None:
        self.owner_id = owner_id
        self.jobs: dict[UUID, JobRecord] = {}

    async def list_jobs(self, **filters) -> list[JobRecord]:
        rows = [j for j in self.jobs.values() if j.owner_id == self.owner_id]
        industry_id = filters.get("industry_id")
        kind = filters.get("kind")
        state = filters.get("state")
        if industry_id:
            rows = [j for j in rows if j.industry_id == industry_id]
        if kind:
            rows = [j for j in rows if j.kind == kind]
        if state:
            rows = [j for j in rows if j.state == state]
        return rows

    async def get_job(self, job_id: UUID) -> JobRecord | None:
        job = self.jobs.get(job_id)
        if job is None or job.owner_id != self.owner_id:
            return None
        return job

    async def request_cancel(self, job_id: UUID, *, at: datetime) -> int:
        job = await self.get_job(job_id)
        if job is None:
            return 0
        job.cancel_requested_at = at
        return 1


class FakeJobEventLog:
    def __init__(self) -> None:
        self.events: dict[UUID, list[dict]] = {}
        self.earliest: dict[UUID, int] = {}
        self.active: dict[UUID, bool] = {}

    async def events_after(self, job_id, after_seq, *, limit):
        return [e for e in self.events.get(job_id, []) if e["seq"] > after_seq][:limit]

    async def earliest_seq(self, job_id):
        return self.earliest.get(job_id)

    async def job_is_active(self, job_id):
        return self.active.get(job_id, False)


def fast_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=1, memory_cost=64, parallelism=1, salt_len=8, hash_len=16
    )


class FakeKnowledgeRepo:
    """KnowledgeReadRepository double: event cards + evolutions by id."""

    def __init__(self) -> None:
        self.event_cards: dict[UUID, dict] = {}
        self.evolutions: dict[UUID, dict] = {}

    async def get_event_card(self, event_id: UUID) -> dict | None:
        return self.event_cards.get(event_id)

    async def get_evolution(self, evolution_id: UUID) -> dict | None:
        return self.evolutions.get(evolution_id)


@pytest.fixture()
def harness():
    identity_repo = FakeIdentityRepo()
    identity_service = IdentityService(session_pepper=PEPPER, hasher=fast_hasher())
    workspace_store = {
        "industries": {},
        "industry_revisions": {},
        "topics": {},
        "topic_revisions": {},
    }
    idempotency_repo = FakeIdempotencyRepo()
    enqueuer = InMemoryEnqueuer()
    conversations = FakeConversationRepo(ALICE)
    reviews = FakeReviewRepo()
    reports = FakeReportRepo()
    search = FakeSearchRepo()
    coverage = FakeCoverageRepo()
    documents = FakeDocumentRepo()
    generation = FakeGenerationRepo()
    jobs = FakeJobsRepo(ALICE)
    event_log = FakeJobEventLog()
    knowledge = FakeKnowledgeRepo()

    app = create_app()
    app.state.settings.session_pepper = PEPPER
    app.state.identity_service = identity_service
    app.state.enqueuer = enqueuer

    def _workspace_repo_for(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> BoundWorkspaceFake:
        return BoundWorkspaceFake(workspace_store, principal.user_id)

    app.dependency_overrides[get_identity_repo] = lambda: identity_repo
    app.dependency_overrides[get_workspace_repo] = _workspace_repo_for
    app.dependency_overrides[get_industry_workspace_repo] = _workspace_repo_for
    app.dependency_overrides[get_idempotency_repo] = lambda: idempotency_repo
    app.dependency_overrides[get_enqueuer] = lambda: enqueuer
    app.dependency_overrides[get_conversation_repo] = lambda: conversations
    app.dependency_overrides[get_review_repo] = lambda: reviews
    app.dependency_overrides[get_report_repo] = lambda: reports
    app.dependency_overrides[get_search_repo] = lambda: search
    app.dependency_overrides[get_coverage_repo] = lambda: coverage
    app.dependency_overrides[get_document_repo] = lambda: documents
    app.dependency_overrides[get_generation_repo] = lambda: generation
    app.dependency_overrides[get_jobs_repo] = lambda: jobs
    app.dependency_overrides[get_knowledge_repo] = lambda: knowledge
    app.dependency_overrides[get_job_event_log] = lambda: event_log

    @asynccontextmanager
    async def _open_event_log():
        # The SSE poll opener: production opens one short transaction per
        # batch; the fake just reuses the in-memory log.
        yield event_log

    app.dependency_overrides[get_job_event_opener] = lambda: _open_event_log

    return {
        "app": app,
        "identity_repo": identity_repo,
        "identity_service": identity_service,
        "workspace_store": workspace_store,
        "enqueuer": enqueuer,
        "conversations": conversations,
        "reviews": reviews,
        "reports": reports,
        "jobs": jobs,
        "event_log": event_log,
        "generation": generation,
        "documents": documents,
        "knowledge": knowledge,
    }


@pytest.fixture()
async def client(harness) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=harness["app"])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


class TestKnowledgeNullBody404:
    """I-8: missing event/evolution reads return the unified not_found
    envelope (08 §1), never a 200 with {"event": None} / null body."""

    async def test_missing_event_is_404_envelope(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        resp = await client.get(
            f"/api/v1/industries/{industry_id}/events/{uuid4()}",
            cookies={"intel_session": cookie},
        )
        assert resp.status_code == 404
        assert error_code(resp) == "not_found"

    async def test_present_event_returns_body(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        event_id = uuid4()
        harness["knowledge"].event_cards[event_id] = {"id": event_id, "title": "t"}
        resp = await client.get(
            f"/api/v1/industries/{industry_id}/events/{event_id}",
            cookies={"intel_session": cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["event"]["id"] == str(event_id)

    async def test_missing_evolution_is_404_envelope(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        topic_id = uuid4()
        resp = await client.get(
            f"/api/v1/industries/{industry_id}/topics/{topic_id}/evolution/{uuid4()}",
            cookies={"intel_session": cookie},
        )
        assert resp.status_code == 404
        assert error_code(resp) == "not_found"

    async def test_present_evolution_returns_body(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        topic_id = uuid4()
        evolution_id = uuid4()
        harness["knowledge"].evolutions[evolution_id] = {
            "id": evolution_id,
            "topic_id": topic_id,
        }
        resp = await client.get(
            f"/api/v1/industries/{industry_id}/topics/{topic_id}"
            f"/evolution/{evolution_id}",
            cookies={"intel_session": cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(evolution_id)


class TestRoutesRegistered:
    def test_all_routers_include_task14_modules(self) -> None:
        from intel.api.routes import (
            conversations,
            coverage,
            documents,
            generation_runs,
            jobs,
            reports,
            reviews,
            search,
        )

        included = {id(router) for router in all_routers}
        for router in (
            conversations.router,
            reviews.router,
            reports.router,
            search.router,
            coverage.router,
            jobs.router,
            generation_runs.router,
            documents.router,
        ):
            assert id(router) in included

    def test_jobs_events_is_get_sse(self) -> None:
        from intel.api.routes import jobs

        route = next(
            r
            for r in jobs.router.routes
            if isinstance(r, APIRoute) and r.path.endswith("/events")
        )
        assert "GET" in route.methods


class TestConversationRoutes:
    async def test_create_and_archive_message_enqueues_archive_answer(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        harness["conversations"].bind_industry(UUID(industry_id))
        conv = await client.post(
            f"/api/v1/industries/{industry_id}/conversations",
            json={"title": "PVD"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "c-1"),
        )
        assert conv.status_code == 201
        conversation_id = conv.json()["id"]
        posted = await client.post(
            f"/api/v1/industries/{industry_id}/conversations/{conversation_id}/messages",
            json={"text": "均匀性如何", "mode": "archive"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "m-1"),
        )
        assert posted.status_code == 202
        body = posted.json()
        assert body["events_url"].endswith("/events")
        record = harness["enqueuer"].records[-1]
        assert record.kind == "archive_answer"
        assert isinstance(record.payload["message_id"], str)
        assert isinstance(record.payload["industry"], str)

    async def test_online_message_enqueues_investigate(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        harness["conversations"].bind_industry(UUID(industry_id))
        conv = await client.post(
            f"/api/v1/industries/{industry_id}/conversations",
            json={"title": "在线"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "c-2"),
        )
        conversation_id = conv.json()["id"]
        posted = await client.post(
            f"/api/v1/industries/{industry_id}/conversations/{conversation_id}/messages",
            json={"text": "网上查", "mode": "online"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "m-2"),
        )
        assert posted.status_code == 202
        record = harness["enqueuer"].records[-1]
        assert record.kind == "investigate"
        assert record.payload["online"] is True
        assert isinstance(record.payload["request_version"], str)

    async def test_concurrent_parent_mismatch_409(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        harness["conversations"].bind_industry(UUID(industry_id))
        conv = await client.post(
            f"/api/v1/industries/{industry_id}/conversations",
            json={"title": "串行"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "c-3"),
        )
        conversation_id = conv.json()["id"]
        first = await client.post(
            f"/api/v1/industries/{industry_id}/conversations/{conversation_id}/messages",
            json={"text": "第一问", "mode": "archive"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "m-3a"),
        )
        assert first.status_code == 202
        second = await client.post(
            f"/api/v1/industries/{industry_id}/conversations/{conversation_id}/messages",
            json={"text": "并发", "mode": "archive"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "m-3b"),
        )
        assert second.status_code == 409

    async def test_foreign_industry_404(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        await add_user(harness, BOB, "bob")
        alice_cookie, alice_csrf = await login(client, "alice")
        created = await create_industry(client, alice_cookie, alice_csrf)
        industry_id = created.json()["id"]
        bob_cookie, bob_csrf = await login(client, "bob")
        resp = await client.get(
            f"/api/v1/industries/{industry_id}/conversations",
            cookies={"intel_session": bob_cookie},
        )
        assert resp.status_code == 404
        assert error_code(resp) == "not_found"
        del bob_csrf


class TestReviewReportSearchJobs:
    async def test_review_decision_enqueues_apply_review(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        review_id = uuid4()
        harness["reviews"].items[review_id] = {
            "id": review_id,
            "row_version": 1,
            "type": "event_merge",
            "status": "pending",
            "proposal": {},
            "expected_versions": {f"events:{uuid4()}": 3},
        }
        resp = await client.post(
            f"/api/v1/industries/{industry_id}/reviews/{review_id}/decisions",
            json={"expected_version": 1, "action": "approve", "reason": "ok"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "r-1"),
        )
        assert resp.status_code == 202
        record = harness["enqueuer"].records[-1]
        assert record.kind == "apply_review"
        assert isinstance(record.payload["review_id"], str)
        assert isinstance(record.payload["decision_version"], str)

    async def test_stale_review_expected_version_409(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        review_id = uuid4()
        harness["reviews"].items[review_id] = {
            "id": review_id,
            "row_version": 4,
            "type": "event_merge",
            "status": "pending",
            "proposal": {},
            "expected_versions": {},
        }
        resp = await client.post(
            f"/api/v1/industries/{industry_id}/reviews/{review_id}/decisions",
            json={"expected_version": 1, "action": "approve", "reason": "stale"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "r-stale"),
        )
        assert resp.status_code == 409
        assert error_code(resp) == "version_conflict"
        assert resp.json()["error"]["details"]["current_version"] == 4

    async def test_report_create_enqueues_report_build(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        resp = await client.post(
            f"/api/v1/industries/{industry_id}/reports",
            json={"type": "daily", "title": "日报"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "rep-1"),
        )
        assert resp.status_code == 202
        record = harness["enqueuer"].records[-1]
        assert record.kind == "report_build"
        assert record.payload["report_type"] == "daily"
        assert isinstance(record.payload["input_manifest_hash"], str)

    async def test_search_and_coverage(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        search = await client.post(
            f"/api/v1/industries/{industry_id}/search",
            json={"query": "EUV"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "s-1"),
        )
        assert search.status_code == 200
        assert search.json()["items"] == []
        coverage = await client.get(
            f"/api/v1/industries/{industry_id}/coverage",
            cookies={"intel_session": cookie},
        )
        assert coverage.status_code == 200
        assert coverage.json()["status"] == "unknown"

    async def test_jobs_list_get_cancel_retry_and_sse(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = UUID(created.json()["id"])
        job = JobRecord(
            owner_id=ALICE,
            industry_id=industry_id,
            kind="archive_answer",
            idempotency_key="k",
            state="running",
            input={"q": "1"},
        )
        harness["jobs"].jobs[job.id] = job
        listed = await client.get("/api/v1/jobs", cookies={"intel_session": cookie})
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == str(job.id)
        got = await client.get(
            f"/api/v1/jobs/{job.id}", cookies={"intel_session": cookie}
        )
        assert got.status_code == 200
        cancelled = await client.post(
            f"/api/v1/jobs/{job.id}/cancel",
            json={"reason": "user_requested"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "j-c"),
        )
        assert cancelled.status_code == 202
        job.state = "failed"
        retried = await client.post(
            f"/api/v1/jobs/{job.id}/retry",
            json={"reason": "user_requested"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "j-r"),
        )
        assert retried.status_code == 202
        assert harness["enqueuer"].records[-1].payload.get("retry_of") == str(job.id)

        harness["event_log"].events[job.id] = [
            {"seq": 1, "kind": "progress", "payload": {"stage": "x"}},
        ]
        harness["event_log"].active[job.id] = False
        events = await client.get(
            f"/api/v1/jobs/{job.id}/events",
            cookies={"intel_session": cookie},
        )
        assert events.status_code == 200
        assert "text/event-stream" in events.headers["content-type"]

        harness["event_log"].earliest[job.id] = 5
        expired = await client.get(
            f"/api/v1/jobs/{job.id}/events",
            cookies={"intel_session": cookie},
            headers={"Last-Event-ID": "1"},
        )
        assert expired.status_code == 409
        assert error_code(expired) == "event_cursor_expired"

    async def test_documents_and_generation_viewer(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        listed = await client.get(
            f"/api/v1/industries/{industry_id}/documents",
            cookies={"intel_session": cookie},
        )
        assert listed.status_code == 200
        imported = await client.post(
            f"/api/v1/industries/{industry_id}/documents/import-url",
            json={"url": "https://example.com/doc"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "d-1"),
        )
        assert imported.status_code == 202
        run_id = uuid4()
        viewer = await client.get(
            f"/api/v1/industries/{industry_id}/generation-runs/{run_id}/viewer-link",
            cookies={"intel_session": cookie},
        )
        assert viewer.status_code == 200
        body = viewer.json()
        assert body["available"] is False
        assert body.get("url") in (None,)
