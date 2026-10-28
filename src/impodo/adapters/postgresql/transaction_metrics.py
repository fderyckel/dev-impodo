"""Report privacy-safe budgets for one bounded PostgreSQL transaction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class PostgreSQLTransactionOutcome(StrEnum):
    """Describe whether the transaction committed or left no accepted result."""

    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PostgreSQLTransactionMetric:
    """Expose bounded counts and timing without row values or tenant identity."""

    operation: str
    outcome: PostgreSQLTransactionOutcome
    connection_count: int
    query_count: int
    pool_wait_milliseconds: float
    transaction_duration_milliseconds: float
    retry_count: int


PostgreSQLTransactionMetricObserver = Callable[[PostgreSQLTransactionMetric], None]


def report_postgresql_transaction_metric(
    observer: PostgreSQLTransactionMetricObserver | None,
    metric: PostgreSQLTransactionMetric,
) -> None:
    """Keep optional diagnostics from changing a governed repository result."""

    if observer is None:
        return
    try:
        observer(metric)
    except Exception:  # noqa: BLE001 - optional diagnostics are isolated
        # A diagnostic sink cannot roll back or replace an accepted DB outcome.
        return


__all__ = [
    "PostgreSQLTransactionMetric",
    "PostgreSQLTransactionMetricObserver",
    "PostgreSQLTransactionOutcome",
    "report_postgresql_transaction_metric",
]
