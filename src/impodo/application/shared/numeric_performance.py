"""Observe actual bounded numeric validation work without retaining values."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter


@dataclass(slots=True)
class NumericValidationCollector:
    """Aggregate value-free numeric page and publication measurements."""

    cells_examined: int = 0
    findings_generated: int = 0
    value_page_count: int = 0
    failure_batch_count: int = 0
    max_value_page_rows: int = 0
    max_value_page_bytes: int = 0
    max_failure_batch_rows: int = 0
    max_failure_batch_bytes: int = 0
    elapsed_ms: float = 0.0

    def snapshot(self) -> dict[str, int | float]:
        """Return only scalar observations safe for the diagnostic boundary."""

        return {
            "cells_examined": self.cells_examined,
            "findings_generated": self.findings_generated,
            "value_page_count": self.value_page_count,
            "failure_batch_count": self.failure_batch_count,
            "max_value_page_rows": self.max_value_page_rows,
            "max_value_page_bytes": self.max_value_page_bytes,
            "max_failure_batch_rows": self.max_failure_batch_rows,
            "max_failure_batch_bytes": self.max_failure_batch_bytes,
            "elapsed_ms": round(max(0.0, self.elapsed_ms), 3),
        }


_CURRENT: ContextVar[NumericValidationCollector | None] = ContextVar(
    "impodo_numeric_validation", default=None
)


@contextmanager
def collect_numeric_validation() -> Iterator[NumericValidationCollector]:
    """Collect numeric work owned by one preparation worker."""

    collector = NumericValidationCollector()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


@contextmanager
def time_numeric_validation() -> Iterator[None]:
    """Include failed numeric work without measuring unrelated worker phases."""

    started = perf_counter()
    try:
        yield
    finally:
        collector = _CURRENT.get()
        if collector is not None:
            collector.elapsed_ms += (perf_counter() - started) * 1000


def observe_numeric_value_page(*, cells: int, findings: int, byte_count: int) -> None:
    """Merge one actual scan page; values and row identities never enter it."""

    collector = _CURRENT.get()
    if collector is not None and cells:
        collector.cells_examined += cells
        collector.findings_generated += findings
        collector.value_page_count += 1
        collector.max_value_page_rows = max(collector.max_value_page_rows, cells)
        collector.max_value_page_bytes = max(
            collector.max_value_page_bytes, byte_count
        )


def observe_numeric_failure_batch(*, rows: int, byte_count: int) -> None:
    """Count one successfully inserted set-based failure batch."""

    collector = _CURRENT.get()
    if collector is not None and rows:
        collector.failure_batch_count += 1
        collector.max_failure_batch_rows = max(
            collector.max_failure_batch_rows, rows
        )
        collector.max_failure_batch_bytes = max(
            collector.max_failure_batch_bytes, byte_count
        )
