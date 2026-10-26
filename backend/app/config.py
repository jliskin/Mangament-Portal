from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CP_", extra="ignore")

    app_name: str = "Control Platform C.M / Yosef Golan"
    database_url: str = "postgresql+psycopg://control:control@localhost:5432/control_platform"
    secret_key: str = "change-me"                       # session signing; MUST be overridden in prod
    session_cookie_secure: bool = True                  # set False only for local http dev
    session_max_age_s: int = 8 * 3600
    storage_dir: Path = Path("/data/uploads")
    max_upload_mb: int = 15
    bank_webhook_secret: str = ""                       # HMAC secret for the bank-feed webhook
    timezone: str = "UTC"
    prorate_partial_months: bool = True
    environment: str = "production"

    @property
    def is_dev(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.is_dev and s.secret_key == "change-me":
        raise RuntimeError("CP_SECRET_KEY must be set in production")
    return s
