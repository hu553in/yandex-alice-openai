from __future__ import annotations

from fastapi import Request

from alice_openai_backend.application.bootstrap import Container


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]
