"""JobRunner composition root: register kind handlers + SQL stores.

Task 14 deferred this wiring. One process, one engine, role-specific
handler sets so compose services (pipeline-worker / fetch-worker /
research-runner) share the image and claim only the kinds they can run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from intel.nooa_adapter.agents import route_client
from intel.nooa_adapter.factory import (
    install_investigation_summarizer,
    load_route_registry,
    make_answer_agent,
    make_bare_investigation_agent,
    make_extraction_agent,
    make_investigation_gateway,
    make_query_planner_agent,
    make_routing_agent,
    prepare_job_agent,
)
from intel.nooa_adapter.middleware import InMemoryUsageSink
from intel.nooa_adapter.tracing import SqlUsageSink, job_trace_session
from intel.parsing import Parser, QualityThresholds
from intel.repositories.base import IndustryScope
from intel.repositories.search import SqlAlchemySearchRepository
from intel.retrieval.indexer import (
    IndexWiring,
    register_index_handlers,
    sql_index_txn_factory,
)
from intel.services.jobs import JobService
from intel.settings import Settings
from intel.sources.blobstore import FileObjectStore
from intel.sources.browser import BrowserRenderer
from intel.sources.fetcher import HttpFetcher
from intel.sources.pageclient import HttpPageClient
from intel.sources.politeness import PolitenessGate
from intel.sources.ssrf import UrlGuard
from intel.workers.runner import JobHandler, JobRunner, RunContext
from intel.workers.stores import (
    archive_packet_source,
    bind_app,
    conversation_context_source,
    sql_answer_opener,
    sql_event_build_opener,
    sql_extract_opener,
    sql_jobs_opener,
    sql_report_opener,
    sql_review_opener,
    sql_route_opener,
)
from intel.workflows.answer import AnswerWiring, make_answer_handler
from intel.workflows.event_build import EventBuildWiring, make_event_build_handler
from intel.workflows.extract import ExtractWiring, make_extract_handler
from intel.workflows.ingest import (
    IngestWiring,
    register_ingest_handlers,
    sql_ingest_txn_factory,
)
from intel.workflows.investigate import InvestigateWiring, make_investigate_handler
from intel.workflows.report import ReportWiring, make_report_handler
from intel.workflows.review import ReviewWiring, make_review_handler
from intel.workflows.route import RouteWiring, make_route_handler

#: Kind sets per compose service (spec 10 §4). ``all`` is local-dev.
ROLE_KINDS: dict[str, tuple[str, ...]] = {
    "pipeline": (
        "discover",
        "source_poll",
        "parse",
        "index",
        "route",
        "extract",
        "event_build",
        "apply_review",
        "watch_check",
    ),
    "fetch": ("fetch",),
    "research": ("archive_answer", "investigate", "report_build"),
    "all": (),  # empty → register every handler below
}

REQUIRED_KINDS = (
    "discover",
    "source_poll",
    "fetch",
    "parse",
    "index",
    "route",
    "extract",
    "event_build",
    "archive_answer",
    "investigate",
    "report_build",
    "apply_review",
)


@dataclass(slots=True)
class WorkerRuntime:
    """What a worker/scheduler process holds for its lifetime."""

    settings: Settings
    engine: AsyncEngine
    runner: JobRunner
    service: JobService
    role: str
    kinds: tuple[str, ...]


def _wants(role: str, kind: str) -> bool:
    kinds = ROLE_KINDS.get(role)
    if kinds is None:
        raise ValueError(f"unknown worker role {role!r}; want {sorted(ROLE_KINDS)}")
    return not kinds or kind in kinds


def _prepare(
    agent, ctx: RunContext, settings: Settings, sink: InMemoryUsageSink
) -> None:
    prepare_job_agent(
        agent,
        owner_id=ctx.job.owner_id,
        job_id=ctx.job.id,
        industry_id=ctx.job.industry_id,
        is_job_active=lambda: not ctx.lease_lost,
        usage_sink=sink,
        settings=settings,
    )


def _wrap_per_job(
    *,
    agent_factory: Callable,
    make_handler: Callable[..., JobHandler],
    wiring_factory: Callable[..., object],
    settings: Settings,
    sink: InMemoryUsageSink | SqlUsageSink,
    trace_dir: Path,
    planner_factory: Callable | None = None,
    summarizer_tier: str | None = None,
) -> JobHandler:
    """Build a fresh agent (middleware budget is per-job) then run the handler.

    Every job runs inside :func:`job_trace_session` (JSONL under
    ``trace_dir``, flushed when the session closes — I-5/TRACE-01) with
    usage flowing to the SQL sink (model_runs rows). ``planner_factory``
    (answer/investigate) builds the per-job QueryPlanner agent with the
    same middleware treatment; its wiring factory then receives
    ``(agent, planner)``. ``summarizer_tier`` (investigation) installs
    the TokenBudgetSummarizer on the agent's route client and drains it
    in ``finally`` when the job settles (16 §4).
    """

    async def handler(ctx: RunContext) -> None:
        with job_trace_session(ctx.job.id, trace_dir):
            agent = agent_factory()
            _prepare(agent, ctx, settings, sink)
            summarizer = None
            if summarizer_tier is not None:
                summarizer = install_investigation_summarizer(
                    agent, route_client(agent, summarizer_tier)
                )
            try:
                if planner_factory is None:
                    inner = make_handler(wiring_factory(agent))
                else:
                    planner = planner_factory()
                    _prepare(planner, ctx, settings, sink)
                    inner = make_handler(wiring_factory(agent, planner))
                await inner(ctx)
            finally:
                if summarizer is not None:
                    await summarizer.aclose()

    return handler


def build_runtime(
    settings: Settings | None = None,
    *,
    role: str = "all",
    engine: AsyncEngine | None = None,
) -> WorkerRuntime:
    """Wire SQL stores, agents, and the JobRunner for one worker process."""
    settings = settings if settings is not None else Settings()
    load_route_registry()
    engine = (
        engine if engine is not None else create_async_engine(settings.database_url)
    )
    service = JobService()
    open_jobs = sql_jobs_opener(engine)
    runner = JobRunner(service, open_jobs)
    # I-5/TRACE-06: every LLM call persists a model_runs row; the write
    # is best-effort (a tracing failure must not fail a finished call).
    sink = SqlUsageSink(engine)
    trace_dir = Path(settings.job_trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    object_store = FileObjectStore(settings.object_store_root)
    settings.object_store_root.mkdir(parents=True, exist_ok=True)
    guard = UrlGuard()
    politeness = PolitenessGate()

    def page_client_factory() -> HttpPageClient:
        return HttpPageClient(guard=guard)

    def fetcher_factory() -> HttpFetcher:
        return HttpFetcher(guard=guard, browser=BrowserRenderer(guard=guard))

    ingest = IngestWiring(
        open_ingest=sql_ingest_txn_factory(engine, object_store=object_store),
        page_client_factory=page_client_factory,
        fetcher_factory=fetcher_factory,
        object_store=object_store,
        politeness=politeness,
        parser=Parser(QualityThresholds.from_settings(settings)),
    )
    if _wants(role, "discover"):
        register_ingest_handlers(runner, ingest)
        # fetch-only workers still need fetch but not discover/parse; the
        # register above attaches all three — undo extras below.
        if role == "fetch":
            # register_ingest_handlers always binds discover/source_poll/parse;
            # a fetch-only role must not claim those kinds.
            runner._handlers.pop("discover", None)
            runner._handlers.pop("source_poll", None)
            runner._handlers.pop("parse", None)
        if role == "pipeline":
            runner._handlers.pop("fetch", None)
    elif _wants(role, "fetch"):
        register_ingest_handlers(runner, ingest)
        runner._handlers.pop("discover", None)
        runner._handlers.pop("source_poll", None)
        runner._handlers.pop("parse", None)

    if _wants(role, "index"):
        register_index_handlers(
            runner, IndexWiring(open_store=sql_index_txn_factory(engine))
        )

    open_route = sql_route_opener(engine)
    if _wants(role, "route"):
        runner.register(
            "route",
            _wrap_per_job(
                agent_factory=make_routing_agent,
                make_handler=make_route_handler,
                wiring_factory=lambda agent: RouteWiring(
                    open_store=open_route, agent=agent, jobs=service
                ),
                settings=settings,
                sink=sink,
                trace_dir=trace_dir,
            ),
        )

    open_extract = sql_extract_opener(engine)
    if _wants(role, "extract"):
        runner.register(
            "extract",
            _wrap_per_job(
                agent_factory=make_extraction_agent,
                make_handler=make_extract_handler,
                wiring_factory=lambda agent: ExtractWiring(
                    open_store=open_extract, agent=agent, jobs=service
                ),
                settings=settings,
                sink=sink,
                trace_dir=trace_dir,
            ),
        )

    open_events = sql_event_build_opener(engine)
    if _wants(role, "event_build"):
        runner.register(
            "event_build",
            _wrap_per_job(
                agent_factory=make_extraction_agent,
                make_handler=make_event_build_handler,
                wiring_factory=lambda agent: EventBuildWiring(
                    open_store=open_events, agent=agent
                ),
                settings=settings,
                sink=sink,
                trace_dir=trace_dir,
            ),
        )

    async def packet_source(scope: IndustryScope, question: str) -> dict:
        return await archive_packet_source(engine, scope, question)

    async def conversation_source(scope, conversation_id, pending_message_id=None):
        # CHAT-01: one short transaction; the planner LLM call happens
        # after it returns, outside any transaction.
        return await conversation_context_source(
            engine, scope, conversation_id, exclude_message_id=pending_message_id
        )

    open_answer = sql_answer_opener(engine)
    if _wants(role, "archive_answer"):
        runner.register(
            "archive_answer",
            _wrap_per_job(
                agent_factory=make_answer_agent,
                make_handler=make_answer_handler,
                wiring_factory=lambda agent, planner: AnswerWiring(
                    open_store=open_answer,
                    agent=agent,
                    packet_source=packet_source,
                    query_planner=planner,
                    conversation_source=conversation_source,
                ),
                settings=settings,
                sink=sink,
                trace_dir=trace_dir,
                planner_factory=make_query_planner_agent,
            ),
        )

    if _wants(role, "investigate"):

        def gateway_factory(*, online: bool, job_id, scope: IndustryScope):
            async def is_job_active(jid: UUID) -> bool:
                async with open_jobs(None) as store:
                    job = await store.get_job(jid)
                return (
                    job is not None
                    and job.state == "running"
                    and job.cancel_requested_at is None
                )

            async def search_archive_fn(payload, *, query: str, limit: int = 10):
                industry = (
                    UUID(str(payload.industry_id))
                    if payload.industry_id is not None
                    else scope.industry_id
                )
                gw_scope = IndustryScope(payload.owner_id, industry)
                async with engine.connect() as conn, conn.begin():
                    await bind_app(conn, gw_scope)
                    repo = SqlAlchemySearchRepository(conn, gw_scope)
                    return (await repo.search(query))[:limit]

            seams: dict = {"search_archive_fn": search_archive_fn}
            if online:

                async def fetch_public_fn(payload, *, url: str):
                    del payload
                    from datetime import UTC, datetime, timedelta
                    from uuid import uuid4

                    from intel.sources.dto import FetchRequest

                    fetcher = HttpFetcher(guard=guard)
                    result = await fetcher.fetch(
                        FetchRequest(
                            item_id=uuid4(),
                            url=url,
                            deadline_at=datetime.now(UTC) + timedelta(seconds=30),
                        )
                    )
                    return {
                        "outcome": result.outcome,
                        "status": result.status,
                        "url": result.final_url,
                    }

                seams["fetch_public_fn"] = fetch_public_fn
            _token, gateway = make_investigation_gateway(
                settings=settings,
                owner_id=scope.owner_id,
                industry_id=scope.industry_id,
                job_id=job_id,
                is_job_active=is_job_active,
                online=online,
                seams=seams,
            )
            return gateway

        runner.register(
            "investigate",
            _wrap_per_job(
                agent_factory=make_bare_investigation_agent,
                make_handler=make_investigate_handler,
                wiring_factory=lambda agent, planner: InvestigateWiring(
                    open_store=open_answer,
                    agent=agent,
                    packet_source=packet_source,
                    gateway_factory=gateway_factory,
                    query_planner=planner,
                    conversation_source=conversation_source,
                ),
                settings=settings,
                sink=sink,
                trace_dir=trace_dir,
                planner_factory=make_query_planner_agent,
                # 16 §4: context-budget summarizer on the investigation
                # agent's L3 route client; drained in finally.
                summarizer_tier="L3",
            ),
        )

    open_report = sql_report_opener(engine)
    if _wants(role, "report_build"):
        runner.register(
            "report_build",
            _wrap_per_job(
                agent_factory=make_answer_agent,
                make_handler=make_report_handler,
                wiring_factory=lambda agent: ReportWiring(
                    open_store=open_report,
                    agent=agent,
                    packet_source=packet_source,
                ),
                settings=settings,
                sink=sink,
                trace_dir=trace_dir,
            ),
        )

    if _wants(role, "apply_review"):
        runner.register(
            "apply_review",
            make_review_handler(ReviewWiring(open_store=sql_review_opener(engine))),
        )

    if _wants(role, "watch_check"):

        async def watch_check(ctx: RunContext) -> None:
            await ctx.boundary()
            async with ctx.open_store(ctx.scope) as store:
                await ctx.service.finish(
                    store,
                    ctx.job,
                    state="succeeded",
                    progress={"note": "watch_check: local archive cadence ack"},
                )

        runner.register("watch_check", watch_check)

    kinds = tuple(runner._handlers)
    return WorkerRuntime(
        settings=settings,
        engine=engine,
        runner=runner,
        service=service,
        role=role,
        kinds=kinds,
    )


def registered_kinds(runner: JobRunner) -> tuple[str, ...]:
    return tuple(runner._handlers)


def assert_required_kinds(runner: JobRunner, *, role: str = "all") -> None:
    """Composition-root gate: every kind this role must run is registered."""
    have = set(runner._handlers)
    needed: Sequence[str]
    if role == "all":
        needed = REQUIRED_KINDS
    else:
        needed = (
            tuple(k for k in ROLE_KINDS[role] if k in REQUIRED_KINDS)
            or ROLE_KINDS[role]
        )
    missing = [kind for kind in needed if kind not in have]
    if missing:
        raise RuntimeError(f"worker role {role!r} missing handlers: {missing}")
