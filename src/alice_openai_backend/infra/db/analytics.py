from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alice_openai_backend.domain.models import ConversationTurn, DeferredJob, LLMReply
from alice_openai_backend.infra.db.models import ConversationTurnRecord, JobResultRecord


class SqlAlchemyAnalyticsSink:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._session_factory = session_factory

    async def persist_turns(self, conversation_key: str, turns: Sequence[ConversationTurn]) -> None:
        if self._session_factory is None or not turns:
            return
        async with self._session_factory() as session:
            session.add_all(
                [
                    ConversationTurnRecord(
                        conversation_key=conversation_key,
                        role=turn.role.value,
                        content=turn.content,
                        created_at=turn.created_at,
                    )
                    for turn in turns
                ]
            )
            await session.commit()

    async def persist_job_result(
        self,
        job: DeferredJob,
        reply: LLMReply | None,
        error: str | None,
    ) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            session.add(
                JobResultRecord(
                    job_id=job.job_id,
                    request_id=job.request_id,
                    conversation_key=job.conversation_key,
                    user_text=job.user_text,
                    status="ok" if error is None else "error",
                    reply_text=None if reply is None else reply.short_text,
                    error_message=error,
                )
            )
            await session.commit()
