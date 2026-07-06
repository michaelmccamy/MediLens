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


@dataclass
class Settings:
    anthropic_api_key: str
    model_name: str
    database_url: str


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

    settings = Settings(
        anthropic_api_key=anthropic_api_key,
        model_name=model_name,
        database_url=database_url,
    )
    return settings
