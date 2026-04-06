from __future__ import annotations

from yandex_alice_openai.services.renderer import MAX_ALICE_FIELD_LENGTH, render_voice_response


def test_renderer_clips_to_alice_limit() -> None:
    payload = render_voice_response("A" * 1200)
    assert len(payload.text) <= MAX_ALICE_FIELD_LENGTH
    assert payload.text.endswith("…")
    assert payload.tts == payload.text


def test_renderer_removes_urls_and_markdown_noise() -> None:
    payload = render_voice_response("**Привет** смотри https://example.com и `код`")
    assert "https://" not in payload.text
    assert "`" not in payload.text


def test_renderer_unwraps_markdown_links() -> None:
    payload = render_voice_response("[подробности](https://example.com/docs)")

    assert payload.text == "подробности"


def test_renderer_normalizes_lists_for_tts() -> None:
    payload = render_voice_response("варианты: • первый; • второй")
    assert payload.text == "варианты: первый. второй"


def test_renderer_clips_at_word_boundary_when_no_sentence_end_exists() -> None:
    payload = render_voice_response("фраза " * 250)

    assert payload.text.endswith("…")
    tokens = payload.text[:-1].split()
    assert tokens
    assert all(token == "фраза" for token in tokens)  # nosec B105


def test_renderer_falls_back_when_text_becomes_empty_after_cleanup() -> None:
    payload = render_voice_response("`код`")

    assert payload.text == "Я не могу озвучить ответ в этом виде."
