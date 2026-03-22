FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src

RUN uv sync --frozen --no-dev

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

RUN chown -R app:app /app

USER app

EXPOSE 8080

CMD ["uvicorn", "alice_openai_backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
