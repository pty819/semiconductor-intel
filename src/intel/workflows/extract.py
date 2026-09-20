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
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from intel.contracts.models import ExtractionInput, SourceBlock
from intel.repositories.base import IndustryScope
from intel.services.knowledge import KnowledgeStore, commit_extraction
from intel.workers.runner import JobFailure, JobHandler, RunContext

MAX_REJECTIONS_IN_PROGRESS = 50


class ExtractionAgentProtocol(Protocol):
    async def extract_claims(self, request: ExtractionInput) -> object: ...


#: EVI-02 seam: (claim, quotes) -> "supports" | "refutes" | "uncertain".
SemanticJudge = Callable[[object, list[str]], str]


class ExtractStore(Protocol):
    async def get_source_blocks(self, parse_id: UUID) -> list[SourceBlock] | None: ...

    async def get_retrieval_scope(self, parse_id: UUID) -> str: ...

    knowledge: KnowledgeStore


OpenExtractTxn = Callable[[IndustryScope], AbstractAsyncContextManager[ExtractStore]]


@dataclass(slots=True)
class ExtractWiring:
    open_store: OpenExtractTxn
    agent: ExtractionAgentProtocol
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

        if wiring.semantic_judge is not None:
            for validated in result.accepted:
                status = wiring.semantic_judge(
                    validated.claim,
                    [location.exact_quote for location in validated.locations],
                )
                for location in validated.locations:
                    location_verdict = status
                    location.semantic = location_verdict

        manifest = {
            "parse_id": str(parse_id),
            "claims_proposed": len(claims),
        }
        extraction_run_id = wiring.extraction_run_id or ctx.job.id
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
                origin_ref=wiring.origin_ref,
            )

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
                },
            )

    return handler
