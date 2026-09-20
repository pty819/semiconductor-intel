"""Unit tests: schema manifest — ORM metadata vs hand-written migrations.

Static, DB-free gates for Tasks 2 and 3:

- The exact table manifest (21 core tables in 0001 + 54 knowledge/
  conversation/jobs/generation tables in 0002 + the index_generation
  registry in 0003) exists in BOTH ``Base.metadata`` and the
  ``op.create_table`` calls of the migration files (parsed from source).
- Scope classification agrees everywhere: auth/G tables carry no RLS; the
  O/I tables (owner-only predicate for the 0002 O/I hybrids) all get
  ENABLE+FORCE and a ``TO intel_app`` policy.
- The migrations render offline (``alembic upgrade --sql``) and the
  rendered DDL is column- and constraint-name-identical to the ORM-compiled
  DDL — counting constraints added later via ``ALTER TABLE ... ADD
  CONSTRAINT`` (0002 creates circular/deferred FKs that way because
  use_alter constraints never render), so future autogenerate diffs stay
  meaningful.
- Spec 03 §1 common-column rules: mutable tables have created_at/updated_at/
  row_version; version tables have version/recorded_at/schema_version and
  UNIQUE(parent_id, version) but no mutability columns (INSERT-only);
  append-only history tables and pure link tables (composite PK) carry no
  mutability columns either.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import logging
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"
MIGRATION_FILES = {
    "0001_core_tables.py": MIGRATIONS_DIR / "0001_core_tables.py",
    "0002_knowledge_tables.py": MIGRATIONS_DIR / "0002_knowledge_tables.py",
    "0003_index_generation.py": MIGRATIONS_DIR / "0003_index_generation.py",
}

# The 21 core tables from Task 2 / spec 03 §2-§3.
TASK2_TABLES = {
    # auth
    "users",
    "auth_sessions",
    # G
    "source_templates",
    "parser_versions",
    # O
    "industries",
    "industry_revisions",
    "owner_feeds",
    "source_runs",
    "discovery_items",
    "blobs",
    "documents",
    "document_origins",
    "captures",
    "fetch_observations",
    "parsed_artifacts",
    "document_diffs",
    "chunks",
    "processing_decisions",
    # I
    "topics",
    "topic_revisions",
    "industry_sources",
}

# The 54 tables from Task 3 / spec 03 §4/§6/§7/§9, 15 §3, 16 §7.
TASK3_TABLES = {
    # knowledge (spec 03 §4)
    "industry_documents",
    "document_topics",
    "entities",
    "entity_aliases",
    "claims",
    "claim_revisions",
    "evidence",
    "source_families",
    "events",
    "event_revisions",
    "event_topics",
    "event_topic_revisions",
    "event_relations",
    "event_relation_revisions",
    "watches",
    "overrides",
    "event_read_states",
    # §9 physical association tables
    "topic_revision_entities",
    "claim_revision_entities",
    "event_revision_claims",
    "event_revision_entities",
    "event_topic_revision_claims",
    "relation_revision_evidence",
    "watch_topics",
    "watch_entities",
    "document_association_history",
    "event_association_history",
    "event_merge_operations",
    "event_lifecycle_history",
    # evolution/report/conversation (spec 03 §6, 16 §7)
    "evolutions",
    "evolution_revisions",
    "reports",
    "report_revisions",
    "conversations",
    "messages",
    "conversation_state_revisions",
    "conversation_summary_revisions",
    "review_tasks",
    "audit_log",
    "dependency_edges",
    "derived_status",
    # publication_citations split (03 §9)
    "report_citations",
    "evolution_citations",
    "message_citations",
    # jobs (spec 03 §7)
    "jobs",
    "job_steps",
    "job_events",
    "model_runs",
    "coverage_batches",
    "recall_hits",
    "api_idempotency",
    # generation (doc 15 §3)
    "generation_runs",
    "generation_run_models",
    "output_generations",
}

# The index_generation registry (Task 10 / spec 03 §8): one INSERT-only
# O-scope table created by 0003 alongside the retrieval indexes.
TASK4_TABLES = {"index_generation"}

EXPECTED_TABLES = TASK2_TABLES | TASK3_TABLES | TASK4_TABLES

RLS_TABLES_EXPECTED = EXPECTED_TABLES - {
    "users",
    "auth_sessions",
    "source_templates",
    "parser_versions",
}
# 0001 I tables + every 0002 I table (hybrids use the owner-only predicate).
INDUSTRY_TABLES_EXPECTED = RLS_TABLES_EXPECTED - {
    "industries",
    "industry_revisions",
    "owner_feeds",
    "source_runs",
    "discovery_items",
    "blobs",
    "documents",
    "document_origins",
    "captures",
    "fetch_observations",
    "parsed_artifacts",
    "document_diffs",
    "chunks",
    "processing_decisions",
    # 0002 owner-predicate tables: O + O/I hybrids (nullable industry_id)
    "api_idempotency",
    "jobs",
    "job_steps",
    "job_events",
    "model_runs",
    "coverage_batches",
    "audit_log",
    # 0003 owner-predicate table
    "index_generation",
}

VERSION_TABLES = {
    # 0001
    "industry_revisions": "industry_id",
    "topic_revisions": "topic_id",
    # 0002
    "claim_revisions": "claim_id",
    "event_revisions": "event_id",
    "event_topic_revisions": "event_topic_id",
    "event_relation_revisions": "relation_id",
    "evolution_revisions": "evolution_id",
    "report_revisions": "report_id",
    "conversation_state_revisions": "conversation_id",
}
PARENT_COLUMNS = dict(VERSION_TABLES)

# Append-only tables (spec 03 §9 history / 16 §7 summaries): UUID PK (or the
# job_events PK exception) + recorded_at/created_at, no mutability columns
# and no version counter.
APPEND_ONLY_TABLES = {
    "conversation_summary_revisions",
    "document_association_history",
    "event_association_history",
    "event_merge_operations",
    "event_lifecycle_history",
    "job_events",
    "index_generation",
}

# Pure link tables (spec 03 §1 例外: 复合主键, no common columns).
PURE_LINK_TABLES = {
    "topic_revision_entities",
    "claim_revision_entities",
    "event_revision_claims",
    "event_revision_entities",
    "event_topic_revision_claims",
    "relation_revision_evidence",
    "watch_topics",
    "watch_entities",
    "report_citations",
    "evolution_citations",
    "message_citations",
    "generation_run_models",
}

NO_MUTABILITY_TABLES = set(VERSION_TABLES) | APPEND_ONLY_TABLES | PURE_LINK_TABLES


def _load_migration_module(name: str):
    path = MIGRATION_FILES[name]
    spec = importlib.util.spec_from_file_location(f"migration_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_migration_sql() -> str:
    logging.disable(logging.CRITICAL)
    try:
        from alembic import command
        from alembic.config import Config

        buf = io.StringIO()
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        with contextlib.redirect_stdout(buf):
            command.upgrade(cfg, "head", sql=True)
        return buf.getvalue()
    finally:
        logging.disable(logging.NOTSET)


def _orm_ddl_columns_and_constraints():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import intel.db.models  # noqa: F401  (registers all tables)
    from intel.db import Base

    dialect = postgresql.dialect()
    rendered = {}
    for name, table in Base.metadata.tables.items():
        ddl = str(CreateTable(table).compile(dialect=dialect))
        cols = set(re.findall(r"^\t(\w+) ", ddl, re.MULTILINE))
        cons = set(re.findall(r"CONSTRAINT (\S+)", ddl))
        rendered[name] = (cols, cons)
    return rendered


def _migration_sql_tables(sql: str) -> dict[str, tuple[set[str], set[str]]]:
    out: dict[str, tuple[set[str], set[str]]] = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n+\);", sql, re.DOTALL):
        name, body = m.group(1), m.group(2)
        cols = set(re.findall(r"^\s{4}(\w+) ", body, re.MULTILINE))
        cons = set(re.findall(r"CONSTRAINT (\S+)", body))
        out[name] = (cols, cons)
    # 0002 adds circular/deferred FKs (and repairs 0001's use_alter ones)
    # via ALTER TABLE ... ADD CONSTRAINT — count them per table.
    for table, constraint in re.findall(r"ALTER TABLE (\w+) ADD CONSTRAINT (\w+)", sql):
        if table in out:
            cols, cons = out[table]
            out[table] = (cols, cons | {constraint})
    return out


class TestTableManifest:
    def test_orm_metadata_has_exactly_the_expected_tables(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        assert set(Base.metadata.tables) == EXPECTED_TABLES

    def test_models_manifest_constant_matches_metadata(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base
        from intel.db.models import ALL_TABLES

        assert set(ALL_TABLES) == set(Base.metadata.tables) == EXPECTED_TABLES

    def test_migration_files_declare_exactly_their_tables(self) -> None:
        per_file = {
            "0001_core_tables.py": TASK2_TABLES,
            "0002_knowledge_tables.py": TASK3_TABLES,
            "0003_index_generation.py": TASK4_TABLES,
        }
        for name, expected in per_file.items():
            source = MIGRATION_FILES[name].read_text()
            declared = set(re.findall(r'op\.create_table\(\s*\n?\s*"(\w+)"', source))
            # 0002 builds its uniform §9 link/citation tables via helpers.
            declared |= set(
                re.findall(r'(?:_link_table|_citation_table)\(\s*\n?\s*"(\w+)"', source)
            )
            assert declared == expected, name
            # No accidental extras (count guards regex blind spots). Literal
            # op.create_table calls + helper invocations (indented — the
            # helper *definitions* and their dynamic op.create_table calls
            # must not be counted twice).
            n_create = len(re.findall(r'op\.create_table\(\s*\n?\s*"', source))
            n_helper = len(
                re.findall(
                    r"^\s+(?:_link_table|_citation_table)\(", source, re.MULTILINE
                )
            )
            assert n_create + n_helper == len(expected), (name, n_create, n_helper)


class TestScopeClassification:
    def test_migration_rls_lists_match_expected_scopes(self) -> None:
        owner_tables: set[str] = set()
        industry_tables: set[str] = set()
        for name in MIGRATION_FILES:
            module = _load_migration_module(name)
            owner_tables |= set(module.RLS_OWNER_TABLES)
            industry_tables |= set(module.RLS_INDUSTRY_TABLES)
        assert owner_tables | industry_tables == RLS_TABLES_EXPECTED
        assert industry_tables == INDUSTRY_TABLES_EXPECTED
        # Scope sets are disjoint and never touch auth/G tables.
        assert not (owner_tables & industry_tables)
        assert not (
            RLS_TABLES_EXPECTED
            & {"users", "auth_sessions", "source_templates", "parser_versions"}
        )

    def test_rendered_sql_rls_enable_force_and_policies(self) -> None:
        sql = _render_migration_sql()
        for table in sorted(RLS_TABLES_EXPECTED):
            assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql, table
            assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql, table
            suffix = (
                "industry_scope" if table in INDUSTRY_TABLES_EXPECTED else "owner_scope"
            )
            assert f"CREATE POLICY {table}_{suffix} ON {table} TO intel_app" in sql
        # auth/G tables must NOT get RLS.
        for table in ("users", "auth_sessions", "source_templates", "parser_versions"):
            assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" not in sql
        # I-table policies check both GUCs; WITH CHECK mirrors USING.
        assert sql.count("AND industry_id = current_setting('app.industry_id'") == (
            2 * len(INDUSTRY_TABLES_EXPECTED)
        )
        assert sql.count("WITH CHECK (owner_id = current_setting") == (
            len(RLS_TABLES_EXPECTED)
        )


class TestMigrationMatchesOrm:
    def test_rendered_ddl_matches_orm_columns_and_constraints(self) -> None:
        sql = _render_migration_sql()
        mig = _migration_sql_tables(sql)
        orm = _orm_ddl_columns_and_constraints()
        assert set(mig) == set(orm) | {"alembic_version"}
        for table, (orm_cols, orm_cons) in sorted(orm.items()):
            mig_cols, mig_cons = mig[table]
            assert mig_cols == orm_cols, (
                f"{table}: migration-only={sorted(mig_cols - orm_cols)} "
                f"orm-only={sorted(orm_cols - mig_cols)}"
            )
            assert mig_cons == orm_cons, (
                f"{table}: migration-only={sorted(mig_cons - orm_cons)} "
                f"orm-only={sorted(orm_cons - mig_cons)}"
            )

    def test_rendered_ddl_matches_orm_indexes(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        sql = _render_migration_sql()
        mig_idx = set(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", sql))
        orm_idx = {ix.name for t in Base.metadata.tables.values() for ix in t.indexes}
        assert mig_idx == orm_idx, (
            f"migration-only={sorted(mig_idx - orm_idx)} "
            f"orm-only={sorted(orm_idx - mig_idx)}"
        )

    def test_deferred_fks_land_as_real_alters(self) -> None:
        """use_alter constraints never render — 0002 must create every
        circular/deferred FK (incl. the repaired 0001 ones) via ALTER."""
        sql = _render_migration_sql()
        alters = set(re.findall(r"ALTER TABLE (\w+) ADD CONSTRAINT (\w+)", sql))
        expected = {
            ("industries", "fk_industries_owner_id_industry_revisions"),
            ("topics", "fk_topics_owner_id_topic_revisions"),
            ("documents", "fk_documents_owner_id_captures"),
            ("industry_revisions", "fk_industry_revisions_owner_id_jobs"),
            ("topic_revisions", "fk_topic_revisions_owner_id_jobs"),
            ("source_runs", "fk_source_runs_owner_id_jobs"),
            ("processing_decisions", "fk_processing_decisions_owner_id_model_runs"),
            ("processing_decisions", "fk_processing_decisions_owner_id_overrides"),
            ("claims", "fk_claims_owner_id_claim_revisions"),
            ("events", "fk_events_owner_id_event_revisions"),
            ("event_topics", "fk_event_topics_owner_id_event_topic_revisions"),
            ("event_relations", "fk_event_relations_owner_id_event_relation_revisions"),
            ("watches", "fk_watches_owner_id_reports"),
            ("evolutions", "fk_evolutions_owner_id_evolution_revisions"),
            ("reports", "fk_reports_owner_id_report_revisions"),
            ("conversations", "fk_conversations_owner_id_messages"),
            (
                "conversations",
                "fk_conversations_owner_id_conversation_state_revisions",
            ),
        }
        assert expected <= alters

    def test_no_forward_references_in_statement_order(self) -> None:
        """Online-upgrade ordering gate: Postgres rejects inline FKs whose
        target relation does not exist yet, so in the offline-rendered SQL
        every REFERENCES target (and every ALTER TABLE ... ADD CONSTRAINT
        source) must already have been created earlier in statement order.
        Self-referential FKs are fine — the CREATE TABLE header precedes its
        own body in the text. This gate fails on any table emitted before
        its FK targets (the output_generations bug class)."""
        sql = _render_migration_sql()
        created: set[str] = set()
        problems: list[tuple[str, str]] = []
        for m in re.finditer(
            r"CREATE TABLE (\w+)|REFERENCES (\w+)"
            r"|ALTER TABLE (\w+) ADD CONSTRAINT",
            sql,
        ):
            if m.group(1):
                created.add(m.group(1))
            elif m.group(2):
                if m.group(2) not in created:
                    problems.append(("references-missing-table", m.group(2)))
            elif m.group(3) is not None and m.group(3) not in created:
                problems.append(("alter-on-missing-table", m.group(3)))
        assert not problems, sorted(problems)


class TestCommonColumnRules:
    """Spec 03 §1 公共字段 rules, checked structurally."""

    def test_mutable_tables_have_concurrency_columns(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name, table in Base.metadata.tables.items():
            if name in NO_MUTABILITY_TABLES:
                continue
            cols = set(table.columns.keys())
            assert {"id", "created_at", "updated_at", "row_version"} <= cols, name
            assert table.columns["row_version"].nullable is False, name

    def test_version_tables_are_insert_only_with_parent_unique(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name, parent in VERSION_TABLES.items():
            table = Base.metadata.tables[name]
            cols = set(table.columns.keys())
            assert {"version", "recorded_at", "schema_version", parent} <= cols, name
            assert "updated_at" not in cols and "row_version" not in cols, name
            uniques = [
                {c.name for c in uc.columns}
                for uc in table.constraints
                if uc.__class__.__name__ == "UniqueConstraint"
            ]
            assert {parent, "version"} in uniques, name

    def test_append_only_tables_have_no_mutability_columns(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name in APPEND_ONLY_TABLES:
            table = Base.metadata.tables[name]
            cols = set(table.columns.keys())
            assert "updated_at" not in cols and "row_version" not in cols, name
            if name == "job_events":
                # Spec 03 §1 exception: PK(job_id, seq), created_at only.
                pk = [c.name for c in table.primary_key.columns]
                assert pk == ["job_id", "seq"], name
                assert "id" not in cols and "recorded_at" not in cols, name
            elif name == "event_merge_operations":
                # Spec 03 §9 names applied_at/undone_at, not recorded_at.
                assert {"id", "applied_at"} <= cols, name
            elif name == "index_generation":
                # Spec 03 §8 concept: INSERT-only registry, created_at only.
                assert {"id", "created_at"} <= cols, name
                assert "recorded_at" not in cols, name
            else:
                assert {"id", "recorded_at"} <= cols, name

    def test_pure_link_tables_use_composite_primary_keys(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name in PURE_LINK_TABLES:
            table = Base.metadata.tables[name]
            cols = set(table.columns.keys())
            pk = [c.name for c in table.primary_key.columns]
            assert len(pk) == 2, (name, pk)
            assert "id" not in cols, name
            assert "updated_at" not in cols and "row_version" not in cols, name
            assert {"owner_id", "industry_id"} <= cols, name

    def test_source_templates_id_is_a_stable_string(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        pk = list(Base.metadata.tables["source_templates"].primary_key.columns)
        assert [c.name for c in pk] == ["id"]
        assert pk[0].type.python_type is str

    def test_scope_columns_and_owner_uniques(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name in sorted(RLS_TABLES_EXPECTED):
            table = Base.metadata.tables[name]
            cols = set(table.columns.keys())
            assert "owner_id" in cols, name
            if name in PURE_LINK_TABLES or name == "job_events":
                continue  # composite PK instead of the scope UNIQUE
            if name in INDUSTRY_TABLES_EXPECTED:
                assert "industry_id" in cols, name
                expected_unique = {"owner_id", "industry_id", "id"}
            else:
                expected_unique = {"owner_id", "id"}
            uniques = [
                {c.name for c in uc.columns}
                for uc in table.constraints
                if uc.__class__.__name__ == "UniqueConstraint"
            ]
            assert expected_unique in uniques, name

    def test_every_rls_table_has_owner_leading_index(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name in sorted(RLS_TABLES_EXPECTED):
            table = Base.metadata.tables[name]
            leading = [
                i for i in table.indexes if next(iter(i.columns)).name == "owner_id"
            ]
            assert leading, f"{name} has no owner-leading index (spec 03 §8)"

    def test_jobs_serve_as_composite_fk_targets(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name in ("jobs", "model_runs"):
            uniques = [
                {c.name for c in uc.columns}
                for uc in Base.metadata.tables[name].constraints
                if uc.__class__.__name__ == "UniqueConstraint"
            ]
            assert {"owner_id", "id"} in uniques, name
            assert {"owner_id", "industry_id", "id"} in uniques, name

    def test_output_generations_single_target_check(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        table = Base.metadata.tables["output_generations"]
        checks = [
            ck
            for ck in table.constraints
            if ck.__class__.__name__ == "CheckConstraint"
            and "num_nonnulls"
            in (ck.sqltext.text if hasattr(ck.sqltext, "text") else str(ck.sqltext))
        ]
        assert len(checks) == 1

    def test_every_fk_references_a_unique_or_pk_target(self) -> None:
        """Composite FKs only hold if the referred column set carries a
        UNIQUE/PK — the classic silent failure for (owner_id, industry_id,
        id) references. Without a DB, assert it statically on metadata."""
        import intel.db.models  # noqa: F401
        from intel.db import Base

        def keysets(table):
            return {
                frozenset(c.name for c in uc.columns)
                for uc in table.constraints
                if uc.__class__.__name__ in ("UniqueConstraint", "PrimaryKeyConstraint")
            }

        for name, table in sorted(Base.metadata.tables.items()):
            for fk in table.foreign_key_constraints:
                referred = fk.elements[0].column.table
                refcols = frozenset(e.column.name for e in fk.elements)
                assert refcols in keysets(referred), (
                    f"{name}: FK to {referred.name} {sorted(refcols)} has no "
                    "UNIQUE/PK target"
                )


class TestRlsHelper:
    async def test_set_scope_rejects_none_owner(self) -> None:
        import pytest

        from intel.db.rls import ScopeMissing, set_scope

        with pytest.raises(ScopeMissing):
            await set_scope(conn=None, owner_id=None)  # type: ignore[arg-type]

    def test_guc_names_are_namespaced(self) -> None:
        from intel.db.rls import INDUSTRY_GUC, OWNER_GUC

        assert OWNER_GUC == "app.owner_id"
        assert INDUSTRY_GUC == "app.industry_id"


class TestRetrievalMigration0003:
    """Task 10 gates on the rendered 0003 DDL (spec 03 §8, 04 §5/§6):

    - extensions + jieba text search configuration are created defensively
      (idempotent with deploy/nas/init/40-intel-extensions.sh);
    - the BM25 index binds ``text_config='jieba'`` with k1/b from the
      retrieval constants;
    - the trigram GIN index serves the alias/phrase channel;
    - NO vector index in the default generation — first version is exact
      ordering; the diskann DDL is flag-gated and absent from the default
      render (REC-08: switch only after the 98% golden-set gate).
    """

    def test_extensions_and_jieba_config_are_created_idempotently(self) -> None:
        sql = _render_migration_sql()
        for ext in ("vector", "vectorscale", "pg_textsearch", "pg_jieba", "pg_trgm"):
            assert f"CREATE EXTENSION IF NOT EXISTS {ext}" in sql, ext
        assert (
            "CREATE TEXT SEARCH CONFIGURATION IF NOT EXISTS jieba"
            " (PARSER = jieba)" in sql
        )
        # Mapping is re-applied idempotently (drop-if-exists first), so the
        # migration is safe on databases the init script already configured.
        assert "ALTER TEXT SEARCH CONFIGURATION jieba DROP MAPPING IF EXISTS" in sql
        assert (
            "ALTER TEXT SEARCH CONFIGURATION jieba ADD MAPPING"
            " FOR n, v, a, i, e, l WITH simple" in sql
        )

    def test_bm25_index_binds_jieba_config_and_constants(self) -> None:
        from intel.retrieval.bm25 import BM25_B, BM25_K1

        sql = _render_migration_sql()
        assert "CREATE INDEX ix_chunks_text_bm25 ON chunks USING bm25 (text)" in sql
        assert "text_config = 'jieba'" in sql
        assert f"k1 = '{BM25_K1}'" in sql
        assert f"b = '{BM25_B}'" in sql

    def test_trigram_gin_index_on_normalized_terms(self) -> None:
        sql = _render_migration_sql()
        assert (
            "CREATE INDEX ix_chunks_normalized_terms_trgm ON chunks"
            " USING gin (normalized_terms gin_trgm_ops)" in sql
        )

    def test_no_vector_index_in_default_generation(self) -> None:
        sql = _render_migration_sql()
        # diskann DDL exists as a flag-gated helper only; the default render
        # must not create any vector index (exact ordering is the v1 path).
        assert "diskann" not in sql.lower()
        assert "USING diskann" not in sql

    def test_diskann_generation_sql_is_available_for_joint_debugging(self) -> None:
        module = _load_migration_module("0003_index_generation.py")
        stmts = module.diskann_generation_sql()
        joined = "\n".join(stmts)
        # Verified pgvectorscale syntax (Task 9): diskann + cosine ops.
        assert "USING diskann (embedding vector_cosine_ops)" in joined
        # Spec 03 §8: diskann build memory ~2x table size — the ops
        # runbook's build step raises maintenance_work_mem off-peak.
        assert "maintenance_work_mem" in joined
        assert module.DISKANN_CREATE_FLAG == "INTEL_CREATE_DISKANN"
