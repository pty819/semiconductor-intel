"""index_generation registry (spec 03 §8).

One INSERT-only row per owner per retrieval configuration generation:
which chunker/text-search configuration/k1/b/embedding model produced the
current chunks + indexes. 词典/分词/嵌入配置的变更被视为新的 index
generation — the registry records what a given set of chunk rows and
indexes was built with, so a configuration change can decide a rebuild
instead of silently mixing rows from different generations.

Deliberately NOT mutable: no updated_at/row_version, no status flipping —
the current generation is the latest by (owner_id, generation); an
abandoned build simply never becomes the latest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from intel.db.base import Base, OwnerScopeMixin, UUIDPrimaryKey


class IndexGeneration(OwnerScopeMixin, UUIDPrimaryKey, Base):
    """INSERT-only retrieval-configuration registry (spec 03 §8)."""

    __tablename__ = "index_generation"

    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # created_at only (spec 03 §1 exception, like job_events): INSERT-only.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "id"),
        UniqueConstraint(
            "owner_id", "generation", name="uq_index_generation_owner_generation"
        ),
        ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_index_generation_owner_id_users"
        ),
        Index("ix_index_generation_owner_generation", "owner_id", "generation"),
    )


__all__ = ["IndexGeneration"]
