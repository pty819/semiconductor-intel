"""Answer workflow handler (kind=answer): packet → draft → verify → commit.

Archive mode per 14 §5/16 §6: the workflow assembles the EvidencePacket
BEFORE the model runs. An empty packet in archive mode short-circuits to
``insufficient_evidence`` with ZERO tool calls (QA-01) — missing
evidence is never a silent web search, and the LLM is not invoked at
all. Online mode routes through the InvestigationAgent's gateway-backed
tools instead (kind=investigate; this handler stays archive-shaped).

The drafted answer is re-verified against the packet: every cited id
must be in ``allowed_citation_ids``; out-of-packet citations are dropped
and the block is marked partial rather than trusted (QA-01 fix loop:
one repair pass, then deletion).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from intel.repositories.base import IndustryScope
from intel.services.research import Mode, build_evidence_packet
from intel.workers.runner import JobHandler, RunContext

MAX_CITATION_REPAIR_PASSES = 1


class AnswerAgentProtocol(Protocol):
    async def compose_answer(
        self, question: str, evidence_blocks: list[dict]
    ) -> Any: ...


#: Assemble the archive materials for one question: claims/events/evidence
#: dicts (already fixed-revision) + coverage. Returns the packet inputs.
PacketSource = Callable[[IndustryScope, str], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class AnswerCommit:
    status: str = "ok"  # "ok" | "insufficient_evidence"
    blocks: list[dict[str, Any]] = field(default_factory=list)
    dropped_citations: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    answer_message_id: UUID | None = None


class AnswerStore(Protocol):
    async def insert_answer_message(
        self,
        scope: IndustryScope,
        *,
        conversation_id: UUID,
        question: str,
        blocks: list[dict[str, Any]],
        status: str,
    ) -> UUID: ...


OpenAnswerTxn = Callable[[IndustryScope], AbstractAsyncContextManager[AnswerStore]]


@dataclass(slots=True)
class AnswerWiring:
    open_store: OpenAnswerTxn
    agent: AnswerAgentProtocol
    packet_source: PacketSource


def _repair_or_drop(
    blocks: list[dict[str, Any]], allowed: frozenset[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only in-packet citations; drop statements left uncited."""
    repaired: list[dict[str, Any]] = []
    dropped: list[str] = []
    for block in blocks:
        citations = [
            str(citation)
            for citation in block.get("citations", [])
            if str(citation) in allowed
        ]
        if citations or not block.get("requires_citation", True):
            repaired.append({**block, "citations": citations})
        else:
            dropped.append(str(block.get("text", "")))
    return repaired, dropped


def make_answer_handler(wiring: AnswerWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        question = str(payload["question"])
        conversation_id = UUID(str(payload["conversation_id"]))

        materials = await wiring.packet_source(ctx.scope, question)
        packet = build_evidence_packet(
            question=question,
            mode=Mode.ARCHIVE,
            claims=materials.get("claims"),
            events=materials.get("events"),
            evidence=materials.get("evidence"),
            coverage=materials.get("coverage"),
            read_blocks=materials.get("read_blocks"),
        )
        await ctx.boundary()

        commit = AnswerCommit(tool_calls=[])  # archive mode: none, ever
        if not packet.allowed_citation_ids:
            # QA-01: no evidence in archive mode → insufficient_evidence,
            # zero tool calls, and the LLM is never invoked.
            commit.status = "insufficient_evidence"
            commit.unresolved = [question]
        else:
            draft = await wiring.agent.compose_answer(
                question,
                [
                    {
                        "id": str(item["id"]),
                        "quote": item.get("exact_quote", ""),
                        "context": item.get("context", ""),
                    }
                    for item in packet.evidence
                ],
            )
            await ctx.boundary()
            blocks = list(getattr(draft, "blocks", []) or [])
            commit.unresolved = list(getattr(draft, "unresolved_questions", []) or [])
            # 引用语义不匹配：one repair pass, then deletion.
            for _ in range(MAX_CITATION_REPAIR_PASSES + 1):
                blocks, dropped = _repair_or_drop(blocks, packet.allowed_citation_ids)
                if not dropped:
                    break
                commit.dropped_citations.extend(dropped)
            commit.blocks = blocks
            commit.status = "ok" if blocks else "insufficient_evidence"

        async with wiring.open_store(ctx.scope) as store:
            commit.answer_message_id = await store.insert_answer_message(
                ctx.scope,
                conversation_id=conversation_id,
                question=question,
                blocks=commit.blocks,
                status=commit.status,
            )

        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "status": commit.status,
                    "blocks": len(commit.blocks),
                    "dropped_citations": commit.dropped_citations,
                    "unresolved": commit.unresolved,
                    "tool_calls": commit.tool_calls,  # QA-01: stays []
                    "answer_message_id": (
                        str(commit.answer_message_id)
                        if commit.answer_message_id
                        else None
                    ),
                },
            )

    return handler
