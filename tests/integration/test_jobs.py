"""Integration tests: the durable job queue against real Postgres (07 §2).

JOB-01/02/03 from the task brief, executed with the SqlAlchemyJobsStore
against the migrated schema. Skipped unless ``INTEL_TEST_DATABASE_URL``
points at a maintenance database; the suite creates a dedicated database
and runs alembic head, mirroring tests/integration/test_rls.py.

Two-role pattern (spec 10 §1): claiming runs on the unscoped dispatcher
connection (the table-owning role — the RLS policies are TO intel_app, and
the table owner is not subject to them); business commits run owner-scoped
as intel_app with the GUCs bound. The suite never grants BYPASSRLS.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from intel.db.rls import set_scope
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import JobRecord, SqlAlchemyJobsStore
from intel.services.jobs import JobService, LeaseLost

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("INTEL_TEST_DATABASE_URL"),
        reason="INTEL_TEST_DATABASE_URL not set (needs reachable Postgres)",
    ),
]


async def _create_database(admin_url: str) -> str:
    url = make_url(admin_url)
    dbname = f"intel_jobs_test_{uuid4().hex[:8]}"
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
            await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def _run_migrations(db_url: str) -> None:
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


@pytest.fixture()
async def owner(engine) -> UUID:
    uid = uuid4()
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO users (id, login, password_hash, timezone,"
                " password_version) VALUES (:id, :login, 'x', 'UTC', 1)"
            ),
            {"id": str(uid), "login": f"u-{uid.hex[:8]}"},
        )
    return uid


@asynccontextmanager
async def dispatcher_store(engine) -> AsyncIterator[SqlAlchemyJobsStore]:
    """Unscoped connection: the dispatcher role (table owner, no GUC)."""
    async with engine.connect() as conn, conn.begin():
        yield SqlAlchemyJobsStore(conn)


@asynccontextmanager
async def worker_store(
    engine, owner_id: UUID
) -> AsyncIterator[SqlAlchemyJobsStore]:
    """Owner-scoped connection as intel_app with the RLS GUC bound."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SET ROLE intel_app"))
        await set_scope(conn, owner_id)
        yield SqlAlchemyJobsStore(conn, IndustryScope(owner_id=owner_id))


def service_for() -> JobService:
    """Real clock: lease windows and the DB's now() must agree."""
    return JobService(rng=random.Random(11))


async def _enqueue_owner(
    engine, service: JobService, owner_id: UUID
) -> JobRecord:
    async with dispatcher_store(engine) as store:
        job, created = await service.enqueue(
            store,
            IndustryScope(owner_id=owner_id),
            kind="discover",
            payload={"probe": 1},
            idempotency_key=f"ik-{uuid4().hex[:8]}",
        )
        assert created
        return job


async def _expire_lease(engine, job_id: UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE jobs SET lease_until = now() - interval '1 second'"
                " WHERE id = :id"
            ),
            {"id": str(job_id)},
        )


async def test_job01_concurrent_claims_single_winner(
    engine, owner: UUID
) -> None:
    service = service_for()
    job = await _enqueue_owner(engine, service, owner)

    async def claim_once():
        async with dispatcher_store(engine) as store:
            return await service.claim(store)

    first, second = await asyncio.gather(claim_once(), claim_once())
    winners = [w for w in (first, second) if w is not None]
    assert len(winners) == 1, "SKIP LOCKED must leave exactly one winner"
    winner = winners[0]
    assert winner.id == job.id
    assert winner.state == "running"
    assert winner.attempt == 1
    assert winner.lease_token

    async with engine.connect() as conn, conn.begin():
        row = (
            await conn.execute(
                text("SELECT state, attempt FROM jobs WHERE id = :id"),
                {"id": str(job.id)},
            )
        ).one()
    assert row.state == "running"
    assert row.attempt == 1


async def test_job02_expired_lease_rejects_commit(engine, owner: UUID) -> None:
    service = service_for()
    job = await _enqueue_owner(engine, service, owner)
    async with dispatcher_store(engine) as store:
        claimed = await service.claim(store)
    assert claimed is not None and claimed.id == job.id

    await _expire_lease(engine, job.id)

    async with worker_store(engine, owner) as store:
        with pytest.raises(LeaseLost):
            await service.complete_step(
                store,
                claimed,
                step_key="enumerate",
                input_hash="h1",
                output_ref="obj://1",
            )
        with pytest.raises(LeaseLost):
            await service.finish(store, claimed, state="succeeded")

    async with engine.connect() as conn, conn.begin():
        n = (
            await conn.execute(
                text("SELECT count(*) FROM job_steps WHERE job_id = :id"),
                {"id": str(job.id)},
            )
        ).scalar_one()
        state = (
            await conn.execute(
                text("SELECT state FROM jobs WHERE id = :id"),
                {"id": str(job.id)},
            )
        ).scalar_one()
    assert n == 0, "the stale worker must not write business results"
    assert state == "running"


async def test_job03_rerun_does_not_duplicate_steps(engine, owner: UUID) -> None:
    service = service_for()
    job = await _enqueue_owner(engine, service, owner)

    async with dispatcher_store(engine) as store:
        first = await service.claim(store)
    assert first is not None

    # Step 1 commits durably; the worker then "crashes" (lease expires).
    async with worker_store(engine, owner) as store:
        await service.complete_step(
            store,
            first,
            step_key="enumerate",
            input_hash="h1",
            output_ref="obj://1",
        )
    await _expire_lease(engine, job.id)
    async with dispatcher_store(engine) as store:
        await service.reap_expired(store)  # running → retry_wait
    # Backdate the retry wait (the wall-clock delay elapsed).
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "UPDATE jobs SET available_at = now() - interval '1 second'"
                " WHERE id = :id"
            ),
            {"id": str(job.id)},
        )
    async with dispatcher_store(engine) as store:
        await service.requeue_due(store)
        second = await service.claim(store)
    assert second is not None
    assert second.attempt == 2
    assert second.lease_token != first.lease_token

    # Same step, same input_hash: converges instead of duplicating.
    async with worker_store(engine, owner) as store:
        rerun = await service.complete_step(
            store,
            second,
            step_key="enumerate",
            input_hash="h1",
            output_ref="obj://1",
        )
        assert rerun.state == "succeeded"
        final = await service.finish(store, second, state="succeeded")
    assert final.state == "succeeded"

    async with engine.connect() as conn, conn.begin():
        steps = (
            await conn.execute(
                text(
                    "SELECT step_key, input_hash, state FROM job_steps"
                    " WHERE job_id = :id"
                ),
                {"id": str(job.id)},
            )
        ).all()
        events = (
            await conn.execute(
                text("SELECT seq FROM job_events WHERE job_id = :id ORDER BY seq"),
                {"id": str(job.id)},
            )
        ).scalars().all()
    assert steps == [("enumerate", "h1", "succeeded")]
    assert events == sorted(events) and len(events) == len(set(events))
