from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from alice_openai_backend.api.routes.alice import router as alice_router
from alice_openai_backend.api.routes.health import router as health_router
from alice_openai_backend.application.bootstrap import build_container
from alice_openai_backend.config import get_settings
from alice_openai_backend.infra.observability.logging import configure_logging, get_logger
from alice_openai_backend.infra.observability.metrics import MetricsMiddleware, metrics_router
from alice_openai_backend.infra.observability.request_id import RequestIDMiddleware
from alice_openai_backend.infra.observability.tracing import configure_tracing

settings = get_settings()
configure_logging(settings.app().log_level)
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = build_container(settings)
    await container.start()
    app.state.container = container
    if settings.app().tracing_enabled:
        configure_tracing(
            app,
            service_name=settings.app().service_name,
            endpoint=settings.app().otlp_endpoint,
        )
    logger.info("app_started", env=settings.environment())
    try:
        yield
    finally:
        await container.stop()
        logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Alice OpenAI Backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIDMiddleware)
    if settings.app().metrics_enabled:
        app.add_middleware(MetricsMiddleware)
        app.include_router(metrics_router())
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
        "alice_openai_backend.main:app",
        host=settings.app().host,
        port=settings.app().port,
        reload=settings.environment() == "development",
    )
