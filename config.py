"""
Application configuration.

All values are loaded from environment variables (or a `.env` file in the
working directory) via pydantic-settings. Nothing is hard-coded here so the
same image can be deployed to any environment by swapping the `.env` file.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- App -----------------------------------------------------------
    APP_NAME: str = "AI Chef API"
    APP_ENV: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"

    # ---- CORS ------------------------------------------------------------
    # Comma-separated list of allowed origins, e.g. "https://myapp.com,https://admin.myapp.com"
    CORS_ORIGINS: str = "*"

    # ---- Supabase --------------------------------------------------------
    SUPABASE_URL: str = Field(..., description="Supabase project URL")
    SUPABASE_PUBLISHABLE_KEY: str = Field(
        ..., description="Supabase publishable (anon) key, safe for client-side use"
    )
    SUPABASE_SECRET_KEY: str = Field(
        ..., description="Supabase secret (service_role) key — server-side only, full DB access"
    )

    # ---- Google Gemini / Imagen -------------------------------------------
    GEMINI_API_KEY: str = Field(..., description="Google AI Studio / Gemini API key")
    GEMINI_TEXT_MODEL: str = "gemini-2.5-flash"
    GEMINI_VISION_MODEL: str = "gemini-2.5-flash"
    IMAGEN_MODEL: str = "imagen-3.0-generate-002"

    # ---- Business rules ----------------------------------------------------
    DAILY_FREE_LIMIT: int = 5
    REFERRAL_BONUS_REQUESTS: int = 1
    TRIAL_BONUS_REQUESTS: int = 5

    # ---- External integrations ---------------------------------------------
    WHEEL_OF_FORTUNE_URL: str = "https://your-username.github.io/ai-chef-wheel/"
    FEEDBACK_FORM_URL: str = "https://forms.gle/your-feedback-form"

    # ---- Telegram (optional, only needed if the bot channel stays active) --
    TELEGRAM_BOT_TOKEN: str | None = None

    @property
    def cors_origins_list(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance — .env is read once per process."""
    return Settings()
