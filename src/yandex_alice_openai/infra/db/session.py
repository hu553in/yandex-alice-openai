from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from yandex_alice_openai.config import DatabaseSettings
from yandex_alice_openai.infra.db.base import Base


def build_engine(settings: DatabaseSettings) -> AsyncEngine | None:
    if not settings.url:
        return None
    return create_async_engine(settings.url, echo=settings.echo, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine | None) -> async_sessionmaker[AsyncSession] | None:
    if engine is None:
        return None
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_database(engine: AsyncEngine | None) -> None:
    if engine is None:
        return
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> AsyncIterator[AsyncSession | None]:
    if session_factory is None:
        yield None
        return
    async with session_factory() as session:
        yield session
