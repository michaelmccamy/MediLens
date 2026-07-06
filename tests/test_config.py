"""Tests for medilens.config.

Uses only synthetic values, no real PHI or real API keys (CLAUDE.md section 8).
"""

import pytest

from medilens.config import DEFAULT_MODEL_NAME, load_settings


def test_load_settings_fails_loudly_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/test")

    with pytest.raises(RuntimeError):
        load_settings()


def test_load_settings_fails_loudly_when_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-synthetic-test-value")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError):
        load_settings()


def test_load_settings_uses_default_model_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-synthetic-test-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/test")
    monkeypatch.delenv("MEDILENS_MODEL_NAME", raising=False)

    settings = load_settings()

    assert settings.model_name == DEFAULT_MODEL_NAME
