from __future__ import annotations

from yandex_alice_openai.services.prompting import is_continue_intent


def test_continue_intent_recognizes_natural_voice_followups() -> None:
    assert is_continue_intent("давай")
    assert is_continue_intent("да, давай")
    assert is_continue_intent("давай подробнее")
    assert is_continue_intent("давай ещё")
    assert is_continue_intent("давай ещЁ")
    assert is_continue_intent("давай продолжим")
    assert is_continue_intent("можешь продолжать")
    assert is_continue_intent("ну давай пожалуйста")
    assert is_continue_intent("продолжай пожалуйста")
    assert is_continue_intent("расскажи дальше")
    assert is_continue_intent("можно продолжение")
    assert is_continue_intent("ну продолжай")
    assert is_continue_intent("продолжай")


def test_continue_intent_does_not_match_regular_question() -> None:
    assert not is_continue_intent("Да")
    assert not is_continue_intent("окей")
    assert not is_continue_intent("ладно")
    assert not is_continue_intent("хорошо")
    assert not is_continue_intent("против кого")
    assert not is_continue_intent("давай про team spirit")
    assert not is_continue_intent("окей а теперь про доту")
    assert not is_continue_intent("расскажи про него")


def test_continue_intent_allows_soft_affirmations_only_when_pending_exists() -> None:
    assert is_continue_intent("Да", pending_exists=True)
    assert is_continue_intent("окей", pending_exists=True)
    assert is_continue_intent("ладно", pending_exists=True)
    assert is_continue_intent("хорошо", pending_exists=True)
