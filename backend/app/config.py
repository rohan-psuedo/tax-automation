import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    app_name: str = "Tax Automaton"
    database_url: str = f"sqlite:///{(BACKEND_DIR / 'data' / 'app.db').as_posix()}"
    storage_dir: Path = BACKEND_DIR / "data" / "storage"
    backup_dir: Path = BACKEND_DIR / "data" / "backups"
    log_dir: Path = BACKEND_DIR / "data" / "logs"
    backup_keep: int = 14  # newest backups kept; older ones are deleted
    backup_interval_hours: float = 24.0  # automatic backup when the last one is older

    # Auth
    secret_key: str | None = None  # generated per installation when unset
    access_token_minutes: int = 60 * 12
    cookie_secure: bool = False

    # Accounting connectors
    # Not "localhost": on Windows the IPv6-first lookup adds seconds to every request.
    tally_url: str = "http://127.0.0.1:9000"
    tally_timeout_seconds: float = 30.0

    # Documents
    max_upload_mb: int = 25
    max_pages: int = 60  # pages rendered per document; the rest are skipped with a warning
    page_render_dpi: int = 150
    run_worker_in_process: bool = True  # set false when running app.pipeline.worker separately
    worker_poll_seconds: float = 2.0

    # AI. Settings saved in the app win over these. AI_PROVIDER picks the service by its id
    # (anthropic, gemini, openai, openrouter, groq, xai, deepseek, mistral, custom; see
    # app.extraction.services); without it, the first service with a key is used, Claude
    # first. Each service reads its own key variable below.
    ai_provider: str | None = None
    ai_model: str | None = None  # model for AI_PROVIDER when it isn't Claude (CLAUDE_MODEL)
    ai_base_url: str | None = None  # endpoint of an "Other (OpenAI-compatible)" service
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    gemini_api_key: str | None = None
    google_api_key: str | None = None  # Gemini too, when GEMINI_API_KEY is not set
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    groq_api_key: str | None = None
    xai_api_key: str | None = None
    deepseek_api_key: str | None = None
    mistral_api_key: str | None = None
    ai_api_key: str | None = None  # key for the "Other (OpenAI-compatible)" service

    cors_origins: list[str] = ["http://localhost:3000"]


def _installation_secret() -> str:
    """A random signing key created on first run and kept in the data directory, so a
    fresh install never runs with a guessable default."""
    path = BACKEND_DIR / "data" / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    return key


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.secret_key:
        settings.secret_key = _installation_secret()
    return settings
