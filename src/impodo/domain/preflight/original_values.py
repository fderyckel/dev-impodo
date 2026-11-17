"""Typed original-cell evidence for the portable review workbook.

Values describe the immutable strict-reader snapshot, not spreadsheet display
formatting. Withheld and unavailable evidence cannot carry source contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..source_binding import SourceOriginKind
from ..source_snapshot import EncodedSourceCell, SourceCellKind, SourceSnapshot


class OriginalValueState(StrEnum):
    """Distinguish a recorded value or blank from missing and private evidence."""

    PRESENT = "present"
    BLANK = "blank"
    WITHHELD = "withheld"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class OriginalSourceValue:
    """One accepted source scalar, or an explicit reason it cannot be shown."""

    state: OriginalValueState
    cell: EncodedSourceCell | None = None

    def __post_init__(self) -> None:
        if self.state in {OriginalValueState.WITHHELD, OriginalValueState.UNAVAILABLE}:
            if self.cell is not None:
                raise ValueError(
                    "Unavailable or withheld originals cannot contain source values"
                )
            return
        if self.cell is None:
            raise ValueError("A recorded original requires encoded source evidence")
        blank = self.cell.kind is SourceCellKind.NULL or (
            self.cell.kind is SourceCellKind.STRING and self.cell.text == ""
        )
        if blank != (self.state is OriginalValueState.BLANK):
            raise ValueError("Original source value state does not match its evidence")

    @classmethod
    def from_cell(cls, cell: EncodedSourceCell) -> OriginalSourceValue:
        """Preserve exact snapshot text, including whitespace and empty strings."""
        blank = cell.kind is SourceCellKind.NULL or (
            cell.kind is SourceCellKind.STRING and cell.text == ""
        )
        return cls(
            OriginalValueState.BLANK if blank else OriginalValueState.PRESENT, cell
        )

    @classmethod
    def unavailable(cls) -> OriginalSourceValue:
        """Mark an original whose source-cell evidence was not established."""
        return cls(OriginalValueState.UNAVAILABLE)

    @classmethod
    def withheld(cls) -> OriginalSourceValue:
        """Mark an original that must stay outside this portable export."""
        return cls(OriginalValueState.WITHHELD)

    @property
    def text(self) -> str | None:
        """Return only captured source text; missing evidence stays distinct."""
        return self.cell.text if self.cell is not None else None


@dataclass(frozen=True, slots=True)
class OriginalSourceInput:
    """One proven physical input and its independently available original value."""

    source_key: str
    value: OriginalSourceValue

    def __post_init__(self) -> None:
        if not isinstance(self.source_key, str) or not self.source_key.strip():
            raise ValueError("Original source input requires a source key")
        if not isinstance(self.value, OriginalSourceValue):
            raise TypeError("Original source input requires typed value evidence")


@dataclass(frozen=True, slots=True, order=True)
class OriginalSourceTarget:
    """Authorize one cell for one target and its complete ordered input cohort."""

    source_row: int
    source_key: str
    target_field: str
    input_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.input_keys, tuple):
            raise TypeError("Original source target input keys must be immutable")
        if type(self.source_row) is not int or self.source_row < 1:
            raise ValueError("Original source target requires a positive physical row")
        if not isinstance(self.target_field, str) or not self.target_field.strip():
            raise ValueError("Original source target requires a target field")
        if (
            not self.input_keys
            or len(set(self.input_keys)) != len(self.input_keys)
            or any(
                not isinstance(key, str) or not key.strip() for key in self.input_keys
            )
            or self.source_key not in self.input_keys
        ):
            raise ValueError(
                "Original source target requires its complete ordered input keys"
            )


@dataclass(frozen=True, slots=True)
class OriginalSourceRequest:
    """A bounded set of physical file inputs for complete proven field outcomes."""

    snapshot: SourceSnapshot
    dataset: str
    cells: tuple[tuple[int, str], ...]
    targets: tuple[OriginalSourceTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cells, tuple) or not isinstance(self.targets, tuple):
            raise TypeError(
                "Original source requests must use immutable coordinates and targets"
            )
        if self.snapshot.source.origin is not SourceOriginKind.FILE:
            raise ValueError("Workbook originals require a file source snapshot")
        if self.dataset != self.snapshot.dataset_name:
            raise ValueError("Original source dataset does not match its snapshot")
        keys = {column.stable_key for column in self.snapshot.schema.columns}
        if not self.cells or len(set(self.cells)) != len(self.cells):
            raise ValueError("Original source requests require distinct source cells")
        if any(
            type(row) is not int or row < 1 or key not in keys
            for row, key in self.cells
        ):
            raise ValueError("Original source coordinates are not in the snapshot")
        if any(not isinstance(target, OriginalSourceTarget) for target in self.targets):
            raise TypeError(
                "Original source requests require typed target authorization"
            )
        if (
            not self.targets
            or len(set(self.targets)) != len(self.targets)
            or {(target.source_row, target.source_key) for target in self.targets}
            != set(self.cells)
            or any(
                key not in keys for target in self.targets for key in target.input_keys
            )
        ):
            raise ValueError(
                "Original source requests require proven physical target inputs"
            )
        authorized = set(self.targets)
        if any(
            OriginalSourceTarget(
                target.source_row, key, target.target_field, target.input_keys
            )
            not in authorized
            for target in self.targets
            for key in target.input_keys
        ):
            raise ValueError(
                "Original source requests must authorize every input of each target"
            )
