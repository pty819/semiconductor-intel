"""Identity: accounts, Argon2id passwords, opaque session tokens.

Implements docs/10-operations.md §2 (账号 CLI 建立、Argon2id 密码 hash、登录
限速、防会话固定、注销吊销、CLI 重置密码并使 session 失效) and the session
half of docs/08-api.md §1 (opaque cookie、token 只存 hash、登录轮换). The HTTP
routes that wrap this service land in Task 5.

Layout of this module:

- ``Principal`` / ``SessionTokens`` / ``UserRecord`` / ``SessionRecord`` —
  the data types crossing the service boundary.
- ``IdentityRepository`` — the minimal async storage protocol the service
  needs. Unit tests drive it with a dict-backed fake; production uses
  ``SqlAlchemyIdentityRepository`` (below) on the ``users``/``auth_sessions``
  tables, which carry no RLS because rows are reached only through this
  service (see db/models/auth.py).
- ``IdentityService`` — hashing, rate limiting, session lifecycle; knows
  nothing about SQL.
- ``SqlAlchemyIdentityRepository`` — connection-scoped protocol adapter used
  by the CLI today and by the Task 5 routes later.

Security properties (each covered by tests/unit/test_identity.py):

- Passwords are Argon2id hashes; plaintext never reaches storage.
- Session cookies/CSRF tokens are 32 random bytes; only
  sha256(pepper + token) is stored, so a leaked database cannot be replayed
  against the running service.
- Every login issues fresh tokens (防会话固定); logout and password resets
  revoke (注销吊销); a ``password_version`` bump invalidates every session
  issued before it.
- Failed logins are rate-limited per login: after 5 failures within 15
  minutes further attempts are refused with a clear error, even with the
  correct password. The counter lives in process memory — single-server
  deployment per spec 10 §4; see the class docstring for the caveat.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.contracts import UserView
from intel.db.models.auth import AuthSession, User
from intel.settings import Settings

MIN_PASSWORD_LENGTH = 10
MAX_LOGIN_LENGTH = 64
SESSION_TTL = timedelta(days=14)
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
# Entropy of cookie/CSRF token values, in random bytes (>=32 required).
TOKEN_BYTES = 32


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class LoginRateLimited(RuntimeError):
    """Login refused: too many failures within the window (spec 10 §2)."""

    def __init__(self, login: str, retry_after: datetime) -> None:
        super().__init__(
            f"too many failed logins for {login!r}; "
            f"retry after {retry_after.isoformat()}"
        )
        self.login = login
        self.retry_after = retry_after


class LoginTaken(ValueError):
    """create_user: the login already exists."""


class UnknownLogin(ValueError):
    """reset_password: no such login."""


class WeakPassword(ValueError):
    """Password below MIN_PASSWORD_LENGTH."""

    def __init__(self) -> None:
        super().__init__(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


class InvalidLogin(ValueError):
    """Login name empty, too long, or containing whitespace."""


# --------------------------------------------------------------------------
# data types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated actor a request acts on.

    ``session_id`` is ``None`` until a session exists: ``authenticate``
    identifies the user (no session yet), ``issue_session`` mints tokens,
    and ``validate_session`` returns the session-bound principal that HTTP
    handlers and ``logout`` consume.
    """

    user_id: UUID
    session_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SessionTokens:
    """Freshly minted session secrets — plaintext, shown to the caller once."""

    cookie_value: str
    csrf_token: str
    expires_at: datetime
    session_id: UUID


@dataclass(slots=True)
class UserRecord:
    """Storage-facing snapshot of a ``users`` row."""

    id: UUID
    login: str
    password_hash: str
    timezone: str
    disabled_at: datetime | None
    password_version: int


@dataclass(slots=True)
class SessionRecord:
    """Storage-facing snapshot of an ``auth_sessions`` row (hashes only)."""

    id: UUID
    user_id: UUID
    token_hash: str
    csrf_hash: str
    expires_at: datetime
    password_version: int
    revoked_at: datetime | None


# --------------------------------------------------------------------------
# storage protocol
# --------------------------------------------------------------------------


@runtime_checkable
class IdentityRepository(Protocol):
    """Everything the identity service needs from storage.

    Implementations own transaction scope; the service never commits. Ids
    are application-generated (db/base.py convention).
    """

    async def get_user_by_login(self, login: str) -> UserRecord | None: ...

    async def get_user(self, user_id: UUID) -> UserRecord | None: ...

    async def create_user(
        self, login: str, password_hash: str, timezone: str
    ) -> UserRecord: ...

    async def update_password(
        self, user_id: UUID, password_hash: str, password_version: int
    ) -> None: ...

    async def insert_session(self, record: SessionRecord) -> None: ...

    async def get_session_by_token_hash(
        self, token_hash: str
    ) -> SessionRecord | None: ...

    async def revoke_session(self, session_id: UUID, revoked_at: datetime) -> None: ...

    async def revoke_all_sessions(
        self, user_id: UUID, revoked_at: datetime
    ) -> None: ...


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_login(login: str) -> None:
    if not login or len(login) > MAX_LOGIN_LENGTH or any(c.isspace() for c in login):
        raise InvalidLogin(
            f"login must be 1..{MAX_LOGIN_LENGTH} characters without whitespace"
        )


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword()


class IdentityService:
    """Account + session lifecycle, storage-agnostic.

    ``clock`` returns aware datetimes and is injectable for tests. ``hasher``
    is injectable so tests can run Argon2id with toy parameters without
    weakening production defaults. Login rate limiting is per-process memory
    — correct for the single-server deployment of spec 10 §4; a multi-worker
    or multi-node deployment would need the counter shared (out of scope).
    """

    def __init__(
        self,
        *,
        session_pepper: str,
        hasher: PasswordHasher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pepper = session_pepper
        self._hasher = hasher if hasher is not None else PasswordHasher()
        self._clock = clock if clock is not None else _utcnow
        self._failures: dict[str, list[datetime]] = {}
        self._dummy_hash: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> IdentityService:
        return cls(session_pepper=settings.session_pepper)

    def hash_token(self, token: str) -> str:
        """Hash a plaintext token the way storage does: sha256(pepper+token).

        Public because request handlers need it for the CSRF check (spec 08
        §1: X-CSRF-Token is compared against ``SessionRecord.csrf_hash`` via
        ``hash_token(header_value) == csrf_hash``) — never reimplemented.
        """
        return hashlib.sha256((self._pepper + token).encode("utf-8")).hexdigest()

    def _burn_verify(self, password: str) -> None:
        """Verify against a throwaway hash so unknown/disabled logins pay
        the same Argon2 cost as real ones (anti-enumeration timing)."""
        if self._dummy_hash is None:
            self._dummy_hash = self._hasher.hash("timing-equalizer-dummy")
        try:
            self._hasher.verify(self._dummy_hash, password)
        except (VerificationError, InvalidHash):
            pass

    def _prune_failures(self, login: str, now: datetime) -> list[datetime]:
        failures = [
            t for t in self._failures.get(login, ()) if now - t < LOGIN_FAILURE_WINDOW
        ]
        if failures:
            self._failures[login] = failures
        else:
            self._failures.pop(login, None)
        return failures

    # -- accounts -----------------------------------------------------------

    async def create_user(
        self,
        repo: IdentityRepository,
        login: str,
        password: str,
        timezone: str = "UTC",
    ) -> UserView:
        """Create an account. CSRF is empty: no session exists at creation
        time; /auth/me (Task 5) fills it from the caller's session."""
        _check_login(login)
        _check_password(password)
        if await repo.get_user_by_login(login) is not None:
            raise LoginTaken(f"login {login!r} already exists")
        record = await repo.create_user(
            login, self._hasher.hash(password), timezone
        )
        return UserView(
            id=record.id,
            login=record.login,
            timezone=record.timezone,
            csrf_token="",
        )

    async def authenticate(
        self, repo: IdentityRepository, login: str, password: str
    ) -> Principal | None:
        """Verify credentials; None on mismatch (never on rate limiting).

        After LOGIN_FAILURE_LIMIT failures within LOGIN_FAILURE_WINDOW the
        login is refused with LoginRateLimited before the password is even
        checked — wrong passwords cannot extend knowledge, and the correct
        password does not bypass the lock.
        """
        now = self._clock()
        failures = self._prune_failures(login, now)
        if len(failures) >= LOGIN_FAILURE_LIMIT:
            raise LoginRateLimited(login, failures[0] + LOGIN_FAILURE_WINDOW)

        user = await repo.get_user_by_login(login)
        if user is None or user.disabled_at is not None:
            self._burn_verify(password)
            self._failures.setdefault(login, []).append(now)
            return None
        try:
            self._hasher.verify(user.password_hash, password)
        except (VerificationError, InvalidHash):
            self._failures.setdefault(login, []).append(now)
            return None

        self._failures.pop(login, None)
        return Principal(user_id=user.id)

    async def reset_password(
        self, repo: IdentityRepository, login: str, new_password: str
    ) -> None:
        """Set a new password, bump password_version and revoke every session
        (CLI 重置密码并使 session 失效)."""
        _check_password(new_password)
        user = await repo.get_user_by_login(login)
        if user is None:
            raise UnknownLogin(f"no such login {login!r}")
        await repo.update_password(
            user.id, self._hasher.hash(new_password), user.password_version + 1
        )
        await repo.revoke_all_sessions(user.id, self._clock())

    # -- sessions -----------------------------------------------------------

    async def issue_session(
        self, repo: IdentityRepository, principal: Principal
    ) -> SessionTokens:
        """Mint fresh random cookie/CSRF tokens (防会话固定/登录轮换) and
        store only their peppered hashes. The plaintext values are returned
        exactly once, to the caller that will set the cookie."""
        user = await repo.get_user(principal.user_id)
        if user is None:
            raise UnknownLogin(f"no user {principal.user_id}")
        cookie = secrets.token_urlsafe(TOKEN_BYTES)
        csrf = secrets.token_urlsafe(TOKEN_BYTES)
        expires_at = self._clock() + SESSION_TTL
        record = SessionRecord(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=self.hash_token(cookie),
            csrf_hash=self.hash_token(csrf),
            expires_at=expires_at,
            password_version=user.password_version,
            revoked_at=None,
        )
        await repo.insert_session(record)
        return SessionTokens(
            cookie_value=cookie,
            csrf_token=csrf,
            expires_at=expires_at,
            session_id=record.id,
        )

    async def validate_session(
        self, repo: IdentityRepository, cookie_value: str
    ) -> Principal | None:
        """Resolve an opaque cookie to a Principal, or None.

        Rejects unknown cookies, revoked sessions (注销吊销), expiry, disabled
        users, and sessions whose password_version no longer matches the
        user's (stale after reset_password)."""
        session = await repo.get_session_by_token_hash(
            self.hash_token(cookie_value)
        )
        if session is None or session.revoked_at is not None:
            return None
        if self._clock() >= session.expires_at:
            return None
        user = await repo.get_user(session.user_id)
        if user is None or user.disabled_at is not None:
            return None
        if session.password_version != user.password_version:
            return None
        return Principal(user_id=user.id, session_id=session.id)

    async def logout(self, repo: IdentityRepository, principal: Principal) -> None:
        """Revoke the principal's session; idempotent."""
        if principal.session_id is None:
            raise ValueError("logout requires a session-bound principal")
        await repo.revoke_session(principal.session_id, self._clock())


# --------------------------------------------------------------------------
# SQLAlchemy adapter
# --------------------------------------------------------------------------


def _user_record(row: User) -> UserRecord:
    return UserRecord(
        id=row.id,
        login=row.login,
        password_hash=row.password_hash,
        timezone=row.timezone,
        disabled_at=row.disabled_at,
        password_version=row.password_version,
    )


def _session_record(row: AuthSession) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        token_hash=row.token_hash,
        csrf_hash=row.csrf_hash,
        expires_at=row.expires_at,
        password_version=row.password_version,
        revoked_at=row.revoked_at,
    )


class SqlAlchemyIdentityRepository:
    """IdentityRepository over ``users``/``auth_sessions`` on one connection.

    The connection's transaction scope is the unit of work — the caller
    (CLI today, HTTP dependency in Task 5) commits. All statements are
    parameterized; ids are application-generated.
    """

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get_user_by_login(self, login: str) -> UserRecord | None:
        row = (
            await self._conn.execute(select(User).where(User.login == login))
        ).scalars().one_or_none()
        return None if row is None else _user_record(row)

    async def get_user(self, user_id: UUID) -> UserRecord | None:
        row = (
            await self._conn.execute(select(User).where(User.id == user_id))
        ).scalars().one_or_none()
        return None if row is None else _user_record(row)

    async def create_user(
        self, login: str, password_hash: str, timezone: str
    ) -> UserRecord:
        user_id = uuid.uuid4()
        await self._conn.execute(
            insert(User).values(
                id=user_id,
                login=login,
                password_hash=password_hash,
                timezone=timezone,
                password_version=1,
            )
        )
        return UserRecord(
            id=user_id,
            login=login,
            password_hash=password_hash,
            timezone=timezone,
            disabled_at=None,
            password_version=1,
        )

    async def update_password(
        self, user_id: UUID, password_hash: str, password_version: int
    ) -> None:
        await self._conn.execute(
            update(User)
            .where(User.id == user_id)
            .values(password_hash=password_hash, password_version=password_version)
        )

    async def insert_session(self, record: SessionRecord) -> None:
        await self._conn.execute(
            insert(AuthSession).values(
                id=record.id,
                user_id=record.user_id,
                token_hash=record.token_hash,
                csrf_hash=record.csrf_hash,
                expires_at=record.expires_at,
                password_version=record.password_version,
            )
        )

    async def get_session_by_token_hash(
        self, token_hash: str
    ) -> SessionRecord | None:
        row = (
            await self._conn.execute(
                select(AuthSession).where(AuthSession.token_hash == token_hash)
            )
        ).scalars().one_or_none()
        return None if row is None else _session_record(row)

    async def revoke_session(self, session_id: UUID, revoked_at: datetime) -> None:
        await self._conn.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=revoked_at)
        )

    async def revoke_all_sessions(
        self, user_id: UUID, revoked_at: datetime
    ) -> None:
        await self._conn.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
            .values(revoked_at=revoked_at)
        )
