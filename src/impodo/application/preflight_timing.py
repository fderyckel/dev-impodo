"""Privacy-safe timing hooks for Final review publication.

The collector accepts only an allowlisted phase name, elapsed time, and call
count. It cannot retain Workspace identifiers, paths, SQL, row values, or
credentials. Production behavior is unchanged when no collector is active.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

PREFLIGHT_PUBLICATION_PHASES = frozenset(
    {
        "execution_snapshot_serialization",
        "manifest_serialization",
        "manifest_artifact_write",
        "execution_snapshot_artifact_write",
        "report_header_encoding",
        "report_header_insertion",
        "dataset_row_encoding",
        "dataset_row_insertion",
        "decision_row_encoding",
        "decision_row_insertion",
        "target_snapshot_encoding",
        "target_snapshot_insertion",
        "execution_projection_encoding",
        "execution_projection_insertion",
        "current_pointer_update",
        "local_attempt_finalization",
    }
)


@dataclass(slots=True)
class PreflightPublicationTimings:
    """Accumulate allowlisted phase durations without customer data."""

    milliseconds: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    def record(self, phase: str, duration_ms: float) -> None:
        if phase not in PREFLIGHT_PUBLICATION_PHASES:
            raise ValueError("Preflight publication timing phase is invalid")
        self.milliseconds[phase] = self.milliseconds.get(phase, 0.0) + max(
            0.0,
            float(duration_ms),
        )
        self.calls[phase] = self.calls.get(phase, 0) + 1


_CURRENT_PREFLIGHT_PUBLICATION_TIMINGS: ContextVar[
    PreflightPublicationTimings | None
] = ContextVar("impodo_preflight_publication_timings", default=None)


@contextmanager
def collect_preflight_publication_timings() -> Iterator[PreflightPublicationTimings]:
    """Install one timing collector in the current execution context."""

    collector = PreflightPublicationTimings()
    token = _CURRENT_PREFLIGHT_PUBLICATION_TIMINGS.set(collector)
    try:
        yield collector
    finally:
        _CURRENT_PREFLIGHT_PUBLICATION_TIMINGS.reset(token)


@contextmanager
def timed_preflight_publication(phase: str) -> Iterator[None]:
    """Measure one allowlisted phase only when a collector is active."""

    if phase not in PREFLIGHT_PUBLICATION_PHASES:
        raise ValueError("Preflight publication timing phase is invalid")
    collector = _CURRENT_PREFLIGHT_PUBLICATION_TIMINGS.get()
    if collector is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        collector.record(phase, (perf_counter() - started) * 1000)
