"""Declarative base and shared column mixins (spec 03 §1 公共约定).

Common-column rules implemented here:

- Mutable entities get ``UUIDPrimaryKey`` + ``TimestampMixin``:
  ``id uuid PK`` (application-generated, never DB-generated),
  ``created_at`` / ``updated_at`` (UTC timestamptz) and
  ``row_version bigint`` server-default 1 (optimistic concurrency:
  UPDATE ... WHERE row_version = expected, bump on success, 409 on miss).
- Version tables are immutable (INSERT-only; corrections are new versions),
  so they use ``VersionMixin`` instead: ``version int``, ``recorded_at``,
  ``created_by_job_id?`` (FK added with the jobs tables migration) and
  ``schema_version``, plus per-table ``parent_id`` and
  ``UNIQUE(parent_id, version)`` declared in ``__table_args__``.
- Scope mixins carry the isolation columns (spec 03 §1 作用域):
  O tables carry ``owner_id`` FK users + ``UNIQUE(owner_id, id)``;
  I tables carry ``owner_id, industry_id`` with a composite FK to
  ``industries(owner_id, id)`` + ``UNIQUE(owner_id, industry_id, id)``.
  The UNIQUE constraints and composite FKs live in each table's
  ``__table_args__`` so DDL is explicit per table.

Exceptions (spec 03 §1): ``source_templates.id`` is a stable string derived
from the normalized seed (declared on that table, not via UUIDPrimaryKey);
``job_events`` uses PK (job_id, seq) — jobs tables arrive in a later task.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


class VectorType(UserDefinedType):
    """pgvector ``vector`` column type, DDL-level only.

    The dimension is deliberately NOT fixed here: spec 03 §8 pins dimensions
    to a config-driven index_generation migration. Values are NULL until the
    embedding pipeline lands; the ``pgvector`` Python package (with its
    asyncpg codec) becomes necessary only when queries start reading this
    column — the search task can adopt it then.
    """

    def get_col_spec(self) -> str:
        return "vector"


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base for all ORM models; RLS-scope tables inherit the scope mixins."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    """``id uuid PK`` with an application-side default (UUID 由应用生成)."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """Common columns of mutable entities (spec 03 §1)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    row_version: Mapped[int] = mapped_column(
        BigInteger, server_default=text("1"), nullable=False
    )


class VersionMixin:
    """Common columns of immutable version tables (spec 03 §1).

    The parent link (``parent_id``) and ``UNIQUE(parent_id, version)`` are
    declared per table — the parent column is named after the parent entity
    (``industry_id``, ``topic_id``, ...).
    """

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # FK to jobs.id is added by the jobs migration (jobs tables are a later
    # task); the column exists now so version tables match the spec's common
    # column list.
    created_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    schema_version: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), nullable=False
    )


class OwnerScopeMixin:
    """O scope: ``owner_id`` FK users (spec 03 §1 作用域)."""

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class IndustryScopeMixin:
    """I scope: ``owner_id`` + ``industry_id``.

    The composite FK to ``industries(owner_id, id)`` is declared per table in
    ``__table_args__`` (naming convention derives from column_0 = owner_id).
    """

    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    industry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
