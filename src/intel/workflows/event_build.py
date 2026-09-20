"""Event-build workflow handler (kind=event_build): proposals → identity (05 §2).

Reads one extraction run's committed claim revisions, asks the model for
candidate events (proposals only — identity is never the model's job),
then resolves each through the strong-key registry: boolean auto-merge
into an existing event, or a new independent event with an optional
possible_duplicate proposal for weakly-keyed candidates.

Model output is verified, never trusted: the proposal's ``occurred`` time
expression is parsed into a validated TimeValue by deterministic Python
(:func:`intel.domain.time.parse_occurrence_time` — unparseable downgrades
to unknown, never crashes the job), and ``claim_ids`` are filtered against
the run's committed claim set before any link is written.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from intel.domain.time import parse_occurrence_time
from intel.repositories.base import IndustryScope
from intel.services.knowledge import (
    EventProposalInput,
    KnowledgeStore,
    resolve_event,
)
from intel.workers.runner import JobFailure, JobHandler, RunContext


class EventProposalAgentProtocol(Protocol):
    async def propose_events(self, request: object, claims: object) -> object: ...


class EventBuildStore(Protocol):
    async def get_extraction_claims(
        self, extraction_run_id: UUID
    ) -> Sequence[dict] | None: ...

    knowledge: KnowledgeStore


OpenEventBuildTxn = Callable[
    [IndustryScope], AbstractAsyncContextManager[EventBuildStore]
]

#: identity_fields the model fills per event_type (05 §2 registry's input).
_IDENTITY_FIELD_NAMES = (
    "work_id",
    "version",
    "action",
    "release_ref",
    "product",
    "phase",
    "region",
    "attribute",
    "change_ref",
    "effective_window",
    "announcement_ref",
    "counterparty",
    "result_key",
    "conditions",
    "correction_ref",
    "target_ref",
)


@dataclass(slots=True)
class EventBuildWiring:
    open_store: OpenEventBuildTxn
    agent: EventProposalAgentProtocol
    max_proposals: int = 32
    similar_candidate_finder: Callable[[dict], UUID | None] | None = None


def _identity_fields(proposal: object) -> dict[str, str]:
    raw = getattr(proposal, "identity_fields", None) or {}
    if isinstance(raw, dict):
        return {
            name: str(value)
            for name, value in raw.items()
            if name in _IDENTITY_FIELD_NAMES and str(value).strip()
        }
    return {}


def make_event_build_handler(wiring: EventBuildWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        extraction_run_id = UUID(str(payload["extraction_run_id"]))

        async with wiring.open_store(ctx.scope) as store:
            claims = await store.get_extraction_claims(extraction_run_id)
        if not claims:
            raise JobFailure(
                "extraction_missing",
                f"no committed claims for extraction run {extraction_run_id}",
            )
        await ctx.boundary()

        proposals = await wiring.agent.propose_events(
            {"extraction_run_id": str(extraction_run_id)}, list(claims)
        )
        await ctx.boundary()

        resolutions: list[dict] = []
        dropped_claim_references = 0
        manifest = {"extraction_run_id": str(extraction_run_id)}
        committed_claim_ids = {str(row["id"]) for row in claims}
        for proposal in list(getattr(proposals, "proposals", []) or [])[
            : wiring.max_proposals
        ]:
            # Model-proposed claim ids are verified against the run's
            # committed set (fetched above) — hallucinated ids are
            # dropped and counted, never blindly inserted.
            proposal_claim_ids = [
                str(cid) for cid in (getattr(proposal, "claim_ids", None) or [])
            ]
            claim_revision_ids = [
                UUID(cid) for cid in proposal_claim_ids if cid in committed_claim_ids
            ]
            dropped_claim_references += len(proposal_claim_ids) - len(
                claim_revision_ids
            )
            # Model proposes the time expression verbatim; deterministic
            # Python parses + validates it (03 §5 precision, unknown
            # allowed — an unparseable expression is data, not an error).
            occurred = parse_occurrence_time(
                str(getattr(proposal, "occurred", "") or "")
            )
            candidate = (
                wiring.similar_candidate_finder(proposal.__dict__)
                if wiring.similar_candidate_finder is not None
                else None
            )
            async with wiring.open_store(ctx.scope) as store:
                resolution = await resolve_event(
                    store.knowledge,
                    ctx.scope,
                    EventProposalInput(
                        event_type=str(getattr(proposal, "event_type", "other")),
                        title=str(getattr(proposal, "title", "")),
                        summary=str(getattr(proposal, "summary", "")),
                        identity_fields=_identity_fields(proposal),
                        claim_revision_ids=claim_revision_ids,
                        occurred_time=occurred.model_dump(mode="json"),
                    ),
                    input_manifest=manifest,
                    similar_candidate_event_id=candidate,
                )
            resolutions.append(
                {
                    "event_id": str(resolution.event_id),
                    "created": resolution.created,
                    "merged_into_existing": resolution.merged_into_existing,
                    "possible_duplicate": resolution.possible_duplicate_recorded,
                }
            )

        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "events_created": sum(1 for r in resolutions if r["created"]),
                    "events_linked": sum(
                        1 for r in resolutions if r["merged_into_existing"]
                    ),
                    "resolutions": resolutions,
                    "dropped_claim_references": dropped_claim_references,
                },
            )

    return handler
