from __future__ import annotations

from redis.asyncio import Redis, from_url

from alice_openai_backend.config import RedisSettings


def build_redis(settings: RedisSettings) -> Redis[str]:
    return from_url(settings.url, encoding="utf-8", decode_responses=True)
