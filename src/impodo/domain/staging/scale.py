"""Supported scale boundary for the in-memory browser evaluator."""

from __future__ import annotations

from dataclasses import dataclass

from ...workspace_contracts import SourceSelection
from ..errors import ReadinessError


BROWSER_EVALUATION_ROW_LIMIT = 25_000


@dataclass(frozen=True, slots=True)
class BrowserEvaluationScale:
    """Plain-language supported-size decision for the in-memory evaluator."""

    physical_rows: int
    supported_limit: int = BROWSER_EVALUATION_ROW_LIMIT

    @property
    def supported(self) -> bool:
        return self.physical_rows <= self.supported_limit


def browser_evaluation_scale(selection: SourceSelection) -> BrowserEvaluationScale:
    """Count frozen physical rows once, before derived datasets expand them."""

    return BrowserEvaluationScale(
        physical_rows=sum(item.row_count for item in selection.datasets)
    )


def require_supported_browser_scale(selection: SourceSelection) -> None:
    scale = browser_evaluation_scale(selection)
    if scale.supported:
        return
    raise ReadinessError(
        f"This project contains {scale.physical_rows:,} source rows. "
        f"This version of Impodo can safely check up to "
        f"{scale.supported_limit:,} rows in one project. Split the source into "
        "smaller projects before checking; no data was changed."
    )
