from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from review_orchestrator.api import router
from review_orchestrator.config import Settings, get_settings
from review_orchestrator.db import create_engine, create_session_factory, init_models


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        await init_models(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
