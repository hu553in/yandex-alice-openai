from __future__ import annotations

import json
from datetime import datetime

from redis.asyncio import Redis

from alice_openai_backend.config import RedisSettings
from alice_openai_backend.domain.models import (
    ConversationTurn,
    DeferredJob,
    FollowupMode,
    PendingReply,
    PendingStatus,
    TurnRole,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _pending_to_json(pending: PendingReply) -> str:
    return json.dumps(
        {
            "status": pending.status.value,
            "reply_text": pending.reply_text,
            "reply_tts": pending.reply_tts,
            "tail_text": pending.tail_text,
            "followup_mode": pending.followup_mode.value,
            "continuation_request": pending.continuation_request,
            "error_message": pending.error_message,
            "job_id": pending.job_id,
            "updated_at": pending.updated_at.isoformat(),
        }
    )


class RedisKeyspace:
    def __init__(self, settings: RedisSettings) -> None:
        self._prefix = settings.prefix
        self.session_ttl_seconds = settings.session_ttl_seconds
        self.session_turn_limit = settings.session_turn_limit
        self.pending_ttl_seconds = settings.pending_ttl_seconds
        self.idempotency_ttl_seconds = settings.idempotency_ttl_seconds
        self.rate_limit_window_seconds = settings.rate_limit_window_seconds

    def history(self, conversation_key: str) -> str:
        return f"{self._prefix}:history:{conversation_key}"

    def pending(self, conversation_key: str) -> str:
        return f"{self._prefix}:pending:{conversation_key}"

    def idempotency(self, request_key: str) -> str:
        return f"{self._prefix}:idempotency:{request_key}"

    def stream(self) -> str:
        return f"{self._prefix}:jobs"

    def group(self) -> str:
        return f"{self._prefix}:jobs:group"

    def rate_limit(self, bucket: str) -> str:
        return f"{self._prefix}:ratelimit:{bucket}"


class RedisConversationStore:
    def __init__(self, redis: Redis[str], keys: RedisKeyspace) -> None:
        self._redis = redis
        self._keys = keys

    async def get_history(self, conversation_key: str) -> list[ConversationTurn]:
        raw_items = await self._redis.lrange(self._keys.history(conversation_key), 0, -1)
        turns: list[ConversationTurn] = []
        for item in raw_items:
            payload = json.loads(item)
            turns.append(
                ConversationTurn(
                    role=TurnRole(payload["role"]),
                    content=payload["content"],
                    created_at=_dt(payload["created_at"]),
                )
            )
        return turns

    async def append_turn(self, conversation_key: str, turn: ConversationTurn) -> None:
        key = self._keys.history(conversation_key)
        payload = json.dumps(
            {
                "role": turn.role.value,
                "content": turn.content,
                "created_at": turn.created_at.isoformat(),
            }
        )
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.rpush(key, payload)
            await pipe.ltrim(key, -self._keys.session_turn_limit, -1)
            await pipe.expire(key, self._keys.session_ttl_seconds)
            await pipe.execute()


class RedisPendingReplyStore:
    def __init__(self, redis: Redis[str], keys: RedisKeyspace) -> None:
        self._redis = redis
        self._keys = keys

    async def get_pending(self, conversation_key: str) -> PendingReply | None:
        raw = await self._redis.get(self._keys.pending(conversation_key))
        if raw is None:
            return None
        payload = json.loads(raw)
        return PendingReply(
            status=PendingStatus(payload["status"]),
            reply_text=payload.get("reply_text"),
            reply_tts=payload.get("reply_tts"),
            tail_text=payload.get("tail_text"),
            followup_mode=FollowupMode(payload.get("followup_mode", FollowupMode.NONE.value)),
            continuation_request=payload.get("continuation_request"),
            error_message=payload.get("error_message"),
            job_id=payload["job_id"],
            updated_at=_dt(payload["updated_at"]),
        )

    async def start_pending(self, conversation_key: str, job: DeferredJob) -> PendingReply:
        key = self._keys.pending(conversation_key)
        payload = PendingReply(
            status=PendingStatus.PROCESSING,
            reply_text=None,
            reply_tts=None,
            tail_text=None,
            followup_mode=FollowupMode.NONE,
            continuation_request=None,
            error_message=None,
            job_id=job.job_id,
        )
        created = await self._redis.set(
            key,
            _pending_to_json(payload),
            ex=self._keys.pending_ttl_seconds,
            nx=True,
        )
        if created:
            return payload
        existing = await self.get_pending(conversation_key)
        if existing is not None:
            return existing
        return payload

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
        current = await self.get_pending(conversation_key)
        if current is not None and current.job_id != job_id:
            return None
        payload = PendingReply(
            status=PendingStatus.READY,
            reply_text=reply_text,
            reply_tts=reply_tts,
            tail_text=tail_text,
            followup_mode=followup_mode,
            continuation_request=continuation_request,
            error_message=None,
            job_id=job_id,
        )
        await self._redis.set(
            self._keys.pending(conversation_key),
            _pending_to_json(payload),
            ex=self._keys.pending_ttl_seconds,
        )
        return payload

    async def mark_failed(self, conversation_key: str, *, job_id: str, error_message: str) -> None:
        current = await self.get_pending(conversation_key)
        if current is not None and current.job_id != job_id:
            return
        payload = PendingReply(
            status=PendingStatus.FAILED,
            reply_text=None,
            reply_tts=None,
            tail_text=None,
            followup_mode=FollowupMode.NONE,
            continuation_request=None,
            error_message=error_message,
            job_id=job_id,
        )
        await self._redis.set(
            self._keys.pending(conversation_key),
            _pending_to_json(payload),
            ex=self._keys.pending_ttl_seconds,
        )

    async def mark_delivered(self, conversation_key: str, *, job_id: str) -> None:
        current = await self.get_pending(conversation_key)
        if current is None or current.job_id != job_id:
            return
        payload = PendingReply(
            status=PendingStatus.DELIVERED,
            reply_text=current.reply_text,
            reply_tts=current.reply_tts,
            tail_text=current.tail_text,
            followup_mode=current.followup_mode,
            continuation_request=current.continuation_request,
            error_message=current.error_message,
            job_id=job_id,
        )
        await self._redis.set(
            self._keys.pending(conversation_key),
            _pending_to_json(payload),
            ex=self._keys.pending_ttl_seconds,
        )

    async def clear_pending(self, conversation_key: str, *, job_id: str | None = None) -> None:
        if job_id is None:
            await self._redis.delete(self._keys.pending(conversation_key))
            return
        current = await self.get_pending(conversation_key)
        if current is None or current.job_id != job_id:
            return
        await self._redis.delete(self._keys.pending(conversation_key))


class RedisIdempotencyStore:
    def __init__(self, redis: Redis[str], keys: RedisKeyspace) -> None:
        self._redis = redis
        self._keys = keys

    async def get_cached_response(self, key: str) -> dict[str, object] | None:
        raw = await self._redis.get(self._keys.idempotency(key))
        return None if raw is None else json.loads(raw)

    async def store_response(self, key: str, response: dict[str, object]) -> None:
        await self._redis.set(
            self._keys.idempotency(key),
            json.dumps(response),
            ex=self._keys.idempotency_ttl_seconds,
        )
