from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

from openai import AsyncOpenAI
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from yandex_alice_openai.config import OpenAISettings
from yandex_alice_openai.domain.models import ConversationTurn, FollowupMode, LLMReply, TurnRole

_MIN_RETRY_BUDGET_SECONDS = 0.15
_RETRY_WAIT_SECONDS = 0.05
_SHORT_REPLY_LIMIT = 420
_SHORT_REPLY_HEADROOM = 24
_DEFAULT_MAX_OUTPUT_TOKENS = 256
_FOLLOWUP_MARKER_PATTERN = re.compile(r"\[\[FOLLOWUP_REQUEST:\s*(?P<request>.*?)\s*\]\]", re.DOTALL)
_FOLLOWUP_MARKER_FRAGMENT_PATTERN = re.compile(r"\[\[FOLLOWUP_REQUEST:[\s\S]*$")


class CircuitOpenError(RuntimeError):
    pass


class IncompleteResponseError(RuntimeError):
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        super().__init__(
            "OpenAI returned an incomplete response"
            if reason is None
            else f"OpenAI returned an incomplete response: {reason}"
        )


class SimpleCircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3, reset_after_seconds: float = 20.0) -> None:
        self._failure_threshold = failure_threshold
        self._reset_after_seconds = reset_after_seconds
        self._failures = 0
        self._opened_at = 0.0

    def allow(self) -> bool:
        if self._failures < self._failure_threshold:
            return True
        if monotonic() - self._opened_at >= self._reset_after_seconds:
            self._failures = 0
            self._opened_at = 0.0
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = monotonic()


class OpenAIResponsesAdapter:
    def __init__(self, settings: OpenAISettings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.api_key.get_secret_value())
        self._breaker = SimpleCircuitBreaker()

    async def generate_reply(
        self,
        *,
        user_text: str,
        history: Sequence[ConversationTurn],
        request_id: str,
        deadline_seconds: float,
        max_output_tokens: int | None = None,
    ) -> LLMReply:
        if not self._breaker.allow():
            raise CircuitOpenError("OpenAI circuit breaker is open")

        input_items = _build_input_items(
            system_prompt=self._settings.system_prompt, history=history, user_text=user_text
        )
        tools = _build_tools(
            web_search_enabled=self._settings.web_search_enabled,
            web_search_context_size=self._settings.web_search_context_size,
        )

        try:
            response = await self._create_response_with_budget(
                input_items=input_items,
                tools=tools,
                request_id=request_id,
                deadline_seconds=deadline_seconds,
                max_output_tokens=max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS,
            )
            self._breaker.record_success()
        except Exception:
            self._breaker.record_failure()
            raise

        raw_text = _extract_response_text(response)
        if _response_is_incomplete(response):
            reason = _incomplete_reason(response)
            raise IncompleteResponseError(reason)
        if not raw_text:
            raw_text = "Я не успел собрать ответ. Попробуй спросить иначе."
        return _prepare_llm_reply(raw_text)

    async def _create_response_with_budget(
        self,
        *,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
        request_id: str,
        deadline_seconds: float,
        max_output_tokens: int,
    ) -> Any:
        deadline_at = monotonic() + deadline_seconds
        last_error: TimeoutError | asyncio.TimeoutError | None = None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._settings.max_retries + 1),
            wait=wait_fixed(_RETRY_WAIT_SECONDS),
            retry=retry_if_exception_type((TimeoutError, asyncio.TimeoutError)),
            reraise=True,
        ):
            remaining = deadline_at - monotonic()
            if remaining <= _MIN_RETRY_BUDGET_SECONDS:
                raise TimeoutError("llm fast-path budget exhausted")
            try:
                with attempt:
                    return await asyncio.wait_for(
                        self._client.responses.create(
                            model=self._settings.model,
                            input=cast(list[Any], input_items),
                            tools=cast(list[Any], tools),
                            max_output_tokens=max_output_tokens,
                            temperature=0.4,
                            metadata={"request_id": request_id, "channel": "alice"},
                        ),
                        timeout=remaining,
                    )
            except TimeoutError as exc:
                last_error = exc
                remaining = deadline_at - monotonic()
                if remaining <= _MIN_RETRY_BUDGET_SECONDS + _RETRY_WAIT_SECONDS:
                    raise
        if last_error is not None:
            raise last_error
        raise TimeoutError("llm fast-path budget exhausted")


def _build_input_items(
    *,
    system_prompt: str,
    history: Sequence[ConversationTurn],
    user_text: str,
    current_utc: datetime | None = None,
) -> list[dict[str, object]]:
    utc_now = current_utc or datetime.now(tz=UTC)
    input_items: list[dict[str, object]] = [
        {"role": "developer", "content": _build_developer_prompt(system_prompt, utc_now=utc_now)}
    ]
    for turn in history[-8:]:
        if turn.role == TurnRole.ASSISTANT:
            input_items.append(
                {"role": "assistant", "content": [{"type": "output_text", "text": turn.content}]}
            )
            continue
        if turn.role == TurnRole.SYSTEM:
            input_items.append(
                {"role": "developer", "content": _format_historical_system_turn(turn.content)}
            )
            continue
        input_items.append({"role": "user", "content": turn.content})
    input_items.append({"role": "user", "content": user_text})
    return input_items


def _build_tools(
    *, web_search_enabled: bool, web_search_context_size: str
) -> list[dict[str, object]]:
    if not web_search_enabled:
        return []
    return [{"type": "web_search", "search_context_size": web_search_context_size}]


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", "")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        if getattr(item, "status", None) == "incomplete":
            continue
        for content in getattr(item, "content", []):
            content_type = getattr(content, "type", None)
            if content_type == "output_text":
                text = getattr(content, "text", "")
                if text:
                    parts.append(str(text).strip())
            elif content_type == "refusal":
                refusal = getattr(content, "refusal", "")
                if refusal:
                    parts.append(str(refusal).strip())
    return " ".join(part for part in parts if part).strip()


def _prepare_llm_reply(raw_text: str) -> LLMReply:
    visible_text, continuation_request = _extract_followup_request(raw_text)
    normalized = re.sub(r"\s+", " ", visible_text).strip()
    normalized = normalized.replace("*", "").replace("#", "")
    normalized = re.sub(r"`{1,3}.+?`{1,3}", "", normalized)
    normalized = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", normalized)
    normalized = re.sub(r"[_~]", "", normalized)
    normalized = normalized.strip(" -")
    normalized = _stabilize_incomplete_tail(normalized)
    if len(normalized) <= _SHORT_REPLY_LIMIT:
        return LLMReply(
            short_text=normalized,
            raw_text=normalized,
            followup_mode=(
                FollowupMode.DEFERRED_OFFER
                if continuation_request is not None
                else FollowupMode.NONE
            ),
            followup_text=None,
            continuation_request=continuation_request,
        )

    short_text, tail_text = _split_short_and_tail(normalized, limit=_SHORT_REPLY_LIMIT)
    if tail_text:
        short_text = f"{short_text} Скажи: подробнее."
    return LLMReply(
        short_text=short_text,
        raw_text=normalized,
        followup_mode=FollowupMode.READY_TAIL if tail_text is not None else FollowupMode.NONE,
        followup_text=tail_text,
        continuation_request=None,
    )


def _stabilize_incomplete_tail(text: str) -> str:
    if not text or text[-1] in ".!?…":
        return text
    trimmed = re.sub(r"\s+\S+$", "", text).rstrip(" ,;:")
    if len(trimmed) < max(40, int(len(text) * 0.6)):
        return text
    return f"{trimmed}..."


def _build_developer_prompt(text: str, *, utc_now: datetime) -> str:
    protocol = (
        "Visible answer must stay self-contained and spoken. "
        "Do not promise future actions, ask for confirmation, or say phrases like "
        '"если хочешь, я могу..." in visible text. '
        "Use [[FOLLOWUP_REQUEST: <concise Russian follow-up task>]] only when a follow-up "
        "should run after explicit user confirmation. "
        "The marker must appear at most once, on the final line, and must never be explained "
        "or shown to the user. Omit it otherwise."
    )
    current_utc_line = f"Current UTC date and time: {utc_now.isoformat()}."
    return f"{text}\n\nPrompt protocol: {protocol}\n{current_utc_line}"


def _format_historical_system_turn(text: str) -> str:
    return f"Conversation-specific instruction: {text}"


def _response_is_incomplete(response: Any) -> bool:
    if getattr(response, "status", None) == "incomplete":
        return True
    if _incomplete_reason(response) is not None:
        return True
    for item in getattr(response, "output", []):
        if (
            getattr(item, "type", None) == "message"
            and getattr(item, "status", None) == "incomplete"
        ):
            return True
    return False


def _incomplete_reason(response: Any) -> str | None:
    details = getattr(response, "incomplete_details", None)
    if details is None:
        return None
    reason = getattr(details, "reason", None)
    return str(reason) if reason else None


def _split_short_and_tail(text: str, *, limit: int) -> tuple[str, str | None]:
    if len(text) <= limit:
        return text, None

    preferred_cutoff = limit - _SHORT_REPLY_HEADROOM
    cutoff = text[:preferred_cutoff]
    sentence_end = max(cutoff.rfind("."), cutoff.rfind("!"), cutoff.rfind("?"), cutoff.rfind("…"))
    if sentence_end >= max(120, preferred_cutoff // 2):
        head = cutoff[: sentence_end + 1].rstrip()
        tail = text[sentence_end + 1 :].lstrip()
        return head, tail or None

    word_end = cutoff.rfind(" ")
    if word_end >= max(80, preferred_cutoff // 2):
        head = cutoff[:word_end].rstrip(" ,;:")
        tail = text[word_end:].lstrip()
        return f"{head}...", tail or None

    head = cutoff.rstrip(" ,;:")
    tail = text[len(cutoff) :].lstrip()
    return f"{head}...", tail or None


def _extract_followup_request(raw_text: str) -> tuple[str, str | None]:
    matches = list(_FOLLOWUP_MARKER_PATTERN.finditer(raw_text))
    visible_text = _FOLLOWUP_MARKER_PATTERN.sub("", raw_text)
    visible_text = _FOLLOWUP_MARKER_FRAGMENT_PATTERN.sub("", visible_text).rstrip()

    continuation_request: str | None = None
    for match in reversed(matches):
        candidate = re.sub(r"\s+", " ", match.group("request")).strip(" -")
        if candidate:
            continuation_request = candidate
            break
    return visible_text, continuation_request
