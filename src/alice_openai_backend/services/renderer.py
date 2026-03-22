from __future__ import annotations

import re

from alice_openai_backend.schemas.alice import AliceButtonsItem, AliceResponsePayload

MAX_ALICE_FIELD_LENGTH = 1024


def render_voice_response(
    text: str,
    *,
    end_session: bool = False,
    buttons: list[AliceButtonsItem] | None = None,
) -> AliceResponsePayload:
    normalized = _normalize_text(text)
    clipped = _clip(normalized, limit=MAX_ALICE_FIELD_LENGTH)
    return AliceResponsePayload(
        text=clipped,
        tts=clipped,
        end_session=end_session,
        buttons=buttons or [],
    )


def _normalize_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    normalized = normalized.replace("**", "").replace("*", "")
    normalized = re.sub(r"`{1,3}.+?`{1,3}", "", normalized)
    normalized = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", normalized)
    normalized = re.sub(r"(^|\s)#{1,6}\s*", " ", normalized)
    normalized = normalized.replace("•", " ")
    normalized = re.sub(r"\s*;\s*", ". ", normalized)
    normalized = re.sub(r"\|", ", ", normalized)
    normalized = re.sub(r"https?://\S+", "ссылку я опустил", normalized)
    normalized = re.sub(r",\s*,+", ", ", normalized)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    return normalized.strip(" ,-") or "Я не могу озвучить ответ в этом виде."


def _clip(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    cutoff = text[: limit - 1]
    sentence_end = max(cutoff.rfind("."), cutoff.rfind("!"), cutoff.rfind("?"), cutoff.rfind("…"))
    if sentence_end >= 200:
        cutoff = cutoff[: sentence_end + 1]
    else:
        word_end = cutoff.rfind(" ")
        if word_end >= max(80, limit // 2):
            cutoff = cutoff[:word_end]
    return cutoff.rstrip(" ,") + "…"
