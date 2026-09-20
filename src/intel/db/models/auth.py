"""Auth tables: users, auth_sessions (spec 03 §2, scope = 认证内部).

No RLS here: rows are reached only through the auth service, which resolves
the session to a user id before any scoped query runs. There is no list API
for these tables (spec 03 §1). No plaintext passwords — Argon2id hashes only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from intel.db.base import Base, TimestampMixin, UUIDPrimaryKey


class User(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "users"

    login: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Bumped on password change; sessions carrying an older value are invalid.
    password_version: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )

    __table_args__ = (UniqueConstraint("login"),)


class AuthSession(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "auth_sessions"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    # Snapshot of users.password_version at issue time.
    password_version: Mapped[int] = mapped_column(Integer, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("token_hash"),
        Index("ix_auth_sessions_user_id", "user_id"),
        # 按 expiry 清理 (spec 03 §2).
        Index("ix_auth_sessions_expires_at", "expires_at"),
    )
