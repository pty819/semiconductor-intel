"""FastAPI application factory (spec 08: 同源 HTTPS JSON, /api/v1 前缀).

``create_app`` wires settings, the identity service, the jobs-table
enqueuer (Task 6: ``deps.get_enqueuer`` builds a ``JobServiceEnqueuer``
over the request connection; setting ``app.state.enqueuer`` swaps in a
fake), the error envelope handlers, the request-id middleware and every
Task 5 router. CORS is deliberately NOT configured — the product is
same-origin and cross-origin credentials are never wildcarded (08 §4).

The async engine is created eagerly but connects lazily, so importing the
module or booting uvicorn never touches the database; ``uvicorn
intel.api.app:app`` idles with /health/live green.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from sqlalchemy.ext.asyncio import create_async_engine

from intel.api.errors import register_error_handlers
from intel.api.routes import all_routers
from intel.contracts import HealthView
from intel.services.identity import IdentityService
from intel.settings import Settings

API_PREFIX = "/api/v1"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    app = FastAPI(
        title="semiconductor-intel",
        version="0.1.0",
        lifespan=_lifespan,
        # Contract tests come from the design pack (Task 15); the served
        # schema is generated, not the source of truth.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.identity_service = IdentityService.from_settings(settings)
    # Dispatch goes through the real jobs table now (Task 6): deps builds a
    # JobServiceEnqueuer over the request connection. Setting
    # app.state.enqueuer swaps in a fake (dev/tests).
    app.state.engine = create_async_engine(settings.database_url)

    @app.middleware("http")
    async def request_id(
        request: Request, call_next
    ) -> Response:
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    register_error_handlers(app)

    @app.get("/health/live")
    async def health_live() -> HealthView:
        """Liveness only — no endpoint/version/user content leaks (08 §2)."""
        return HealthView(status="ok")

    for router in all_routers:
        app.include_router(router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health/live")
    async def health_live_prefixed() -> HealthView:
        return HealthView(status="ok")

    return app


app = create_app()
