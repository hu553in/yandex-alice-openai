from __future__ import annotations

from yandex_alice_openai.domain.models import UserScope
from yandex_alice_openai.schemas.alice import AliceWebhookRequest


def build_user_scope(payload: AliceWebhookRequest) -> UserScope:
    application_id = (
        payload.session.application.application_id
        or (payload.application.application_id if payload.application else None)
        or "alice-skill"
    )
    user_id = payload.session.user.user_id or payload.session.user_id
    device_id = payload.device.device_id if payload.device else None
    return UserScope(
        application_id=application_id,
        user_id=user_id,
        session_id=payload.session.session_id,
        device_id=device_id,
    )
