"""Route workflow handler (kind=route): one parse × active industries (04 §6).

Per parse, judge every ACTIVE industry of the owner (完整增量语义 — no
top-k pre-selection of documents, no silent per-document cap: hitting the
wiring's verdict ceiling raises instead of quietly skipping the tail) and
every ACTIVE topic of a ``direct`` industry (04 §6 A topic association,
output-coverage validated per 14 §3: each active topic gets a verdict or
an explicit fallback). Outcomes land as processing_decisions rows (03 §3):

- ``direct``    → enqueue the extract job for that industry;
- ``uncertain`` → recorded as the 待判断 entry (human triage list);
- ``background``/``unrelated`` → recorded, nothing spawned.

The model's block citations are verified before anything persists: a
verdict's ``block_references`` survive only when the block id exists in
the parse AND the quote is an exact substring of that block — the rest
are dropped and annotated, because the judge sees the digest, not the
store (模型产候选、业务代码验证提交).

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

from intel.contracts.models import SourceBlock
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

    async def judge_industry(self, digest: object, industry_profile: str) -> object: ...

    async def judge_topics(self, digest: object, topics: list[str]) -> object: ...


class RouteStore(Protocol):
    """What routing needs from storage (one short transaction each)."""

    async def get_blocks(self, parse_id: UUID) -> list[SourceBlock] | None: ...

    async def active_industries(self, owner_id: UUID) -> Sequence[dict]: ...

    async def active_topics(self, industry_id: UUID) -> Sequence[dict]: ...

    async def insert_processing_decision(
        self,
        scope: IndustryScope,
        *,
        parse_id: UUID,
        outcome: str,
        reasons: list[str],
        block_references: list[dict],
        input_manifest: dict,
        topic_verdicts: list[dict] | None = None,
    ) -> UUID: ...

    #: same-transaction job queue handle (the IngestTxn pattern)
    jobs: JobsStore


OpenRouteTxn = Callable[[IndustryScope], AbstractAsyncContextManager[RouteStore]]


@dataclass(slots=True)
class RouteWiring:
    open_store: OpenRouteTxn
    agent: RoutingAgentProtocol
    jobs: JobService = field(default_factory=JobService)
    #: Safety ceiling that RAISES when hit (04 §6 所有候选行业都处理 — a
    #: silent truncation would leave industries unjudged while progress
    #: claimed otherwise).
    max_verdicts_per_doc: int = 1024


def _digest_input(blocks: Sequence[SourceBlock]) -> list[str]:
    """Id-bearing block text: the digest step sees which block id said
    what, so downstream citations can name real ids."""
    return [f"[{block.block_id}] {block.text}" for block in blocks]


def _verified_block_references(
    raw_references: Sequence[object], block_texts: dict[str, str]
) -> tuple[list[dict], list[dict]]:
    """Keep only references resolving to a real block id + exact substring
    of that block's text; return (kept, dropped-with-reason)."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for reference in raw_references:
        block_id = str(getattr(reference, "block_id", "") or "")
        quote = str(getattr(reference, "quote", "") or "")
        text = block_texts.get(block_id)
        if text is not None and quote and quote in text:
            kept.append({"block_id": block_id, "quote": quote})
        else:
            dropped.append(
                {
                    "block_id": block_id,
                    "reason": ("block_missing" if text is None else "quote_not_found"),
                }
            )
    return kept, dropped


def _covered_topic_verdicts(
    raw_verdicts: Sequence[object], active_topics: Sequence[dict]
) -> list[dict]:
    """Output-coverage validation (14 §3): every ACTIVE topic appears —
    the model's verdict when given, an explicit fallback otherwise — and
    verdicts for ids outside the active set are dropped."""
    by_topic: dict[str, dict] = {}
    for verdict in raw_verdicts:
        topic_id = str(getattr(verdict, "topic_id", "") or "")
        by_topic[topic_id] = {
            "topic_id": topic_id,
            "relevant": bool(getattr(verdict, "relevant", False)),
            "reason": str(getattr(verdict, "reason", "") or ""),
        }
    out: list[dict] = []
    for topic in active_topics:
        topic_id = str(topic["topic_id"])
        verdict = by_topic.get(topic_id)
        out.append(
            verdict
            if verdict is not None
            else {
                "topic_id": topic_id,
                "relevant": False,
                "reason": "fallback:no_verdict",
            }
        )
    return out


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
        if len(industries) > wiring.max_verdicts_per_doc:
            # 完整增量语义: truncating would silently leave industries
            # unjudged — fail loudly instead (04 §6).
            raise JobFailure(
                "too_many_industries",
                f"{len(industries)} active industries exceed the verdict"
                f" ceiling {wiring.max_verdicts_per_doc}; refusing to"
                " truncate",
            )
        await ctx.boundary()

        block_texts = {block.block_id: block.text for block in blocks}
        digest = await wiring.agent.describe_document(_digest_input(blocks))
        await ctx.boundary()

        spawned: list[str] = []
        judged = 0
        topics_judged = 0
        for industry in industries:
            verdict = await wiring.agent.judge_industry(
                digest, industry.get("profile", "")
            )
            decision = getattr(verdict, "decision", "uncertain")
            reasons = list(getattr(verdict, "reasons", []) or [])
            block_refs, dropped_refs = _verified_block_references(
                getattr(verdict, "block_references", None) or [], block_texts
            )
            scope = IndustryScope(ctx.job.owner_id, UUID(str(industry["industry_id"])))
            topic_verdicts: list[dict] | None = None
            if decision == "direct":
                async with wiring.open_store(scope) as store:
                    active_topics = await store.active_topics(
                        UUID(str(industry["industry_id"]))
                    )
                topics = [
                    f"{topic['topic_id']} {topic.get('name', '')}".strip()
                    for topic in active_topics
                ]
                if topics:
                    topics_verdict = await wiring.agent.judge_topics(digest, topics)
                    topic_verdicts = _covered_topic_verdicts(
                        getattr(topics_verdict, "verdicts", None) or [],
                        active_topics,
                    )
                    topics_judged += len(topic_verdicts)
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
                        "dropped_block_references": dropped_refs,
                    },
                    topic_verdicts=topic_verdicts,
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
            judged += 1
        await ctx.boundary()

        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "industries_judged": judged,
                    "topics_judged": topics_judged,
                    "extract_jobs": spawned,
                },
            )

    return handler
