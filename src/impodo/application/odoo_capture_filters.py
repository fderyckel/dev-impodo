"""Protected, selection-bound Odoo source predicates."""

from __future__ import annotations

from typing import Protocol

from ..domain.odoo_capture import OdooCaptureFilterClause, OdooCaptureSelection


class OdooCaptureFilterStore(Protocol):
    """Keep business predicate values outside ordinary selection evidence."""

    def put(
        self,
        project_id: str,
        *,
        selection_id: str,
        version: int,
        data_version_id: str,
        clauses: tuple[OdooCaptureFilterClause, ...],
    ) -> str:
        """Return the encrypted artifact hash for a new selection version."""

    def read(
        self,
        project_id: str,
        selection: OdooCaptureSelection,
    ) -> tuple[OdooCaptureFilterClause, ...]:
        """Fail closed if the bound artifact is missing or changed."""
