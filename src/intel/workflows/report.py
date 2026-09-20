"""Report-build workflow handler (kind=report_build).

Citation coverage is validated with :func:`validate_citation_coverage`.
coverage != 1.0 is DATA: the revision is persisted with
``unsupported_statements`` and is not publishable. Stale is a banner on
the snapshot, never a mutation of prior revisions (05 §8).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from intel.repositories.base import IndustryScope
from intel.services.reports import ReportComposition, validate_citation_coverage
from intel.services.research import Mode, build_evidence_packet
from intel.workers.runner import JobHandler, RunContext

PacketSource = Callable[[IndustryScope, str], Awaitable[dict[str, Any]]]


class ReportAgentProtocol(Protocol):
    async def compose_report(self, brief: str, materials: list[dict]) -> Any: ...


class ReportStore(Protocol):
    async def persist_report(
        self,
        scope: IndustryScope,
        *,
        report_type: str,
        title: str,
        period: str,
        composition: ReportComposition,
        input_manifest: dict,
    ) -> UUID: ...


OpenReportTxn = Callable[[IndustryScope], AbstractAsyncContextManager[ReportStore]]


@dataclass(slots=True)
class ReportWiring:
    open_store: OpenReportTxn
    agent: ReportAgentProtocol
    packet_source: PacketSource


def make_report_handler(wiring: ReportWiring) -> JobHandler:
    async def handler(ctx: RunContext) -> None:
        await ctx.boundary()
        payload = ctx.job.input
        report_type = str(payload["report_type"])
        period = str(payload["period"])
        title = str(payload.get("title") or report_type)
        question = f"{report_type}:{period}:{title}"

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

        draft = await wiring.agent.compose_report(
            question,
            [
                {
                    "id": str(item.get("id", "")),
                    "quote": item.get("exact_quote", ""),
                }
                for item in packet.evidence
            ],
        )
        await ctx.boundary()

        composition = validate_citation_coverage(
            kind=report_type,
            title=str(getattr(draft, "title", title) or title),
            sections=list(getattr(draft, "sections", []) or []),
            allowed_citation_ids=packet.allowed_citation_ids,
        )
        composition.stale = bool(payload.get("stale", False))
        composition.stale_reason = str(payload.get("stale_reason") or "")

        manifest = {
            "report_type": report_type,
            "period": period,
            "input_manifest_hash": str(payload["input_manifest_hash"]),
        }
        async with wiring.open_store(ctx.scope) as store:
            report_id = await store.persist_report(
                ctx.scope,
                report_type=report_type,
                title=composition.title,
                period=period,
                composition=composition,
                input_manifest=manifest,
            )

        async with ctx.open_store(ctx.scope) as job_store:
            await ctx.service.finish(
                job_store,
                ctx.job,
                state="succeeded",
                progress={
                    "report_id": str(report_id),
                    "revision_id": str(composition.revision_id),
                    "publishable": composition.publishable,
                    "citation_coverage": composition.citation_coverage,
                    "unsupported_statements": list(composition.unsupported_statements),
                    "stale": composition.stale,
                    "stale_reason": composition.stale_reason,
                },
            )

    return handler
