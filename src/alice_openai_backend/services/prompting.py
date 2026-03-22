from __future__ import annotations

import re

CONTINUE_MARKERS = {
    "да давай",
    "да продолжай",
    "давай",
    "давай еще",
    "давай детальнее",
    "давай дальше",
    "давай подробнее",
    "давай продолжай",
    "давай продолжим",
    "можно дальше",
    "можно подробнее",
    "можно продолжение",
    "можешь продолжать",
    "ну давай",
    "ну продолжай",
    "продолжай",
    "продолжай дальше",
    "продолжай пожалуйста",
    "продолжай подробнее",
    "расскажи дальше",
    "продолжить",
    "подробнее",
    "расскажи еще",
    "расскажи еще подробнее",
    "еще",
    "дальше",
    "дальше давай",
    "расскажи подробнее",
    "угу давай",
}

_AFFIRMATION_TOKENS = {
    "ага",
    "да",
    "ладно",
    "ок",
    "окей",
    "угу",
    "хорошо",
}
_CONTINUE_TOKENS = {
    "давай",
    "дальше",
    "детальнее",
    "еще",
    "подробнее",
    "продолжай",
    "продолжить",
    "продолжим",
    "продолжение",
    "продолжать",
    "расскажи",
}
_FILLER_TOKENS = {
    "можешь",
    "можно",
    "ну",
    "пожалуйста",
}
_ALLOWED_CONTINUE_TOKENS = _AFFIRMATION_TOKENS | _CONTINUE_TOKENS | _FILLER_TOKENS
_MAX_CONTINUE_TOKENS = 5


def is_continue_intent(text: str, *, pending_exists: bool = False) -> bool:
    normalized = _normalize_intent_text(text)
    if normalized in CONTINUE_MARKERS:
        return True
    if not normalized:
        return False

    tokens = normalized.split()
    if len(tokens) > _MAX_CONTINUE_TOKENS:
        return False
    if any(token not in _ALLOWED_CONTINUE_TOKENS for token in tokens):
        return False

    has_continue_token = any(token in _CONTINUE_TOKENS for token in tokens)
    has_affirmation_only = all(token in (_AFFIRMATION_TOKENS | _FILLER_TOKENS) for token in tokens)
    if has_continue_token:
        return True
    if has_affirmation_only:
        return pending_exists
    return False


def _normalize_intent_text(text: str) -> str:
    normalized = text.strip().lower().replace("ё", "е")
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def sanitize_user_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1200]
