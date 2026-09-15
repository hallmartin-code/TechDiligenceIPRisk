"""Runtime configuration, read from environment variables (prefix ``TDIP_``).

A `.env` file at the project root is loaded first, so `ANTHROPIC_API_KEY` and
the `TDIP_*` settings can live there during local development. Consent to share
data with TEN Capital is deliberately *not* configurable here: it can only be
granted per run, explicitly, by the person running the analysis.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_PATH = ASSETS_DIR / "TEN_Capital_logo_footer.png"

try:  # Loaded here so the Anthropic SDK also sees ANTHROPIC_API_KEY from .env.
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ModuleNotFoundError:  # pragma: no cover - optional convenience
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TDIP_", extra="ignore", populate_by_name=True)

    # Analysis engine: "auto" uses Claude when credentials exist, else rules.
    engine: Literal["auto", "llm", "rules"] = "auto"
    llm_model: str = "claude-opus-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_max_tokens: int = Field(default=64000, ge=4000, le=128000)
    llm_timeout_seconds: float = Field(default=600.0, gt=0)
    llm_server_fallbacks: bool = True
    llm_attach_pdf: bool = True
    llm_attach_max_mb: float = Field(default=20.0, gt=0)

    # Ingestion limits.
    max_upload_mb: float = Field(default=50.0, gt=0)
    max_slides: int = Field(default=250, ge=1)
    soffice_path: str | None = None

    # Scoring policy: score given to a category with no deck evidence at all.
    unverified_score: int = Field(default=2, ge=1, le=3)

    # Output.
    output_dir: Path = PROJECT_ROOT / "output"
    font_dir: Path | None = None

    # Logging.
    log_level: str = "INFO"
    log_file: Path | None = None

    # TEN Capital lead-intelligence record (sent only with explicit consent).
    ten_capital_endpoint: str | None = None
    ten_capital_api_key: SecretStr | None = None
    ten_capital_timeout_seconds: float = Field(default=15.0, gt=0)
    default_user: str | None = None

    # Automated result emails via Resend: every completed analysis goes to report_email_to.
    resend_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("RESEND_API_KEY", "TDIP_RESEND_API_KEY", "resend_api_key"))
    email_reports: bool = True
    report_email_to: str = "Info@tencapital.group"
    report_email_from: str = "TEN Capital Tech & IP Diligence <reports@tencapital.group>"
    resend_timeout_seconds: float = Field(default=20.0, gt=0)

    # Web server. Set app_password on any public deployment: every upload spends API credits.
    app_password: SecretStr | None = None
    web_host: str = "127.0.0.1"
    web_port: int = 8040
    web_max_concurrent_jobs: int = Field(default=2, ge=1)
    web_job_ttl_minutes: int = Field(default=60, ge=1)

    @field_validator("ten_capital_endpoint", "soffice_path", "default_user", "app_password", "ten_capital_api_key",
                     "resend_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def email_configured(self) -> bool:
        key = self.resend_api_key.get_secret_value().strip() if self.resend_api_key else ""
        return self.email_reports and bool(key) and bool(self.report_email_to.strip())

    def llm_credentials_available(self) -> bool:
        return bool(
            (os.getenv("ANTHROPIC_API_KEY") or "").strip()
            or (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
