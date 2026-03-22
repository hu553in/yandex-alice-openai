from __future__ import annotations

from dataclasses import dataclass

import pytest

from alice_openai_backend.domain.models import DeferredJob
from alice_openai_backend.workers.deferred_worker import _process_job


class StubConversationService:
    def __init__(self, *, fail_process: bool = False, fail_persist: bool = False) -> None:
        self.fail_process = fail_process
        self.fail_persist = fail_persist
        self.process_calls = 0
        self.fail_calls = 0

    async def process_deferred_job(self, job: DeferredJob, *, deadline_seconds: float) -> None:
        self.process_calls += 1
        if self.fail_process:
            raise RuntimeError("process failed")

    async def fail_deferred_job(self, job: DeferredJob, *, error_message: str) -> None:
        self.fail_calls += 1
        if self.fail_persist:
            raise RuntimeError("persist failed")


@dataclass
class StubContainer:
    conversation_service: StubConversationService


@pytest.mark.asyncio
async def test_process_job_requests_ack_after_success() -> None:
    container = StubContainer(conversation_service=StubConversationService())
    job = DeferredJob(
        job_id="job-1",
        request_id="req-1",
        conversation_key="scope-1",
        user_text="вопрос",
    )

    should_ack = await _process_job(container, job)

    assert should_ack is True
    assert container.conversation_service.process_calls == 1
    assert container.conversation_service.fail_calls == 0


@pytest.mark.asyncio
async def test_process_job_requests_ack_after_failure_is_persisted() -> None:
    container = StubContainer(conversation_service=StubConversationService(fail_process=True))
    job = DeferredJob(
        job_id="job-2",
        request_id="req-2",
        conversation_key="scope-2",
        user_text="вопрос",
    )

    should_ack = await _process_job(container, job)

    assert should_ack is True
    assert container.conversation_service.process_calls == 1
    assert container.conversation_service.fail_calls == 1


@pytest.mark.asyncio
async def test_process_job_keeps_message_pending_when_failure_persist_fails() -> None:
    container = StubContainer(
        conversation_service=StubConversationService(fail_process=True, fail_persist=True)
    )
    job = DeferredJob(
        job_id="job-3",
        request_id="req-3",
        conversation_key="scope-3",
        user_text="вопрос",
    )

    should_ack = await _process_job(container, job)

    assert should_ack is False
    assert container.conversation_service.process_calls == 1
    assert container.conversation_service.fail_calls == 1
