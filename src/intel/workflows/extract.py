"""Extract workflow handler (kind=extract): proposal → validate → commit (05 §1).

The five steps: blocks in scope → model proposal → deterministic
validation (:mod:`intel.domain.validation` — the model never writes
quotes) → second-pass semantic judgement seam (EVI-02; unwired leaves
``pending`` — literal hit is not semantic support) → ONE transaction
committing claim_revisions + evidence + source_family
(:func:`intel.services.knowledge.commit_extraction`).

All-rejected proposals are DATA, not errors: the job succeeds with
``extraction_failed`` in progress and every rejection reason preserved
(05 §1: 失败时保留 extraction_failed 或 proposal，不能用模型重写 quote).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from intel.contracts.models import ExtractionInput, SourceBlock
from intel.repositories.base import IndustryScope
from intel.repositories.jobs import JobsStore
from intel.services.jobs import JobService, build_idempotency_key
from intel.services.knowledge import KnowledgeStore, commit_extraction
from intel.workers.runner import JobFailure, JobHandler, RunContext

MAX_REJECTIONS_IN_PROGRESS = 50

#: Event identity policy version riding the event_build job's idempotency
#: key (Task 6 KIND_SPECS: industry/extraction_commit/event_policy_version)
#: — a bump re-runs event building for a committed extraction, mirroring
#: EXTRACTION_VERSION on the extract spawn.
EVENT_POLICY_VERSION = "event-policy@1"


class ExtractionAgentProtocol(Protocol):
    async def extract_claims(self, request: ExtractionInput) -> object: ...


#: EVI-02 seam: (claim, quotes) -> "supports" | "refutes" | "uncertain".
SemanticJudge = Callable[[object, list[str]], str]


class ExtractStore(Protocol):
    async def get_source_blocks(self, parse_id: UUID) -> list[SourceBlock] | None: ...

    async def get_retrieval_scope(self, parse_id: UUID) -> str: ...

    async def get_origin_ref(self, parse_id: UUID) -> str | None:
        """Per-document origin reference (parse → capture → document's
        canonical URL) — what source families dedupe on (EVT-01)."""

    knowledge: KnowledgeStore

    #: same-transaction job queue handle (the RouteStore pattern)
    jobs: JobsStore


OpenExtractTxn = Callable[[IndustryScope], AbstractAsyncContextManager[ExtractStore]]


@dataclass(slots=True)
class ExtractWiring:
    open_store: OpenExtractTxn
    agent: ExtractionAgentProtocol
    #: Queue service for the chained event_build spawn (05 §2).
    jobs: JobService = field(default_factory=JobService)
    semantic_judge: SemanticJudge | None = None
    extraction_run_id: UUID | None = None
    origin_ref: str | None = None


def make_extract_handler(wiring: ExtractWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        parse_id = UUID(str(payload["parse_id"]))

        async with wiring.open_store(ctx.scope) as store:
            blocks = await store.get_source_blocks(parse_id)
            retrieval_scope = await store.get_retrieval_scope(parse_id)
            # Origin is per-document data (the document's canonical URL),
            # not a static wiring constant; the wiring value, when set,
            # stays as an explicit override (I-4).
            origin_ref = wiring.origin_ref or await store.get_origin_ref(parse_id)
        if blocks is None:
            raise JobFailure("parse_missing", f"parse {parse_id} gone")
        await ctx.boundary()

        proposal = await wiring.agent.extract_claims(
            ExtractionInput(
                parse_id=parse_id,
                retrieval_scope=retrieval_scope,  # type: ignore[arg-type]
                blocks=blocks,
            )
        )
        await ctx.boundary()

        # Deterministic gates; the model's quotes are verified, never
        # trusted and never rewritten.
        from intel.domain.validation import validate_proposal

        block_map = {block.block_id: block for block in blocks}
        claims = list(getattr(proposal, "claims", []) or [])
        result = validate_proposal(claims, block_map)

        # EVI-02 seam: the judge RETURNS one status per accepted claim;
        # commit_extraction consumes them by index. Nothing mutates the
        # frozen validated DTOs (that path raised FrozenInstanceError).
        semantic_status_by_index: dict[int, str] = {}
        if wiring.semantic_judge is not None:
            for index, validated in enumerate(result.accepted):
                semantic_status_by_index[index] = wiring.semantic_judge(
                    validated.claim,
                    [location.exact_quote for location in validated.locations],
                )

        manifest = {
            "parse_id": str(parse_id),
            "claims_proposed": len(claims),
        }
        extraction_run_id = wiring.extraction_run_id or ctx.job.id
        event_build_job_id: str | None = None
        async with wiring.open_store(ctx.scope) as store:
            commit = await commit_extraction(
                store.knowledge,
                ctx.scope,
                validated_claims=result.accepted,
                rejected=[
                    {"code": r.code, "detail": r.detail} for r in result.rejected
                ],
                parse_id=parse_id,
                extraction_run_id=extraction_run_id,
                input_manifest=manifest,
                origin_ref=origin_ref,
                semantic_status_by_index=semantic_status_by_index,
            )
            if result.accepted:
                # Claims landed: schedule this run's event building in the
                # SAME commit (05 §2). The commit identity in the key is the
                # extraction run — every committed run builds events once;
                # a failed enqueue rolls the whole commit back, so the
                # extract job retries and the spawn converges (07 §3).
                event_job, _created = await wiring.jobs.enqueue(
                    store.jobs,
                    ctx.scope,
                    kind="event_build",
                    payload={"extraction_run_id": str(extraction_run_id)},
                    idempotency_key=build_idempotency_key(
                        "event_build",
                        {
                            "industry": str(ctx.scope.require_industry_id()),
                            "extraction_commit": str(extraction_run_id),
                            "event_policy_version": EVENT_POLICY_VERSION,
                        },
                    ),
                )
                event_build_job_id = str(event_job.id)

        state = "extraction_failed" if result.all_rejected else "ok"
        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "state": state,
                    "accepted": len(result.accepted),
                    "rejected": len(result.rejected),
                    "rejections": [
                        {"code": r.code, "detail": r.detail}
                        for r in result.rejected[:MAX_REJECTIONS_IN_PROGRESS]
                    ],
                    "claim_revision_ids": [
                        str(rid) for rid in commit.claim_revision_ids
                    ],
                    "extraction_run_id": str(extraction_run_id),
                    "event_build_job": event_build_job_id,
                },
            )

    return handler
