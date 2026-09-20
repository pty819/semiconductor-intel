"""Integration tests: row-level security isolation (ISO-03 gate).

Requires a reachable Postgres with the pgvector extension available (the
deployment image ships it, spec 10 §4). Skipped unless
``INTEL_TEST_DATABASE_URL`` is set. The URL points at a *maintenance*
database (any existing DB on the server, e.g. ``.../postgres``); the suite
creates a dedicated database, runs ``alembic upgrade head`` against it,
grants the ``intel_app`` role the privileges the real grants migration
(Task 17) will carry, exercises the policies, and drops the database.

What must hold (spec 03 §1/§8, 10 §1):
- With ``app.owner_id = A`` set, SELECTs never return another owner's rows.
- Cross-owner INSERTs are rejected by WITH CHECK (RLS violation).
- A write path without the owner GUC fails closed — both at the DB layer
  (policy expression is NULL → deny) and via ``require_owner_guc`` raising
  ``ScopeMissing``.
- I tables additionally scope on ``app.industry_id``.
- Transaction-local GUCs do not leak across transactions (pool safety).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from intel.db.rls import ScopeMissing, require_owner_guc, set_scope

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("INTEL_TEST_DATABASE_URL"),
        reason="INTEL_TEST_DATABASE_URL not set (needs reachable Postgres)",
    ),
]


async def _create_database(admin_url: str) -> str:
    """Create a dedicated test database next to the maintenance one."""
    url = make_url(admin_url)
    dbname = f"intel_rls_test_{uuid4().hex[:8]}"
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text(f'CREATE DATABASE "{dbname}" TEMPLATE template1')
            )
    finally:
        await engine.dispose()
    return str(url.set(database=dbname))


async def _drop_database(admin_url: str, dbname: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # WITH (FORCE) needs PG >= 13.
            await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def _run_migrations(db_url: str) -> None:
    """Run alembic upgrade head against the fresh database.

    Alembic's async template calls asyncio.run() internally, so this must
    run in a thread without a running loop.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="module")
async def db_url() -> str:
    admin_url = os.environ["INTEL_TEST_DATABASE_URL"]
    url = await _create_database(admin_url)
    try:
        await asyncio.to_thread(_run_migrations, url)
        yield url
    finally:
        await _drop_database(admin_url, make_url(url).database)


@pytest.fixture(scope="module")
async def engine(db_url: str):
    engine = create_async_engine(db_url, poolclass=NullPool)
    # Test-only stand-in for the Task 17 grants migration: give the app role
    # exactly the DML it needs to exercise the policies. The migration
    # itself deliberately ships only the role stub.
    async with engine.begin() as conn:
        await conn.execute(text("GRANT USAGE ON SCHEMA public TO intel_app"))
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
                " public TO intel_app"
            )
        )
    yield engine
    await engine.dispose()


@pytest.fixture(scope="module")
async def seed(engine) -> dict[str, str]:
    """Owner A with two industries (one topic each), owner B empty."""
    owner_a, owner_b = uuid4(), uuid4()
    ind_1, ind_2 = uuid4(), uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SET ROLE intel_app"))
        for uid, login in ((owner_a, f"a-{owner_a.hex[:8]}"),
                           (owner_b, f"b-{owner_b.hex[:8]}")):
            await conn.execute(
                text(
                    "INSERT INTO users (id, login, password_hash, timezone,"
                    " password_version)"
                    " VALUES (:id, :login, 'x', 'Asia/Shanghai', 1)"
                ),
                {"id": str(uid), "login": login},
            )
        # Owner A's industries; owner B owns nothing.
        await set_scope(conn, owner_a)
        for iid in (ind_1, ind_2):
            await conn.execute(
                text(
                    "INSERT INTO industries (id, owner_id, name, status)"
                    " VALUES (:id, :owner, :name, 'draft')"
                ),
                {"id": str(iid), "owner": str(owner_a), "name": f"ind-{iid.hex[:6]}"},
            )
            await set_scope(conn, owner_a, iid)
            await conn.execute(
                text(
                    "INSERT INTO topics (id, owner_id, industry_id, name,"
                    " status) VALUES (:id, :owner, :industry, :name, 'active')"
                ),
                {
                    "id": str(uuid4()),
                    "owner": str(owner_a),
                    "industry": str(iid),
                    "name": f"topic-{iid.hex[:6]}",
                },
            )
    return {
        "owner_a": str(owner_a),
        "owner_b": str(owner_b),
        "industry_1": str(ind_1),
        "industry_2": str(ind_2),
    }


async def test_owner_a_select_returns_zero_rows_for_owner_b(
    engine, seed: dict[str, str]
) -> None:
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, seed["owner_b"])
            n_industries = (
                await conn.execute(text("SELECT count(*) FROM industries"))
            ).scalar_one()
            n_topics = (
                await conn.execute(text("SELECT count(*) FROM topics"))
            ).scalar_one()
        assert n_industries == 0, "owner B must not see owner A's industries"
        assert n_topics == 0, "owner B must not see owner A's topics"

        async with conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, seed["owner_a"])
            n_industries = (
                await conn.execute(text("SELECT count(*) FROM industries"))
            ).scalar_one()
        assert n_industries == 2


async def test_cross_owner_insert_is_rejected(engine, seed: dict[str, str]) -> None:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="row-level security"):
            async with conn.begin():
                await conn.execute(text("SET ROLE intel_app"))
                await set_scope(conn, seed["owner_a"])
                # Try to create a row owned by B while scoped to A.
                await conn.execute(
                    text(
                        "INSERT INTO industries (id, owner_id, name, status)"
                        " VALUES (:id, :owner, 'smuggled', 'draft')"
                    ),
                    {"id": str(uuid4()), "owner": seed["owner_b"]},
                )
        # Nothing was actually written.
        async with conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, seed["owner_a"])
            smuggled = (
                await conn.execute(
                    text("SELECT count(*) FROM industries WHERE name = 'smuggled'")
                )
            ).scalar_one()
        assert smuggled == 0


async def test_missing_owner_guc_fails_closed_on_write(
    engine, seed: dict[str, str]
) -> None:
    async with engine.connect() as conn:
        # DB layer: WITH CHECK over a NULL GUC denies the insert.
        with pytest.raises(DBAPIError, match="row-level security"):
            async with conn.begin():
                await conn.execute(text("SET ROLE intel_app"))
                await conn.execute(
                    text(
                        "INSERT INTO industries (id, owner_id, name, status)"
                        " VALUES (:id, :owner, 'orphan', 'draft')"
                    ),
                    {"id": str(uuid4()), "owner": seed["owner_a"]},
                )
        # Application guard: write paths call require_owner_guc first.
        with pytest.raises(ScopeMissing):
            async with conn.begin():
                await require_owner_guc(conn)


async def test_industry_guc_scopes_i_tables(engine, seed: dict[str, str]) -> None:
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, seed["owner_a"], seed["industry_1"])
            in_scope = (
                await conn.execute(text("SELECT count(*) FROM topics"))
            ).scalar_one()
        assert in_scope == 1

        async with conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, seed["owner_a"], seed["industry_2"])
            other = (
                await conn.execute(text("SELECT count(*) FROM topics"))
            ).scalar_one()
        assert other == 0, "industry GUC must hide other industries' topics"

        # Writing into industry 1 while scoped to industry 2 is rejected.
        with pytest.raises(DBAPIError, match="row-level security"):
            async with conn.begin():
                await conn.execute(text("SET ROLE intel_app"))
                await set_scope(conn, seed["owner_a"], seed["industry_2"])
                await conn.execute(
                    text(
                        "INSERT INTO topics (id, owner_id, industry_id, name,"
                        " status)"
                        " VALUES (:id, :owner, :industry, 'wrong-scope',"
                        " 'active')"
                    ),
                    {
                        "id": str(uuid4()),
                        "owner": seed["owner_a"],
                        "industry": seed["industry_1"],
                    },
                )


async def test_local_guc_does_not_leak_across_transactions(
    engine, seed: dict[str, str]
) -> None:
    """Pool safety (spec 10 §1): after A's transaction ends, the next
    transaction on the same connection has no GUC — B never inherits it."""
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET ROLE intel_app"))
            await set_scope(conn, seed["owner_a"])
            assert await require_owner_guc(conn) == seed["owner_a"]
        async with conn.begin():
            value = (
                await conn.execute(
                    text("SELECT current_setting('app.owner_id', true)")
                )
            ).scalar_one()
            assert value is None
            with pytest.raises(ScopeMissing):
                await require_owner_guc(conn)
