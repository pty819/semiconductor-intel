"""Unit tests: report_build workflow (citation coverage; stale as data)."""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService
from intel.workers.runner import JobRunner
from intel.workflows.report import ReportWiring, make_report_handler

OWNER = uuid4()
INDUSTRY = uuid4()


@dataclass
class Draft:
    title: str
    sections: list[dict] = field(default_factory=list)


class RecordingAgent:
    def __init__(self, draft: Draft) -> None:
        self.draft = draft
        self.calls = 0

    async def compose_report(self, brief: str, materials: list[dict]) -> Draft:
        self.calls += 1
        return self.draft


@dataclass
class ReportStoreDouble:
    reports: list[dict] = field(default_factory=list)

    async def persist_report(
        self,
        scope,
        *,
        report_type: str,
        title: str,
        period: str,
        composition,
        input_manifest: dict,
    ):
        report_id = uuid4()
        self.reports.append(
            {
                "id": report_id,
                "type": report_type,
                "title": title,
                "period": period,
                "publishable": composition.publishable,
                "citation_coverage": composition.citation_coverage,
                "unsupported_statements": list(composition.unsupported_statements),
                "stale": composition.stale,
                "stale_reason": composition.stale_reason,
                "sections": composition.sections,
                "input_manifest": input_manifest,
            }
        )
        return report_id


async def _run(wiring, payload):
    db = InMemoryJobsDatabase()
    service = JobService(clock=lambda: datetime.now(UTC), rng=random.Random(1))

    @asynccontextmanager
    async def open_jobs(scope):
        yield InMemoryJobsStore(db)

    runner = JobRunner(service, open_jobs)
    runner.register("report_build", make_report_handler(wiring))
    await service.enqueue(
        InMemoryJobsStore(db),
        IndustryScope(OWNER, INDUSTRY),
        kind="report_build",
        payload=payload,
        idempotency_key=(
            f"report_build:{payload['industry']}/{payload['report_type']}"
            f"/{payload['period']}/{payload['input_manifest_hash']}"
        ),
    )
    return await runner.run_once(), wiring


def _wiring(store, agent, materials):
    async def packet_source(scope, question):
        return materials

    @asynccontextmanager
    async def open(scope):
        yield store

    return ReportWiring(open_store=open, agent=agent, packet_source=packet_source)


MATERIALS = {
    "evidence": [{"id": "ev-1", "exact_quote": "引文"}],
    "read_blocks": ["doc-1#b001"],
}


class TestReportBuild:
    async def test_uncited_statements_not_publishable(self):
        store = ReportStoreDouble()
        agent = RecordingAgent(
            Draft(
                title="日报",
                sections=[
                    {
                        "title": "进展",
                        "statements": [
                            {"text": "有据陈述", "citations": ["ev-1"]},
                            {"text": "无据断言", "citations": []},
                        ],
                    }
                ],
            )
        )
        result, _wiring_out = await _run(
            _wiring(store, agent, MATERIALS),
            {
                "industry": str(INDUSTRY),
                "report_type": "daily",
                "period": "2026-09-20",
                "input_manifest_hash": "abc",
                "title": "日报",
            },
        )
        assert result.state == "succeeded"
        saved = store.reports[0]
        assert saved["publishable"] is False
        assert saved["citation_coverage"] == 0.5
        assert saved["unsupported_statements"] == ["无据断言"]
        assert result.progress["publishable"] is False
        assert agent.calls == 1

    async def test_full_coverage_is_publishable_and_stale_is_data(self):
        store = ReportStoreDouble()
        agent = RecordingAgent(
            Draft(
                title="专题",
                sections=[
                    {
                        "title": "进展",
                        "statements": [{"text": "有据", "citations": ["ev-1"]}],
                    }
                ],
            )
        )
        result, _ = await _run(
            _wiring(store, agent, MATERIALS),
            {
                "industry": str(INDUSTRY),
                "report_type": "topic",
                "period": "2026-09",
                "input_manifest_hash": "def",
                "title": "专题",
                "stale": True,
                "stale_reason": "new material after as_of",
            },
        )
        saved = store.reports[0]
        assert saved["publishable"] is True
        assert saved["stale"] is True
        assert saved["stale_reason"]
        assert result.progress["stale"] is True
        assert isinstance(result.input["industry"], str)
