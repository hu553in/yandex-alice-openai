from __future__ import annotations

import asyncio
import socket
from typing import Protocol

from yandex_alice_openai.application.bootstrap import build_container
from yandex_alice_openai.config import get_settings
from yandex_alice_openai.domain.models import DeferredJob
from yandex_alice_openai.infra.observability.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.app().log_level)
logger = get_logger()


class _DeferredConversationService(Protocol):
    async def process_deferred_job(
        self, job: DeferredJob, *, deadline_seconds: float
    ) -> object: ...

    async def fail_deferred_job(self, job: DeferredJob, *, error_message: str) -> None: ...


class _WorkerContainer(Protocol):
    @property
    def conversation_service(self) -> _DeferredConversationService: ...


async def worker_loop() -> None:
    container = build_container(settings)
    await container.start()
    consumer_name = f"{socket.gethostname()}-{id(container)}"
    try:
        while True:
            jobs = await container.queue.read_group(consumer_name)
            if not jobs:
                await asyncio.sleep(settings.worker().idle_sleep_seconds)
                continue
            for job in jobs:
                should_ack = await _process_job(container, job)
                if not should_ack or not job.stream_id:
                    continue
                try:
                    await container.queue.ack(job.stream_id)
                except Exception as ack_exc:
                    logger.exception("worker_ack_failed", job_id=job.job_id, error=str(ack_exc))
    finally:
        await container.stop()


async def _process_job(container: _WorkerContainer, job: DeferredJob) -> bool:
    try:
        await container.conversation_service.process_deferred_job(
            job, deadline_seconds=settings.worker().job_timeout_seconds
        )
        return True
    except Exception as exc:
        logger.exception("worker_job_failed", job_id=job.job_id, error=str(exc))
        try:
            await container.conversation_service.fail_deferred_job(
                job, error_message="deferred_generation_failed"
            )
        except Exception as fail_exc:
            logger.exception(
                "worker_job_fail_persist_failed", job_id=job.job_id, error=str(fail_exc)
            )
            return False
        return True


def run() -> None:
    asyncio.run(worker_loop())
