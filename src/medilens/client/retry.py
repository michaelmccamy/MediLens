"""Retry policy for model API calls.

CLAUDE.md section 5 requires: exponential backoff with jitter on 429 and 529,
respecting the retry-after header, failing fast on all other 4xx errors, and
never logging request payloads (they could contain PHI once real data flows
through this system). Only error types, status codes, and timing are logged.

Server errors (500 and 529) and network connection errors are also treated as
retryable because they are transient by nature; retrying them cannot change
the outcome of a well-formed request, only recover from a bad moment.
"""

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import anthropic

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryPolicy:
    """Backoff parameters, defaulted to the values in CLAUDE.md section 5."""

    base_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 60.0
    max_retries: int = 5
    max_jitter_seconds: float = 1.0


def _is_retryable_status(status_code: int) -> bool:
    """429 (rate limited) and 5xx (server-side, includes 529 overloaded).

    All other 4xx errors are caused by the request itself and will not
    succeed on retry, so they must fail fast (CLAUDE.md section 5).
    """
    if status_code == 429:
        return True
    if status_code >= 500:
        return True
    return False


def _read_retry_after_seconds(error: anthropic.APIStatusError) -> float | None:
    """Pull the retry-after header off a 429, tolerating absence or garbage.

    The header is advisory input from the network, so a malformed value is
    ignored rather than crashing the retry loop.
    """
    raw_value = error.response.headers.get("retry-after")
    if raw_value is None:
        return None
    try:
        parsed_value = float(raw_value)
    except ValueError:
        return None
    if parsed_value < 0:
        return None
    return parsed_value


def _delay_for_attempt(
    policy: RetryPolicy, attempt_index: int, retry_after_seconds: float | None
) -> float:
    """Exponential backoff with jitter, never shorter than retry-after.

    CLAUDE.md section 5 requires waiting at least as long as the server's
    retry-after header, so the computed backoff acts as a floor-raiser only.
    """
    backoff = policy.base_delay_seconds * (policy.multiplier**attempt_index)
    backoff = min(backoff, policy.max_delay_seconds)
    jitter = random.uniform(0.0, policy.max_jitter_seconds)
    delay = backoff + jitter
    if retry_after_seconds is not None and retry_after_seconds > delay:
        delay = retry_after_seconds
    return delay


def execute_with_retries(request: Callable[[], T], policy: RetryPolicy) -> T:
    """Run a request callable, retrying transient failures per the policy.

    The callable is expected to raise anthropic SDK exceptions. Anything
    non-retryable propagates immediately. After max_retries retryable
    failures, the last error propagates so callers fail loudly rather than
    receiving a silent None (CLAUDE.md section 7).
    """
    for attempt_index in range(policy.max_retries + 1):
        try:
            return request()
        except anthropic.APIStatusError as error:
            if not _is_retryable_status(error.status_code):
                logger.error(
                    "Model API request failed with non-retryable HTTP %s (%s)",
                    error.status_code,
                    type(error).__name__,
                )
                raise
            retryable_error: Exception = error
            retry_after_seconds = _read_retry_after_seconds(error)
        except anthropic.APIConnectionError as error:
            retryable_error = error
            retry_after_seconds = None

        # On the final attempt there is nothing left to retry, so surface the
        # error to the caller rather than swallowing it (CLAUDE.md section 7).
        if attempt_index == policy.max_retries:
            logger.error(
                "Model API request failed after %d retries (%s)",
                policy.max_retries,
                type(retryable_error).__name__,
            )
            raise retryable_error

        delay = _delay_for_attempt(policy, attempt_index, retry_after_seconds)
        logger.warning(
            "Model API request failed with retryable %s; retrying in %.1fs (attempt %d of %d)",
            type(retryable_error).__name__,
            delay,
            attempt_index + 1,
            policy.max_retries,
        )
        time.sleep(delay)

    # Only reachable if max_retries is negative, which yields an empty loop and
    # no request attempt at all. Treat that as a configuration error.
    raise RuntimeError(
        f"RetryPolicy.max_retries must be >= 0, got: {policy.max_retries}"
    )
