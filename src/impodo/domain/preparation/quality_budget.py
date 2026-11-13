"""Bound retained quality findings without changing their evidence identity."""

from __future__ import annotations

from dataclasses import dataclass

QUALITY_FINDING_INDEX_MAX_BYTES = 64 * 1024 * 1024
QUALITY_FINDING_BUDGET_MESSAGE = (
    "The data-check findings exceed the bounded review memory budget. "
    "Correct the repeated source errors and prepare again; no whole-run fallback was run."
)


class QualityFindingBudgetExceeded(ValueError):
    """Reject an incomplete quality overlay before publication."""


@dataclass(slots=True)
class QualityFindingBudget:
    """Account one aggregate retained-evidence allowance across all datasets."""

    limit_bytes: int = QUALITY_FINDING_INDEX_MAX_BYTES
    used_bytes: int = 0

    def consume(self, byte_count: int) -> None:
        """Charge one retained representation before extending its collection."""

        if byte_count < 0:
            raise ValueError("Quality finding sizes cannot be negative")
        if self.used_bytes + byte_count > self.limit_bytes:
            raise QualityFindingBudgetExceeded(QUALITY_FINDING_BUDGET_MESSAGE)
        self.used_bytes += byte_count
