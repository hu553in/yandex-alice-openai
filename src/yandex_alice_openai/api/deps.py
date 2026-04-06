from __future__ import annotations

from fastapi import Request

from yandex_alice_openai.application.bootstrap import Container


def get_container(request: Request) -> Container:
    return request.app.state.container
