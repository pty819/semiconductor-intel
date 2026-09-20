"""Unit tests: exact SQL shapes for claim and fencing (spec 07 §2).

The statement builders in ``intel.repositories.jobs`` are compiled against
the PostgreSQL dialect without touching a database; the tests assert the
load-bearing fragments:

- claim: ``FOR UPDATE OF jobs SKIP LOCKED`` + ``LIMIT`` (领取互斥)
- every step/terminal commit: ``lease_token = :... AND lease_until > now()``
  (fencing — 旧 worker 失去 lease 后无法写业务结果)
- steps: ``ON CONFLICT (job_id, step_key, input_hash) DO NOTHING``
  (idempotent commit, JOB-03)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from intel.repositories.jobs import (
    claim_select_stmt,
    event_seq_select_stmt,
    fenced_update_stmt,
    step_upsert_stmt,
)
from intel.workers.scheduler import due_feeds_stmt

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _sql(stmt) -> str:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled)


def test_claim_select_uses_for_update_skip_locked_limit_1() -> None:
    sql = _sql(claim_select_stmt(NOW))
    assert "FOR UPDATE OF jobs SKIP LOCKED" in sql
    assert "LIMIT" in sql
    assert "FROM jobs" in sql
    assert "jobs.state =" in sql
    assert "jobs.available_at <=" in sql


def test_fenced_update_carries_lease_predicates() -> None:
    sql = _sql(
        fenced_update_stmt(
            job_id=uuid4(),
            owner_id=uuid4(),
            lease_token="tok",
            now=NOW,
            values={"state": "succeeded", "output_ref": "obj://x"},
        )
    )
    assert "UPDATE jobs" in sql
    assert "lease_token =" in sql
    assert "lease_until > now()" in sql
    assert "jobs.id =" in sql
    assert "jobs.owner_id =" in sql
    # The mutation itself is parameterized.
    assert "state" in sql and "output_ref" in sql


def test_step_upsert_conflicts_on_job_step_input_hash() -> None:
    sql = _sql(step_upsert_stmt())
    assert "INSERT INTO job_steps" in sql
    # The constraint named in the DDL (uq_job_steps_step_input) is exactly
    # UNIQUE(job_id, step_key, input_hash).
    assert "ON CONFLICT ON CONSTRAINT uq_job_steps_step_input DO NOTHING" in sql


def test_event_seq_select_allocates_max_plus_one() -> None:
    sql = _sql(event_seq_select_stmt(uuid4()))
    assert "coalesce(max(job_events.seq), 0) + 1" in sql.lower()
    assert "FROM job_events" in sql


def test_due_feeds_requires_user_enabled_or_active_subscription() -> None:
    """spec 04 §2/§7: a due feed is polled only when user_enabled OR some
    active subscription in an active industry uses it."""
    sql = _sql(due_feeds_stmt(NOW))
    assert "user_enabled" in sql
    assert "EXISTS" in sql
    assert "industry_sources" in sql
    assert "next_poll_at <=" in sql
    assert "owner_feeds.status =" in sql
