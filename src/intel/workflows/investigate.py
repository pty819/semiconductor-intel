"""Investigate workflow handler (kind=investigate): online/archive tools.

Archive tools (search_archive/read_evidence/read_document_blocks) are always
granted. ``fetch_public`` / ``search_web`` are attached only when
``payload.online`` is true. Archive mode with an empty EvidencePacket
short-circuits to ``insufficient_evidence`` with ZERO tool calls (QA-01) —
missing evidence is never a silent web search.

LLM calls stay outside transactions; each persist is a short commit
(``ctx.boundary()``). Payload ids are strings (UUID objects crash KIND_SPECS).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from intel.repositories.base import IndustryScope
from intel.services.research import Mode, build_evidence_packet
from intel.workers.runner import JobHandler, RunContext
from intel.workflows.answer import (
    AnswerStore,
    ConversationSource,
    QueryPlannerProtocol,
    resolve_followup_question,
)

PacketSource = Callable[[IndustryScope, str], Awaitable[dict[str, Any]]]
OpenInvestigateTxn = Callable[[IndustryScope], AbstractAsyncContextManager[AnswerStore]]
GatewayFactory = Callable[..., Any]


class InvestigationAgentProtocol(Protocol):
    async def investigate(self, question: str) -> Any: ...


@dataclass(slots=True)
class InvestigateWiring:
    open_store: OpenInvestigateTxn
    agent: InvestigationAgentProtocol
    packet_source: PacketSource | None = None
    gateway_factory: GatewayFactory | None = None
    #: CHAT-01 planner (optional): restates the question against recent
    #: turns before the packet/agent run. Unwired → raw question.
    query_planner: QueryPlannerProtocol | None = None
    conversation_source: ConversationSource | None = None


def _attach_gateway(
    wiring: InvestigateWiring, *, online: bool, ctx: RunContext
) -> None:
    if wiring.gateway_factory is None:
        return
    gateway = wiring.gateway_factory(online=online, job_id=ctx.job.id, scope=ctx.scope)
    attach = getattr(wiring.agent, "attach_gateway", None)
    if callable(attach):
        attach(gateway)


def make_investigate_handler(wiring: InvestigateWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        question = str(payload["question"])
        conversation_id = UUID(str(payload["conversation_id"]))
        pending_message_id = (
            UUID(str(payload["message_id"])) if payload.get("message_id") else None
        )
        online = bool(payload.get("online", False))

        # CHAT-01: the packet and the agent see the RESTATED question —
        # pronouns/shorthand resolved against the conversation, outside
        # any transaction. referenced_ids persist with the answer so the
        # NEXT turn's planner can resolve against them.
        question, referenced_ids = await resolve_followup_question(
            wiring,
            ctx,
            question=question,
            conversation_id=conversation_id,
            pending_message_id=pending_message_id,
        )

        tool_calls: list[str] = []
        status = "ok"
        blocks: list[dict[str, Any]] = []
        unresolved: list[str] = []

        if not online:
            materials = (
                await wiring.packet_source(ctx.scope, question)
                if wiring.packet_source is not None
                else {}
            )
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
            if not packet.allowed_citation_ids:
                status = "insufficient_evidence"
                unresolved = [question]
            else:
                _attach_gateway(wiring, online=False, ctx=ctx)
                draft = await wiring.agent.investigate(question)
                await ctx.boundary()
                blocks = [
                    {
                        "text": str(getattr(draft, "findings", "") or ""),
                        "citations": [
                            str(item)
                            for item in (getattr(draft, "evidence_ids", []) or [])
                        ],
                    }
                ]
                unresolved = list(getattr(draft, "open_questions", []) or [])
                # Archive: never report web tools, even if the agent probed.
                tool_calls = []
        else:
            _attach_gateway(wiring, online=True, ctx=ctx)
            draft = await wiring.agent.investigate(question)
            await ctx.boundary()
            blocks = [
                {
                    "text": str(getattr(draft, "findings", "") or ""),
                    "citations": [
                        str(item) for item in (getattr(draft, "evidence_ids", []) or [])
                    ],
                }
            ]
            unresolved = list(getattr(draft, "open_questions", []) or [])
            tool_calls = list(getattr(wiring.agent, "tool_calls", []) or [])
            status = "ok" if blocks and blocks[0]["text"] else "insufficient_evidence"

        async with wiring.open_store(ctx.scope) as store:
            answer_message_id = await store.insert_answer_message(
                ctx.scope,
                conversation_id=conversation_id,
                question=question,
                blocks=blocks,
                status=status,
                referenced_ids=referenced_ids,
            )

        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "status": status,
                    "blocks": len(blocks),
                    "unresolved": unresolved,
                    "tool_calls": tool_calls,
                    "referenced_ids": referenced_ids,
                    "answer_message_id": str(answer_message_id),
                },
            )

    return handler
