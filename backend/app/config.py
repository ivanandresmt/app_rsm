from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://rsm:rsm@localhost:5432/rsm"
    jwt_secret: str = "dev-only-secret-change-me-please-32bytes"
    jwt_ttl_minutes: int = 480
    cors_origins: str = "http://localhost:3000"
    # Dev login is a stand-in for a real IdP; disable outside demos.
    dev_login_enabled: bool = True

    # --- external provider (only the public contract is used) ---
    provider_base_url: str = "http://localhost:4010/v1"
    provider_token: str = "travel-demo-token"
    provider_connect_timeout_s: float = 2.0
    provider_read_timeout_s: float = 4.0
    provider_deadline_s: float = 8.0  # total time budget for one logical call, incl. retries
    provider_max_retry_after_s: float = 3.0

    # --- freshness ---
    search_cache_ttl_s: int = 30
    estimate_stale_after_s: int = 3600  # estimates older than this are flagged even if validUntil is later

    # --- booking reconciliation ---
    reconcile_grace_s: int = 20  # wait before concluding "provider has no record"
    reconcile_replay_window_s: int = 900  # replaying the same idempotent request is only attempted this long
    reconcile_max_attempts: int = 12

    # --- background jobs / documents ---
    storage_dir: str = "./data/documents"
    job_lease_s: int = 60
    job_poll_interval_s: float = 1.0
    document_max_attempts: int = 3
    audit_interval_s: int = 300
    pdf_render_delay_s: float = 0.0  # demo knob: make generation visibly slow

    # --- MCP + assistant ---
    mcp_url: str = "http://localhost:8001/mcp"
    anthropic_api_key: str = Field(default="")
    anthropic_model: str = "claude-sonnet-5-5"
    assistant_max_steps: int = 8
    assistant_timeout_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
