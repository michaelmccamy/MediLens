"""Tests for the retry policy: backoff, retry-after, and fail-fast rules."""

import anthropic
import httpx
import pytest

import medilens.client.retry as retry_module
from medilens.client.retry import RetryPolicy, execute_with_retries


def _make_status_error(status_code: int, headers: dict[str, str] | None = None) -> anthropic.APIStatusError:
    """Build a real SDK exception around a synthetic HTTP response.

    Using the real exception classes keeps the tests honest about what the
    retry code will actually catch in production.
    """
    if headers is None:
        headers = {}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, headers=headers, request=request)
    if status_code == 429:
        return anthropic.RateLimitError(
            "rate limited", response=response, body=None
        )
    return anthropic.APIStatusError(
        f"http {status_code}", response=response, body=None
    )


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture sleep durations instead of actually waiting."""
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(retry_module.time, "sleep", fake_sleep)
    return sleeps


def test_retries_429_then_succeeds(recorded_sleeps: list[float]) -> None:
    attempts: list[int] = []

    def flaky_request() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise _make_status_error(429)
        return "ok"

    result = execute_with_retries(flaky_request, RetryPolicy(max_retries=5))

    assert result == "ok"
    assert len(attempts) == 3
    assert len(recorded_sleeps) == 2


def test_retries_529_overloaded(recorded_sleeps: list[float]) -> None:
    attempts: list[int] = []

    def flaky_request() -> str:
        attempts.append(1)
        if len(attempts) < 2:
            raise _make_status_error(529)
        return "ok"

    result = execute_with_retries(flaky_request, RetryPolicy(max_retries=5))

    assert result == "ok"
    assert len(attempts) == 2


def test_does_not_retry_400(recorded_sleeps: list[float]) -> None:
    attempts: list[int] = []

    def bad_request() -> str:
        attempts.append(1)
        raise _make_status_error(400)

    with pytest.raises(anthropic.APIStatusError):
        execute_with_retries(bad_request, RetryPolicy(max_retries=5))

    assert len(attempts) == 1
    assert len(recorded_sleeps) == 0


def test_respects_retry_after_header(recorded_sleeps: list[float]) -> None:
    attempts: list[int] = []

    def flaky_request() -> str:
        attempts.append(1)
        if len(attempts) < 2:
            raise _make_status_error(429, headers={"retry-after": "30"})
        return "ok"

    result = execute_with_retries(flaky_request, RetryPolicy(max_retries=5))

    assert result == "ok"
    # First backoff would be roughly 1-2s; retry-after of 30s must win.
    assert recorded_sleeps[0] >= 30.0


def test_gives_up_after_max_retries(recorded_sleeps: list[float]) -> None:
    attempts: list[int] = []

    def always_rate_limited() -> str:
        attempts.append(1)
        raise _make_status_error(429)

    with pytest.raises(anthropic.RateLimitError):
        execute_with_retries(always_rate_limited, RetryPolicy(max_retries=3))

    # 1 initial attempt plus 3 retries.
    assert len(attempts) == 4
    assert len(recorded_sleeps) == 3


def test_backoff_grows_and_caps(recorded_sleeps: list[float]) -> None:
    policy = RetryPolicy(
        base_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=4.0,
        max_retries=4,
        max_jitter_seconds=0.0,
    )
    attempts: list[int] = []

    def always_rate_limited() -> str:
        attempts.append(1)
        raise _make_status_error(429)

    with pytest.raises(anthropic.RateLimitError):
        execute_with_retries(always_rate_limited, policy)

    # With jitter disabled the delays are deterministic: 1, 2, 4, then capped at 4.
    assert recorded_sleeps == [1.0, 2.0, 4.0, 4.0]


def test_ignores_malformed_retry_after(recorded_sleeps: list[float]) -> None:
    attempts: list[int] = []

    def flaky_request() -> str:
        attempts.append(1)
        if len(attempts) < 2:
            raise _make_status_error(429, headers={"retry-after": "soon"})
        return "ok"

    result = execute_with_retries(flaky_request, RetryPolicy(max_retries=5))

    assert result == "ok"
    # Malformed header is ignored; normal backoff (about 1-2s) applies.
    assert recorded_sleeps[0] < 30.0
