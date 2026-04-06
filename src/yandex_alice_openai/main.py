from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from yandex_alice_openai.api.routes.alice import router as alice_router
from yandex_alice_openai.api.routes.health import router as health_router
from yandex_alice_openai.application.bootstrap import build_container
from yandex_alice_openai.config import get_settings
from yandex_alice_openai.infra.observability.logging import configure_logging, get_logger
from yandex_alice_openai.infra.observability.request_id import RequestIDMiddleware

settings = get_settings()
configure_logging(settings.app().log_level)
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = build_container(settings)
    await container.start()
    app.state.container = container
    logger.info("app_started", env=settings.environment())
    try:
        yield
    finally:
        await container.stop()
        logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Yandex Alice OpenAI", version="0.1.0", lifespan=lifespan)
    app.add_middleware(cast(Any, RequestIDMiddleware))
    app.include_router(health_router)
    app.include_router(alice_router)
    return app


app = create_app()


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error=str(exc))
    return JSONResponse(status_code=500, content={"detail": "internal_server_error"})


def run() -> None:
    uvicorn.run(
        "yandex_alice_openai.main:app",
        host=settings.app().host,
        port=settings.app().port,
        reload=settings.environment() == "dev",
    )
