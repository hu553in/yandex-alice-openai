from __future__ import annotations

from fastapi import HTTPException, status
from redis.asyncio import Redis

from yandex_alice_openai.config import AppSettings
from yandex_alice_openai.infra.redis.stores import RedisKeyspace


class RedisRateLimiter:
    def __init__(self, redis: Redis[str], keys: RedisKeyspace, settings: AppSettings) -> None:
        self._redis = redis
        self._keys = keys
        self._settings = settings

    async def check(self, bucket: str) -> None:
        key = self._keys.rate_limit(bucket)
        async with self._redis.pipeline(transaction=True) as pipe:
            await pipe.incr(key)
            await pipe.expire(key, self._keys.rate_limit_window_seconds, nx=True)
            count, _ = await pipe.execute()
        if int(count) > self._settings.rate_limit_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded"
            )
