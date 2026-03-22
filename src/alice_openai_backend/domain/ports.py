from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from alice_openai_backend.domain.models import (
    ConversationTurn,
    DeferredJob,
    FollowupMode,
    LLMReply,
    PendingReply,
)


class ConversationStore(Protocol):
    async def get_history(self, conversation_key: str) -> Sequence[ConversationTurn]: ...

    async def append_turn(self, conversation_key: str, turn: ConversationTurn) -> None: ...


class PendingReplyStore(Protocol):
    async def get_pending(self, conversation_key: str) -> PendingReply | None: ...

    async def start_pending(self, conversation_key: str, job: DeferredJob) -> PendingReply: ...

    async def mark_ready(
        self,
        conversation_key: str,
        *,
        job_id: str,
        reply_text: str,
        reply_tts: str,
        tail_text: str | None,
        followup_mode: FollowupMode = FollowupMode.NONE,
        continuation_request: str | None = None,
    ) -> PendingReply | None: ...

    async def mark_failed(
        self,
        conversation_key: str,
        *,
        job_id: str,
        error_message: str,
    ) -> None: ...

    async def mark_delivered(self, conversation_key: str, *, job_id: str) -> None: ...

    async def clear_pending(self, conversation_key: str, *, job_id: str | None = None) -> None: ...


class IdempotencyStore(Protocol):
    async def get_cached_response(self, key: str) -> dict[str, object] | None: ...

    async def store_response(self, key: str, response: dict[str, object]) -> None: ...


class JobQueue(Protocol):
    async def enqueue(self, job: DeferredJob) -> None: ...

    async def read_group(self, consumer_name: str) -> list[DeferredJob]: ...

    async def ack(self, stream_id: str) -> None: ...


class AnalyticsSink(Protocol):
    async def persist_turns(
        self,
        conversation_key: str,
        turns: Sequence[ConversationTurn],
    ) -> None: ...

    async def persist_job_result(
        self,
        job: DeferredJob,
        reply: LLMReply | None,
        error: str | None,
    ) -> None: ...


class LLMProvider(Protocol):
    async def generate_reply(
        self,
        *,
        user_text: str,
        history: Sequence[ConversationTurn],
        request_id: str,
        deadline_seconds: float,
        max_output_tokens: int | None = None,
    ) -> LLMReply: ...
