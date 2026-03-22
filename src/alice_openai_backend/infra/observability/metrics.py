from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_COUNT = Counter(
    "alice_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "alice_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0),
)
LLM_COUNT = Counter(
    "alice_llm_requests_total",
    "LLM requests by outcome",
    ["outcome"],
)
DEFERRED_JOB_COUNT = Counter(
    "alice_deferred_jobs_total",
    "Deferred jobs by state",
    ["state"],
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = perf_counter()
        response = await call_next(request)
        elapsed = perf_counter() - start
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        REQUEST_COUNT.labels(request.method, path, response.status_code).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(elapsed)
        return response


def metrics_router() -> APIRouter:
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router
