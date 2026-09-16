"""Application-owned contract for rendering an authorized fallout workbook."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from impodo.domain.reconciliation import ReconciliationRun


@dataclass(frozen=True, slots=True)
class FalloutWorkbookCell:
    """One authorized difference resolved to an optional physical source cell."""

    source_file: str
    worksheet: str
    coordinate: str
    business_key: str
    odoo_record: str
    field: str
    source_value: Any
    prepared_value: Any
    odoo_value: Any
    result: str
    reason_code: str
    recommended_action: str


class FalloutWorkbookRenderer(Protocol):
    """Render a source-preserving copy without choosing an adapter in the service."""

    def __call__(
        self,
        source_path: Path,
        *,
        source_display_name: str,
        source_header_row: int,
        source_encoding: str | None = None,
        source_delimiter: str | None = None,
        report: ReconciliationRun,
        cells: Iterable[FalloutWorkbookCell],
    ) -> bytes: ...
