"""Route workflow handler (kind=route): one parse × active industries (04 §6).

Per parse, judge every ACTIVE industry of the owner (完整增量语义 — no
top-k pre-selection of documents): describe once (L1), then a verdict
per industry. Outcomes land as processing_decisions rows (03 §3):

- ``direct``    → enqueue the extract job for that industry;
- ``uncertain`` → recorded as the 待判断 entry (human triage list);
- ``background``/``unrelated`` → recorded, nothing spawned.

LLM calls happen OUTSIDE transactions (07 §2); each persistence is one
short worker-role transaction. The agent is injected as a structural
protocol so tests run on fakes and production on
:class:`intel.nooa_adapter.agents.RoutingAgent`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from intel.repositories.base import IndustryScope
from intel.repositories.jobs import JobsStore
from intel.services.jobs import JobService, build_idempotency_key
from intel.workers.runner import JobFailure, JobHandler, RunContext

#: Extraction pipeline version riding the extract job's idempotency key
#: (Task 6 KIND_SPECS: industry/parse/extraction_version) — a bump
#: re-extracts, mirroring the index job's version semantics.
EXTRACTION_VERSION = "extract@1"


class RoutingAgentProtocol(Protocol):
    async def describe_document(self, blocks: list[str]) -> object: ...


class RouteStore(Protocol):
    """What routing needs from storage (one short transaction each)."""

    async def get_blocks(self, parse_id: UUID) -> list[str] | None: ...

    async def active_industries(self, owner_id: UUID) -> Sequence[dict]: ...

    async def insert_processing_decision(
        self,
        scope: IndustryScope,
        *,
        parse_id: UUID,
        outcome: str,
        reasons: list[str],
        block_references: list[dict],
        input_manifest: dict,
    ) -> UUID: ...

    #: same-transaction job queue handle (the IngestTxn pattern)
    jobs: JobsStore


OpenRouteTxn = Callable[[IndustryScope], AbstractAsyncContextManager[RouteStore]]


@dataclass(slots=True)
class RouteWiring:
    open_store: OpenRouteTxn
    agent: RoutingAgentProtocol
    jobs: JobService = field(default_factory=JobService)
    max_verdicts_per_doc: int = 64


def make_route_handler(wiring: RouteWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        parse_id = UUID(str(payload["parse_id"]))

        async with wiring.open_store(ctx.scope) as store:
            blocks = await store.get_blocks(parse_id)
        if blocks is None:
            raise JobFailure("parse_missing", f"parse {parse_id} gone")
        async with wiring.open_store(ctx.scope) as store:
            industries = await store.active_industries(ctx.job.owner_id)
        await ctx.boundary()

        digest = await wiring.agent.describe_document(blocks)
        await ctx.boundary()

        spawned: list[str] = []
        for industry in industries[: wiring.max_verdicts_per_doc]:
            verdict = await wiring.agent.judge_industry(
                digest, industry.get("profile", "")
            )
            decision = getattr(verdict, "decision", "uncertain")
            reasons = list(getattr(verdict, "reasons", []) or [])
            block_refs = [
                {"block_id": ref.block_id, "quote": ref.quote}
                for ref in (getattr(verdict, "block_references", None) or [])
            ]
            scope = IndustryScope(ctx.job.owner_id, UUID(str(industry["industry_id"])))
            async with wiring.open_store(scope) as store:
                await store.insert_processing_decision(
                    scope,
                    parse_id=parse_id,
                    outcome=decision,
                    reasons=reasons,
                    block_references=block_refs,
                    input_manifest={
                        "parse_id": str(parse_id),
                        "industry_id": industry["industry_id"],
                    },
                )
                if decision == "direct":
                    job, _created = await wiring.jobs.enqueue(
                        store.jobs,
                        scope,
                        kind="extract",
                        payload={
                            "parse_id": str(parse_id),
                            "industry_id": str(industry["industry_id"]),
                        },
                        idempotency_key=build_idempotency_key(
                            "extract",
                            {
                                "industry": str(industry["industry_id"]),
                                "parse": str(parse_id),
                                "extraction_version": EXTRACTION_VERSION,
                            },
                        ),
                    )
                    spawned.append(str(job.id))
        await ctx.boundary()

        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "industries_judged": len(industries),
                    "extract_jobs": spawned,
                },
            )

    return handler
