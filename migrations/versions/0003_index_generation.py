"""index_generation registry + retrieval indexes (spec 03 §8, 04 §5/§6).

Task 10's migration, on top of 0001/0002:

- The retrieval extensions, created defensively (IF NOT EXISTS): they are
  preinstalled in the deploy image (deploy/nas/Containerfile) and also
  configured by deploy/nas/init/40-intel-extensions.sh — every path must
  be re-runnable against a database the other one already set up.
- The ``jieba`` text search configuration (parser ``jieba`` from pg_jieba,
  tokens mapped to the ``simple`` dictionary) — the D13 custom
  configuration the BM25 index binds via ``text_config='jieba'``. The
  mapping is dropped-if-exists before being re-added so an older
  mapping (e.g. from the init script) is replaced, not merged.
- The BM25 index on chunks.text: pg_textsearch ``USING bm25`` with the
  jieba config and k1/b from intel.retrieval.bm25 (pinned together with
  the ORM index declaration by tests/unit/test_retrieval_sql.py).
  pg_textsearch returns NEGATIVE scores from ``<@>``, ordered ascending —
  the recall channel relies on that ordering (Task 9 verification).
- The trigram GIN index on chunks.normalized_terms for the alias /
  短语-精确复查 channel.
- The ``index_generation`` table: INSERT-only per-owner registry of
  retrieval configuration generations (spec 03 §8: 词典/分词/嵌入配置
  变更视为新 generation), RLS owner scope like every O table.

NO vector index in this default generation: the first version orders
exactly over ``chunks.embedding`` (vector.py). The approximate-index DDL
exists as :func:`diskann_generation_sql` — pgvectorscale diskann with
cosine ops, built off-peak with raised maintenance_work_mem (spec 03 §8:
build memory ~2x table size) — and is executed only when the
``INTEL_CREATE_DISKANN`` flag is set at migration time. REC-08 gates the
switch: filtered recall vs exact ordering must be >= 98% on the golden
set before an approximate index becomes the default generation.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_index_generation"
down_revision: str | None = "0002_knowledge_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirror of intel.db.models scope classification for THIS migration's tables.
RLS_OWNER_TABLES = ("index_generation",)
RLS_INDUSTRY_TABLES: tuple[str, ...] = ()

_UUID = postgresql.UUID()
_JSONB = postgresql.JSONB()
_TS = sa.DateTime(timezone=True)

#: Environment flag that opts a migration run into ALSO creating the
#: approximate vector index (default off — REC-08 gates the switch).
DISKANN_CREATE_FLAG = "INTEL_CREATE_DISKANN"


def diskann_generation_sql() -> list[str]:
    """Approximate vector-index DDL for a NON-default index generation.

    Kept callable (and importable without a DB) so the ops runbook and the
    golden-set tooling can render exactly what would run. The session-local
    maintenance_work_mem raise covers the build's ~2x-table memory profile
    and is reset afterwards (spec 03 §8).
    """
    return [
        "SET maintenance_work_mem = '512MB'",
        (
            "CREATE INDEX ix_chunks_embedding_diskann ON chunks"
            " USING diskann (embedding vector_cosine_ops)"
        ),
        "RESET maintenance_work_mem",
    ]


def _apply_rls() -> None:
    """Same policy shape as 0001/0002: ENABLE + FORCE, TO intel_app."""
    owner_pred = "owner_id = current_setting('app.owner_id', true)::uuid"
    for table, predicate, policy in [
        (t, owner_pred, f"{t}_owner_scope") for t in RLS_OWNER_TABLES
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {policy} ON {table} TO intel_app"
            f" USING ({predicate}) WITH CHECK ({predicate})"
        )


def upgrade() -> None:
    # -- retrieval extensions (idempotent with the NAS init script) -------
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_textsearch")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_jieba")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vectorscale")

    # -- jieba text search configuration (D13) -----------------------------
    op.execute("CREATE TEXT SEARCH CONFIGURATION IF NOT EXISTS jieba (PARSER = jieba)")
    # Replace, never merge: drop whatever mapping exists first.
    op.execute(
        "ALTER TEXT SEARCH CONFIGURATION jieba DROP MAPPING IF EXISTS"
        " FOR n, v, a, i, e, l"
    )
    op.execute(
        "ALTER TEXT SEARCH CONFIGURATION jieba ADD MAPPING"
        " FOR n, v, a, i, e, l WITH simple"
    )

    # -- index_generation registry (INSERT-only, O scope) ------------------
    op.create_table(
        "index_generation",
        sa.Column("owner_id", _UUID, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("config", _JSONB, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_generation")),
        sa.UniqueConstraint(
            "owner_id", "id", name=op.f("uq_index_generation_owner_id")
        ),
        sa.UniqueConstraint(
            "owner_id",
            "generation",
            name=op.f("uq_index_generation_owner_generation"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_index_generation_owner_id_users"),
        ),
    )
    op.create_index(
        "ix_index_generation_owner_generation",
        "index_generation",
        ["owner_id", "generation"],
    )
    _apply_rls()

    # -- retrieval indexes --------------------------------------------------
    # k1/b mirror intel.retrieval.bm25.BM25_K1/BM25_B and the ORM index
    # declaration (db/models/pool.py); test_retrieval_sql pins the three
    # together. Values are quoted strings — reloptions are text.
    op.execute(
        "CREATE INDEX ix_chunks_text_bm25 ON chunks USING bm25 (text)"
        " WITH (text_config = 'jieba', k1 = '1.5', b = '0.75')"
    )
    op.execute(
        "CREATE INDEX ix_chunks_normalized_terms_trgm ON chunks"
        " USING gin (normalized_terms gin_trgm_ops)"
    )

    # Approximate vector index: flag-gated, NEVER in the default render
    # (REC-08: exact ordering is the v1 path until the golden-set gate).
    if os.environ.get(DISKANN_CREATE_FLAG):
        for statement in diskann_generation_sql():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_normalized_terms_trgm")
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_bm25")
    op.drop_table("index_generation")
    # Extensions and the jieba configuration are deployment-level state
    # (shared with the init script); the downgrade leaves them in place.
