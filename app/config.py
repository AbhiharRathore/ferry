from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from FERRY_* environment variables (or a local .env)."""

    model_config = SettingsConfigDict(env_prefix="FERRY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ferry:ferry@localhost:5433/ferry"
    admin_token: str = Field(default="", description="Bearer token for POST /v1/apps")

    # Outbound safety. Off by default: endpoints may not point at private networks.
    allow_private_targets: bool = False
    allow_http: bool = True
    request_timeout_seconds: float = 10.0
    max_payload_bytes: int = 256 * 1024
    response_snippet_bytes: int = 1024

    # Retry policy: attempt n waits about base * 2^(n-1) seconds, capped, with jitter.
    max_attempts: int = 8
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 3600.0

    # Endpoint is disabled after this many failed attempts in a row.
    breaker_threshold: int = 20

    # Worker
    batch_size: int = 10
    lease_seconds: float = 60.0
    poll_interval_seconds: float = 1.0

    # Receivers reject signatures older than this.
    signature_tolerance_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
