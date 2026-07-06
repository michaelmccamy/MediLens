"""Application configuration.

Centralizes environment-derived settings (API keys, model name, database URL)
so that a model swap or connection-string change is a one-line edit here,
not a hunt through the codebase. See CLAUDE.md section 2 and section 5.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Loaded once at import time so any entrypoint (CLI, tests, future API) gets
# the same environment without repeating this call everywhere.
load_dotenv()

# Kept as a plain module constant, not inline in call sites, so switching to
# Haiku for high-volume extraction later is a one-line change (CLAUDE.md section 2).
DEFAULT_MODEL_NAME = "claude-sonnet-5"


# Conservative defaults sized for a low API tier. Actual per-tier limits change,
# so read the current numbers from https://docs.claude.com/en/api/rate-limits and
# override via environment variables rather than editing these constants.
DEFAULT_MAX_REQUESTS_PER_MINUTE = 50
DEFAULT_MAX_TOKENS_PER_MINUTE = 30000


@dataclass
class Settings:
    anthropic_api_key: str
    model_name: str
    database_url: str
    max_requests_per_minute: int
    max_tokens_per_minute: int


def load_settings() -> Settings:
    """Read required settings from the environment and fail loudly if missing.

    CLAUDE.md section 7 requires failing loudly on missing configuration
    rather than silently guessing or falling back to a default that could
    mask a misconfigured deployment.
    """
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )

    model_name = os.environ.get("MEDILENS_MODEL_NAME")
    if not model_name:
        model_name = DEFAULT_MODEL_NAME

    max_requests_per_minute = _read_positive_int(
        "MEDILENS_MAX_REQUESTS_PER_MINUTE", DEFAULT_MAX_REQUESTS_PER_MINUTE
    )
    max_tokens_per_minute = _read_positive_int(
        "MEDILENS_MAX_TOKENS_PER_MINUTE", DEFAULT_MAX_TOKENS_PER_MINUTE
    )

    settings = Settings(
        anthropic_api_key=anthropic_api_key,
        model_name=model_name,
        database_url=database_url,
        max_requests_per_minute=max_requests_per_minute,
        max_tokens_per_minute=max_tokens_per_minute,
    )
    return settings


def _read_positive_int(env_var_name: str, default_value: int) -> int:
    """Parse a positive integer setting, failing loudly on garbage values.

    A silently ignored typo in a rate limit could let the client exceed the
    account tier, so malformed values are an error rather than a fallback.
    """
    raw_value = os.environ.get(env_var_name)
    if raw_value is None or raw_value == "":
        return default_value
    try:
        parsed_value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{env_var_name} must be an integer, got: {raw_value!r}"
        ) from exc
    if parsed_value <= 0:
        raise RuntimeError(f"{env_var_name} must be positive, got: {parsed_value}")
    return parsed_value
