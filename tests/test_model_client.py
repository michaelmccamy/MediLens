"""Tests for the ModelClient wrapper: structured output parsing and guardrails.

The underlying Anthropic SDK client is replaced with a stub so no network
calls happen. Only synthetic content is used (CLAUDE.md section 8).
"""

from types import SimpleNamespace
from typing import Any

import pytest

from medilens.client.anthropic_client import ModelClient, ModelResponseError
from medilens.client.rate_limiter import RequestAndTokenLimiter
from medilens.client.retry import RetryPolicy
from medilens.config import Settings

TEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diagnosis_supported": {"type": "boolean"},
    },
    "required": ["diagnosis_supported"],
    "additionalProperties": False,
}


def _make_settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-synthetic-test-value",
        model_name="claude-sonnet-5",
        database_url="postgresql+psycopg://user:pass@localhost:5432/test",
        max_requests_per_minute=100,
        max_tokens_per_minute=100000,
    )


class StubAnthropicClient:
    """Stands in for anthropic.Anthropic; returns canned responses."""

    def __init__(self, response: Any) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self._response = response
        outer = self

        class _Messages:
            def create(self, **kwargs: Any) -> Any:
                outer.create_calls.append(kwargs)
                return outer._response

            def count_tokens(self, **kwargs: Any) -> Any:
                outer.count_calls.append(kwargs)
                return SimpleNamespace(input_tokens=100)

        self.messages = _Messages()


def _make_response(
    text: str,
    stop_reason: str = "end_turn",
    content: list[Any] | None = None,
) -> SimpleNamespace:
    if content is None:
        content = [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        _request_id="req_synthetic_test",
    )


def _make_client(response: SimpleNamespace) -> tuple[ModelClient, StubAnthropicClient]:
    stub = StubAnthropicClient(response)
    client = ModelClient(
        settings=_make_settings(),
        limiter=RequestAndTokenLimiter(
            max_requests_per_minute=1000, max_tokens_per_minute=1000000
        ),
        retry_policy=RetryPolicy(max_retries=0),
        anthropic_client=stub,  # type: ignore[arg-type]
    )
    return client, stub


def test_returns_parsed_json_with_audit_metadata() -> None:
    response = _make_response('{"diagnosis_supported": true}')
    client, _ = _make_client(response)

    result = client.create_structured(
        system="You are a coding validation assistant.",
        user_content="Synthetic note text here.",
        json_schema=TEST_SCHEMA,
    )

    assert result.data == {"diagnosis_supported": True}
    assert result.model == "claude-sonnet-5"
    assert result.request_id == "req_synthetic_test"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 100
    assert result.output_tokens == 20


def test_sends_schema_in_output_config() -> None:
    response = _make_response('{"diagnosis_supported": true}')
    client, stub = _make_client(response)

    client.create_structured(
        system="system prompt",
        user_content="user content",
        json_schema=TEST_SCHEMA,
    )

    assert len(stub.create_calls) == 1
    sent = stub.create_calls[0]
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"] == TEST_SCHEMA
    # No sampling parameters: claude-sonnet-5 rejects them with a 400.
    assert "temperature" not in sent
    assert "top_p" not in sent
    assert "top_k" not in sent


def test_counts_tokens_before_sending() -> None:
    response = _make_response('{"diagnosis_supported": true}')
    client, stub = _make_client(response)

    client.create_structured(
        system="system prompt",
        user_content="user content",
        json_schema=TEST_SCHEMA,
    )

    # The Token Counting API must be called before the message request.
    assert len(stub.count_calls) == 1


def test_refusal_raises_loudly() -> None:
    response = _make_response("", stop_reason="refusal", content=[])
    client, _ = _make_client(response)

    with pytest.raises(ModelResponseError, match="refus"):
        client.create_structured(
            system="s", user_content="u", json_schema=TEST_SCHEMA
        )


def test_truncation_raises_loudly() -> None:
    response = _make_response('{"diagnosis_su', stop_reason="max_tokens")
    client, _ = _make_client(response)

    with pytest.raises(ModelResponseError, match="max_tokens"):
        client.create_structured(
            system="s", user_content="u", json_schema=TEST_SCHEMA
        )


def test_invalid_json_raises_without_leaking_content() -> None:
    secret_marker = "SYNTHETIC-CONTENT-MARKER"
    response = _make_response(f"not json {secret_marker}")
    client, _ = _make_client(response)

    with pytest.raises(ModelResponseError) as exc_info:
        client.create_structured(
            system="s", user_content="u", json_schema=TEST_SCHEMA
        )

    # The exception message must not contain response content, because once
    # real notes flow through, content could be PHI and exceptions get logged.
    assert secret_marker not in str(exc_info.value)


def test_missing_text_block_raises() -> None:
    response = _make_response("", content=[SimpleNamespace(type="thinking", thinking="")])
    client, _ = _make_client(response)

    with pytest.raises(ModelResponseError, match="no text block"):
        client.create_structured(
            system="s", user_content="u", json_schema=TEST_SCHEMA
        )
