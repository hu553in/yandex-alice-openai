from __future__ import annotations

from pydantic import BaseModel, Field


class AliceMeta(BaseModel):
    locale: str | None = None
    timezone: str | None = None
    client_id: str | None = None


class AliceApplication(BaseModel):
    application_id: str | None = None


class AliceUser(BaseModel):
    user_id: str | None = None


class AliceDevice(BaseModel):
    device_id: str | None = None


class AliceSession(BaseModel):
    session_id: str
    message_id: int
    user_id: str | None = None
    new: bool = False
    application: AliceApplication = Field(default_factory=AliceApplication)
    user: AliceUser = Field(default_factory=AliceUser)


class AliceRequestPayload(BaseModel):
    command: str = ""
    original_utterance: str = ""
    type: str | None = None
    markup: dict[str, object] | None = None
    nlu: dict[str, object] | None = None
    payload: dict[str, object] | None = None


class AliceState(BaseModel):
    session: dict[str, object] | None = None
    user: dict[str, object] | None = None
    application: dict[str, object] | None = None


class AliceWebhookRequest(BaseModel):
    meta: AliceMeta = Field(default_factory=AliceMeta)
    session: AliceSession
    request: AliceRequestPayload
    version: str = "1.0"
    state: AliceState | None = None
    application: AliceApplication | None = None
    device: AliceDevice | None = None

    def utterance(self) -> str:
        return (self.request.original_utterance or self.request.command or "").strip()

    def request_key(self) -> str:
        application_id = (
            self.session.application.application_id
            or (self.application.application_id if self.application else None)
            or "alice-skill"
        )
        user_id = self.session.user.user_id or self.session.user_id or "anonymous"
        return f"{application_id}:{user_id}:{self.session.session_id}:{self.session.message_id}"


class AliceButtonsItem(BaseModel):
    title: str
    hide: bool = True


class AliceResponsePayload(BaseModel):
    text: str
    tts: str
    end_session: bool = False
    buttons: list[AliceButtonsItem] = Field(default_factory=list)


class AliceWebhookResponse(BaseModel):
    version: str = "1.0"
    response: AliceResponsePayload
    session: AliceSession
