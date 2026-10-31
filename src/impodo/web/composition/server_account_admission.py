"""Bound public account work per process without retaining secrets or DB owners."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from time import monotonic


class ServerAccountBusyError(RuntimeError):
    """Reject account work before opening a database or deriving a password hash."""


@dataclass(slots=True)
class _Bucket:
    remaining: float
    updated_at: float


class ServerAccountAdmission:
    """Bound concurrency, request rates, and source bookkeeping for one process."""

    def __init__(
        self,
        *,
        concurrency: int = 2,
        global_burst: int = 60,
        source_burst: int = 30,
        max_sources: int = 1024,
        window_seconds: float = 60,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            min(concurrency, global_burst, source_burst, max_sources, window_seconds)
            <= 0
        ):
            raise ValueError("Account admission limits must be positive")
        self._capacity = BoundedSemaphore(concurrency)
        self._lock = Lock()
        self._clock = clock
        self._global_burst = global_burst
        self._source_burst = source_burst
        self._max_sources = max_sources
        self._window = window_seconds
        self._global = _Bucket(global_burst, clock())
        self._sources: dict[str, _Bucket] = {}

    @contextmanager
    def admit(self, source_digest: str) -> Iterator[None]:
        """Admit immediately or reject; release capacity on every completion path."""
        with self._lock:
            now = self._clock()
            source = self._sources.get(source_digest)
            if source is None:
                if len(self._sources) >= self._max_sources:
                    self._sources = {
                        key: value
                        for key, value in self._sources.items()
                        if now - value.updated_at < self._window
                    }
                if len(self._sources) >= self._max_sources:
                    raise ServerAccountBusyError
                source = self._sources[source_digest] = _Bucket(self._source_burst, now)
            _refill(self._global, self._global_burst, now, self._window)
            _refill(source, self._source_burst, now, self._window)
            if self._global.remaining < 1 or source.remaining < 1:
                raise ServerAccountBusyError
            if not self._capacity.acquire(blocking=False):
                raise ServerAccountBusyError
            self._global.remaining -= 1
            source.remaining -= 1
        try:
            yield
        finally:
            self._capacity.release()


def _refill(bucket: _Bucket, burst: int, now: float, window: float) -> None:
    bucket.remaining = min(
        burst, bucket.remaining + max(0, now - bucket.updated_at) * burst / window
    )
    bucket.updated_at = now
