"""Provide a deterministic clock for sequential and concurrent Odoo reads."""

from __future__ import annotations

from threading import Lock

from impodo.adapters.odoo.read_pacing import Json2ReadPacer


class ReadClock:
    """Advance cooldown time without real sleeps or shared production state."""

    def __init__(self):
        self.elapsed = 0.0
        self.waits = []
        self._lock = Lock()

    def time(self):
        with self._lock:
            return self.elapsed

    def sleep(self, seconds):
        with self._lock:
            self.waits.append(seconds)
            self.elapsed += seconds

    def pacer(self):
        return Json2ReadPacer(clock=self.time, sleep=self.sleep, jitter=lambda: 0.0)
