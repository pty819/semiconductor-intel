"""Route modules for the /api/v1 surface (spec 08 §2)."""

from intel.api.routes import (
    auth,
    conversations,
    coverage,
    documents,
    feeds,
    generation_runs,
    industries,
    jobs,
    knowledge,
    reports,
    reviews,
    search,
    sources,
    topics,
)

all_routers = (
    auth.router,
    industries.router,
    topics.router,
    feeds.router,
    sources.templates_router,
    sources.sources_router,
    knowledge.timeline_router,
    knowledge.evidence_router,
    knowledge.entities_router,
    knowledge.watches_router,
    knowledge.evolutions_router,
    conversations.router,
    reviews.router,
    reports.router,
    search.router,
    coverage.router,
    jobs.router,
    generation_runs.router,
    documents.router,
)

__all__ = ["all_routers"]
