"""Client-side rate limiting for model API calls.

CLAUDE.md section 5 requires a token-bucket limiter so bursts never exceed the
account tier. Two budgets are tracked separately, because Anthropic enforces
them separately: requests per minute, and tokens per minute. Throttling here,
before the request is sent, keeps latency predictable and saves the retry
budget for genuinely transient failures instead of self-inflicted 429s.
"""

import threading
import time


class TokenBucket:
    """A continuously refilling token bucket that blocks callers until capacity.

    The bucket starts full and refills at capacity_per_minute / 60 per second.
    acquire() blocks the calling thread rather than raising, so call sites do
    not need their own sleep-and-retry loops. Thread safety matters because
    CLAUDE.md section 5 plans for a bounded worker pool of concurrent requests
    later; this class is safe to share across those workers unchanged.
    """

    def __init__(self, capacity_per_minute: float) -> None:
        if capacity_per_minute <= 0:
            raise ValueError(
                f"capacity_per_minute must be positive, got: {capacity_per_minute}"
            )
        self.capacity = float(capacity_per_minute)
        self.refill_per_second = float(capacity_per_minute) / 60.0
        self.available = float(capacity_per_minute)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        """Credit the bucket for time elapsed since the last refill.

        Caller must hold self.lock.
        """
        now = time.monotonic()
        elapsed_seconds = now - self.last_refill
        refill_amount = elapsed_seconds * self.refill_per_second
        self.available = min(self.capacity, self.available + refill_amount)
        self.last_refill = now

    def acquire(self, amount: float = 1.0) -> None:
        """Block until amount is available, then consume it.

        An amount larger than the bucket capacity can never be satisfied and
        would block forever, so it fails loudly instead (CLAUDE.md section 7).
        """
        if amount > self.capacity:
            raise ValueError(
                f"requested amount {amount} exceeds bucket capacity {self.capacity}; "
                "raise the configured limit or split the request"
            )
        while True:
            with self.lock:
                self._refill()
                if self.available >= amount:
                    self.available -= amount
                    return
                deficit = amount - self.available
                wait_seconds = deficit / self.refill_per_second
            # Sleep outside the lock so other threads can acquire smaller
            # amounts while this caller waits for capacity.
            time.sleep(wait_seconds)


class RequestAndTokenLimiter:
    """Combined limiter covering both budgets the API enforces per minute."""

    def __init__(self, max_requests_per_minute: int, max_tokens_per_minute: int) -> None:
        self.request_bucket = TokenBucket(max_requests_per_minute)
        self.token_bucket = TokenBucket(max_tokens_per_minute)

    def acquire_request(self) -> None:
        """Reserve capacity for one request that carries no token cost.

        Used for lightweight calls such as the Token Counting API, which
        count against the request budget but not the token budget.
        """
        self.request_bucket.acquire(1.0)

    def acquire(self, estimated_tokens: int) -> None:
        """Reserve capacity for one request plus its estimated token cost.

        The token estimate should cover input tokens (from the Token Counting
        API) plus the max output tokens requested, since output tokens count
        against the per-minute token budget as well.
        """
        if estimated_tokens < 0:
            raise ValueError(f"estimated_tokens must not be negative, got: {estimated_tokens}")
        self.acquire_request()
        self.token_bucket.acquire(float(estimated_tokens))
