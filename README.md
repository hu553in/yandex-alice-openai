# Alice OpenAI Backend

Production-grade starter kit for a Yandex Alice skill webhook that routes user utterances to the OpenAI Responses API, keeps short-term dialog memory in Redis, persists analytics to PostgreSQL, and survives Alice's tight response deadline with a deferred reply flow.

Published container image naming convention:

- `ghcr.io/<github_owner>/<repo_name>`

## Why this stack

- Python 3.14 + `uv`: strongest balance here for async backend work, typing, testability, and low-friction DX.
- FastAPI + Pydantic v2: typed webhook contracts, fast iteration, mature ASGI deployment story.
- Redis: short-term memory, idempotency, rate limiting, and deferred job state with low latency.
- PostgreSQL: durable turn storage and job-result analytics without coupling online latency to writes.
- Official OpenAI Python SDK with Responses API: modern API surface, no legacy ChatCompletion usage.
- Redis Streams worker instead of Celery/RQ/Arq: fewer moving parts and lower compatibility risk for Python 3.14 in a starter template.

## High-level architecture

```mermaid
flowchart LR
    Alice["Yandex Alice"] -->|POST /webhooks/alice| API["FastAPI webhook layer"]
    API --> Guard["Validation, rate limit, idempotency, correlation ID"]
    Guard --> Session["Conversation service"]
    Session --> Redis["Redis<br/>history + pending replies + job queue"]
    Session --> OpenAI["OpenAI Responses API"]
    Session --> Renderer["Voice-friendly renderer"]
    Session --> Postgres["PostgreSQL analytics sink"]
    Redis --> Worker["Deferred worker"]
    Worker --> OpenAI
    Worker --> Redis
    Worker --> Postgres
    API --> Metrics["Prometheus / OTEL hooks"]
```

## How the 4.5-second Alice limit is handled

- Fast path: the webhook gives OpenAI a strict short timeout, tuned by `OPENAI_TIMEOUT_SECONDS` and intended to stay around 2.2 seconds so the webhook still has buffer for network jitter and response rendering.
- Slow path: if the fast path times out or the provider is degraded, the request is written to Redis Streams, a pending state is stored in Redis, and Alice gets: `Я готовлю ответ. Скажи: продолжай.`
- Resume path: when the user says `продолжай` or `подробнее`, the webhook checks Redis for a ready answer and returns it immediately.
- Duplicate delivery path: exact webhook retries reuse the cached Alice response and bypass rate limiting, so platform retries do not create duplicate jobs or spurious `429` user-facing failures.
- Failure path: rate limiting and unexpected runtime faults are translated into Alice-compatible spoken responses instead of raw JSON error payloads.
- No in-memory state: all state is in Redis, so retries, duplicates, and multi-instance deployments stay coherent.

## How the 1024-character Alice limit is handled

- All responses are normalized for speech and stripped of Markdown-like noise.
- The renderer clips `text` and `tts` to 1024 chars.
- Long LLM outputs are compressed into a first voice-friendly chunk plus an optional continuation chunk that can be retrieved with `подробнее`.

## How context is stored

- Redis list per conversation key for recent dialog turns with TTL.
- Conversation key uses `application_id + user identity + device/session identity`, so anonymous traffic cannot accidentally share history.
- Redis keeps only the latest bounded number of turns per conversation to keep prompts small and predictable.
- PostgreSQL stores turn history and deferred job results for durable analytics and operational debugging.

## Repository layout

```text
.
├── .github/workflows/ci.yml
├── .pre-commit-config.yaml
├── .env.example
├── Dockerfile
├── Makefile
├── docker-compose.yml
├── docs/
│   └── architecture.md
├── examples/
│   ├── alice-request.json
│   └── alice-response.json
├── scripts/
│   └── dev_tunnel.sh
├── src/alice_openai_backend/
│   ├── api/
│   ├── application/
│   ├── domain/
│   ├── infra/
│   ├── schemas/
│   ├── services/
│   ├── workers/
│   ├── __init__.py
│   ├── config.py
│   └── main.py
└── tests/
```

## Local development

### Prerequisites

- Python 3.14
- `uv`
- Docker and Docker Compose

### Bootstrap

```bash
cp .env.example .env
uv sync --all-extras
docker compose up -d redis postgres
uv run alice-api
```

In another shell:

```bash
uv run alice-worker
```

### Run the published GHCR image

Start Redis and PostgreSQL first. For the same local setup used by the source checkout:

```bash
docker compose up -d redis postgres
```

API container:

```bash
docker pull ghcr.io/<github_owner>/<repo_name>:latest
docker run --rm \
  --env-file .env \
  -e REDIS_URL=redis://host.docker.internal:6379/0 \
  -e DATABASE_URL=postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/alice_openai \
  -p 8080:8080 \
  ghcr.io/<github_owner>/<repo_name>:latest
```

Worker container:

```bash
docker run --rm \
  --env-file .env \
  -e REDIS_URL=redis://host.docker.internal:6379/0 \
  -e DATABASE_URL=postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/alice_openai \
  ghcr.io/<github_owner>/<repo_name>:latest \
  alice-worker
```

On Linux, replace `host.docker.internal` with the address Docker uses to reach the host, or run the containers on a shared user-defined network.

### Common commands

```bash
uv lock
uv sync --all-extras
uv run ruff check .
uv run ruff format .
uv run mypy src tests
uv run pytest
uv run alice-api
uv run alice-worker
```

Or via `make`:

```bash
make sync
make lint
make typecheck
make test
make run
make worker
```

The repository includes a committed `uv.lock`, and Docker builds use `uv sync --frozen` for reproducible environments.

## API endpoints

- `POST /webhooks/alice`
- `GET /health`
- `GET /metrics`

## Example Alice webhook request

See [examples/alice-request.json](./examples/alice-request.json).

## Example Alice webhook response

See [examples/alice-response.json](./examples/alice-response.json).

## Security defaults

- Optional webhook secret check via `X-Alice-Secret`.
- Redis-backed rate limiting.
- Idempotent duplicate handling is scoped by application + user + Alice session message, avoiding cross-skill cache collisions.
- Strict Pydantic request validation.
- No secrets in code, only env-based configuration.
- Request correlation ID in every response header.
- Graceful degradation when OpenAI is slow or failing.

## Observability

- Structured JSON logs with request correlation.
- Prometheus counters and latency histograms.
- Optional OTLP tracing export.
- Separate metrics for LLM fast-path outcomes and deferred job states.

## Container publishing

- CI always builds the Docker image after the test job passes.
- Pushes to the repository default branch and version tags publish the image to public GHCR using `GITHUB_TOKEN`.
- Pull requests build the image without pushing, so Docker build breakage is caught before merge.

## Deployment options

### VPS / Docker Compose

- Best for early-stage production and predictable costs.
- Simple to operate, easiest local-to-prod parity.
- You manage OS patching and redundancy.

### Kubernetes

- Best when you need horizontal scale, HA, and managed observability.
- Clean split between API and worker deployments.
- More operational overhead for a small skill.

### Serverless / Cloud Functions

- Good for webhook API only if cold starts are controlled.
- Worker is still better as a long-lived process or managed queue consumer.
- Redis and Postgres remain external; network latency budget gets tighter.

## Python 3.14 compatibility note

This template intentionally avoids a heavier Python queue framework because compatibility lag is likelier there than in the selected core stack. FastAPI, SQLAlchemy, Pydantic, `redis-py`, `httpx`, and the official OpenAI SDK are all mainstream actively maintained libraries; `uv` is used as the package manager, locker, and runner across local dev, Docker, and CI.

## GHCR permissions note

The workflow expects the repository to be public and uses the built-in `GITHUB_TOKEN` to publish to `ghcr.io/<github_owner>/<repo_name>`. Package visibility should remain public in the repository's package settings.

## License

MIT
