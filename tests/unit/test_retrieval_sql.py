"""Unit tests: exact SQL shapes for the retrieval channels (spec 04 §6, 03 §8).

Statement builders compile against the PostgreSQL dialect without touching
a database (the test_jobs_sql.py pattern); the tests pin the load-bearing
fragments:

- BM25: pg_textsearch ``<@>`` score operator ordered ASCENDING (matching
  documents score NEGATIVE — ascending puts the most relevant first) with
  owner/industry scope conditions above RLS (ISO-03 变体);
- vector: pgvector cosine ``<=>`` exact KNN (no index in the default
  generation), ``embedding IS NOT NULL`` (missing embeddings are excluded
  from the channel, never an error — REC-04), parameter cast to ``vector``
  so any driver can pass the literal string form;
- alias: pg_trgm ``%`` similarity operator on ``normalized_terms``;
- proximity: document_topics → industry_documents join (历史正例);
- chunk writes: ``ON CONFLICT (parsed_artifact_id, ordinal) DO NOTHING``
  (idempotent index re-runs);
- recall provenance: recall_hits insert conflicts on the
  (job, parse, chunk, channel) constraint.

Plus the REC-08 stub: recall@k of diskann vs exact result sets, gate 98%.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from intel.retrieval.bm25 import (
    BM25_B,
    BM25_INDEX_NAME,
    BM25_K1,
    bm25_search_stmt,
    build_bm25_query,
    positive_bm25_score,
)
from intel.retrieval.indexer import chunks_insert_stmt
from intel.retrieval.recall import (
    alias_search_stmt,
    proximity_search_stmt,
    recall_hits_insert_stmt,
)
from intel.retrieval.vector import (
    DISKANN_RECALL_GATE,
    embedding_gap_stmt,
    knn_search_stmt,
    passes_diskann_gate,
    recall_at_k,
)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class TestBm25Query:
    def test_build_joins_terms_whitespace_deduped(self) -> None:
        assert build_bm25_query(["刻蚀", "EUV", "EUV", " "]) == "刻蚀 EUV"

    def test_empty_terms_give_empty_query(self) -> None:
        assert build_bm25_query([]) == ""
        assert build_bm25_query(["", "  "]) == ""

    def test_negative_column_score_negated_for_display(self) -> None:
        # pg_textsearch: "<@> returns negative BM25 scores for ascending
        # index scans, so lower scores rank first" (verified Task 9).
        assert positive_bm25_score(-12.3) == 12.3
        assert positive_bm25_score(-0.0) == 0.0

    def test_k1_b_constants_match_settings_defaults(self) -> None:
        from intel.settings import Settings

        s = Settings(_env_file=None)
        assert BM25_K1 == s.bm25_k1
        assert BM25_B == s.bm25_b


class TestBm25Statement:
    def test_uses_score_operator_ascending_with_scope(self) -> None:
        sql = _sql(bm25_search_stmt())
        assert "FROM chunks" in sql
        assert "<@>" in sql
        # Negative scores ⇒ ascending order ranks the best match first.
        assert "ASC" in sql
        assert "LIMIT" in sql
        assert "bm25_q" in sql

    def test_scoped_above_rls_by_owner_and_industry(self) -> None:
        sql = _sql(bm25_search_stmt())
        assert "chunks.owner_id =" in sql
        assert "JOIN industry_documents" in sql
        assert "industry_documents.industry_id =" in sql
        assert "industry_documents.active" in sql
        # The parse→document chain makes the industry binding explicit.
        assert "JOIN parsed_artifacts" in sql
        assert "JOIN captures" in sql


class TestVectorStatement:
    def test_exact_knn_with_cosine_and_null_guard(self) -> None:
        sql = _sql(knn_search_stmt())
        assert "<=>" in sql
        assert "embedding IS NOT NULL" in sql
        assert "ORDER BY" in sql and "ASC" in sql
        assert "LIMIT" in sql
        # The query parameter is cast so drivers can pass '[0.1,0.2]' text.
        assert "AS vector" in sql
        # Same owner/industry scope conditions as every other channel.
        assert "chunks.owner_id =" in sql
        assert "industry_documents.industry_id =" in sql

    def test_embedding_gap_stmt_counts_null_embeddings_in_scope(self) -> None:
        sql = _sql(embedding_gap_stmt())
        assert "IS NULL" in sql
        assert "count" in sql.lower()
        assert "FROM chunks" in sql
        assert "JOIN industry_documents" in sql


class TestAliasStatement:
    def test_trigram_similarity_on_normalized_terms(self) -> None:
        sql = _sql(alias_search_stmt())
        # %% is the pyformat-paramstyle escape of the pg_trgm % operator.
        assert "chunks.normalized_terms %% %(alias)s" in sql
        assert "similarity" in sql.lower()
        assert "ORDER BY" in sql
        assert "LIMIT" in sql
        assert "chunks.owner_id =" in sql


class TestProximityStatement:
    def test_joins_positive_documents_via_document_topics(self) -> None:
        sql = _sql(proximity_search_stmt())
        assert "FROM document_topics" in sql
        assert "JOIN industry_documents" in sql
        assert "topic_id =" in sql
        assert "document_topics.industry_id =" in sql


class TestWriteStatements:
    def test_chunks_insert_conflicts_on_parse_ordinal(self) -> None:
        sql = _sql(chunks_insert_stmt())
        assert "INSERT INTO chunks" in sql
        assert "ON CONFLICT (parsed_artifact_id, ordinal) DO NOTHING" in sql

    def test_recall_hits_insert_conflicts_on_provenance_unique(self) -> None:
        sql = _sql(recall_hits_insert_stmt())
        assert "INSERT INTO recall_hits" in sql
        assert (
            "ON CONFLICT ON CONSTRAINT"
            " uq_recall_hits_job_parse_chunk_channel DO NOTHING" in sql
        )


class TestOrmIndexParity:
    """The BM25/trigram indexes are declared in ORM metadata AND migration
    0003 with identical options — pinned here so the two cannot drift (the
    schema-manifest test then enforces name parity against rendered DDL)."""

    def test_bm25_index_options_match_constants(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        table = Base.metadata.tables["chunks"]
        ix = next(i for i in table.indexes if i.name == BM25_INDEX_NAME)
        assert ix.dialect_kwargs["postgresql_using"] == "bm25"
        assert ix.dialect_kwargs["postgresql_with"] == {
            "text_config": "jieba",
            "k1": BM25_K1,
            "b": BM25_B,
        }

    def test_trigram_index_options(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        table = Base.metadata.tables["chunks"]
        ix = next(
            i for i in table.indexes if i.name == "ix_chunks_normalized_terms_trgm"
        )
        assert ix.dialect_kwargs["postgresql_using"] == "gin"
        assert ix.dialect_kwargs["postgresql_ops"] == {
            "normalized_terms": "gin_trgm_ops"
        }


class TestRecallAtK:
    """REC-08 stub: diskann vs exact filtered recall, offline-testable."""

    def test_perfect_overlap_is_one(self) -> None:
        assert recall_at_k(["a", "b", "c"], ["a", "b", "c"], k=3) == 1.0

    def test_partial_overlap_fraction(self) -> None:
        # exact top-3 = {a,b,c}; approx top-3 = {b,a,d} → 2/3.
        assert (
            abs(recall_at_k(["a", "b", "c", "d"], ["b", "a", "d"], k=3) - 2 / 3) < 1e-9
        )

    def test_empty_exact_is_full_recall(self) -> None:
        assert recall_at_k([], ["a"]) == 1.0

    def test_gate_at_98_percent(self) -> None:
        assert DISKANN_RECALL_GATE == 0.98
        # 99/100 recalled → passes; 98/100 → passes at the boundary;
        # 97/100 → fails (the diskann generation must not become default).
        exact = [f"doc{i}" for i in range(100)]
        assert passes_diskann_gate(exact, exact[:99] + ["extra"], k=100)
        assert passes_diskann_gate(exact, exact[:98] + ["x", "y"], k=100)
        assert not passes_diskann_gate(exact, exact[:97] + ["x", "y", "z"], k=100)
