"""Configuration, loaded from the environment with sane defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # optional - containers inject real environment variables
    load_dotenv = None

# Load .env once at import, before any field default reads os.environ. Real
# environment variables always win, so a container's config is never overridden
# by a stray .env that got copied into the image.
if load_dotenv is not None:
    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    # --- Flask ---
    secret_key: str = field(
        default_factory=lambda: os.environ.get("SECRET_KEY", "dev-only-change-me")
    )
    max_content_length: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 16 * 1024 * 1024))

    # --- Provider selection ---
    # "gemini" or "claude". Both engines send the identical rubric from
    # prompts.py and return the identical schema, so this is a one-word switch.
    llm_provider: str = field(
        default_factory=lambda: os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    )

    # --- Gemini ---
    gemini_api_key: str = field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY", "")
    )
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    )
    # Screening should be as reproducible as the provider allows; the rubric,
    # not sampling variety, is meant to drive the score.
    gemini_temperature: float = field(
        default_factory=lambda: float(os.environ.get("GEMINI_TEMPERATURE", "0.2"))
    )

    # --- Job matching ---
    embedding_model: str = field(
        default_factory=lambda: os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001")
    )
    job_db_path: str = field(
        default_factory=lambda: os.environ.get("JOB_DB_PATH", "data/jobs.db")
    )
    # Funnel widths. Retrieval is free, so it stays wide; the LLM stage is the
    # expensive one, so it stays narrow.
    job_retrieval_limit: int = field(default_factory=lambda: _int("JOB_RETRIEVAL_LIMIT", 2000))
    job_shortlist_size: int = field(default_factory=lambda: _int("JOB_SHORTLIST_SIZE", 30))
    job_llm_scored_count: int = field(default_factory=lambda: _int("JOB_LLM_SCORED_COUNT", 10))
    # Concurrent LLM calls in stage 3. Too high trips provider rate limits, which
    # on a free tier means every call fails instead of just being slow.
    job_llm_concurrency: int = field(default_factory=lambda: _int("JOB_LLM_CONCURRENCY", 5))

    # --- Claude ---
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    model: str = field(default_factory=lambda: os.environ.get("CLAUDE_MODEL", "claude-opus-5"))
    effort: str = field(default_factory=lambda: os.environ.get("CLAUDE_EFFORT", "medium"))
    max_tokens: int = field(default_factory=lambda: _int("CLAUDE_MAX_TOKENS", 8000))
    request_timeout: int = field(default_factory=lambda: _int("CLAUDE_TIMEOUT_SECONDS", 120))
    max_retries: int = field(default_factory=lambda: _int("CLAUDE_MAX_RETRIES", 3))

    # --- Behaviour ---
    llm_enabled: bool = field(default_factory=lambda: _bool("LLM_ENABLED", True))
    fallback_to_baseline: bool = field(
        default_factory=lambda: _bool("FALLBACK_TO_BASELINE", True)
    )
    # Resumes and JDs are truncated to a generous ceiling to bound cost, not to fit context.
    max_resume_chars: int = field(default_factory=lambda: _int("MAX_RESUME_CHARS", 60000))
    max_jd_chars: int = field(default_factory=lambda: _int("MAX_JD_CHARS", 30000))

    # --- Observability ---
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    json_logs: bool = field(default_factory=lambda: _bool("JSON_LOGS", True))
    service_name: str = field(
        default_factory=lambda: os.environ.get("SERVICE_NAME", "resume-screener")
    )
    environment: str = field(default_factory=lambda: os.environ.get("APP_ENV", "development"))
    # Optional second sink. Containers log to stdout and Filebeat reads the
    # container log; when running without Docker there is no such file, so this
    # writes the same JSON lines somewhere Filebeat (or you) can still read them.
    log_file: str = field(default_factory=lambda: os.environ.get("LOG_FILE", ""))

    @property
    def api_key_for_provider(self) -> str:
        """The credential the selected provider needs."""
        return self.gemini_api_key if self.llm_provider == "gemini" else self.anthropic_api_key

    @property
    def active_model(self) -> str:
        """The model id the selected provider will use."""
        return self.gemini_model if self.llm_provider == "gemini" else self.model

    @property
    def llm_available(self) -> bool:
        """Whether an LLM call can even be attempted."""
        return self.llm_enabled and bool(self.api_key_for_provider)


def load_config() -> Config:
    return Config()
