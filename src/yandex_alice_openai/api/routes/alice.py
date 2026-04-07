from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from yandex_alice_openai.api.deps import get_container
from yandex_alice_openai.application.bootstrap import Container
from yandex_alice_openai.infra.observability.logging import get_logger
from yandex_alice_openai.schemas.alice import AliceWebhookRequest, AliceWebhookResponse
from yandex_alice_openai.services.identity import build_user_scope
from yandex_alice_openai.services.renderer import render_voice_response

router = APIRouter(tags=["alice"])
logger = get_logger()


@router.post("/webhooks/alice", response_model=AliceWebhookResponse)
async def alice_webhook(
    payload: AliceWebhookRequest,
    request: Request,
    container: Container = Depends(get_container),
    secret: str | None = Query(default=None),
) -> AliceWebhookResponse:
    app_settings = container.settings.app()
    expected_secret = app_settings.webhook_secret
    if expected_secret is not None and secret != expected_secret.get_secret_value():
        return AliceWebhookResponse(
            session=payload.session,
            response=render_voice_response("Запрос не прошел проверку. Попробуй позже."),
        )

    request_key = payload.request_key()
    scope = build_user_scope(payload).conversation_key
    client_host = request.client.host if request.client else "unknown"
    cached = await container.idempotency_store.get_cached_response(request_key)

    try:
        if cached is None:
            await container.rate_limiter.check(f"{scope}:{client_host}")
        return await container.conversation_service.handle(payload)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        return AliceWebhookResponse(
            session=payload.session,
            response=render_voice_response("Слишком много запросов подряд. Попробуй чуть позже."),
        )
    except Exception as exc:
        logger.exception(
            "alice_webhook_failed",
            request_key=request_key,
            conversation_key=scope,
            error=type(exc).__name__,
        )
        return AliceWebhookResponse(
            session=payload.session,
            response=render_voice_response("Сервис временно недоступен. Попробуй чуть позже."),
        )
