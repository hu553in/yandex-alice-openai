from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alice_openai_backend.config import Settings
from alice_openai_backend.domain.ports import AnalyticsSink, IdempotencyStore
from alice_openai_backend.infra.db.analytics import SqlAlchemyAnalyticsSink
from alice_openai_backend.infra.db.session import (
    build_engine,
    build_session_factory,
    dispose_engine,
    init_database,
)
from alice_openai_backend.infra.llm.openai_adapter import OpenAIResponsesAdapter
from alice_openai_backend.infra.queue.redis_streams import RedisStreamQueue
from alice_openai_backend.infra.redis.client import build_redis
from alice_openai_backend.infra.redis.stores import (
    RedisConversationStore,
    RedisIdempotencyStore,
    RedisKeyspace,
    RedisPendingReplyStore,
)
from alice_openai_backend.infra.security.rate_limit import RedisRateLimiter
from alice_openai_backend.services.conversation import ConversationService


@dataclass(slots=True)
class Container:
    settings: Settings
    redis: Redis[str]
    engine: AsyncEngine | None
    session_factory: async_sessionmaker[AsyncSession] | None
    keys: RedisKeyspace
    queue: RedisStreamQueue
    analytics: AnalyticsSink
    idempotency_store: IdempotencyStore
    conversation_service: ConversationService
    rate_limiter: RedisRateLimiter

    async def start(self) -> None:
        await self.queue.ensure_group()
        await init_database(self.engine)

    async def stop(self) -> None:
        await self.redis.close()
        await dispose_engine(self.engine)


def build_container(settings: Settings) -> Container:
    redis = build_redis(settings.redis())
    keys = RedisKeyspace(settings.redis())
    conversation_store = RedisConversationStore(redis, keys)
    pending_store = RedisPendingReplyStore(redis, keys)
    idempotency_store = RedisIdempotencyStore(redis, keys)
    queue = RedisStreamQueue(
        redis,
        keys,
        reclaim_idle_ms=int(settings.worker().job_timeout_seconds * 1000),
        poll_timeout_ms=settings.worker().poll_timeout_ms,
    )
    engine = build_engine(settings.database())
    session_factory = build_session_factory(engine)
    analytics = SqlAlchemyAnalyticsSink(session_factory)
    llm = OpenAIResponsesAdapter(settings.openai())
    conversation_service = ConversationService(
        conversation_store=conversation_store,
        pending_store=pending_store,
        idempotency_store=idempotency_store,
        queue=queue,
        llm=llm,
        analytics=analytics,
        llm_fast_timeout=settings.openai().timeout_seconds,
    )
    rate_limiter = RedisRateLimiter(redis, keys, settings.app())
    return Container(
        settings=settings,
        redis=redis,
        engine=engine,
        session_factory=session_factory,
        keys=keys,
        queue=queue,
        analytics=analytics,
        idempotency_store=idempotency_store,
        conversation_service=conversation_service,
        rate_limiter=rate_limiter,
    )
