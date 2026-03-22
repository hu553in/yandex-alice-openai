from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic import SecretStr

from alice_openai_backend.api.routes.alice import router
from alice_openai_backend.schemas.alice import (
    AliceApplication,
    AliceRequestPayload,
    AliceSession,
    AliceUser,
    AliceWebhookRequest,
)


class StubConversationService:
    def __init__(self, *, idempotency_store: StubIdempotencyStore | None = None) -> None:
        self.idempotency_store = idempotency_store
        self.raise_runtime_error = False
        self.call_count = 0

    async def handle(self, payload: AliceWebhookRequest) -> dict[str, Any]:
        self.call_count += 1
        if self.raise_runtime_error:
            raise RuntimeError("boom")
        if self.idempotency_store is not None:
            cached = await self.idempotency_store.get_cached_response(payload.request_key())
            if cached is not None:
                return cached
        return {
            "version": "1.0",
            "session": payload.session.model_dump(),
            "response": {
                "text": "Маршрут работает.",
                "tts": "Маршрут работает.",
                "end_session": False,
                "buttons": [],
            },
        }


class StubRateLimiter:
    def __init__(self) -> None:
        self.raise_limit = False
        self.checked_buckets: list[str] = []

    async def check(self, bucket: str) -> None:
        self.checked_buckets.append(bucket)
        if self.raise_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
            )
        return None


class StubIdempotencyStore:
    def __init__(self) -> None:
        self.cached: dict[str, dict[str, object]] = {}
        self.seen_keys: list[str] = []

    async def get_cached_response(self, key: str) -> dict[str, object] | None:
        self.seen_keys.append(key)
        return self.cached.get(key)

    async def store_response(self, key: str, response: dict[str, object]) -> None:
        self.cached[key] = response


@dataclass
class StubAppSettings:
    webhook_secret: SecretStr | None = None


@dataclass
class StubSettings:
    webhook_secret: SecretStr | None = None

    def app(self) -> StubAppSettings:
        return StubAppSettings(webhook_secret=self.webhook_secret)


@dataclass
class StubContainer:
    settings: StubSettings
    rate_limiter: StubRateLimiter
    conversation_service: StubConversationService
    idempotency_store: StubIdempotencyStore


def build_test_app(
    *,
    conversation_service: StubConversationService | None = None,
    rate_limiter: StubRateLimiter | None = None,
    idempotency_store: StubIdempotencyStore | None = None,
    webhook_secret: SecretStr | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    idempotency_store_instance = idempotency_store or StubIdempotencyStore()
    app.state.container = StubContainer(
        settings=StubSettings(webhook_secret=webhook_secret),
        rate_limiter=rate_limiter or StubRateLimiter(),
        conversation_service=conversation_service
        or StubConversationService(idempotency_store=idempotency_store_instance),
        idempotency_store=idempotency_store_instance,
    )
    return app


def test_webhook_route_returns_alice_shape(alice_session: AliceSession) -> None:
    app = build_test_app()
    client = TestClient(app)
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )

    response = client.post("/webhooks/alice", json=payload.model_dump())

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["text"] == "Маршрут работает."
    assert body["session"]["session_id"] == "session-1"


def test_webhook_route_returns_alice_shape_for_invalid_webhook_secret(
    alice_session: AliceSession,
) -> None:
    app = build_test_app(webhook_secret=SecretStr("expected-secret"))
    client = TestClient(app)
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )

    response = client.post(
        "/webhooks/alice",
        json=payload.model_dump(),
        headers={"x-alice-secret": "wrong-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["text"] == "Запрос не прошел проверку. Попробуй позже."
    assert body["response"]["tts"] == "Запрос не прошел проверку. Попробуй позже."
    assert body["session"]["session_id"] == "session-1"


def test_webhook_route_returns_alice_shape_for_rate_limit(alice_session: AliceSession) -> None:
    rate_limiter = StubRateLimiter()
    rate_limiter.raise_limit = True
    app = build_test_app(rate_limiter=rate_limiter)
    client = TestClient(app)
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )

    response = client.post("/webhooks/alice", json=payload.model_dump())

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["text"] == "Слишком много запросов подряд. Попробуй чуть позже."


def test_webhook_route_skips_rate_limit_for_cached_duplicate(alice_session: AliceSession) -> None:
    idempotency_store = StubIdempotencyStore()
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )
    idempotency_store.cached[payload.request_key()] = {
        "version": "1.0",
        "session": payload.session.model_dump(),
        "response": {
            "text": "Из кэша.",
            "tts": "Из кэша.",
            "end_session": False,
            "buttons": [],
        },
    }
    rate_limiter = StubRateLimiter()
    rate_limiter.raise_limit = True
    app = build_test_app(rate_limiter=rate_limiter, idempotency_store=idempotency_store)
    client = TestClient(app)

    response = client.post("/webhooks/alice", json=payload.model_dump())

    assert response.status_code == 200
    assert response.json()["response"]["text"] == "Из кэша."
    assert rate_limiter.checked_buckets == []


def test_request_key_is_scoped_by_application_and_user() -> None:
    base_session = AliceSession(
        session_id="session-1",
        message_id=7,
        user_id="user-1",
    )
    payload_a = AliceWebhookRequest(
        session=base_session.model_copy(
            update={
                "application": AliceApplication(application_id="app-a"),
                "user": AliceUser(user_id="user-1"),
            }
        ),
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )
    payload_b = AliceWebhookRequest(
        session=base_session.model_copy(
            update={
                "application": AliceApplication(application_id="app-b"),
                "user": AliceUser(user_id="user-1"),
            }
        ),
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )
    payload_c = AliceWebhookRequest(
        session=base_session.model_copy(
            update={
                "application": AliceApplication(application_id="app-a"),
                "user": AliceUser(user_id="user-2"),
            }
        ),
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )

    assert payload_a.request_key() != payload_b.request_key()
    assert payload_a.request_key() != payload_c.request_key()


def test_webhook_route_returns_alice_shape_for_runtime_error(alice_session: AliceSession) -> None:
    conversation_service = StubConversationService()
    conversation_service.raise_runtime_error = True
    app = build_test_app(conversation_service=conversation_service)
    client = TestClient(app)
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )

    response = client.post("/webhooks/alice", json=payload.model_dump())

    assert response.status_code == 200
    body = response.json()
    assert body["response"]["text"] == "Сервис временно недоступен. Попробуй чуть позже."
