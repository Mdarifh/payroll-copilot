"""Retry with exponential backoff and a token-bucket rate limiter (thread-safe)."""

import threading
import time
from collections.abc import Callable

from sqlalchemy.exc import OperationalError


class TransientError(Exception):
    """A temporary failure worth retrying (timeout, rate limit, locked database...)."""


RETRYABLE: tuple[type[BaseException], ...] = (TransientError, OperationalError,
                                              TimeoutError, ConnectionError)  # fmt: skip


def with_retry[T](
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = RETRYABLE,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``; on a retryable error wait 0.5 s, 1 s, 2 s ... and try again."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on:
            if attempt == attempts:
                raise
            sleep(base_delay * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


class RateLimiter:
    """At most ``rate`` calls per second on average, bursts up to ``burst`` calls."""

    def __init__(self, rate: float, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:  # fmt: skip
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = float(burst or max(1, int(rate)))
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                self.sleep(wait)
                self.tokens, self.updated = 0.0, self.clock()
            else:
                self.tokens -= 1
