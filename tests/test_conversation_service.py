from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest

from alice_openai_backend.domain.models import (
    ConversationTurn,
    FollowupMode,
    LLMReply,
    PendingReply,
    PendingStatus,
)
from alice_openai_backend.infra.llm.openai_adapter import IncompleteResponseError
from alice_openai_backend.schemas.alice import (
    AliceRequestPayload,
    AliceSession,
    AliceWebhookRequest,
)
from alice_openai_backend.services.conversation import ConversationService
from tests.conftest import (
    FailingQueue,
    FakeAnalytics,
    FakeConversationStore,
    FakeIdempotencyStore,
    FakeLLM,
    FakePendingStore,
    FakeQueue,
    ready_pending,
)


@dataclass
class _Step:
    max_output_tokens: int | None
    outcome: str
    reply_text: str = "Готовый ответ."


class EscalatingFakeLLM:
    def __init__(self, steps: list[_Step]) -> None:
        self.steps = steps
        self.call_count = 0
        self.max_output_tokens_seen: list[int | None] = []

    async def generate_reply(
        self,
        *,
        user_text: str,
        history: Sequence[ConversationTurn],
        request_id: str,
        deadline_seconds: float,
        max_output_tokens: int | None = None,
    ) -> LLMReply:
        _ = user_text, history, request_id, deadline_seconds
        self.call_count += 1
        self.max_output_tokens_seen.append(max_output_tokens)
        step = self.steps[self.call_count - 1]
        if step.outcome == "incomplete":
            raise IncompleteResponseError("max_output_tokens")
        if step.outcome == "error":
            raise RuntimeError("boom")

        return LLMReply(
            short_text=step.reply_text,
            raw_text=step.reply_text,
            followup_mode=FollowupMode.NONE,
            followup_text=None,
            continuation_request=None,
        )


@pytest.mark.asyncio
async def test_fast_path_returns_immediate_answer(alice_session: AliceSession) -> None:
    llm = FakeLLM(reply_text="Привет. Чем помочь?")
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=FakePendingStore(),
        idempotency_store=FakeIdempotencyStore(),
        queue=FakeQueue(),
        llm=llm,
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="привет", original_utterance="привет"),
    )

    response = await service.handle(payload)

    assert response.response.text == "Привет. Чем помочь?"
    assert llm.max_output_tokens_seen == [256]


@pytest.mark.asyncio
async def test_slow_path_enqueues_job_and_returns_prompt(alice_session: AliceSession) -> None:
    queue = FakeQueue()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=FakePendingStore(),
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=FakeLLM(fail=True),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(
            command="сложный вопрос",
            original_utterance="сложный вопрос",
        ),
    )

    response = await service.handle(payload)

    assert response.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert len(queue.jobs) == 1


@pytest.mark.asyncio
async def test_continue_returns_ready_reply(alice_session: AliceSession) -> None:
    conversation_store = FakeConversationStore()
    pending_store = FakePendingStore()
    idempotency = FakeIdempotencyStore()
    service = ConversationService(
        conversation_store=conversation_store,
        pending_store=pending_store,
        idempotency_store=idempotency,
        queue=FakeQueue(),
        llm=FakeLLM(),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    pending_store.pending["app-1:user-1:session-1:session-1"] = ready_pending()
    payload = AliceWebhookRequest(
        session=alice_session.model_copy(update={"message_id": 2}),
        request=AliceRequestPayload(command="продолжай", original_utterance="продолжай"),
    )

    response = await service.handle(payload)

    assert response.response.text == "Готовый ответ."


@pytest.mark.asyncio
async def test_new_question_is_blocked_while_previous_reply_is_processing(
    alice_session: AliceSession,
) -> None:
    pending_store = FakePendingStore()
    pending_store.pending["app-1:user-1:session-1:session-1"] = PendingReply(
        status=PendingStatus.PROCESSING,
        reply_text=None,
        reply_tts=None,
        tail_text=None,
        followup_mode=FollowupMode.NONE,
        continuation_request=None,
        error_message=None,
        job_id="job-processing",
    )
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=FakeQueue(),
        llm=FakeLLM(),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session.model_copy(update={"message_id": 3}),
        request=AliceRequestPayload(
            command="новый вопрос",
            original_utterance="новый вопрос",
        ),
    )

    response = await service.handle(payload)

    assert response.response.text == "Я еще готовлю предыдущий ответ. Скажи: продолжай."


@pytest.mark.asyncio
async def test_new_question_is_blocked_when_ready_reply_is_waiting(
    alice_session: AliceSession,
) -> None:
    pending_store = FakePendingStore()
    pending_store.pending["app-1:user-1:session-1:session-1"] = ready_pending()
    queue = FakeQueue()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=FakeLLM(),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session.model_copy(update={"message_id": 4}),
        request=AliceRequestPayload(
            command="еще вопрос",
            original_utterance="еще вопрос",
        ),
    )

    response = await service.handle(payload)

    assert response.response.text == "У меня готов предыдущий ответ. Скажи: продолжай."
    assert queue.jobs == []


@pytest.mark.asyncio
async def test_failed_pending_is_cleared_before_new_question(alice_session: AliceSession) -> None:
    pending_store = FakePendingStore()
    pending_store.pending["app-1:user-1:session-1:session-1"] = PendingReply(
        status=PendingStatus.FAILED,
        reply_text=None,
        reply_tts=None,
        tail_text=None,
        followup_mode=FollowupMode.NONE,
        continuation_request=None,
        error_message="boom",
        job_id="job-failed",
    )
    queue = FakeQueue()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=FakeLLM(fail=True),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session.model_copy(update={"message_id": 5}),
        request=AliceRequestPayload(command="новый запрос", original_utterance="новый запрос"),
    )

    response = await service.handle(payload)

    assert response.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert len(queue.jobs) == 1


@pytest.mark.asyncio
async def test_duplicate_delivery_returns_cached_response_and_does_not_enqueue_twice(
    alice_session: AliceSession,
) -> None:
    queue = FakeQueue()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=FakePendingStore(),
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=FakeLLM(fail=True),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="сложный вопрос", original_utterance="сложный вопрос"),
    )

    first = await service.handle(payload)
    second = await service.handle(payload)

    assert first.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert second.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert len(queue.jobs) == 1


@pytest.mark.asyncio
async def test_enqueue_failure_clears_pending_state(alice_session: AliceSession) -> None:
    pending_store = FakePendingStore()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=FailingQueue(),
        llm=FakeLLM(fail=True),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="сложный вопрос", original_utterance="сложный вопрос"),
    )

    with pytest.raises(RuntimeError, match="queue down"):
        await service.handle(payload)

    assert pending_store.pending == {}


@pytest.mark.asyncio
async def test_deferred_job_escalates_max_output_tokens_after_incomplete(
    alice_session: AliceSession,
) -> None:
    llm = EscalatingFakeLLM(
        steps=[
            _Step(max_output_tokens=256, outcome="incomplete"),
            _Step(max_output_tokens=256, outcome="incomplete"),
            _Step(max_output_tokens=512, outcome="success", reply_text="Готово после эскалации."),
        ]
    )
    conversation_store = FakeConversationStore()
    pending_store = FakePendingStore()
    queue = FakeQueue()
    service = ConversationService(
        conversation_store=conversation_store,
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=llm,
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="сложный вопрос", original_utterance="сложный вопрос"),
    )

    first_response = await service.handle(payload)
    assert first_response.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert len(queue.jobs) == 1

    reply = await service.process_deferred_job(queue.jobs[0], deadline_seconds=1.0)

    assert reply.short_text == "Готово после эскалации."
    assert llm.max_output_tokens_seen == [256, 256, 512]


@pytest.mark.asyncio
async def test_fast_path_deferred_offer_is_stored_as_protocol_state(
    alice_session: AliceSession,
) -> None:
    llm = FakeLLM(reply_text="Черновой ответ.")

    async def generate_reply_with_offer(**kwargs: object) -> LLMReply:
        llm.call_count += 1
        llm.max_output_tokens_seen.append(cast(int | None, kwargs.get("max_output_tokens")))
        return LLMReply(
            short_text="Я не нашёл подтверждённого лора.",
            raw_text="Я не нашёл подтверждённого лора.",
            followup_mode=FollowupMode.DEFERRED_OFFER,
            followup_text=None,
            continuation_request="Собери подробнее лор стримера EgorFromGor по каналу и клипам.",
        )

    llm.generate_reply = cast(Any, generate_reply_with_offer)
    pending_store = FakePendingStore()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=FakeQueue(),
        llm=llm,
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(
            command="расскажи про лор",
            original_utterance="расскажи про лор",
        ),
    )

    response = await service.handle(payload)
    pending = pending_store.pending["app-1:user-1:session-1:session-1"]

    assert response.response.text == "Я не нашёл подтверждённого лора. Скажи: продолжай."
    assert pending.status == PendingStatus.DELIVERED
    assert pending.followup_mode == FollowupMode.DEFERRED_OFFER
    assert pending.continuation_request == (
        "Собери подробнее лор стримера EgorFromGor по каналу и клипам."
    )


@pytest.mark.asyncio
async def test_continue_after_deferred_offer_enqueues_followup_job(
    alice_session: AliceSession,
) -> None:
    pending_store = FakePendingStore()
    pending_store.pending["app-1:user-1:session-1:session-1"] = PendingReply(
        status=PendingStatus.DELIVERED,
        reply_text="Я не нашёл подтверждённого лора.",
        reply_tts="Я не нашёл подтверждённого лора.",
        tail_text=None,
        followup_mode=FollowupMode.DEFERRED_OFFER,
        continuation_request="Собери подробнее лор стримера EgorFromGor по каналу и клипам.",
        error_message=None,
        job_id="offer-1",
    )
    queue = FakeQueue()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=FakeLLM(),
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session.model_copy(update={"message_id": 2}),
        request=AliceRequestPayload(command="давай", original_utterance="давай"),
    )

    response = await service.handle(payload)

    assert response.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert len(queue.jobs) == 1
    assert queue.jobs[0].user_text == (
        "Собери подробнее лор стримера EgorFromGor по каналу и клипам."
    )


@pytest.mark.asyncio
async def test_soft_affirmation_without_pending_is_treated_as_new_question(
    alice_session: AliceSession,
) -> None:
    llm = FakeLLM(reply_text="Понял как новый запрос.")
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=FakePendingStore(),
        idempotency_store=FakeIdempotencyStore(),
        queue=FakeQueue(),
        llm=llm,
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session,
        request=AliceRequestPayload(command="хорошо", original_utterance="хорошо"),
    )

    response = await service.handle(payload)

    assert response.response.text == "Понял как новый запрос."
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_soft_affirmation_with_pending_is_treated_as_continue(
    alice_session: AliceSession,
) -> None:
    pending_store = FakePendingStore()
    pending_store.pending["app-1:user-1:session-1:session-1"] = PendingReply(
        status=PendingStatus.DELIVERED,
        reply_text="Я не нашёл подтверждённого лора.",
        reply_tts="Я не нашёл подтверждённого лора.",
        tail_text=None,
        followup_mode=FollowupMode.DEFERRED_OFFER,
        continuation_request="Собери подробнее лор стримера EgorFromGor по каналу и клипам.",
        error_message=None,
        job_id="offer-2",
    )
    queue = FakeQueue()
    llm = FakeLLM()
    service = ConversationService(
        conversation_store=FakeConversationStore(),
        pending_store=pending_store,
        idempotency_store=FakeIdempotencyStore(),
        queue=queue,
        llm=llm,
        analytics=FakeAnalytics(),
        llm_fast_timeout=0.1,
    )
    payload = AliceWebhookRequest(
        session=alice_session.model_copy(update={"message_id": 3}),
        request=AliceRequestPayload(command="хорошо", original_utterance="хорошо"),
    )

    response = await service.handle(payload)

    assert response.response.text == "Я готовлю ответ. Скажи: продолжай."
    assert llm.call_count == 0
    assert len(queue.jobs) == 1
