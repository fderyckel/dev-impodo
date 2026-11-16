"""Coordinate bounded, adaptive cooldowns for read-only JSON-2 requests.

Only hashed target identities and timing state are retained. Healthy targets
keep their configured read concurrency. A 429 pauses new reads to that target
and spaces subsequent starts for one minute. This process-local coordination
does not retain credentials, metadata, database owners, or HTTP responses.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock

READ_RETRY_BUDGET_SECONDS = 120.0
_MAX_TARGETS = 256
_RECOVERY_SECONDS = 60.0


def retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
    """Parse bounded Retry-After seconds or an HTTP date without retaining it."""

    if value is None or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        instant = parsedate_to_datetime(value)
        if instant.tzinfo is None:
            return None
        delay = (instant - now.astimezone(UTC)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None
    return max(0.0, delay) if math.isfinite(delay) else None


@dataclass(slots=True)
class _ReadWindow:
    """Keep one target's cooldown and next permissible request start."""

    cooldown_until: float = 0.0
    next_start: float = 0.0
    spacing: float = 0.0
    expires_at: float = 0.0


class Json2ReadPacer:
    """Share throttling across connectors without holding a lock during I/O."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """Allow deterministic timing tests without changing production policy."""

        self.clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.25))
        self._lock = Lock()
        self._targets: dict[str, _ReadWindow] = {}
        self._overflow = _ReadWindow()

    def _prune(self, now: float) -> None:
        """Discard expired windows while preserving every active cooldown."""

        self._targets = {
            key: window
            for key, window in self._targets.items()
            if window.expires_at > now
        }
        if self._overflow.expires_at <= now:
            self._overflow = _ReadWindow()

    def defer(self, target: str, *, attempt: int, retry_after: float | None) -> None:
        """Extend a shared cooldown even when the caller has exhausted retries."""

        fallback = min(30.0, 2.0 ** min(attempt + 1, 5))
        delay = max(retry_after or 0.0, fallback * (1.0 + self._jitter()))
        with self._lock:
            now = self.clock()
            self._prune(now)
            window = self._targets.get(target)
            if window is None:
                if len(self._targets) < _MAX_TARGETS:
                    window = self._targets.setdefault(target, _ReadWindow())
                else:
                    # Preserve active targets instead of evicting their waits.
                    # Excess targets conservatively share one bounded window.
                    window = self._overflow
            window.cooldown_until = max(window.cooldown_until, now + delay)
            window.spacing = min(8.0, max(1.0, window.spacing * 2.0))
            window.expires_at = max(
                window.expires_at, window.cooldown_until + _RECOVERY_SECONDS
            )

    def wait(self, target: str, *, deadline: float) -> bool:
        """Admit one read within its budget, rechecking extended cooldowns."""

        while True:
            with self._lock:
                now = self.clock()
                if now >= deadline:
                    return False
                self._prune(now)
                window = self._targets.get(target)
                if window is None and self._overflow.expires_at > now:
                    window = self._overflow
                if window is None:
                    return True
                delay = max(window.cooldown_until, window.next_start) - now
                if delay <= 0.0:
                    window.next_start = now + window.spacing
                    return True
                if now + delay >= deadline:
                    return False
            self._sleep(delay)


SHARED_JSON2_READ_PACER = Json2ReadPacer()
