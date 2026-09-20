"""Unit tests: schema manifest — ORM metadata vs hand-written migration 0001.

Static, DB-free gates for Task 2:

- The exact 21-table manifest from the task brief exists in BOTH
  ``Base.metadata`` and the ``op.create_table`` calls of
  migrations/versions/0001_core_tables.py (parse the migration source).
- Scope classification agrees everywhere: auth/G tables carry no RLS; the
  17 O/I tables all get ENABLE+FORCE and a ``TO intel_app`` policy.
- The migration renders offline (``alembic upgrade --sql``) and the rendered
  DDL is column- and constraint-name-identical to the ORM-compiled DDL, so
  future autogenerate diffs stay meaningful.
- Spec 03 §1 common-column rules: mutable tables have created_at/updated_at/
  row_version; version tables have version/recorded_at/schema_version and
  UNIQUE(parent_id, version) but no mutability columns (INSERT-only).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import logging
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = (
    REPO_ROOT / "migrations" / "versions" / "0001_core_tables.py"
)

# The 21 tables from the task brief / spec 03 §2-§3.
EXPECTED_TABLES = {
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

RLS_TABLES_EXPECTED = EXPECTED_TABLES - {
    "users",
    "auth_sessions",
    "source_templates",
    "parser_versions",
}
INDUSTRY_TABLES_EXPECTED = {"topics", "topic_revisions", "industry_sources"}

VERSION_TABLES = {"industry_revisions", "topic_revisions"}
PARENT_COLUMNS = {"industry_revisions": "industry_id", "topic_revisions": "topic_id"}


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0001_core_tables", MIGRATION_FILE
    )
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
    return out


class TestTableManifest:
    def test_orm_metadata_has_exactly_the_21_tables(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        assert set(Base.metadata.tables) == EXPECTED_TABLES

    def test_models_manifest_constant_matches_metadata(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base
        from intel.db.models import ALL_TABLES

        assert set(ALL_TABLES) == set(Base.metadata.tables) == EXPECTED_TABLES

    def test_migration_file_declares_exactly_the_21_tables(self) -> None:
        source = MIGRATION_FILE.read_text()
        declared = set(re.findall(r'op\.create_table\(\s*\n?\s*"(\w+)"', source))
        assert declared == EXPECTED_TABLES
        # No accidental extras (count guards regex blind spots).
        assert len(re.findall(r"op\.create_table\(", source)) == 21


class TestScopeClassification:
    def test_migration_rls_lists_match_expected_scopes(self) -> None:
        module = _load_migration_module()
        assert set(module.RLS_OWNER_TABLES) | set(module.RLS_INDUSTRY_TABLES) == (
            RLS_TABLES_EXPECTED
        )
        assert set(module.RLS_INDUSTRY_TABLES) == INDUSTRY_TABLES_EXPECTED
        # Scope sets are disjoint and never touch auth/G tables.
        assert not (set(module.RLS_OWNER_TABLES) & set(module.RLS_INDUSTRY_TABLES))
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
                "industry_scope"
                if table in INDUSTRY_TABLES_EXPECTED
                else "owner_scope"
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
        # App role stub exists; grants are a later migration's job.
        assert "CREATE ROLE intel_app NOLOGIN" in sql


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


class TestCommonColumnRules:
    """Spec 03 §1 公共字段 rules, checked structurally."""

    def test_mutable_tables_have_concurrency_columns(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name, table in Base.metadata.tables.items():
            if name in VERSION_TABLES:
                continue
            cols = set(table.columns.keys())
            assert {"id", "created_at", "updated_at", "row_version"} <= cols, name
            assert table.columns["row_version"].nullable is False, name

    def test_version_tables_are_insert_only_with_parent_unique(self) -> None:
        import intel.db.models  # noqa: F401
        from intel.db import Base

        for name in VERSION_TABLES:
            table = Base.metadata.tables[name]
            cols = set(table.columns.keys())
            parent = PARENT_COLUMNS[name]
            assert {"version", "recorded_at", "schema_version", parent} <= cols, name
            assert "updated_at" not in cols and "row_version" not in cols, name
            uniques = [
                {c.name for c in uc.columns}
                for uc in table.constraints
                if uc.__class__.__name__ == "UniqueConstraint"
            ]
            assert {parent, "version"} in uniques, name

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
