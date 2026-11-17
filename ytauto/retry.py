"""Retry dengan exponential backoff + jitter untuk semua panggilan API."""
from __future__ import annotations

import functools
import logging
import random
import time
from typing import Callable, TypeVar

log = logging.getLogger("ytauto.retry")
T = TypeVar("T")


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


# Diganti di tes supaya tidak benar-benar menunggu.
SLEEP: Callable[[float], None] = _default_sleep


class RetryableError(Exception):
    """Error sementara (rate limit, 5xx, koneksi putus) yang layak dicoba ulang."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """attempt dimulai dari 1. Hasil: base*2^(attempt-1) dengan jitter 50-100%."""
    raw = min(cap, base * (2 ** (attempt - 1)))
    return raw * (0.5 + random.random() / 2)


def retry(
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_on: tuple[type[BaseException], ...] = (RetryableError,),
):
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_on as e:  # type: ignore[misc]
                    if attempt >= attempts:
                        raise
                    delay = getattr(e, "retry_after", None) or backoff_delay(attempt, base_delay, max_delay)
                    delay = min(float(delay), max_delay)
                    log.warning(
                        "%s gagal (percobaan %d/%d): %s. Coba lagi dalam %.1f detik",
                        fn.__name__, attempt, attempts, e, delay,
                    )
                    SLEEP(delay)
            raise AssertionError("unreachable")

        return wrapper

    return deco
