"""FastAPI application assembly and lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from review_orchestrator.infrastructure.config import Settings, get_settings
from review_orchestrator.infrastructure.db import (
    create_engine,
    create_session_factory,
    init_models,
)
from review_orchestrator.integrations.provider_plugins import create_provider_registry
from review_orchestrator.presentation.api import router
from review_orchestrator.presentation.dashboard import DASHBOARD_HTML
from review_orchestrator.presentation.provider_core import (
    router as provider_core_router,
)
from review_orchestrator.presentation.reviews_dashboard import REVIEWS_DASHBOARD_HTML


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        provider_registry = None
        try:
            app.state.settings = settings
            app.state.engine = engine
            app.state.session_factory = create_session_factory(engine)
            provider_registry = create_provider_registry(settings)
            app.state.provider_registry = provider_registry
            await init_models(engine)
            yield
        finally:
            try:
                if provider_registry is not None:
                    await provider_registry.aclose()
            finally:
                await engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(router)
    app.include_router(provider_core_router)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if not request.url.path.startswith("/v1/"):
            return await request_validation_exception_handler(request, exc)
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": errors},
        )

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_redirect() -> RedirectResponse:
        return RedirectResponse("/dashboard/", status_code=307)

    @app.get("/dashboard/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/reviews", include_in_schema=False)
    async def reviews_dashboard_redirect() -> RedirectResponse:
        return RedirectResponse("/reviews/", status_code=307)

    @app.get("/reviews/", response_class=HTMLResponse, include_in_schema=False)
    async def reviews_dashboard() -> HTMLResponse:
        return HTMLResponse(REVIEWS_DASHBOARD_HTML)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
