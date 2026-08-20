import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class AppEnv(StrEnum):
    DEV = "dev"
    DEV_DOCKER = "dev_docker"
    PROD = "prod"


def _resolve_env_file() -> Path:
    env_name = os.getenv("ENV", AppEnv.DEV)
    env_file = ROOT_DIR / f".env.{env_name}"
    if not env_file.exists():
        raise FileNotFoundError(
            f"Environment file not found: {env_file}. "
            f"Set ENV to one of {[e.value for e in AppEnv]}."
        )
    print(f"env_file: {env_file}")
    return env_file


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_resolve_env_file()),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: AppEnv = Field(alias="ENV")
    app_name: str = "GTFS Realtime"
    admin_email: str
    database_url: str = Field(alias="DATABASE_URL")
    secret_key: str = Field(alias="SECRET_KEY")
    debug: bool = False
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0",
        alias="CELERY_BROKER_URL",
    )
    # Defaults on (prod's .env.prod doesn't need to set this) - dev/local
    # environments that don't want PostHog running set POSTHOG_ENABLED=false
    # explicitly, rather than just leaving the token/host blank, which would
    # otherwise look like a misconfiguration (see lifespan()/telemetry.py -
    # both raise in debug mode if enabled but unconfigured).
    posthog_enabled: bool = Field(default=True, alias="POSTHOG_ENABLED")
    posthog_project_token: str | None = Field(
        default=None,
        alias="POSTHOG_PROJECT_TOKEN",
    )
    posthog_host: str | None = Field(default=None, alias="POSTHOG_HOST")


@lru_cache
def get_settings() -> Settings:
    return Settings(ENV="dev")
