from __future__ import annotations

from collections.abc import Sequence
from time import monotonic
from uuid import uuid4

from alice_openai_backend.domain.models import (
    ConversationTurn,
    DeferredJob,
    FollowupMode,
    LLMReply,
    PendingReply,
    PendingStatus,
    TurnRole,
)
from alice_openai_backend.domain.ports import (
    AnalyticsSink,
    ConversationStore,
    IdempotencyStore,
    JobQueue,
    LLMProvider,
    PendingReplyStore,
)
from alice_openai_backend.infra.observability.logging import get_logger
from alice_openai_backend.infra.observability.metrics import DEFERRED_JOB_COUNT, LLM_COUNT
from alice_openai_backend.schemas.alice import (
    AliceButtonsItem,
    AliceWebhookRequest,
    AliceWebhookResponse,
)
from alice_openai_backend.services.identity import build_user_scope
from alice_openai_backend.services.prompting import is_continue_intent, sanitize_user_text
from alice_openai_backend.services.renderer import render_voice_response

_FAST_PATH_MAX_OUTPUT_TOKENS = 256
_DEFERRED_MAX_OUTPUT_TOKEN_STEPS = (256, 512, 1024, 2048)


class ConversationService:
    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        pending_store: PendingReplyStore,
        idempotency_store: IdempotencyStore,
        queue: JobQueue,
        llm: LLMProvider,
        analytics: AnalyticsSink,
        llm_fast_timeout: float,
    ) -> None:
        self._conversation_store = conversation_store
        self._pending_store = pending_store
        self._idempotency_store = idempotency_store
        self._queue = queue
        self._llm = llm
        self._analytics = analytics
        self._llm_fast_timeout = llm_fast_timeout
        self._logger = get_logger()

    async def handle(self, payload: AliceWebhookRequest) -> AliceWebhookResponse:
        cached = await self._idempotency_store.get_cached_response(payload.request_key())
        if cached is not None:
            return AliceWebhookResponse.model_validate(cached)

        scope = build_user_scope(payload)
        utterance = sanitize_user_text(payload.utterance())
        pending = await self._pending_store.get_pending(scope.conversation_key)
        if not utterance:
            response = AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response("Скажи вопрос еще раз, коротко и без паузы."),
            )
            await self._idempotency_store.store_response(
                payload.request_key(),
                response.model_dump(),
            )
            return response

        if is_continue_intent(utterance, pending_exists=pending is not None):
            response = await self._handle_continue(payload, scope.conversation_key, pending)
            await self._idempotency_store.store_response(
                payload.request_key(),
                response.model_dump(),
            )
            return response

        if pending is not None:
            blocking_response = await self._handle_active_pending(
                payload,
                scope.conversation_key,
                pending,
            )
            if blocking_response is not None:
                await self._idempotency_store.store_response(
                    payload.request_key(),
                    blocking_response.model_dump(),
                )
                return blocking_response

        history = await self._conversation_store.get_history(scope.conversation_key)
        user_turn = ConversationTurn(role=TurnRole.USER, content=utterance)

        try:
            reply = await self._llm.generate_reply(
                user_text=utterance,
                history=history,
                request_id=payload.request_key(),
                deadline_seconds=self._llm_fast_timeout,
                max_output_tokens=_FAST_PATH_MAX_OUTPUT_TOKENS,
            )
            LLM_COUNT.labels("fast_success").inc()
        except Exception as exc:
            self._logger.warning(
                "llm_fast_path_deferred",
                request_id=payload.request_key(),
                conversation_key=scope.conversation_key,
                error=type(exc).__name__,
            )
            LLM_COUNT.labels("fast_deferred").inc()
            response = await self._defer_request(payload, scope.conversation_key, utterance)
        else:
            response = AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response(
                    _present_reply_text(reply.short_text, reply.followup_mode),
                    buttons=_continuation_buttons(reply.followup_mode),
                ),
            )
            await self._record_completed_turns(
                scope.conversation_key,
                user_turn,
                ConversationTurn(role=TurnRole.ASSISTANT, content=reply.raw_text),
            )
            await self._store_immediate_followup(scope.conversation_key, reply)

        await self._idempotency_store.store_response(payload.request_key(), response.model_dump())
        return response

    async def _handle_active_pending(
        self,
        payload: AliceWebhookRequest,
        conversation_key: str,
        pending: PendingReply,
    ) -> AliceWebhookResponse | None:
        if pending.status == PendingStatus.PROCESSING:
            return AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response("Я еще готовлю предыдущий ответ. Скажи: продолжай."),
            )
        if pending.status == PendingStatus.READY:
            return AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response("У меня готов предыдущий ответ. Скажи: продолжай."),
            )
        if pending.status == PendingStatus.FAILED:
            await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
            return None
        if pending.status == PendingStatus.DELIVERED and (
            pending.tail_text or pending.followup_mode == FollowupMode.DEFERRED_OFFER
        ):
            await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
            return None
        if pending.status == PendingStatus.DELIVERED:
            await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
        return None

    async def _handle_continue(
        self,
        payload: AliceWebhookRequest,
        conversation_key: str,
        pending: PendingReply | None,
    ) -> AliceWebhookResponse:
        if pending is None:
            return AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response("Сейчас продолжения нет. Задай новый вопрос."),
            )
        if pending.status == PendingStatus.PROCESSING:
            return AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response("Я еще готовлю ответ. Скажи: продолжай."),
            )
        if pending.status == PendingStatus.FAILED:
            await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
            return AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response(
                    "Не удалось подготовить ответ. Задай вопрос еще раз другими словами."
                ),
            )
        if (
            pending.status == PendingStatus.DELIVERED
            and pending.followup_mode == FollowupMode.DEFERRED_OFFER
            and pending.continuation_request
        ):
            await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
            return await self._defer_request(
                payload,
                conversation_key,
                pending.continuation_request,
            )
        if pending.status in {PendingStatus.READY, PendingStatus.DELIVERED} and pending.reply_text:
            response = AliceWebhookResponse(
                session=payload.session,
                response=render_voice_response(
                    _present_reply_text(pending.reply_text, pending.followup_mode),
                    buttons=_continuation_buttons(
                        FollowupMode.READY_TAIL
                        if pending.tail_text is not None
                        else pending.followup_mode
                    ),
                ),
            )
            if pending.status == PendingStatus.READY:
                await self._pending_store.mark_delivered(conversation_key, job_id=pending.job_id)
            if pending.tail_text:
                updated = await self._pending_store.mark_ready(
                    conversation_key,
                    job_id=pending.job_id,
                    reply_text=pending.tail_text,
                    reply_tts=pending.tail_text,
                    tail_text=None,
                    followup_mode=FollowupMode.READY_TAIL,
                    continuation_request=None,
                )
                if updated is None:
                    await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
            elif (
                pending.followup_mode == FollowupMode.DEFERRED_OFFER
                and pending.continuation_request
            ):
                return response
            else:
                await self._pending_store.clear_pending(conversation_key, job_id=pending.job_id)
            return response
        return AliceWebhookResponse(
            session=payload.session,
            response=render_voice_response(
                "Продолжения пока нет. Попробуй еще раз через пару секунд."
            ),
        )

    async def process_deferred_job(self, job: DeferredJob, *, deadline_seconds: float) -> LLMReply:
        current_pending = await self._pending_store.get_pending(job.conversation_key)
        if current_pending is None or current_pending.job_id != job.job_id:
            self._logger.info(
                "deferred_job_skipped",
                job_id=job.job_id,
                conversation_key=job.conversation_key,
                reason="missing_or_replaced_pending",
            )
            return LLMReply(
                short_text="",
                raw_text="",
                followup_mode=FollowupMode.NONE,
                followup_text=None,
                continuation_request=None,
            )
        if current_pending.status != PendingStatus.PROCESSING:
            self._logger.info(
                "deferred_job_skipped",
                job_id=job.job_id,
                conversation_key=job.conversation_key,
                reason=current_pending.status.value,
            )
            return LLMReply(
                short_text="",
                raw_text="",
                followup_mode=FollowupMode.NONE,
                followup_text=None,
                continuation_request=None,
            )
        history = await self._conversation_store.get_history(job.conversation_key)
        reply = await self._generate_deferred_reply(
            job=job,
            history=history,
            deadline_seconds=deadline_seconds,
        )
        user_turn = ConversationTurn(role=TurnRole.USER, content=job.user_text)
        assistant_turn = ConversationTurn(role=TurnRole.ASSISTANT, content=reply.raw_text)
        await self._append_turns(job.conversation_key, user_turn, assistant_turn)
        updated = await self._pending_store.mark_ready(
            job.conversation_key,
            job_id=job.job_id,
            reply_text=reply.short_text,
            reply_tts=reply.short_text,
            tail_text=reply.followup_text,
            followup_mode=reply.followup_mode,
            continuation_request=reply.continuation_request,
        )
        if updated is None:
            self._logger.warning(
                "deferred_job_stale",
                job_id=job.job_id,
                conversation_key=job.conversation_key,
            )
            return reply
        DEFERRED_JOB_COUNT.labels("ready").inc()
        await self._persist_analytics(
            job.conversation_key,
            [user_turn, assistant_turn],
            job,
            reply,
            None,
        )
        return reply

    async def fail_deferred_job(self, job: DeferredJob, *, error_message: str) -> None:
        await self._pending_store.mark_failed(
            job.conversation_key,
            job_id=job.job_id,
            error_message=error_message,
        )
        DEFERRED_JOB_COUNT.labels("failed").inc()
        await self._persist_analytics(job.conversation_key, [], job, None, error_message)

    async def _store_immediate_followup(self, conversation_key: str, reply: LLMReply) -> None:
        if reply.followup_mode == FollowupMode.NONE:
            return

        job_id = uuid4().hex
        if reply.followup_mode == FollowupMode.READY_TAIL and reply.followup_text:
            await self._pending_store.mark_ready(
                conversation_key,
                job_id=job_id,
                reply_text=reply.followup_text,
                reply_tts=reply.followup_text,
                tail_text=None,
                followup_mode=FollowupMode.READY_TAIL,
                continuation_request=None,
            )
            return

        if (
            reply.followup_mode == FollowupMode.DEFERRED_OFFER
            and reply.continuation_request is not None
        ):
            await self._pending_store.mark_ready(
                conversation_key,
                job_id=job_id,
                reply_text=reply.short_text,
                reply_tts=reply.short_text,
                tail_text=None,
                followup_mode=FollowupMode.DEFERRED_OFFER,
                continuation_request=reply.continuation_request,
            )
            await self._pending_store.mark_delivered(conversation_key, job_id=job_id)

    async def _generate_deferred_reply(
        self,
        *,
        job: DeferredJob,
        history: Sequence[ConversationTurn],
        deadline_seconds: float,
    ) -> LLMReply:
        deadline_at = monotonic() + deadline_seconds
        last_incomplete: Exception | None = None

        for max_output_tokens in _DEFERRED_MAX_OUTPUT_TOKEN_STEPS:
            remaining = deadline_at - monotonic()
            if remaining <= 0:
                break
            try:
                return await self._llm.generate_reply(
                    user_text=job.user_text,
                    history=history,
                    request_id=job.request_id,
                    deadline_seconds=remaining,
                    max_output_tokens=max_output_tokens,
                )
            except Exception as exc:
                reason = getattr(exc, "reason", None)
                if reason != "max_output_tokens":
                    raise
                last_incomplete = exc

        if last_incomplete is not None:
            raise last_incomplete
        raise TimeoutError("deferred generation budget exhausted")

    async def _defer_request(
        self,
        payload: AliceWebhookRequest,
        conversation_key: str,
        utterance: str,
    ) -> AliceWebhookResponse:
        job = DeferredJob(
            job_id=uuid4().hex,
            request_id=payload.request_key(),
            conversation_key=conversation_key,
            user_text=utterance,
        )
        pending = await self._pending_store.start_pending(conversation_key, job)
        if pending.job_id == job.job_id:
            try:
                await self._queue.enqueue(job)
            except Exception:
                await self._pending_store.clear_pending(conversation_key, job_id=job.job_id)
                raise
            DEFERRED_JOB_COUNT.labels("queued").inc()
        return AliceWebhookResponse(
            session=payload.session,
            response=render_voice_response(
                "Я готовлю ответ. Скажи: продолжай.",
                buttons=[AliceButtonsItem(title="Продолжай")],
            ),
        )

    async def _record_completed_turns(
        self,
        conversation_key: str,
        user_turn: ConversationTurn,
        assistant_turn: ConversationTurn,
    ) -> None:
        await self._append_turns(conversation_key, user_turn, assistant_turn)
        await self._persist_analytics(
            conversation_key,
            [user_turn, assistant_turn],
            None,
            None,
            None,
        )

    async def _append_turns(
        self,
        conversation_key: str,
        user_turn: ConversationTurn,
        assistant_turn: ConversationTurn,
    ) -> None:
        try:
            await self._conversation_store.append_turn(conversation_key, user_turn)
            await self._conversation_store.append_turn(conversation_key, assistant_turn)
        except Exception as exc:
            self._logger.warning(
                "conversation_history_persist_failed",
                conversation_key=conversation_key,
                error=type(exc).__name__,
            )

    async def _persist_analytics(
        self,
        conversation_key: str,
        turns: list[ConversationTurn],
        job: DeferredJob | None,
        reply: LLMReply | None,
        error: str | None,
    ) -> None:
        try:
            if turns:
                await self._analytics.persist_turns(conversation_key, turns)
            if job is not None:
                await self._analytics.persist_job_result(job, reply, error)
        except Exception as exc:
            self._logger.warning(
                "analytics_persist_failed",
                conversation_key=conversation_key,
                error=type(exc).__name__,
            )


def _continuation_buttons(followup_mode: FollowupMode) -> list[AliceButtonsItem]:
    if followup_mode == FollowupMode.READY_TAIL:
        return [AliceButtonsItem(title="Подробнее")]
    if followup_mode == FollowupMode.DEFERRED_OFFER:
        return [AliceButtonsItem(title="Продолжай")]
    return []


def _present_reply_text(text: str, followup_mode: FollowupMode) -> str:
    if followup_mode == FollowupMode.DEFERRED_OFFER:
        return f"{text} Скажи: продолжай."
    return text
