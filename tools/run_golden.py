"""Offline golden-set runner: FakeLLM extract/route pipeline (spec 11 §3).

Loads synthetic Etching fixtures, scripts FakeLLM responses, runs the
route then extract handlers, and writes a nooa-bench-style trajectory
JSON list plus a behavior-report placeholder. Never calls a live provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from nooa.llm_types import LLMResponse, LLMUsage
from nooa.unifiedllm import FakeLLMClient

from intel.contracts.models import ExtractionInput, ExtractionProposal, SourceBlock
from intel.nooa_adapter.agents import DocumentDigest, RoutingVerdict
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import InMemoryJobsDatabase, InMemoryJobsStore
from intel.services.jobs import JobService
from intel.services.knowledge import InMemoryKnowledgeStore
from intel.workers.runner import JobRunner
from intel.workflows.extract import ExtractWiring, make_extract_handler
from intel.workflows.route import RouteWiring, make_route_handler

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = REPO_ROOT / "fixtures" / "golden"
DEFAULT_OUT = REPO_ROOT / "traces" / "golden-eval"

GOLDEN_TOPICS = {
    "etching-rf": "RF/射频",
    "etching-materials": "材料选择",
    "etching-oes": "OES 监控算法",
}

MIN_SAMPLES_PER_TOPIC = 5
NAMESPACE = UUID("00000000-0000-4000-8000-000000000015")
OWNER_ID = UUID("00000000-0000-4000-8000-000000000001")
INDUSTRY_ID = UUID("00000000-0000-4000-8000-000000000002")

# 11 §3 leak-stage buckets the full eval will fill; the skeleton reports zeros.
LEAK_STAGES = (
    "source_not_fetched",
    "fetched_not_indexed",
    "recall_miss",
    "route_error",
    "extract_error",
)

# nooa-bench behavior_analyzer.SIGNAL_DESCRIPTIONS keys (schema_version 2).
BEHAVIOR_SIGNALS = (
    "python_cells",
    "self_references",
    "persistent_state_uses",
    "todo_state_uses",
    "todo_creations",
    "todo_activations",
    "todo_comments",
    "delegations",
    "parallel_delegations",
    "shell_commands",
    "shell_argv_commands",
    "repo_queries",
    "user_messages",
    "completion_calls",
    "execution_attempts",
    "execution_errors",
    "text_only_replies",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _llm_response(payload: dict[str, Any]) -> LLMResponse:
    body = json.dumps(payload, ensure_ascii=False)
    return LLMResponse(
        raw_response=None,
        content=body,
        tool_calls=[],
        finish_reason="stop",
        usage=LLMUsage(input_tokens=8, output_tokens=8, total_tokens=16),
    )


class FakeLLMRoutingAgent:
    """RoutingAgentProtocol backed by FakeLLM scripted JSON (no live provider)."""

    def __init__(self, llm: FakeLLMClient) -> None:
        self.llm = llm

    async def describe_document(self, blocks: list[str]) -> DocumentDigest:
        response = await self.llm.acall([{"role": "user", "content": "describe"}])
        return DocumentDigest.model_validate_json(response.content)

    async def judge_industry(
        self, digest: DocumentDigest, profile: str
    ) -> RoutingVerdict:
        response = await self.llm.acall([{"role": "user", "content": "judge"}])
        return RoutingVerdict.model_validate_json(response.content)


class FakeLLMExtractAgent:
    """ExtractionAgentProtocol backed by FakeLLM scripted JSON."""

    def __init__(self, llm: FakeLLMClient) -> None:
        self.llm = llm

    async def extract_claims(self, request: ExtractionInput) -> ExtractionProposal:
        response = await self.llm.acall([{"role": "user", "content": "extract"}])
        return ExtractionProposal.model_validate_json(response.content)


@dataclass
class RouteStoreDouble:
    jobs_db: InMemoryJobsDatabase
    blocks: dict[UUID, list[str]]
    industries: list[dict]
    decisions: list[dict] = field(default_factory=list)

    @property
    def jobs(self):
        return InMemoryJobsStore(self.jobs_db)

    async def get_blocks(self, parse_id: UUID) -> list[str] | None:
        return self.blocks.get(parse_id)

    async def active_industries(self, owner_id: UUID) -> list[dict]:
        return self.industries

    async def insert_processing_decision(
        self, scope, *, parse_id, outcome, reasons, block_references, input_manifest
    ) -> UUID:
        decision_id = uuid4()
        self.decisions.append(
            {
                "id": decision_id,
                "scope": scope,
                "parse_id": parse_id,
                "outcome": outcome,
                "reasons": reasons,
            }
        )
        return decision_id


@dataclass
class ExtractStoreDouble:
    knowledge: InMemoryKnowledgeStore
    blocks: dict[UUID, list[SourceBlock]]
    jobs_db: InMemoryJobsDatabase = field(default_factory=InMemoryJobsDatabase)

    @property
    def jobs(self):
        # Same-transaction queue handle for the event_build spawn.
        return InMemoryJobsStore(self.jobs_db)

    async def get_source_blocks(self, parse_id):
        return self.blocks.get(parse_id)

    async def get_retrieval_scope(self, parse_id):
        return "fulltext"

    async def get_origin_ref(self, parse_id):
        return None


def load_samples(fixtures_root: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for topic in GOLDEN_TOPICS:
        topic_dir = fixtures_root / topic
        if not topic_dir.is_dir():
            raise FileNotFoundError(f"missing golden topic directory: {topic_dir}")
        files = sorted(topic_dir.glob("*.json"))
        if len(files) < MIN_SAMPLES_PER_TOPIC:
            raise ValueError(
                f"{topic} has {len(files)} samples; need ≥{MIN_SAMPLES_PER_TOPIC}"
            )
        for path in files:
            payload = json.loads(path.read_text())
            payload["_path"] = str(path)
            payload["_topic"] = topic
            samples.append(payload)
    return samples


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "prefill": False,
        "synthetic": True,
        **fields,
    }


async def _run_sample(sample: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    parse_id = uuid5(NAMESPACE, sample["id"])
    source_blocks = [SourceBlock.model_validate(block) for block in sample["blocks"]]
    scripted = sample["scripted"]
    llm = FakeLLMClient(
        scripted_responses=[
            _llm_response(scripted["digest"]),
            _llm_response(scripted["verdict"]),
            _llm_response(scripted["extraction"]),
        ]
    )
    jobs_db = InMemoryJobsDatabase()
    service = JobService(clock=_now, rng=random.Random(1))
    route_store = RouteStoreDouble(
        jobs_db=jobs_db,
        blocks={parse_id: [block.text for block in source_blocks]},
        industries=[
            {
                "industry_id": str(INDUSTRY_ID),
                "profile": f"{sample.get('industry_profile', 'etching')} {INDUSTRY_ID}",
            }
        ],
    )
    extract_store = ExtractStoreDouble(
        knowledge=InMemoryKnowledgeStore(),
        blocks={parse_id: source_blocks},
    )

    @asynccontextmanager
    async def open_route(_scope):
        yield route_store

    @asynccontextmanager
    async def open_extract(_scope):
        yield extract_store

    @asynccontextmanager
    async def open_jobs(_scope):
        yield InMemoryJobsStore(jobs_db)

    route_wiring = RouteWiring(
        open_store=open_route,
        agent=FakeLLMRoutingAgent(llm),
        jobs=JobService(clock=_now, rng=random.Random(2)),
    )
    extract_wiring = ExtractWiring(
        open_store=open_extract,
        agent=FakeLLMExtractAgent(llm),
        jobs=JobService(clock=_now, rng=random.Random(3)),
    )
    route_runner = JobRunner(service, open_jobs)
    route_runner.register("route", make_route_handler(route_wiring))
    extract_runner = JobRunner(service, open_jobs)
    extract_runner.register("extract", make_extract_handler(extract_wiring))

    await service.enqueue(
        InMemoryJobsStore(jobs_db),
        IndustryScope(OWNER_ID),
        kind="route",
        payload={"parse_id": str(parse_id)},
        idempotency_key=f"golden-route:{sample['id']}",
    )
    route_result = await route_runner.run_once()
    if route_result is None:
        raise RuntimeError(f"route job did not run for {sample['id']}")

    expected = sample["labels"]["decision"]
    actual = route_store.decisions[0]["outcome"] if route_store.decisions else "missing"
    events = [
        _event(
            "LlmCall",
            sample_id=sample["id"],
            topic=sample["_topic"],
            stage="route",
            function_name="describe_document",
        ),
        _event(
            "LlmCall",
            sample_id=sample["id"],
            topic=sample["_topic"],
            stage="route",
            function_name="judge_industry",
        ),
        _event(
            "RouteStage",
            sample_id=sample["id"],
            topic=sample["_topic"],
            stage="route",
            expected=expected,
            actual=actual,
            job_state=route_result.state,
            leak_stage="route_error" if actual != expected else None,
        ),
    ]

    extract_jobs = list((route_result.progress or {}).get("extract_jobs") or [])
    if extract_jobs:
        extract_result = await extract_runner.run_once()
        if extract_result is None:
            raise RuntimeError(f"extract job did not run for {sample['id']}")
        events.append(
            _event(
                "LlmCall",
                sample_id=sample["id"],
                topic=sample["_topic"],
                stage="extract",
                function_name="extract_claims",
            )
        )
        extract_state = (extract_result.progress or {}).get("state")
        events.append(
            _event(
                "ExtractStage",
                sample_id=sample["id"],
                topic=sample["_topic"],
                stage="extract",
                job_state=extract_result.state,
                extraction_state=extract_state,
                accepted=(extract_result.progress or {}).get("accepted"),
                rejected=(extract_result.progress or {}).get("rejected"),
                leak_stage="extract_error"
                if extract_state == "extraction_failed"
                else None,
            )
        )
    return events, llm.call_count


async def run_golden(
    fixtures_root: Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    fixtures_root = fixtures_root or DEFAULT_FIXTURES
    out_dir = out_dir or DEFAULT_OUT
    samples = load_samples(fixtures_root)
    trajectory: list[dict[str, Any]] = []
    llm_calls = 0
    for sample in samples:
        events, calls = await _run_sample(sample)
        trajectory.extend(events)
        llm_calls += calls

    leak_counts = {stage: 0 for stage in LEAK_STAGES}
    for event in trajectory:
        stage = event.get("leak_stage")
        if stage in leak_counts:
            leak_counts[stage] += 1

    behavior = {
        "task_id": "golden-etching",
        "model": "fake",
        "agent_type": "route-extract",
        "change_id": "baseline",
        "schema_version": 2,
        "content_policy": "aggregate-counts-only",
        "signals": {name: 0 for name in BEHAVIOR_SIGNALS},
        "rates": {
            "self_reference_rate": 0.0,
            "execution_error_rate": 0.0,
            "completion_rate": 0.0,
        },
        # Placeholder: 11 §3 leak-stage attribution is not scored yet.
        "placeholder": True,
        "leak_stage_counts": leak_counts,
        "samples": len(samples),
        "llm_calls": llm_calls,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectory.json").write_text(
        json.dumps(trajectory, ensure_ascii=False, indent=2) + "\n"
    )
    (out_dir / "behavior.json").write_text(
        json.dumps(behavior, ensure_ascii=False, indent=2) + "\n"
    )
    return {
        "samples": len(samples),
        "events": len(trajectory),
        "llm_calls": llm_calls,
        "out_dir": str(out_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    summary = asyncio.run(run_golden(args.fixtures, args.out))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
