"""Bound diagnostic logging costs without logging the observations themselves.

Totals describe this recorder's process lifetime. UUID correlations are bounded
and overlap the totals and each other; component timings are inclusive, not an
additive breakdown. No filename, message, or customer value is retained here.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from uuid import UUID

MAX_LOGGING_CORRELATIONS = 64
LOGGING_COST_METRICS = frozenset({
    "diagnostic_encode_ms", "diagnostic_thread_lock_wait_ms",
    "diagnostic_file_lock_wait_ms", "diagnostic_emit_ms",
    "diagnostic_open_ms", "diagnostic_write_flush_ms", "diagnostic_close_ms",
    "diagnostic_rotation_ms", "diagnostic_caller_ms",
    "diagnostic_write_count", "diagnostic_write_attempt_count",
    "diagnostic_bytes_written", "diagnostic_open_count", "diagnostic_close_count",
    "diagnostic_lock_timeout_count", "diagnostic_rotation_count",
    "diagnostic_capture_failure_count", "request_reported_ms",
    "request_lifecycle_ms", "request_completed_count",
})
_MAX_VALUE = 2**53 - 1
_CORRELATIONS: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "impodo_diagnostic_cost_correlations", default=(),
)


def _uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 36:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


@contextmanager
def logging_correlations(payload: Mapping[str, object]) -> Iterator[None]:
    """Bind only validated UUIDs for one synchronous logging call."""

    identities = []
    request = _uuid(payload.get("request_id"))
    if request is not None:
        identities.append(("request", request))
    operation = _uuid(payload.get("performance_operation_id")) or _uuid(
        payload.get("operation_id")
    )
    nested = payload.get("performance")
    if operation is None and isinstance(nested, Mapping):
        operation = _uuid(nested.get("attempt_id"))
    if operation is not None:
        identities.append(("operation", operation))
    token = _CORRELATIONS.set(tuple(identities))
    try:
        yield
    finally:
        _CORRELATIONS.reset(token)


class DiagnosticCostTracker:
    """Accumulate fixed metric names and at most 64 UUID correlations."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._totals: dict[str, int | float] = {}
        self._correlations: OrderedDict[tuple[str, str], dict[str, int | float]] = OrderedDict()
        self._evicted = 0
        self.measurement_failed = False

    def record(self, metrics: Mapping[str, int | float]) -> None:
        """Merge bounded numeric observations; never retain arbitrary fields."""

        safe = {
            name: min(value, _MAX_VALUE)
            for name, value in metrics.items()
            if name in LOGGING_COST_METRICS and type(value) in {int, float}
            and math.isfinite(value) and value >= 0
        }
        with self._lock:
            self._merge(self._totals, safe)
            for identity in _CORRELATIONS.get():
                target = self._correlations.get(identity)
                if target is None:
                    if len(self._correlations) >= MAX_LOGGING_CORRELATIONS:
                        self._correlations.popitem(last=False)
                        self._evicted = min(_MAX_VALUE, self._evicted + 1)
                    target = {}
                    self._correlations[identity] = target
                self._correlations.move_to_end(identity)
                self._merge(target, safe)

    @staticmethod
    def _merge(target: dict[str, int | float], metrics: Mapping[str, int | float]) -> None:
        for name, value in metrics.items():
            target[name] = min(_MAX_VALUE, target.get(name, 0) + value)

    def snapshot(self) -> dict[str, object]:
        """Return detached process-local costs, scope, and capture coverage."""

        with self._lock:
            return {
                "schema_version": 1,
                "scope": "process_recorder",
                "timing_scope": "inclusive",
                "component_timings_may_overlap": True,
                "correlations_are_non_additive": True,
                "capture_complete": not self.measurement_failed,
                "child_process_coverage": "unavailable",
                "correlation_limit": MAX_LOGGING_CORRELATIONS,
                "dropped_correlation_count": self._evicted,
                "metrics": dict(self._totals),
                "correlations": [
                    {"kind": kind, "id": identity, "metrics": dict(metrics)}
                    for (kind, identity), metrics in self._correlations.items()
                ],
            }


def record_logging_costs(
    tracker: DiagnosticCostTracker, metrics: Mapping[str, int | float],
) -> None:
    """Keep optional accounting failures outside diagnostic write outcomes."""

    try:
        tracker.record(metrics)
    except Exception:  # noqa: BLE001 - observation failures cannot affect requests
        tracker.measurement_failed = True
