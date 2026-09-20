"""Per-job tracing sessions and generation_runs provenance (doc 15 §2/§3).

Production shape (doc 06 §7): the job process explicitly enables tracing
with a JSONL exporter under a per-job directory, opens a session scope
named after the job, and lets the session end flush that session's
spans. The exporter is configured ONCE per process (doc 06 §7: 进程级
exporter 不在并发请求中反复切目录) — one job per process keeps that
trivially true. Business audit lives in the database; traces are the
"how", not the "why", record.

generation_runs (doc 15 §3) is the displayable step layer on top of
model_runs; these helpers write the rows plus the producer/verifier
linkage (generation_run_models, output_generations.role).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from intel.db.models.generation import GenerationRun, GenerationRunModel
from intel.db.models.jobs import ModelRun
from intel.db.rls import set_scope
from intel.repositories.base import IndustryScope

__all__ = [
    "GenerationRunRecord",
    "ModelRunRecord",
    "job_trace_session",
    "link_generation_run_models",
    "record_generation_run",
    "record_model_run",
]


@contextmanager
def job_trace_session(job_id: UUID, trace_dir: str | Path):
    """Enable JSONL tracing and scope the session to one job (15 §2).

    Session id is ``job-{job_id}`` so the exporter routes spans to
    ``{trace_dir}/job-{job_id}.jsonl`` and the trace_session_id recorded
    on generation_runs/model_runs rows matches the file on disk.
    """
    from nooa.tracing import enable_tracing, exporters, session_scope

    enable_tracing(exporters=[exporters.jsonl(trace_dir)])
    with session_scope(f"job-{job_id}"):
        yield


def trace_session_id_for(job_id: UUID) -> str:
    return f"job-{job_id}"


@dataclass(slots=True)
class ModelRunRecord:
    """One model_runs row (spec 03 §7 / 15 §1): usage 缺失为 null, 不是 0."""

    job_id: UUID
    step_key: str
    model_route: str
    prompt_version: str
    settings_hash: str
    input_manifest: dict
    result_status: str
    nooa_session_ref: str | None = None
    trace_id: str | None = None
    provider_model: str | None = None
    usage: dict | None = None
    industry_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def params(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": None,  # filled by the writer from scope
            "industry_id": self.industry_id,
            "job_id": self.job_id,
            "step_key": self.step_key,
            "nooa_session_ref": self.nooa_session_ref,
            "trace_id": self.trace_id,
            "model_route": self.model_route,
            "provider_model": self.provider_model,
            "nooa_commit": _nooa_commit(),
            "prompt_version": self.prompt_version,
            "settings_hash": self.settings_hash,
            "input_manifest": self.input_manifest,
            "usage": self.usage,
            "result_status": self.result_status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(slots=True)
class GenerationRunRecord:
    """One generation_runs row (doc 15 §3): the displayable step."""

    job_id: UUID
    step_key: str
    trace_session_id: str
    prompt_version: str
    config_hash: str
    input_manifest: dict
    attempt: int = 1
    trace_state: str = "recording"
    viewer_import_state: str = "not_requested"
    trace_id: str | None = None
    root_span_id: str | None = None
    step_span_id: str | None = None
    artifact_ref: str | None = None
    model_route_display: str | None = None
    error_code: str | None = None
    id: UUID = field(default_factory=uuid4)

    def params(self, *, owner_id: UUID, industry_id: UUID) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": owner_id,
            "industry_id": industry_id,
            "job_id": self.job_id,
            "attempt": self.attempt,
            "step_key": self.step_key,
            "trace_session_id": self.trace_session_id,
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "step_span_id": self.step_span_id,
            "artifact_ref": self.artifact_ref,
            "trace_state": self.trace_state,
            "viewer_import_state": self.viewer_import_state,
            "model_route_display": self.model_route_display,
            "nooa_commit": _nooa_commit(),
            "prompt_version": self.prompt_version,
            "config_hash": self.config_hash,
            "input_manifest": self.input_manifest,
            "error_code": self.error_code,
        }


def _nooa_commit() -> str:
    """The pinned NOOA commit recorded on every provenance row (15 §3)."""
    from importlib.metadata import metadata

    try:
        return str(metadata("nooa")["Version"] or "unknown")
    except Exception:  # noqa: BLE001  # pragma: no cover - editable dev checkouts
        return "unknown"


async def record_model_run(
    conn: AsyncConnection, scope: IndustryScope, record: ModelRunRecord
) -> UUID:
    """Insert one model_runs row (O-scope table: owner GUC bound)."""
    await set_scope(conn, scope.owner_id, scope.industry_id)
    params = record.params()
    params["owner_id"] = scope.owner_id
    await conn.execute(
        pg_insert(ModelRun).values(
            id=bindparam("id"),
            owner_id=bindparam("owner_id"),
            industry_id=bindparam("industry_id"),
            job_id=bindparam("job_id"),
            step_key=bindparam("step_key"),
            nooa_session_ref=bindparam("nooa_session_ref"),
            trace_id=bindparam("trace_id"),
            model_route=bindparam("model_route"),
            provider_model=bindparam("provider_model"),
            nooa_commit=bindparam("nooa_commit"),
            prompt_version=bindparam("prompt_version"),
            settings_hash=bindparam("settings_hash"),
            input_manifest=bindparam("input_manifest"),
            usage=bindparam("usage"),
            result_status=bindparam("result_status"),
            started_at=bindparam("started_at"),
            finished_at=bindparam("finished_at"),
        ),
        params,
    )
    return record.id


async def record_generation_run(
    conn: AsyncConnection,
    scope: IndustryScope,
    record: GenerationRunRecord,
) -> UUID:
    """Insert one generation_runs row (I-scope: owner+industry required)."""
    if scope.industry_id is None:
        raise ValueError("generation_runs requires an industry scope")
    await set_scope(conn, scope.owner_id, scope.industry_id)
    await conn.execute(
        pg_insert(GenerationRun).values(
            **record.params(owner_id=scope.owner_id, industry_id=scope.industry_id)
        )
    )
    return record.id


async def link_generation_run_models(
    conn: AsyncConnection,
    scope: IndustryScope,
    *,
    generation_run_id: UUID,
    model_run_ids: list[UUID],
    attempt: int,
) -> int:
    """Link a step's model calls/retries (doc 15 §3: keep old attempts).

    INSERT ... DO NOTHING on the composite PK: re-linking after an
    idempotent job retry stays safe.
    """
    if scope.industry_id is None:
        raise ValueError("generation_run_models requires an industry scope")
    await set_scope(conn, scope.owner_id, scope.industry_id)
    stmt = (
        pg_insert(GenerationRunModel)
        .values(
            owner_id=scope.owner_id,
            industry_id=scope.industry_id,
            generation_run_id=generation_run_id,
            model_run_id=bindparam("model_run_id"),
            attempt=attempt,
        )
        .on_conflict_do_nothing()
    )
    for model_run_id in model_run_ids:
        await conn.execute(stmt, {"model_run_id": model_run_id})
    return len(model_run_ids)
