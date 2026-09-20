"""intel_app GRANTs land in migration 0004 (Task 17 / spec 10 §1)."""

from __future__ import annotations

import contextlib
import io
import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _render_head_sql() -> str:
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


def test_rendered_sql_grants_dml_to_intel_app() -> None:
    sql = _render_head_sql()
    assert "GRANT USAGE ON SCHEMA public TO intel_app" in sql
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
        " public TO intel_app" in sql
    )
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO intel_app" in sql
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT" in sql
    assert "GRANT intel_app TO" in sql
    assert "NOSUPERUSER" in sql or "NOLOGIN" in sql


def test_grant_migration_is_revision_0004() -> None:
    source = (
        REPO_ROOT / "migrations" / "versions" / "0004_intel_app_grants.py"
    ).read_text()
    assert 'revision: str = "0004_intel_app_grants"' in source
    assert 'down_revision: str | None = "0003_index_generation"' in source
    assert "BYPASSRLS" in source
