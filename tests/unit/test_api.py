"""Unit tests: HTTP API surface via httpx ASGI transport, fake repos (no DB).

Spec references:
- docs/08-api.md §1  session cookie + CSRF、404 统一（不存在与无权访问）、
  Idempotency-Key 存储重放/冲突、expected_version 409
- docs/08-api.md §2  /auth/*、/industries*、/topics*、/source-templates、
  /feeds*、/industries/{id}/sources、/health/live
- docs/08-api.md §6  ErrorEnvelope 稳定错误码 + request_id

Dependency overrides replace the SQLAlchemy repositories with dict-backed
fakes; the real IdentityService, WorkspaceService, SourcesService, routing,
error envelope, CSRF, and idempotency machinery all run for real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import Depends

from intel.api.app import create_app
from intel.api.deps import (
    get_enqueuer,
    get_idempotency_repo,
    get_identity_repo,
    get_industry_sources_repo,
    get_industry_workspace_repo,
    get_principal,
    get_sources_repo,
    get_workspace_repo,
)
from intel.repositories.idempotency import IdempotencyRecord
from intel.repositories.sources import (
    FeedRecord,
    IndustryContext,
    SourceTemplateRecord,
    SubscriptionRecord,
)
from intel.repositories.workspace import IndustryRecord
from intel.services.acquisition import InMemoryEnqueuer
from intel.services.identity import IdentityService, Principal

PEPPER = "api-test-pepper"
PASSWORD = "correct horse battery"
ALICE = uuid4()
BOB = uuid4()
INDUSTRY_DEFAULTS = 90  # days; must match IndustrySettings default


def fast_hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=1, memory_cost=64, parallelism=1, salt_len=8, hash_len=16
    )


class FakeIdentityRepo:
    """users + auth_sessions, dict-backed (hashes only, as the real one)."""

    def __init__(self) -> None:
        self.users_by_login: dict[str, dict] = {}
        self.users_by_id: dict[UUID, dict] = {}
        self.sessions_by_token: dict[str, dict] = {}

    async def get_user_by_login(self, login: str):
        return self.users_by_login.get(login)

    async def get_user(self, user_id: UUID):
        return self.users_by_id.get(user_id)

    async def create_user(self, login: str, password_hash: str, timezone: str):
        # Only the test helper seeds accounts this way; no HTTP route calls
        # it (accounts are CLI-only per 01 §5).
        from intel.services.identity import UserRecord

        record = UserRecord(
            id=uuid4(),
            login=login,
            password_hash=password_hash,
            timezone=timezone,
            disabled_at=None,
            password_version=1,
        )
        self.users_by_login[login] = record
        self.users_by_id[record.id] = record
        return record

    async def update_password(self, user_id, password_hash, password_version):
        raise AssertionError("not used over HTTP")

    async def insert_session(self, record) -> None:
        self.sessions_by_token[record.token_hash] = record

    async def get_session_by_token_hash(self, token_hash: str):
        return self.sessions_by_token.get(token_hash)

    async def revoke_session(self, session_id: UUID, revoked_at) -> None:
        for session in self.sessions_by_token.values():
            if session.id == session_id and session.revoked_at is None:
                session.revoked_at = revoked_at

    async def revoke_all_sessions(self, user_id: UUID, revoked_at) -> None:
        for session in self.sessions_by_token.values():
            if session.user_id == user_id and session.revoked_at is None:
                session.revoked_at = revoked_at


class BoundWorkspaceFake:
    """WorkspaceRepository bound to one owner over a shared store.

    Two instances over one store reproduce what RLS gives the SQL repo: each
    owner sees only its own rows, so a foreign industry_id resolves to 404.
    """

    def __init__(self, store: dict, owner_id: UUID) -> None:
        self.store = store
        self.owner_id = owner_id

    def _mine(self, industry_id: UUID) -> IndustryRecord | None:
        record = self.store["industries"].get(industry_id)
        if (
            record is None
            or record.owner_id != self.owner_id
            or record.deleted_at is not None
        ):
            return None
        return record

    async def industry_owned(self, industry_id: UUID) -> bool:
        return self._mine(industry_id) is not None

    async def get_industry(self, industry_id: UUID):
        return self._mine(industry_id)

    async def list_industries(self):
        return [
            r
            for r in self.store["industries"].values()
            if r.owner_id == self.owner_id and r.deleted_at is None
        ]

    async def industry_name_taken(self, name, *, exclude_id=None):
        return any(
            r.name == name
            and r.id != exclude_id
            and r.status != "archived"
            and r.owner_id == self.owner_id
            and r.deleted_at is None
            for r in self.store["industries"].values()
        )

    async def insert_industry(self, industry, revision):
        industry.owner_id = self.owner_id
        self.store["industries"][industry.id] = industry
        self.store["industry_revisions"][revision.id] = revision
        industry.current_revision_id = revision.id
        return industry

    async def update_industry(
        self, industry_id, expected_version, *, name=None, status=None
    ):
        record = self._mine(industry_id)
        if record is None or record.row_version != expected_version:
            return None
        if name is not None:
            record.name = name
        if status is not None:
            record.status = status
        record.row_version += 1
        return record

    async def latest_industry_revision(self, industry_id):
        revisions = [
            r
            for r in self.store["industry_revisions"].values()
            if r.industry_id == industry_id
        ]
        return max(revisions, key=lambda r: r.version) if revisions else None

    async def append_industry_revision(
        self, revision, industry_id, expected_version
    ):
        record = self._mine(industry_id)
        if record is None or record.row_version != expected_version:
            return None
        self.store["industry_revisions"][revision.id] = revision
        record.current_revision_id = revision.id
        return record  # no bump: update_industry already versioned this PATCH

    async def get_topic(self, industry_id, topic_id):
        record = self.store["topics"].get((industry_id, topic_id))
        if record is None or self._mine(industry_id) is None:
            return None
        return record

    async def list_topics(self, industry_id):
        if self._mine(industry_id) is None:
            return []
        return [
            t for (ind, _), t in self.store["topics"].items() if ind == industry_id
        ]

    async def topic_name_taken(self, industry_id, name, *, exclude_id=None):
        if self._mine(industry_id) is None:
            return False
        return any(
            t.name == name and t.id != exclude_id and t.status != "archived"
            for (ind, _), t in self.store["topics"].items()
            if ind == industry_id
        )

    async def insert_topic(self, topic, revision):
        self.store["topics"][(topic.industry_id, topic.id)] = topic
        self.store["topic_revisions"][revision.id] = revision
        topic.current_revision_id = revision.id
        return topic

    async def update_topic(
        self,
        industry_id,
        topic_id,
        expected_version,
        *,
        name=None,
        status=None,
        priority=None,
    ):
        record = await self.get_topic(industry_id, topic_id)
        if record is None or record.row_version != expected_version:
            return None
        if name is not None:
            record.name = name
        if status is not None:
            record.status = status
        if priority is not None:
            record.priority = priority
        record.row_version += 1
        return record

    async def latest_topic_revision(self, industry_id, topic_id):
        revisions = [
            r
            for r in self.store["topic_revisions"].values()
            if r.industry_id == industry_id and r.topic_id == topic_id
        ]
        return max(revisions, key=lambda r: r.version) if revisions else None

    async def append_topic_revision(
        self, revision, industry_id, topic_id, expected_version
    ):
        record = await self.get_topic(industry_id, topic_id)
        if record is None or record.row_version != expected_version:
            return None
        self.store["topic_revisions"][revision.id] = revision
        record.current_revision_id = revision.id
        return record  # no bump: update_topic already versioned this PATCH


class FakeSourcesRepo:
    def __init__(self, owner_id: UUID) -> None:
        self.owner_id = owner_id
        self.feeds: dict[UUID, FeedRecord] = {}
        self.subscriptions: dict[UUID, SubscriptionRecord] = {}
        self.industry_contexts: dict[UUID, IndustryContext] = {}
        parser = uuid4()
        self.templates = [
            SourceTemplateRecord(
                id="tpl-a",
                name="Feed A",
                homepage="https://a.example",
                canonical_seed="https://a.example/rss",
                kind="rss",
                tags=[],
                access_notes="",
            )
        ]
        self.parsers = {"rss": parser}

    async def list_templates(self):
        return list(self.templates)

    async def get_template(self, template_id):
        return next((t for t in self.templates if t.id == template_id), None)

    async def latest_published_parser_version(self, parser_key):
        return self.parsers.get(parser_key)

    async def insert_feed(self, feed):
        self.feeds[feed.id] = feed

    async def get_feed(self, feed_id):
        return self.feeds.get(feed_id)

    async def list_feeds(self):
        return list(self.feeds.values())

    async def feed_seed_taken(self, seed_url, access_scope_key):
        return any(
            f.seed_url == seed_url and f.access_scope_key == access_scope_key
            for f in self.feeds.values()
        )

    async def update_feed(
        self,
        feed_id,
        expected_version,
        *,
        interval_seconds=None,
        user_enabled=None,
        parser_version_id=None,
        status=None,
    ):
        record = self.feeds.get(feed_id)
        if record is None or record.row_version != expected_version:
            return None
        if interval_seconds is not None:
            record.interval_seconds = interval_seconds
        if user_enabled is not None:
            record.user_enabled = user_enabled
        if parser_version_id is not None:
            record.parser_version_id = parser_version_id
        if status is not None:
            record.status = status
        record.row_version += 1
        return record

    async def get_industry_context(self, industry_id):
        return self.industry_contexts.get(industry_id)

    async def insert_subscription(self, sub):
        self.subscriptions[sub.id] = sub

    async def get_subscription(self, industry_id, subscription_id):
        record = self.subscriptions.get(subscription_id)
        if record is None or record.industry_id != industry_id:
            return None
        return record

    async def list_subscriptions(self, industry_id):
        return [
            s
            for s in self.subscriptions.values()
            if s.industry_id == industry_id
        ]

    async def subscription_feed_taken(self, industry_id, feed_id):
        return any(
            s.industry_id == industry_id and s.feed_id == feed_id
            for s in self.subscriptions.values()
        )

    async def update_subscription(
        self, industry_id, subscription_id, expected_version, *, status
    ):
        record = await self.get_subscription(industry_id, subscription_id)
        if record is None or record.row_version != expected_version:
            return None
        record.status = status
        record.row_version += 1
        return record

    async def list_runs(self, feed_id):
        return []


class FakeIdempotencyRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, str], IdempotencyRecord] = {}

    async def get(self, owner_id, route, key):
        return self.rows.get((owner_id, route, key))

    async def put(self, record: IdempotencyRecord) -> None:
        self.rows[(record.owner_id, record.route, record.key)] = record


@pytest.fixture()
def harness():
    identity_repo = FakeIdentityRepo()
    workspace_store = {
        "industries": {},
        "industry_revisions": {},
        "topics": {},
        "topic_revisions": {},
    }
    sources_repo = FakeSourcesRepo(ALICE)
    idempotency_repo = FakeIdempotencyRepo()
    enqueuer = InMemoryEnqueuer()
    identity_service = IdentityService(
        session_pepper=PEPPER, hasher=fast_hasher()
    )

    app = create_app()
    app.state.identity_service = identity_service

    def _workspace_repo_for(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> BoundWorkspaceFake:
        # Bound per request — the same binding the SQL repo gets from its
        # IndustryScope, so foreign industries resolve to 404 for other users.
        return BoundWorkspaceFake(workspace_store, principal.user_id)

    app.dependency_overrides[get_identity_repo] = lambda: identity_repo
    app.dependency_overrides[get_workspace_repo] = _workspace_repo_for
    app.dependency_overrides[get_industry_workspace_repo] = _workspace_repo_for
    app.dependency_overrides[get_sources_repo] = lambda: sources_repo
    app.dependency_overrides[get_industry_sources_repo] = lambda: sources_repo
    app.dependency_overrides[get_idempotency_repo] = lambda: idempotency_repo
    app.dependency_overrides[get_enqueuer] = lambda: enqueuer

    return {
        "app": app,
        "identity_repo": identity_repo,
        "identity_service": identity_service,
        "workspace_store": workspace_store,
        "sources_repo": sources_repo,
        "idempotency_repo": idempotency_repo,
        "enqueuer": enqueuer,
        "bound_bob": BoundWorkspaceFake(workspace_store, BOB),
    }


@pytest.fixture()
async def client(harness) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=harness["app"])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


async def add_user(harness, user_id: UUID, login: str) -> None:
    """Seed an account the way the ops CLI would (never over HTTP)."""
    service = harness["identity_service"]
    repo = harness["identity_repo"]
    await service.create_user(repo, login, PASSWORD)
    record = repo.users_by_login[login]
    del repo.users_by_id[record.id]
    record.id = user_id  # pin the id the workspace fake is bound to
    repo.users_by_id[user_id] = record


async def login(
    client: httpx.AsyncClient, login_name: str
) -> tuple[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login": login_name, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    jar = SimpleCookie()
    jar.load(resp.headers["set-cookie"])
    morsel = jar["intel_session"]
    assert morsel.value
    return morsel.value, resp.json()["csrf_token"]


def auth_headers(csrf: str, idempotency_key: str | None = None) -> dict:
    headers = {"X-CSRF-Token": csrf}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def create_industry(
    client: httpx.AsyncClient,
    cookie: str,
    csrf: str,
    *,
    name: str = "Etching",
    key: str | None = "create-1",
) -> httpx.Response:
    return await client.post(
        "/api/v1/industries",
        json={"name": name, "description": "范围"},
        cookies={"intel_session": cookie},
        headers=auth_headers(csrf, key),
    )


def error_code(resp: httpx.Response) -> str:
    body = resp.json()
    assert set(body) == {"error"}, body
    assert body["error"]["request_id"]
    return body["error"]["code"]


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


class TestHealth:
    async def test_live_under_api_prefix(self, client) -> None:
        resp = await client.get("/api/v1/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_live_at_root_for_probes(self, client) -> None:
        resp = await client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# auth (08 §1: cookie/CSRF/login rotation; 10 §2: rate limiting)
# --------------------------------------------------------------------------


class TestAuth:
    async def test_login_sets_cookie_flags_and_returns_csrf(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        _cookie, csrf = await login(client, "alice")
        assert csrf

    async def test_cookie_flags(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        resp = await client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )
        jar = SimpleCookie()
        jar.load(resp.headers["set-cookie"])
        morsel = jar["intel_session"]
        assert morsel["httponly"]
        assert morsel["secure"]
        assert morsel["samesite"].lower() == "lax"

    async def test_wrong_password_401_envelope(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        resp = await client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        assert error_code(resp) == "unauthenticated"

    async def test_cross_origin_login_refused(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        resp = await client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403
        assert error_code(resp) == "csrf_failed"

    async def test_me_requires_session(self, client) -> None:
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401
        assert error_code(resp) == "unauthenticated"

    async def test_me_returns_user(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, _ = await login(client, "alice")
        resp = await client.get(
            "/api/v1/auth/me", cookies={"intel_session": cookie}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["login"] == "alice"
        # The CSRF plaintext is delivered once at login; only hashes persist,
        # so /auth/me cannot resurrect it (csrf_token == "" here).
        assert body["csrf_token"] == ""

    async def test_logout_requires_csrf(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, _ = await login(client, "alice")
        resp = await client.post(
            "/api/v1/auth/logout", cookies={"intel_session": cookie}
        )
        assert resp.status_code == 403
        assert error_code(resp) == "csrf_failed"

    async def test_logout_revokes_session(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        resp = await client.post(
            "/api/v1/auth/logout",
            cookies={"intel_session": cookie},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        me = await client.get(
            "/api/v1/auth/me", cookies={"intel_session": cookie}
        )
        assert me.status_code == 401


# --------------------------------------------------------------------------
# CSRF on command routes (08 §1)
# --------------------------------------------------------------------------


class TestCsrf:
    async def test_post_without_csrf_token_403(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, _ = await login(client, "alice")
        resp = await client.post(
            "/api/v1/industries",
            json={"name": "X", "description": "d"},
            cookies={"intel_session": cookie},
            headers={"Idempotency-Key": "k"},
        )
        assert resp.status_code == 403
        assert error_code(resp) == "csrf_failed"

    async def test_post_with_wrong_csrf_token_403(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, _ = await login(client, "alice")
        resp = await client.post(
            "/api/v1/industries",
            json={"name": "X", "description": "d"},
            cookies={"intel_session": cookie},
            headers={"X-CSRF-Token": "forged", "Idempotency-Key": "k"},
        )
        assert resp.status_code == 403
        assert error_code(resp) == "csrf_failed"


# --------------------------------------------------------------------------
# industries
# --------------------------------------------------------------------------


class TestIndustriesApi:
    async def test_create_get_list_roundtrip(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "draft"
        assert body["settings"]["pool_scope"] == "all_public"
        industry_id = body["id"]

        got = await client.get(
            f"/api/v1/industries/{industry_id}",
            cookies={"intel_session": cookie},
        )
        assert got.status_code == 200
        assert got.json() == body

        listed = await client.get(
            "/api/v1/industries", cookies={"intel_session": cookie}
        )
        assert listed.status_code == 200
        page = listed.json()
        assert [item["id"] for item in page["items"]] == [industry_id]
        assert page["next_cursor"] is None

    async def test_create_missing_idempotency_key_422(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        resp = await create_industry(client, cookie, csrf, key=None)
        assert resp.status_code == 422
        assert error_code(resp) == "validation_error"

    async def test_create_invalid_body_422(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        resp = await client.post(
            "/api/v1/industries",
            json={"description": "missing name"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "k"),
        )
        assert resp.status_code == 422
        assert error_code(resp) == "validation_error"

    async def test_foreign_industry_404_not_403(
        self, harness, client
    ) -> None:
        # U04 个人隔离: B 猜 A 的 industry_id → 404 统一，绝不 403 泄露存在性。
        await add_user(harness, ALICE, "alice")
        await add_user(harness, BOB, "bob")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]

        bob_cookie, _bob_csrf = await login(client, "bob")
        resp = await client.get(
            f"/api/v1/industries/{industry_id}",
            cookies={"intel_session": bob_cookie},
        )
        assert resp.status_code == 404
        assert error_code(resp) == "not_found"

        # And every nested route under the same foreign industry 404s too.
        topics = await client.get(
            f"/api/v1/industries/{industry_id}/topics",
            cookies={"intel_session": bob_cookie},
        )
        assert topics.status_code == 404
        assert error_code(topics) == "not_found"

    async def test_version_conflict_409_with_current(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/industries/{industry_id}",
            json={"expected_version": 99, "description": "stale"},
            cookies={"intel_session": cookie},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 409
        assert error_code(resp) == "version_conflict"
        assert resp.json()["error"]["details"]["current_version"] == 1

    async def test_patch_creates_revision(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        body = created.json()
        patched = await client.patch(
            f"/api/v1/industries/{body['id']}",
            json={"expected_version": 1, "description": "新描述"},
            cookies={"intel_session": cookie},
            headers={"X-CSRF-Token": csrf},
        )
        assert patched.status_code == 200
        new_body = patched.json()
        assert new_body["description"] == "新描述"
        assert new_body["revision_id"] != body["revision_id"]
        assert new_body["row_version"] == 2

    async def test_lifecycle_activate_then_illegal_reactivate(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        created = await create_industry(client, cookie, csrf)
        industry_id = created.json()["id"]

        activated = await client.post(
            f"/api/v1/industries/{industry_id}/lifecycle",
            json={"expected_version": 1, "action": "activate"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "lc-1"),
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"

        again = await client.post(
            f"/api/v1/industries/{industry_id}/lifecycle",
            json={"expected_version": 2, "action": "activate"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "lc-2"),
        )
        assert again.status_code == 409
        assert error_code(again) == "invalid_state_transition"


# --------------------------------------------------------------------------
# idempotency (08 §1: store-and-replay; 同键不同内容 409)
# --------------------------------------------------------------------------


class TestIdempotency:
    async def test_same_key_same_body_replays_first_response(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        first = await create_industry(client, cookie, csrf, key="idem-1")
        assert first.status_code == 201
        second = await create_industry(client, cookie, csrf, key="idem-1")
        assert second.status_code == 201
        assert second.json() == first.json()
        assert second.headers.get("Idempotency-Replayed") == "true"
        # Only one industry row exists.
        listed = await client.get(
            "/api/v1/industries", cookies={"intel_session": cookie}
        )
        assert len(listed.json()["items"]) == 1

    async def test_same_key_different_body_409(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        first = await create_industry(client, cookie, csrf, key="idem-2")
        assert first.status_code == 201
        resp = await client.post(
            "/api/v1/industries",
            json={"name": "Different", "description": "other body"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "idem-2"),
        )
        assert resp.status_code == 409
        assert error_code(resp) == "idempotency_conflict"


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------


class TestTopicsApi:
    async def test_create_and_patch_topic(self, harness, client) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        industry_id = (await create_industry(client, cookie, csrf)).json()["id"]

        created = await client.post(
            f"/api/v1/industries/{industry_id}/topics",
            json={"name": "腔体", "description": "腔体监控"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "topic-1"),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "active"

        patched = await client.patch(
            f"/api/v1/industries/{industry_id}/topics/{body['id']}",
            json={"expected_version": 1, "description": "扩展范围"},
            cookies={"intel_session": cookie},
            headers={"X-CSRF-Token": csrf},
        )
        assert patched.status_code == 200
        assert patched.json()["revision_id"] != body["revision_id"]

    async def test_replay_returns_202_with_job(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        industry_id = (await create_industry(client, cookie, csrf)).json()["id"]
        topic_id = (
            await client.post(
                f"/api/v1/industries/{industry_id}/topics",
                json={"name": "t", "description": "d"},
                cookies={"intel_session": cookie},
                headers=auth_headers(csrf, "topic-2"),
            )
        ).json()["id"]

        resp = await client.post(
            f"/api/v1/industries/{industry_id}/topics/{topic_id}/replay",
            json={},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "replay-1"),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["state"] == "queued"
        assert body["events_url"].endswith(f"/jobs/{body['job_id']}/events")
        (record,) = harness["enqueuer"].records
        assert record.kind == "topic_replay"


# --------------------------------------------------------------------------
# feeds + subscriptions + templates
# --------------------------------------------------------------------------


class TestFeedsApi:
    async def test_create_feed_poll_and_list_templates(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")

        templates = await client.get(
            "/api/v1/source-templates", cookies={"intel_session": cookie}
        )
        assert templates.status_code == 200
        assert templates.json()["items"][0]["id"] == "tpl-a"

        created = await client.post(
            "/api/v1/feeds",
            json={
                "template_id": "tpl-a",
                "seed_url": "https://a.example/rss",
                "adapter_type": "rss",
            },
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "feed-1"),
        )
        assert created.status_code == 201, created.text
        feed_id = created.json()["id"]

        polled = await client.post(
            f"/api/v1/feeds/{feed_id}/poll",
            json={"mode": "trial"},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "poll-1"),
        )
        assert polled.status_code == 202, polled.text
        assert polled.json()["state"] == "queued"
        (record,) = harness["enqueuer"].records
        assert record.kind == "source_poll"
        assert record.payload["feed_id"] == feed_id

        runs = await client.get(
            f"/api/v1/feeds/{feed_id}/runs",
            cookies={"intel_session": cookie},
        )
        assert runs.status_code == 200
        assert runs.json() == {"items": [], "next_cursor": None}


class TestSubscriptionsApi:
    async def test_subscribe_with_default_backfill(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        industry_id = (await create_industry(client, cookie, csrf)).json()["id"]
        harness["sources_repo"].industry_contexts[UUID(industry_id)] = (
            IndustryContext(status="draft", backfill_days=INDUSTRY_DEFAULTS)
        )
        feed_id = (
            await client.post(
                "/api/v1/feeds",
                json={
                    "seed_url": "https://a.example/rss",
                    "adapter_type": "rss",
                },
                cookies={"intel_session": cookie},
                headers=auth_headers(csrf, "feed-2"),
            )
        ).json()["id"]

        created = await client.post(
            f"/api/v1/industries/{industry_id}/sources",
            json={"feed_id": feed_id},
            cookies={"intel_session": cookie},
            headers=auth_headers(csrf, "sub-1"),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "active"
        expected = datetime.now(UTC) - timedelta(days=INDUSTRY_DEFAULTS)
        actual = datetime.fromisoformat(body["backfill_from"])
        assert abs((actual - expected).total_seconds()) < 60

        listed = await client.get(
            f"/api/v1/industries/{industry_id}/sources",
            cookies={"intel_session": cookie},
        )
        assert listed.status_code == 200
        assert [item["feed_id"] for item in listed.json()["items"]] == [feed_id]

    async def test_patch_subscription_pause(
        self, harness, client
    ) -> None:
        await add_user(harness, ALICE, "alice")
        cookie, csrf = await login(client, "alice")
        industry_id = (await create_industry(client, cookie, csrf)).json()["id"]
        harness["sources_repo"].industry_contexts[UUID(industry_id)] = (
            IndustryContext(status="active", backfill_days=90)
        )
        feed_id = (
            await client.post(
                "/api/v1/feeds",
                json={
                    "seed_url": "https://a.example/rss",
                    "adapter_type": "rss",
                },
                cookies={"intel_session": cookie},
                headers=auth_headers(csrf, "feed-3"),
            )
        ).json()["id"]
        sub = (
            await client.post(
                f"/api/v1/industries/{industry_id}/sources",
                json={"feed_id": feed_id},
                cookies={"intel_session": cookie},
                headers=auth_headers(csrf, "sub-2"),
            )
        ).json()

        paused = await client.patch(
            f"/api/v1/industries/{industry_id}/sources/{sub['id']}",
            json={"expected_version": 1, "status": "paused"},
            cookies={"intel_session": cookie},
            headers={"X-CSRF-Token": csrf},
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
