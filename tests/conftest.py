from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from yandex_alice_openai.domain.models import (
    ConversationTurn,
    DeferredJob,
    FollowupMode,
    LLMReply,
    PendingReply,
    PendingStatus,
)
from yandex_alice_openai.schemas.alice import AliceApplication, AliceSession, AliceUser


class FakeConversationStore:
    def __init__(self) -> None:
        self.history: dict[str, list[ConversationTurn]] = {}

    async def get_history(self, conversation_key: str) -> list[ConversationTurn]:
        return list(self.history.get(conversation_key, []))

    async def append_turn(self, conversation_key: str, turn: ConversationTurn) -> None:
        self.history.setdefault(conversation_key, []).append(turn)


class FakePendingStore:
    def __init__(self) -> None:
        self.pending: dict[str, PendingReply] = {}

    async def get_pending(self, conversation_key: str) -> PendingReply | None:
        return self.pending.get(conversation_key)

    async def start_pending(self, conversation_key: str, job: DeferredJob) -> PendingReply:
        current = self.pending.get(conversation_key)
        if current is not None:
            return current
        pending = PendingReply(
            status=PendingStatus.PROCESSING,
            reply_text=None,
            reply_tts=None,
            tail_text=None,
            followup_mode=FollowupMode.NONE,
            continuation_request=None,
            error_message=None,
            job_id=job.job_id,
        )
        self.pending[conversation_key] = pending
        return pending

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
    ) -> PendingReply | None:
        current = self.pending.get(conversation_key)
        if current is not None and current.job_id != job_id:
            return None
        pending = PendingReply(
            status=PendingStatus.READY,
            reply_text=reply_text,
            reply_tts=reply_tts,
            tail_text=tail_text,
            followup_mode=followup_mode,
            continuation_request=continuation_request,
            error_message=None,
            job_id=job_id,
        )
        self.pending[conversation_key] = pending
        return pending

    async def mark_failed(self, conversation_key: str, *, job_id: str, error_message: str) -> None:
        self.pending[conversation_key] = PendingReply(
            status=PendingStatus.FAILED,
            reply_text=None,
            reply_tts=None,
            tail_text=None,
            followup_mode=FollowupMode.NONE,
            continuation_request=None,
            error_message=error_message,
            job_id=job_id,
        )

    async def mark_delivered(self, conversation_key: str, *, job_id: str) -> None:
        current = self.pending[conversation_key]
        self.pending[conversation_key] = PendingReply(
            status=PendingStatus.DELIVERED,
            reply_text=current.reply_text,
            reply_tts=current.reply_tts,
            tail_text=current.tail_text,
            followup_mode=current.followup_mode,
            continuation_request=current.continuation_request,
            error_message=current.error_message,
            job_id=job_id,
        )

    async def clear_pending(self, conversation_key: str, *, job_id: str | None = None) -> None:
        current = self.pending.get(conversation_key)
        if current is None:
            return
        if job_id is not None and current.job_id != job_id:
            return
        self.pending.pop(conversation_key, None)


class FakeIdempotencyStore:
    def __init__(self) -> None:
        self.responses: dict[str, dict[str, object]] = {}

    async def get_cached_response(self, key: str) -> dict[str, object] | None:
        return self.responses.get(key)

    async def store_response(self, key: str, response: dict[str, object]) -> None:
        self.responses[key] = response


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[DeferredJob] = []

    async def enqueue(self, job: DeferredJob) -> None:
        self.jobs.append(job)

    async def read_group(self, consumer_name: str) -> list[DeferredJob]:
        _ = consumer_name
        return []

    async def ack(self, stream_id: str) -> None:
        _ = stream_id


class FailingQueue(FakeQueue):
    async def enqueue(self, job: DeferredJob) -> None:
        _ = job
        raise RuntimeError("queue down")


class FakeAnalytics:
    async def persist_turns(self, conversation_key: str, turns: Sequence[ConversationTurn]) -> None:
        _ = conversation_key, turns

    async def persist_job_result(
        self, job: DeferredJob, reply: LLMReply | None, error: str | None
    ) -> None:
        _ = job, reply, error


class FakeLLM:
    def __init__(
        self, *, fail: bool = False, reply_text: str = "Короткий ответ.", delay_seconds: float = 0.0
    ) -> None:
        self.fail = fail
        self.reply_text = reply_text
        self.delay_seconds = delay_seconds
        self.call_count = 0
        self.deadlines: list[float] = []
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
        _ = user_text, history, request_id
        self.call_count += 1
        self.deadlines.append(deadline_seconds)
        self.max_output_tokens_seen.append(max_output_tokens)
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            raise TimeoutError("fast path timeout")
        return LLMReply(
            short_text=self.reply_text,
            raw_text=self.reply_text,
            followup_mode=FollowupMode.NONE,
            followup_text=None,
            continuation_request=None,
        )


@dataclass
class FakeSettingsApp:
    webhook_secret = None


@dataclass
class FakeSettings:
    def app(self) -> FakeSettingsApp:
        return FakeSettingsApp()


@pytest.fixture
def alice_session() -> AliceSession:
    return AliceSession(
        session_id="session-1",
        message_id=1,
        user_id="user-1",
        application=AliceApplication(application_id="app-1"),
        user=AliceUser(user_id="user-1"),
    )


def ready_pending(
    *,
    job_id: str = "job-1",
    text: str = "Готовый ответ.",
    tail_text: str | None = None,
    followup_mode: FollowupMode = FollowupMode.NONE,
    continuation_request: str | None = None,
) -> PendingReply:
    return PendingReply(
        status=PendingStatus.READY,
        reply_text=text,
        reply_tts=text,
        tail_text=tail_text,
        followup_mode=followup_mode,
        continuation_request=continuation_request,
        error_message=None,
        job_id=job_id,
        updated_at=datetime.now(tz=UTC),
    )
