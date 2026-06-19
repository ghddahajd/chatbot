"""Application configuration."""

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[1]


def resolve_project_dir() -> Path:
    """Resolve the runtime project root for local dev and Docker."""

    repo_root = BASE_DIR.parent
    if (repo_root / "widget").exists() and (repo_root / "demo").exists():
        return repo_root
    return BASE_DIR


PROJECT_DIR = resolve_project_dir()


class Settings(BaseSettings):
    """Runtime settings for the backend application."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Chat Widget MVP"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    llm_provider: str = "mock"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    gemini_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    operator_token: str = "demo-operator-token"
    allowed_origins: str = "http://localhost:8000"

    data_dir: Path = Field(default_factory=lambda: BASE_DIR / "data")
    logs_dir: Path = Field(default_factory=lambda: BASE_DIR / "logs")
    leads_file: Path = Field(default_factory=lambda: BASE_DIR / "logs" / "leads.jsonl")
    widget_path: Path = Field(default_factory=lambda: PROJECT_DIR / "widget" / "widget.js")
    demo_dir: Path = Field(default_factory=lambda: PROJECT_DIR / "demo")

    def cors_origins(self) -> List[str]:
        """Return CORS origins from a comma-separated env value."""

        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    settings = Settings()
    if not settings.llm_api_key:
        if settings.llm_provider.lower() == "gemini" and settings.gemini_api_key:
            settings.llm_api_key = settings.gemini_api_key
        elif settings.openai_api_key:
            settings.llm_api_key = settings.openai_api_key

    if settings.llm_provider.lower() == "gemini" and settings.llm_base_url == "https://api.openai.com/v1":
        settings.llm_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        if settings.llm_model == "gpt-4o-mini":
            settings.llm_model = "gemini-3.5-flash"

    return settings
