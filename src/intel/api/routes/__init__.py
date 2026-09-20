"""Route modules for the /api/v1 surface (spec 08 §2)."""

from intel.api.routes import auth, feeds, industries, sources, topics

all_routers = (
    auth.router,
    industries.router,
    topics.router,
    feeds.router,
    sources.templates_router,
    sources.sources_router,
)

__all__ = ["all_routers"]
