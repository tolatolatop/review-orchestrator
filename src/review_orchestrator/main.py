from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from review_orchestrator.api import router
from review_orchestrator.config import Settings, get_settings
from review_orchestrator.dashboard import DASHBOARD_HTML
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import create_github_client
from review_orchestrator.reviews_dashboard import REVIEWS_DASHBOARD_HTML


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        github_client = None
        try:
            app.state.settings = settings
            app.state.engine = engine
            app.state.session_factory = create_session_factory(engine)
            github_client = create_github_client(settings)
            app.state.github_client = github_client
            await init_models(engine)
            yield
        finally:
            try:
                if github_client is not None:
                    await github_client.aclose()
            finally:
                await engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(router)

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
