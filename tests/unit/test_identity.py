"""Unit tests: identity service + account CLI (spec 10 §2, 08 §1).

Spec references:
- docs/10-operations.md §2  账号 CLI 建立、Argon2id、登录限速、防会话固定、
  注销吊销、CLI 重置密码并使 session 失效
- docs/08-api.md §1  opaque session cookie、token 只存 hash、登录轮换

No real database here: the service runs against a dict-backed fake
repository implementing IdentityRepository. The CLI is exercised only
through argument parsing and dispatch seams (--help stays offline; the
command runner is mocked — engine/DB wiring is an integration concern).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher

from intel import cli
from intel.services.identity import (
    IdentityRepository,
    IdentityService,
    InvalidLogin,
    LoginRateLimited,
    LoginTaken,
    Principal,
    SessionRecord,
    SessionTokens,
    SqlAlchemyIdentityRepository,
    UnknownLogin,
    UserRecord,
    WeakPassword,
)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
PEPPER = "unit-test-pepper"
PASSWORD = "correct horse battery"
MIN_LEN_EXP = 43  # 32 random bytes -> >=43 unpadded base64url chars


def fast_hasher() -> PasswordHasher:
    """Argon2id with toy parameters: same hash/verify code path, no 64 MiB cost."""
    return PasswordHasher(
        time_cost=1, memory_cost=64, parallelism=1, salt_len=8, hash_len=16
    )


class FakeClock:
    """Mutable clock so expiry / rate-limit windows can be advanced in tests."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class FakeRepo:
    """Dict-backed IdentityRepository — the whole observable storage state."""

    def __init__(self) -> None:
        self.users_by_login: dict[str, UserRecord] = {}
        self.users_by_id: dict[UUID, UserRecord] = {}
        self.sessions_by_id: dict[UUID, SessionRecord] = {}
        self.sessions_by_token: dict[str, SessionRecord] = {}

    async def get_user_by_login(self, login: str) -> UserRecord | None:
        return self.users_by_login.get(login)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        return self.users_by_id.get(user_id)

    async def create_user(
        self, login: str, password_hash: str, timezone: str = "UTC"
    ) -> UserRecord:
        if login in self.users_by_login:
            raise AssertionError("service must pre-check duplicate logins")
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

    async def update_password(
        self, user_id: UUID, password_hash: str, password_version: int
    ) -> None:
        user = self.users_by_id[user_id]
        user.password_hash = password_hash
        user.password_version = password_version

    async def insert_session(self, record: SessionRecord) -> None:
        if record.token_hash in self.sessions_by_token:
            raise AssertionError("token_hash collision")
        self.sessions_by_id[record.id] = record
        self.sessions_by_token[record.token_hash] = record

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        return self.sessions_by_token.get(token_hash)

    async def revoke_session(self, session_id: UUID, revoked_at: datetime) -> None:
        session = self.sessions_by_id[session_id]
        if session.revoked_at is None:
            session.revoked_at = revoked_at

    async def revoke_all_sessions(self, user_id: UUID, revoked_at: datetime) -> None:
        for session in self.sessions_by_id.values():
            if session.user_id == user_id and session.revoked_at is None:
                session.revoked_at = revoked_at


def peppered(token: str) -> str:
    return hashlib.sha256((PEPPER + token).encode("utf-8")).hexdigest()


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture()
def service(clock: FakeClock) -> IdentityService:
    return IdentityService(session_pepper=PEPPER, hasher=fast_hasher(), clock=clock)


async def make_logged_in(
    service: IdentityService,
    repo: FakeRepo,
    login: str = "alice",
    password: str = PASSWORD,
) -> tuple[UserRecord, Principal, SessionTokens]:
    await service.create_user(repo, login, password)
    user = repo.users_by_login[login]
    principal = await service.authenticate(repo, login, password)
    assert principal is not None
    tokens = await service.issue_session(repo, principal)
    return user, principal, tokens


# --------------------------------------------------------------------------
# users, hashing, authenticate
# --------------------------------------------------------------------------


class TestPasswords:
    async def test_create_user_stores_argon2id_hash_and_returns_view(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        view = await service.create_user(repo, "alice", PASSWORD)
        assert view.login == "alice"
        assert view.timezone == "UTC"
        assert isinstance(view.id, UUID)
        stored = repo.users_by_login["alice"].password_hash
        assert stored.startswith("$argon2id$")
        assert PASSWORD not in stored

    async def test_authenticate_roundtrip_returns_principal_without_session(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        user = await service.create_user(repo, "alice", PASSWORD)
        principal = await service.authenticate(repo, "alice", PASSWORD)
        assert principal == Principal(user_id=user.id)
        assert principal.session_id is None

    async def test_wrong_password_returns_none(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        assert await service.authenticate(repo, "alice", "wrong-password") is None

    async def test_unknown_login_returns_none(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        assert await service.authenticate(repo, "ghost", PASSWORD) is None

    async def test_duplicate_login_rejected(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        with pytest.raises(LoginTaken):
            await service.create_user(repo, "alice", "other-password")

    @pytest.mark.parametrize("password", ["", "short", "123456789"])
    async def test_short_password_rejected_on_create_and_reset(
        self, service: IdentityService, repo: FakeRepo, password: str
    ) -> None:
        with pytest.raises(WeakPassword):
            await service.create_user(repo, "alice", password)
        await service.create_user(repo, "alice", PASSWORD)
        with pytest.raises(WeakPassword):
            await service.reset_password(repo, "alice", password)

    @pytest.mark.parametrize("login", ["", " ", "has space", "x" * 65])
    async def test_invalid_login_rejected(
        self, service: IdentityService, repo: FakeRepo, login: str
    ) -> None:
        with pytest.raises(InvalidLogin):
            await service.create_user(repo, login, PASSWORD)

    async def test_custom_timezone_round_trips(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        view = await service.create_user(repo, "alice", PASSWORD, timezone="Asia/Shanghai")
        assert view.timezone == "Asia/Shanghai"


# --------------------------------------------------------------------------
# login rate limiting (登录限速)
# --------------------------------------------------------------------------


class TestRateLimit:
    async def test_locks_after_five_failures_within_window(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        for _ in range(5):
            assert await service.authenticate(repo, "alice", "wrong-password") is None
        # 6th attempt is refused outright — even with the correct password.
        with pytest.raises(LoginRateLimited):
            await service.authenticate(repo, "alice", PASSWORD)

    async def test_success_resets_the_counter(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        for _ in range(4):
            assert await service.authenticate(repo, "alice", "wrong-password") is None
        assert await service.authenticate(repo, "alice", PASSWORD) is not None
        # 4 more failures after the reset still leave headroom.
        for _ in range(4):
            assert await service.authenticate(repo, "alice", "wrong-password") is None
        assert await service.authenticate(repo, "alice", PASSWORD) is not None

    async def test_window_expiry_clears_the_lock(
        self, service: IdentityService, repo: FakeRepo, clock: FakeClock
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        for _ in range(5):
            await service.authenticate(repo, "alice", "wrong-password")
        with pytest.raises(LoginRateLimited):
            await service.authenticate(repo, "alice", PASSWORD)
        clock.advance(timedelta(minutes=15, seconds=1))
        assert await service.authenticate(repo, "alice", PASSWORD) is not None

    async def test_retry_after_points_past_the_oldest_failure(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        for _ in range(5):
            await service.authenticate(repo, "alice", "wrong-password")
        with pytest.raises(LoginRateLimited) as exc_info:
            await service.authenticate(repo, "alice", PASSWORD)
        assert exc_info.value.retry_after == T0 + timedelta(minutes=15)

    async def test_failures_are_tracked_per_login(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await service.create_user(repo, "alice", PASSWORD)
        await service.create_user(repo, "bob", PASSWORD)
        for _ in range(5):
            await service.authenticate(repo, "alice", "wrong-password")
        with pytest.raises(LoginRateLimited):
            await service.authenticate(repo, "alice", PASSWORD)
        assert await service.authenticate(repo, "bob", PASSWORD) is not None


# --------------------------------------------------------------------------
# session issue / validate (token 只存 hash、登录轮换、注销吊销)
# --------------------------------------------------------------------------


class TestSessions:
    async def test_issue_session_stores_only_peppered_hashes(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        _, _, tokens = await make_logged_in(service, repo)
        record = repo.sessions_by_id[tokens.session_id]
        assert record.token_hash == peppered(tokens.cookie_value)
        assert record.csrf_hash == peppered(tokens.csrf_token)
        # The public hasher is the one request handlers must use for the
        # CSRF-header check (spec 08 §1): hash_token(header) == csrf_hash.
        assert service.hash_token(tokens.csrf_token) == record.csrf_hash
        # Hashes are sha256 hex; plaintext secrets appear nowhere in storage.
        for stored in (record.token_hash, record.csrf_hash):
            assert len(stored) == 64
            int(stored, 16)
        for secret in (tokens.cookie_value, tokens.csrf_token):
            assert secret not in (record.token_hash, record.csrf_hash)

    async def test_cookie_and_csrf_differ_and_carry_32_bytes(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        _, _, tokens = await make_logged_in(service, repo)
        assert tokens.cookie_value != tokens.csrf_token
        assert len(tokens.cookie_value) >= MIN_LEN_EXP
        assert len(tokens.csrf_token) >= MIN_LEN_EXP

    async def test_expiry_is_14_days(
        self, service: IdentityService, repo: FakeRepo, clock: FakeClock
    ) -> None:
        _, _, tokens = await make_logged_in(service, repo)
        assert tokens.expires_at == clock.now + timedelta(days=14)

    async def test_validate_roundtrip(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        user, _, tokens = await make_logged_in(service, repo)
        principal = await service.validate_session(repo, tokens.cookie_value)
        assert principal == Principal(user_id=user.id, session_id=tokens.session_id)

    async def test_unknown_cookie_returns_none(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        await make_logged_in(service, repo)
        assert await service.validate_session(repo, "no-such-cookie") is None

    async def test_expired_session_rejected(
        self, service: IdentityService, repo: FakeRepo, clock: FakeClock
    ) -> None:
        _, _, tokens = await make_logged_in(service, repo)
        clock.advance(timedelta(days=14, seconds=1))
        assert await service.validate_session(repo, tokens.cookie_value) is None

    async def test_logout_revokes_session_and_is_idempotent(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        _, _, tokens = await make_logged_in(service, repo)
        principal = await service.validate_session(repo, tokens.cookie_value)
        assert principal is not None
        await service.logout(repo, principal)
        assert await service.validate_session(repo, tokens.cookie_value) is None
        await service.logout(repo, principal)  # second logout must not raise

    async def test_logout_requires_a_session_bound_principal(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        user = await service.create_user(repo, "alice", PASSWORD)
        principal = await service.authenticate(repo, "alice", PASSWORD)
        assert principal is not None
        with pytest.raises(ValueError, match="session"):
            await service.logout(repo, Principal(user_id=user.id))

    async def test_password_version_bump_invalidates_session(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        user, _, tokens = await make_logged_in(service, repo)
        await repo.update_password(
            user.id, user.password_hash, user.password_version + 1
        )
        assert await service.validate_session(repo, tokens.cookie_value) is None

    async def test_disabled_user_rejected_in_both_paths(
        self, service: IdentityService, repo: FakeRepo, clock: FakeClock
    ) -> None:
        user, _, tokens = await make_logged_in(service, repo)
        user.disabled_at = clock.now
        assert await service.validate_session(repo, tokens.cookie_value) is None
        assert await service.authenticate(repo, "alice", PASSWORD) is None

    async def test_login_rotation_issues_independent_sessions(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        # 防会话固定: every login mints a fresh random token; earlier
        # sessions keep working until explicitly revoked or expired.
        user, _, first = await make_logged_in(service, repo)
        principal = await service.authenticate(repo, "alice", PASSWORD)
        assert principal is not None
        second = await service.issue_session(repo, principal)
        assert second.cookie_value != first.cookie_value
        assert second.csrf_token != first.csrf_token
        assert await service.validate_session(repo, first.cookie_value) is not None
        assert await service.validate_session(repo, second.cookie_value) is not None
        assert user.id == (await service.validate_session(repo, second.cookie_value)).user_id

    async def test_pepper_is_binding(self, repo: FakeRepo, clock: FakeClock) -> None:
        service = IdentityService(
            session_pepper=PEPPER, hasher=fast_hasher(), clock=clock
        )
        _, _, tokens = await make_logged_in(service, repo)
        other = IdentityService(
            session_pepper="a-different-pepper", hasher=fast_hasher(), clock=clock
        )
        assert await other.validate_session(repo, tokens.cookie_value) is None


# --------------------------------------------------------------------------
# reset_password (CLI 重置密码并使 session 失效)
# --------------------------------------------------------------------------


class TestResetPassword:
    async def test_reset_invalidates_sessions_and_old_password(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        user, _, tokens = await make_logged_in(service, repo)
        await service.reset_password(repo, "alice", "new-password-123")
        assert await service.validate_session(repo, tokens.cookie_value) is None
        assert await service.authenticate(repo, "alice", "new-password-123") is not None
        assert await service.authenticate(repo, "alice", PASSWORD) is None
        assert repo.users_by_id[user.id].password_version == 2

    async def test_reset_unknown_login_raises(
        self, service: IdentityService, repo: FakeRepo
    ) -> None:
        with pytest.raises(UnknownLogin):
            await service.reset_password(repo, "ghost", "new-password-123")


# --------------------------------------------------------------------------
# protocol wiring
# --------------------------------------------------------------------------


def test_repositories_satisfy_the_protocol() -> None:
    assert isinstance(FakeRepo(), IdentityRepository)
    assert issubclass(SqlAlchemyIdentityRepository, IdentityRepository)


# --------------------------------------------------------------------------
# CLI (argument parsing + dispatch seams only; no engine, no DB)
# --------------------------------------------------------------------------


class TestCli:
    def test_help_works_offline(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "create-user" in out
        assert "reset-password" in out

    def test_missing_command_exits_with_usage_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli.main([])
        assert exc_info.value.code == 2
        assert "command" in capsys.readouterr().err

    def test_create_user_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_prompt_password", lambda: "long-enough-pass")
        run_calls: list[tuple[str, str, str]] = []

        async def fake_run(command: str, login: str, password: str) -> int:
            run_calls.append((command, login, password))
            return 0

        monkeypatch.setattr(cli, "_run_command", fake_run)
        assert cli.main(["create-user", "alice"]) == 0
        assert run_calls == [("create-user", "alice", "long-enough-pass")]

    def test_reset_password_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_prompt_password", lambda: "long-enough-pass")
        run_calls: list[tuple[str, str, str]] = []

        async def fake_run(command: str, login: str, password: str) -> int:
            run_calls.append((command, login, password))
            return 0

        monkeypatch.setattr(cli, "_run_command", fake_run)
        assert cli.main(["reset-password", "bob"]) == 0
        assert run_calls == [("reset-password", "bob", "long-enough-pass")]

    def test_password_mismatch_never_reaches_the_database(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        entries = iter(["first-pass-123", "second-pass-456"])
        monkeypatch.setattr(cli, "getpass", lambda prompt="": next(entries))

        async def boom(*args: object, **kwargs: object) -> int:
            raise AssertionError("must not open an engine for a bad prompt")

        monkeypatch.setattr(cli, "_run_command", boom)
        assert cli.main(["create-user", "alice"]) == 1
        assert "do not match" in capsys.readouterr().err

    @pytest.mark.parametrize("password", ["short", ""])
    def test_short_password_rejected_before_any_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        password: str,
    ) -> None:
        monkeypatch.setattr(cli, "getpass", lambda prompt="": password)

        async def boom(*args: object, **kwargs: object) -> int:
            raise AssertionError("must not open an engine for a bad prompt")

        monkeypatch.setattr(cli, "_run_command", boom)
        assert cli.main(["reset-password", "alice"]) == 1
        assert "at least" in capsys.readouterr().err
