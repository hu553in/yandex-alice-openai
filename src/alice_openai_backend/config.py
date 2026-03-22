from __future__ import annotations

from functools import lru_cache
from typing import Literal, cast

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseModel):
    env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    metrics_enabled: bool = True
    tracing_enabled: bool = False
    service_name: str = "alice-openai-backend"
    otlp_endpoint: str | None = None
    rate_limit_per_minute: int = 30
    webhook_secret: SecretStr | None = None


class OpenAISettings(BaseModel):
    api_key: SecretStr
    model: str = "gpt-5-mini"
    timeout_seconds: float = 2.2
    max_retries: int = 1
    web_search_enabled: bool = True
    web_search_context_size: Literal["low", "medium", "high"] = "low"
    system_prompt: str = Field(
        default=(
            "You are the backend for a Yandex Alice voice skill. "
            "Reply in Russian unless the user clearly asks for another language. "
            "Return one short spoken answer in plain text. "
            "Sound natural and direct. "
            "Give a useful answer immediately when possible. "
            "If uncertain, say so briefly. "
            "Do not repeat the user's question unless needed. "
            "Do not use markdown, lists, headings, code, meta commentary, "
            "or bracketed service syntax."
        )
    )


class RedisSettings(BaseModel):
    url: str = "redis://localhost:6379/0"
    prefix: str = "alice-openai"
    session_ttl_seconds: int = 60 * 60 * 24
    session_turn_limit: int = 24
    pending_ttl_seconds: int = 60 * 30
    idempotency_ttl_seconds: int = 60 * 15
    rate_limit_window_seconds: int = 60


class DatabaseSettings(BaseModel):
    url: str | None = None
    echo: bool = False


class WorkerSettings(BaseModel):
    poll_timeout_ms: int = 3000
    idle_sleep_seconds: float = 1.0
    job_timeout_seconds: float = 20.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="_",
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_log_level: str = "INFO"
    app_metrics_enabled: bool = True
    app_tracing_enabled: bool = False
    app_service_name: str = "alice-openai-backend"
    app_otlp_endpoint: str | None = None
    app_rate_limit_per_minute: int = 30
    app_webhook_secret: SecretStr | None = None

    openai_api_key: SecretStr
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = 2.2
    openai_max_retries: int = 1
    openai_web_search_enabled: bool = True
    openai_web_search_context_size: Literal["low", "medium", "high"] = "low"
    openai_system_prompt: str = (
        "You are the backend for a Yandex Alice voice skill. "
        "Reply in Russian unless the user clearly asks for another language. "
        "Return one short spoken answer in plain text. "
        "Sound natural and direct. "
        "Give a useful answer immediately when possible. "
        "If uncertain, say so briefly. "
        "Do not repeat the user's question unless needed. "
        "Do not use markdown, lists, headings, code, meta commentary, "
        "or bracketed service syntax."
    )

    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "alice-openai"
    redis_session_ttl_seconds: int = 60 * 60 * 24
    redis_session_turn_limit: int = 24
    redis_pending_ttl_seconds: int = 60 * 30
    redis_idempotency_ttl_seconds: int = 60 * 15
    redis_rate_limit_window_seconds: int = 60

    database_url: str | None = None
    database_echo: bool = False

    worker_poll_timeout_ms: int = 3000
    worker_idle_sleep_seconds: float = 1.0
    worker_job_timeout_seconds: float = 20.0

    def app(self) -> AppSettings:
        return AppSettings(
            env=self.app_env,
            host=self.app_host,
            port=self.app_port,
            log_level=self.app_log_level,
            metrics_enabled=self.app_metrics_enabled,
            tracing_enabled=self.app_tracing_enabled,
            service_name=self.app_service_name,
            otlp_endpoint=self.app_otlp_endpoint,
            rate_limit_per_minute=self.app_rate_limit_per_minute,
            webhook_secret=self.app_webhook_secret,
        )

    def openai(self) -> OpenAISettings:
        return OpenAISettings(
            api_key=self.openai_api_key,
            model=self.openai_model,
            timeout_seconds=self.openai_timeout_seconds,
            max_retries=self.openai_max_retries,
            web_search_enabled=self.openai_web_search_enabled,
            web_search_context_size=self.openai_web_search_context_size,
            system_prompt=self.openai_system_prompt,
        )

    def redis(self) -> RedisSettings:
        return RedisSettings(
            url=self.redis_url,
            prefix=self.redis_prefix,
            session_ttl_seconds=self.redis_session_ttl_seconds,
            session_turn_limit=self.redis_session_turn_limit,
            pending_ttl_seconds=self.redis_pending_ttl_seconds,
            idempotency_ttl_seconds=self.redis_idempotency_ttl_seconds,
            rate_limit_window_seconds=self.redis_rate_limit_window_seconds,
        )

    def database(self) -> DatabaseSettings:
        return DatabaseSettings(url=self.database_url, echo=self.database_echo)

    def worker(self) -> WorkerSettings:
        return WorkerSettings(
            poll_timeout_ms=self.worker_poll_timeout_ms,
            idle_sleep_seconds=self.worker_idle_sleep_seconds,
            job_timeout_seconds=self.worker_job_timeout_seconds,
        )

    def environment(self) -> Literal["development", "test", "production"]:
        if self.app_env in {"development", "test", "production"}:
            return cast(Literal["development", "test", "production"], self.app_env)
        return "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
