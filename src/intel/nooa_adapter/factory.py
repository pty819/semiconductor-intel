"""Composition helpers: route registry, agent builders, summarizer wiring.

The factory is the ONLY place that mixes Settings with the agent classes:
job entrypoints call a builder here and get a fully wired agent. Route
aliases resolve lazily (see agents.route_client), so building an agent
performs no LLM I/O — safe at process start and per job alike.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from nooa.agents import TokenBudgetSummarizer, context_budget
from nooa.config.summarizer_config import TokenBudgetConfig
from nooa.unifiedllm import UnifiedLLM, get_llm_client, reload_registry

from intel.nooa_adapter.agents import (
    AnswerAgent,
    EvolutionAgent,
    ExtractionAgent,
    InvestigationAgent,
    InvestigationAgentV2,
    QueryPlannerAgent,
    RelationAgent,
    RoutingAgent,
    investigation_codeact_config,
)
from intel.nooa_adapter.gateway import (
    GATEWAY_ACTIONS,
    GatewayRejected,
    ToolGateway,
    sign_scoped_token,
)
from intel.nooa_adapter.middleware import LlmBudget, install_intel_middleware
from intel.settings import Settings

#: Shipped route registry (spec 14 §3.1 tiers → grok2api endpoint).
ROUTES_YAML = Path(__file__).parent / "registry" / "routes.yaml"


def load_route_registry(path: str | Path = ROUTES_YAML) -> dict:
    """Load the route registry; returns the merged alias → config map.

    Idempotent per process: ``reload_registry`` replaces the global MODELS
    mapping in place; the factory calls this once at startup so method
    aliases resolve against the file, not auto-discovery.
    """
    return reload_registry(Path(path))


def _default_route_client(tier: str = "L2") -> UnifiedLLM:
    """Instance-level default client for agent construction.

    NOOA validates at instantiation that an LLM resolves; generation
    methods still override per-method via their tier lambda, so this is
    the fallback binding only. Construction performs no network I/O.
    """
    from nooa.unifiedllm.registry import MODELS

    if not MODELS:
        load_route_registry()
    alias = Settings().route_aliases.get(tier, tier)
    return get_llm_client(alias)


def make_routing_agent() -> RoutingAgent:
    return RoutingAgent(llm=_default_route_client("L1"))


def make_extraction_agent() -> ExtractionAgent:
    return ExtractionAgent(llm=_default_route_client("L2"))


def make_relation_agent() -> RelationAgent:
    return RelationAgent(llm=_default_route_client("L3"))


def make_evolution_agent() -> EvolutionAgent:
    return EvolutionAgent(llm=_default_route_client("L3"))


def make_answer_agent() -> AnswerAgent:
    return AnswerAgent(llm=_default_route_client("L2"))


def make_query_planner_agent() -> QueryPlannerAgent:
    return QueryPlannerAgent(llm=_default_route_client("L2"))


def make_investigation_gateway(
    *,
    settings: Settings,
    owner_id: UUID,
    industry_id: UUID | None,
    job_id: UUID,
    is_job_active,
    online: bool = False,
    seams: dict | None = None,
) -> tuple[str, ToolGateway]:
    """Sign the scoped token and build the per-job ToolGateway.

    Returns ``(token, gateway)`` — the token stays with the runner; the
    gateway goes to the agent. ``online`` gates fetch_public/search_web
    (16 §2: 显式在线模式才提供); archive-reading tools are always granted.
    Fail-closed on an empty gateway secret.
    """
    if not settings.gateway_secret:
        raise GatewayRejected(
            "gateway_unconfigured",
            "Settings.gateway_secret is empty; refusing to sign tool tokens",
        )
    actions = set(GATEWAY_ACTIONS)
    if not online:
        actions -= {"fetch_public", "search_web"}
    seams = dict(seams or {})
    if not online:
        seams.pop("fetch_public_fn", None)
        seams.pop("search_web_fn", None)
    token = sign_scoped_token(
        settings.gateway_secret,
        owner_id=owner_id,
        industry_id=industry_id,
        job_id=job_id,
        actions=actions,
        ttl_seconds=settings.gateway_token_ttl_seconds,
    )
    gateway = ToolGateway(
        token=token,
        secret=settings.gateway_secret,
        is_job_active=is_job_active,
        **seams,
    )
    return token, gateway


def make_investigation_agent(
    *, gateway: ToolGateway, v2: bool = False
) -> InvestigationAgent:
    """One investigation agent with its gateway attached.

    ``v2`` selects the CodeActV2 candidate class (16 §2) — same config
    (see agents.investigation_codeact_config), single-tool protocol.
    """
    agent = (
        InvestigationAgentV2(llm=_default_route_client("L3"))
        if v2
        else InvestigationAgent(llm=_default_route_client("L3"))
    )
    agent.attach_gateway(gateway)
    return agent


def install_investigation_summarizer(
    agent, llm: UnifiedLLM
) -> TokenBudgetSummarizer | None:
    """Wire TokenBudgetSummarizer per doc 16 §4.

    threshold = context_budget(llm, 0.65) with NO fallback guess — when
    the client does not expose a window, return None and run without
    compression rather than invent a number (16 §4: 非费用预算, 触发阈值
    必须来自真实窗口). The caller owns the summarizer's lifetime: await
    ``aclose()`` when the job settles to drain pending summaries.
    """
    threshold = context_budget(llm, percent=0.65, fallback=None)
    if threshold is None:
        return None
    return TokenBudgetSummarizer.install(
        agent,
        config=TokenBudgetConfig(
            max_tokens=threshold,
            preserve_recent=16,
            target_chars=6000,
        ),
    )


def prepare_job_agent(
    agent,
    *,
    owner_id: UUID,
    job_id: UUID,
    industry_id: UUID | None,
    is_job_active,
    usage_sink,
    settings: Settings | None = None,
) -> None:
    """Standard job-entry wiring: D16 middleware with settings knobs."""
    settings = settings or Settings()
    install_intel_middleware(
        agent,
        scope_ctx=middleware_scope(
            owner_id=owner_id,
            job_id=job_id,
            industry_id=industry_id,
            is_active=is_job_active,
        ),
        usage_sink=usage_sink,
        budget=LlmBudget(settings.llm_max_calls_per_job),
        max_output_chars=settings.cell_output_max_chars,
    )


def middleware_scope(**kwargs):
    from intel.nooa_adapter.middleware import ScopeContext

    return ScopeContext(**kwargs)


__all__ = [
    "ROUTES_YAML",
    "install_investigation_summarizer",
    "investigation_codeact_config",
    "load_route_registry",
    "make_answer_agent",
    "make_evolution_agent",
    "make_extraction_agent",
    "make_investigation_agent",
    "make_investigation_gateway",
    "make_query_planner_agent",
    "make_relation_agent",
    "make_routing_agent",
    "prepare_job_agent",
]
