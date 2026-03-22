from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from alice_openai_backend.domain.models import DeferredJob
from alice_openai_backend.infra.redis.stores import RedisKeyspace


class RedisStreamQueue:
    def __init__(
        self,
        redis: Redis[str],
        keys: RedisKeyspace,
        *,
        reclaim_idle_ms: int,
        poll_timeout_ms: int,
    ) -> None:
        self._redis = redis
        self._keys = keys
        self._consumer_group = keys.group()
        self._reclaim_idle_ms = reclaim_idle_ms
        self._poll_timeout_ms = poll_timeout_ms

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._keys.stream(),
                self._consumer_group,
                id="$",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, job: DeferredJob) -> None:
        stream_id = await self._redis.xadd(
            self._keys.stream(),
            {
                "job_id": job.job_id,
                "request_id": job.request_id,
                "conversation_key": job.conversation_key,
                "user_text": job.user_text,
                "enqueued_at": job.enqueued_at.isoformat(),
            },
        )
        object.__setattr__(job, "stream_id", stream_id)

    async def read_group(self, consumer_name: str) -> list[DeferredJob]:
        reclaimed = await self._redis.xautoclaim(
            self._keys.stream(),
            self._consumer_group,
            consumer_name,
            self._reclaim_idle_ms,
            "0-0",
            count=1,
        )
        reclaimed_messages = reclaimed[1] if reclaimed else []
        if reclaimed_messages:
            return _decode_jobs(reclaimed_messages)

        records = await self._redis.xreadgroup(
            groupname=self._consumer_group,
            consumername=consumer_name,
            streams={self._keys.stream(): ">"},
            count=1,
            block=self._poll_timeout_ms,
        )
        for _, stream_items in records:
            return _decode_jobs(stream_items)
        return []

    async def ack(self, stream_id: str) -> None:
        await self._redis.xack(  # type: ignore[no-untyped-call]
            self._keys.stream(),
            self._consumer_group,
            stream_id,
        )


def _decode_jobs(stream_items: list[tuple[str, dict[str, str]]]) -> list[DeferredJob]:
    jobs: list[DeferredJob] = []
    for stream_id, payload in stream_items:
        jobs.append(
            DeferredJob(
                job_id=payload.get("job_id", stream_id or uuid4().hex),
                request_id=payload["request_id"],
                conversation_key=payload["conversation_key"],
                user_text=payload["user_text"],
                stream_id=stream_id,
                enqueued_at=datetime.fromisoformat(
                    payload.get("enqueued_at", datetime.now(tz=UTC).isoformat())
                ),
            )
        )
    return jobs
