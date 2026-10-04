"""Runtime configuration, read from environment variables.

Every knob has a sensible default so the service runs with zero configuration.
"""
import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # Upstream Hospital Directory API
    hospital_api_base_url: str = field(
        default_factory=lambda: os.getenv(
            "HOSPITAL_API_BASE_URL", "https://hospital-directory.onrender.com"
        ).rstrip("/")
    )
    # Generous default: the upstream runs on Render's free tier and can take
    # ~50s to wake from a cold start.
    request_timeout_seconds: float = field(
        default_factory=lambda: _env_float("REQUEST_TIMEOUT_SECONDS", 60.0)
    )

    # Concurrency / resilience
    max_concurrency: int = field(default_factory=lambda: _env_int("MAX_CONCURRENCY", 10))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 3))
    retry_backoff_seconds: float = field(
        default_factory=lambda: _env_float("RETRY_BACKOFF_SECONDS", 0.5)
    )

    # Input limits
    max_csv_rows: int = field(default_factory=lambda: _env_int("MAX_CSV_ROWS", 20))
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("MAX_UPLOAD_BYTES", 1024 * 1024)
    )

    # In-memory job store: oldest finished jobs are evicted past this size.
    max_stored_jobs: int = field(default_factory=lambda: _env_int("MAX_STORED_JOBS", 1000))


def get_settings() -> Settings:
    return Settings()
