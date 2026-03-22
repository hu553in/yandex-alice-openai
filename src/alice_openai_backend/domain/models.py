from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class PendingStatus(StrEnum):
    PROCESSING = "processing"
    READY = "ready"
    DELIVERED = "delivered"
    FAILED = "failed"


class FollowupMode(StrEnum):
    NONE = "none"
    READY_TAIL = "ready_tail"
    DEFERRED_OFFER = "deferred_offer"


@dataclass(slots=True, frozen=True)
class UserScope:
    application_id: str
    user_id: str | None
    session_id: str
    device_id: str | None

    @property
    def conversation_key(self) -> str:
        parts = [
            self.application_id,
            self.user_id or "anonymous",
            self.device_id or self.session_id,
            self.session_id,
        ]
        return ":".join(parts)


@dataclass(slots=True, frozen=True)
class ConversationTurn:
    role: TurnRole
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(slots=True, frozen=True)
class PendingReply:
    status: PendingStatus
    reply_text: str | None
    reply_tts: str | None
    tail_text: str | None
    error_message: str | None
    job_id: str
    followup_mode: FollowupMode = FollowupMode.NONE
    continuation_request: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(slots=True, frozen=True)
class LLMReply:
    short_text: str
    raw_text: str
    followup_mode: FollowupMode = FollowupMode.NONE
    followup_text: str | None = None
    continuation_request: str | None = None

    @property
    def needs_followup(self) -> bool:
        return self.followup_mode != FollowupMode.NONE


@dataclass(slots=True, frozen=True)
class DeferredJob:
    job_id: str
    request_id: str
    conversation_key: str
    user_text: str
    stream_id: str | None = None
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
