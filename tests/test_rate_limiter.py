"""Tests for the client-side token bucket rate limiter."""

import time

import pytest

from medilens.client.rate_limiter import RequestAndTokenLimiter, TokenBucket


def test_bucket_starts_full_and_acquires_immediately() -> None:
    bucket = TokenBucket(capacity_per_minute=60)

    start = time.monotonic()
    bucket.acquire(30)
    elapsed = time.monotonic() - start

    assert elapsed < 0.1


def test_bucket_blocks_until_refill_when_drained() -> None:
    # 6000 per minute refills at 100 per second, so draining the bucket and
    # asking for 10 more should block for roughly 0.1 seconds.
    bucket = TokenBucket(capacity_per_minute=6000)
    bucket.acquire(6000)

    start = time.monotonic()
    bucket.acquire(10)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.05


def test_bucket_rejects_amount_beyond_capacity() -> None:
    bucket = TokenBucket(capacity_per_minute=100)

    with pytest.raises(ValueError):
        bucket.acquire(101)


def test_bucket_rejects_nonpositive_capacity() -> None:
    with pytest.raises(ValueError):
        TokenBucket(capacity_per_minute=0)


def test_limiter_rejects_negative_token_estimate() -> None:
    limiter = RequestAndTokenLimiter(
        max_requests_per_minute=10, max_tokens_per_minute=1000
    )

    with pytest.raises(ValueError):
        limiter.acquire(-1)


def test_limiter_consumes_both_budgets() -> None:
    limiter = RequestAndTokenLimiter(
        max_requests_per_minute=10, max_tokens_per_minute=1000
    )

    limiter.acquire(500)

    assert limiter.request_bucket.available == pytest.approx(9, abs=0.1)
    assert limiter.token_bucket.available == pytest.approx(500, abs=1.0)


def test_acquire_request_skips_token_budget() -> None:
    limiter = RequestAndTokenLimiter(
        max_requests_per_minute=10, max_tokens_per_minute=1000
    )

    limiter.acquire_request()

    assert limiter.request_bucket.available == pytest.approx(9, abs=0.1)
    assert limiter.token_bucket.available == pytest.approx(1000, abs=1.0)
