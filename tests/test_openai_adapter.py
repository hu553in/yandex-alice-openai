from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from alice_openai_backend.config import OpenAISettings
from alice_openai_backend.domain.models import ConversationTurn, FollowupMode, TurnRole
from alice_openai_backend.infra.llm.openai_adapter import (
    IncompleteResponseError,
    OpenAIResponsesAdapter,
    _build_input_items,
    _build_tools,
    _extract_followup_request,
    _extract_response_text,
    _prepare_llm_reply,
    _response_is_incomplete,
    _split_short_and_tail,
)

MAX_DEADLINE_ELAPSED_SECONDS = 0.15
REQUESTED_MAX_OUTPUT_TOKENS = 512
SHORT_REPLY_LIMIT = 420


def test_build_input_items_uses_output_text_for_assistant_history() -> None:
    history = [
        ConversationTurn(role=TurnRole.USER, content="Привет"),
        ConversationTurn(role=TurnRole.ASSISTANT, content="Здравствуйте"),
        ConversationTurn(role=TurnRole.SYSTEM, content="Отвечай кратко"),
    ]

    items = _build_input_items(
        system_prompt="Основной системный промпт",
        history=history,
        user_text="Кто ты?",
        current_utc=datetime(2026, 3, 22, 12, 30, tzinfo=UTC),
    )

    assert items[0]["role"] == "developer"
    assert "Основной системный промпт" in str(items[0]["content"])
    assert "[[FOLLOWUP_REQUEST:" in str(items[0]["content"])
    assert "Prompt protocol:" in str(items[0]["content"])
    assert "Current UTC date and time: 2026-03-22T12:30:00+00:00." in str(items[0]["content"])
    assert items[1] == {"role": "user", "content": "Привет"}
    assert items[2] == {
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Здравствуйте"}],
    }
    assert items[3] == {
        "role": "developer",
        "content": "Conversation-specific instruction: Отвечай кратко",
    }
    assert items[4] == {"role": "user", "content": "Кто ты?"}

    developer_contents = [str(item["content"]) for item in items if item["role"] == "developer"]
    combined_developer_prompt = "\n".join(developer_contents)
    assert combined_developer_prompt.count("[[FOLLOWUP_REQUEST:") == 1
    assert combined_developer_prompt.count("Current UTC date and time:") == 1


def test_build_tools_enables_web_search_with_low_context_by_default() -> None:
    assert _build_tools(web_search_enabled=True, web_search_context_size="low") == [
        {
            "type": "web_search",
            "search_context_size": "low",
        }
    ]


def test_build_tools_can_disable_web_search() -> None:
    assert _build_tools(web_search_enabled=False, web_search_context_size="low") == []


def test_extract_response_text_falls_back_to_output_messages() -> None:
    response = SimpleNamespace(
        output_text="",
        output=[
            SimpleNamespace(
                type="message",
                status="completed",
                content=[
                    SimpleNamespace(type="output_text", text="Team Spirit играет в 19:00."),
                    SimpleNamespace(type="refusal", refusal=""),
                ],
            )
        ],
    )

    assert _extract_response_text(response) == "Team Spirit играет в 19:00."


def test_extract_response_text_ignores_incomplete_messages() -> None:
    response = SimpleNamespace(
        output_text="",
        output=[
            SimpleNamespace(
                type="message",
                status="incomplete",
                content=[SimpleNamespace(type="output_text", text="Обрезанный хвост")],
            ),
            SimpleNamespace(
                type="message",
                status="completed",
                content=[SimpleNamespace(type="output_text", text="Готовый ответ.")],
            ),
        ],
    )

    assert _extract_response_text(response) == "Готовый ответ."


def test_response_is_incomplete_when_response_status_is_incomplete() -> None:
    response = SimpleNamespace(status="incomplete", incomplete_details=None, output=[])

    assert _response_is_incomplete(response) is True


def test_response_is_incomplete_when_incomplete_reason_is_present() -> None:
    response = SimpleNamespace(
        status="completed",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[],
    )

    assert _response_is_incomplete(response) is True


def test_response_is_incomplete_when_message_status_is_incomplete() -> None:
    response = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output=[SimpleNamespace(type="message", status="incomplete", content=[])],
    )

    assert _response_is_incomplete(response) is True


@pytest.mark.asyncio
async def test_adapter_enforces_total_deadline_budget_across_attempts() -> None:
    class SlowResponses:
        async def create(self, **_kwargs: object) -> object:
            await asyncio.sleep(0.2)
            return object()

    settings = OpenAISettings(
        api_key=SecretStr("test-key"),
        model="gpt-5-mini",
        timeout_seconds=0.05,
        max_retries=1,
        web_search_enabled=False,
        web_search_context_size="low",
        system_prompt="test",
    )
    adapter = OpenAIResponsesAdapter(settings)
    adapter._client = SimpleNamespace(responses=SlowResponses())

    started_at = monotonic()
    with pytest.raises(TimeoutError):
        await adapter.generate_reply(
            user_text="привет",
            history=[],
            request_id="req-1",
            deadline_seconds=0.05,
        )
    elapsed = monotonic() - started_at

    assert elapsed < MAX_DEADLINE_ELAPSED_SECONDS


@pytest.mark.asyncio
async def test_adapter_rejects_incomplete_openai_response() -> None:
    class IncompleteResponses:
        async def create(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                output_text="Частичный ответ",
                output=[],
            )

    settings = OpenAISettings(
        api_key=SecretStr("test-key"),
        model="gpt-5-mini",
        timeout_seconds=0.2,
        max_retries=0,
        web_search_enabled=False,
        web_search_context_size="low",
        system_prompt="test",
    )
    adapter = OpenAIResponsesAdapter(settings)
    adapter._client = SimpleNamespace(responses=IncompleteResponses())

    with pytest.raises(IncompleteResponseError):
        await adapter.generate_reply(
            user_text="привет",
            history=[],
            request_id="req-2",
            deadline_seconds=0.2,
        )


@pytest.mark.asyncio
async def test_adapter_uses_requested_max_output_tokens() -> None:
    captured_kwargs: dict[str, object] = {}

    class Responses:
        async def create(self, **kwargs: object) -> object:
            captured_kwargs.update(kwargs)
            return SimpleNamespace(
                status="completed",
                incomplete_details=None,
                output_text="Готовый ответ.",
                output=[],
            )

    settings = OpenAISettings(
        api_key=SecretStr("test-key"),
        model="gpt-5-mini",
        timeout_seconds=0.2,
        max_retries=0,
        web_search_enabled=False,
        web_search_context_size="low",
        system_prompt="test",
    )
    adapter = OpenAIResponsesAdapter(settings)
    adapter._client = SimpleNamespace(responses=Responses())

    reply = await adapter.generate_reply(
        user_text="привет",
        history=[],
        request_id="req-3",
        deadline_seconds=0.2,
        max_output_tokens=REQUESTED_MAX_OUTPUT_TOKENS,
    )

    assert reply.short_text == "Готовый ответ."
    assert captured_kwargs["max_output_tokens"] == REQUESTED_MAX_OUTPUT_TOKENS


def test_prepare_llm_reply_does_not_leave_truncated_word_at_the_end() -> None:
    reply = _prepare_llm_reply("Сегодня у Team Spirit матч есть, но точное время появится в распис")

    assert reply.short_text == "Сегодня у Team Spirit матч есть, но точное время появится в..."


def test_extract_followup_request_strips_protocol_marker() -> None:
    visible, continuation_request = _extract_followup_request(
        "Ответ по делу.\n\n[[FOLLOWUP_REQUEST: Собери подробнее лор стримера EgorFromGor.]]"
    )

    assert visible == "Ответ по делу."
    assert continuation_request == "Собери подробнее лор стримера EgorFromGor."


def test_extract_followup_request_uses_last_non_empty_marker_and_strips_all_markers() -> None:
    visible, continuation_request = _extract_followup_request(
        "Ответ по делу.\n"
        "[[FOLLOWUP_REQUEST:   ]]\n"
        "Техническая строка.\n"
        "[[FOLLOWUP_REQUEST: Собери подтвержденные детали по каналу и клипам.]]"
    )

    assert "[[FOLLOWUP_REQUEST:" not in visible
    assert visible == "Ответ по делу.\n\nТехническая строка."
    assert continuation_request == "Собери подтвержденные детали по каналу и клипам."


def test_extract_followup_request_strips_dangling_marker_fragment_from_visible_text() -> None:
    visible, continuation_request = _extract_followup_request(
        "Ответ по делу.\n[[FOLLOWUP_REQUEST: Собери детали"
    )

    assert visible == "Ответ по делу."
    assert continuation_request is None


def test_prepare_llm_reply_uses_deferred_offer_mode_for_followup_marker() -> None:
    reply = _prepare_llm_reply(
        "Я не нашёл подтверждённого лора.\n\n"
        "[[FOLLOWUP_REQUEST: "
        "Собери лор стримера EgorFromGor по каналу, клипам и мемам сообщества."
        "]]"
    )

    assert reply.short_text == "Я не нашёл подтверждённого лора."
    assert reply.followup_mode == FollowupMode.DEFERRED_OFFER
    assert reply.continuation_request == (
        "Собери лор стримера EgorFromGor по каналу, клипам и мемам сообщества."
    )


def test_split_short_and_tail_keeps_first_long_sentence_within_limit() -> None:
    text = (
        "Очень длинное предложение без нормального раннего завершения "
        + "слово " * 120
        + "и только потом заканчивается. Второе предложение короткое."
    )

    head, tail = _split_short_and_tail(text, limit=SHORT_REPLY_LIMIT)

    assert len(head) <= SHORT_REPLY_LIMIT
    assert head.endswith("...")
    assert tail is not None
