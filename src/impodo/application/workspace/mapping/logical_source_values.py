"""Define bounded value reads shared by mapping choices and validation.

Readers resolve logical source columns without depending on Odoo model names.
The caller owns the synchronous operation; implementations must close their
database and artifact owners before returning.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import polars as pl

from impodo.domain.workspace.contracts import SourceSelection


class LogicalSourceValues(Protocol):
    """Project reviewed logical datasets and count their exact source choices."""

    def supports(self, workspace_id: str, selection: SourceSelection) -> bool:
        """Return whether the current derivation has a bounded reader."""
        ...

    def project(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        column_keys: Sequence[str],
    ) -> pl.DataFrame:
        """Return only requested value columns in logical row order."""
        ...

    def counts(
        self,
        workspace_id: str,
        selection: SourceSelection,
        dataset_id: str,
        column_key: str,
        maximum_values: int,
    ) -> dict[str, int]:
        """Return at most maximum_values plus one nonblank trimmed choices."""
        ...
