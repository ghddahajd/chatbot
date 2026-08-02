"""конфигурация приложения."""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[1]


def resolve_project_dir() -> Path:
    """определяет runtime-корень проекта для локальной разработки и docker."""

    repo_root = BASE_DIR.parent
    if (repo_root / "widget").exists() and (repo_root / "demo").exists():
        return repo_root
    return BASE_DIR


PROJECT_DIR = resolve_project_dir()


class Settings(BaseSettings):
    """runtime-настройки backend-приложения."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Chat Widget MVP"
    app_env: str = "development"
    dev_mode: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    llm_provider: str = "mock"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_use_structured_classifier: bool = True
    llm_skip_classifier_for_local: Optional[bool] = None
    openai_api_key: str = ""
    gemini_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_operators_group_id: str = ""
    telegram_bridge_enabled: bool = True
    delivery_retry_enabled: bool = True
    delivery_retry_interval_seconds: int = 60
    session_ttl_seconds: int = 86400
    session_eviction_interval_seconds: int = 3600
    session_eviction_enabled: bool = True
    session_snapshot_file: str = ""
    chat_rate_limit_enabled: bool = True
    chat_rate_limit_per_minute: int = 30
    # Сколько доверенных обратных прокси стоит перед приложением (Render = минимум 1).
    # Клиент управляет только тем, что сам вписал в X-Forwarded-For, — всё, что ДОПИСАЛИ
    # наши прокси (справа), подделать нельзя. Если появится ещё слой (например Cloudflare
    # перед Render) — увеличить до 2, иначе rate-limit снова обходится подменой заголовка.
    trusted_proxy_count: int = 1
    operator_token: str = "demo-operator-token"
    allowed_origins: str = "http://localhost:8000"
    default_company_id: str = "rosh_demo"

    data_dir: Path = Field(default_factory=lambda: BASE_DIR / "data")
    clients_data_dir: Path = Field(default_factory=lambda: BASE_DIR / "data" / "clients")
    defaults_data_dir: Path = Field(default_factory=lambda: BASE_DIR / "data" / "defaults")
    logs_dir: Path = Field(default_factory=lambda: BASE_DIR / "logs")
    leads_file: Path = Field(default_factory=lambda: BASE_DIR / "logs" / "leads.jsonl")
    analytics_file: Path = Field(default_factory=lambda: BASE_DIR / "logs" / "analytics.jsonl")
    delivery_outbox_file: Path = Field(default_factory=lambda: BASE_DIR / "logs" / "delivery_outbox.jsonl")
    widget_path: Path = Field(default_factory=lambda: PROJECT_DIR / "widget" / "widget.js")
    demo_dir: Path = Field(default_factory=lambda: PROJECT_DIR / "demo")

    def cors_origins(self) -> List[str]:
        """возвращает cors origins из env-строки через запятую."""

        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """возвращает закешированный экземпляр настроек."""

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
