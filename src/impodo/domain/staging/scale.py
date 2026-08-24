"""Supported scale boundaries for bounded-direct and materialized preparation."""

from __future__ import annotations

from dataclasses import dataclass

from ...workspace_contracts import SourceSelection
from ..errors import ReadinessError


MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT = 25_000
BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT = 50_000
COLUMNAR_DIRECT_BROWSER_EVALUATION_ROW_LIMIT = 100_000


@dataclass(frozen=True, slots=True)
class BrowserEvaluationScale:
    """Plain-language supported-size decision for one preparation path."""

    physical_rows: int
    supported_limit: int = MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT

    @property
    def supported(self) -> bool:
        """Whether the physical selection fits the in-memory safety limit."""

        return self.physical_rows <= self.supported_limit


def browser_evaluation_scale(
    selection: SourceSelection,
    *,
    supported_limit: int = MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT,
) -> BrowserEvaluationScale:
    """Count frozen physical rows once, before derived datasets expand them."""

    if supported_limit < 1:
        raise ValueError("supported_limit must be positive")
    return BrowserEvaluationScale(
        physical_rows=sum(item.row_count for item in selection.datasets),
        supported_limit=supported_limit,
    )


def require_supported_browser_scale(
    selection: SourceSelection,
    *,
    supported_limit: int = MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT,
) -> None:
    """Stop Stage E before loading data when the project exceeds the limit."""

    scale = browser_evaluation_scale(
        selection,
        supported_limit=supported_limit,
    )
    if scale.supported:
        return
    raise ReadinessError(
        f"This workspace selection contains {scale.physical_rows:,} source rows. "
        f"This version of Impodo can safely check up to "
        f"{scale.supported_limit:,} rows in one project. Split the source into "
        "smaller projects before checking; no data was changed."
    )
