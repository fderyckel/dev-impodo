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


@dataclass(frozen=True, slots=True, order=True)
class OriginalSourceCoordinate:
    """Identify one exact accepted physical file cell, independently of its use."""

    dataset_id: str
    dataset: str
    source_row: int
    source_key: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.dataset_id, self.dataset, self.source_key)
        ):
            raise ValueError(
                "Original source coordinates require physical dataset and column identities"
            )
        if type(self.source_row) is not int or self.source_row < 1:
            raise ValueError(
                "Original source coordinates require a positive physical row"
            )


@dataclass(frozen=True, slots=True)
class OriginalSourceInput:
    """One proven physical input and its independently available original value."""

    coordinate: OriginalSourceCoordinate
    value: OriginalSourceValue

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, OriginalSourceCoordinate):
            raise TypeError(
                "Original source input requires a typed physical coordinate"
            )
        if not isinstance(self.value, OriginalSourceValue):
            raise TypeError("Original source input requires typed value evidence")


@dataclass(frozen=True, slots=True, order=True)
class OriginalSourceOccurrence:
    """Bind an original cohort to one saved logical field occurrence and trace."""

    dataset: str
    source_row: int
    target_field: str
    source_keys: tuple[str, ...]
    source_trace_id: str = ""

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.dataset, self.target_field)
        ):
            raise ValueError(
                "Original source occurrence requires logical dataset and target field"
            )
        if type(self.source_row) is not int or self.source_row < 1:
            raise ValueError(
                "Original source occurrence requires a positive logical row"
            )
        if not isinstance(self.source_keys, tuple) or any(
            not isinstance(key, str) or not key.strip() for key in self.source_keys
        ):
            raise TypeError(
                "Original source occurrence requires immutable saved source keys"
            )
        if not isinstance(self.source_trace_id, str):
            raise TypeError(
                "Original source occurrence requires a saved trace identifier"
            )


@dataclass(frozen=True, slots=True, order=True)
class OriginalSourceTarget:
    """Authorize one physical cell for an occurrence's complete ordered cohort."""

    occurrence: OriginalSourceOccurrence
    coordinate: OriginalSourceCoordinate
    input_coordinates: tuple[OriginalSourceCoordinate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.occurrence, OriginalSourceOccurrence):
            raise TypeError(
                "Original source target requires a typed logical occurrence"
            )
        if not isinstance(self.coordinate, OriginalSourceCoordinate):
            raise TypeError(
                "Original source target requires a typed physical coordinate"
            )
        if not isinstance(self.input_coordinates, tuple):
            raise TypeError(
                "Original source target requires immutable input coordinates"
            )
        if not self.input_coordinates:
            raise ValueError(
                "Original source target requires its complete ordered physical cohort"
            )
        # The request validates each shared cohort once. Scanning all components
        # here for every aggregate-row target would make construction quadratic.

    def __hash__(self) -> int:
        # Cohorts can span many aggregate rows; hashing each full cohort for
        # every authorized component would otherwise be quadratic.
        return hash((self.occurrence, self.coordinate))


@dataclass(frozen=True, slots=True)
class OriginalSourceRequest:
    """A bounded physical snapshot projection with cross-snapshot authorizations.

    Each request validates its own snapshot. The complete ordered cohort may
    span snapshots; the artifact adapter validates global target completeness
    before it reads any values.
    """

    snapshot: SourceSnapshot
    cells: tuple[OriginalSourceCoordinate, ...]
    targets: tuple[OriginalSourceTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.cells, tuple) or not isinstance(self.targets, tuple):
            raise TypeError(
                "Original source requests must use immutable cells and targets"
            )
        if self.snapshot.source.origin is not SourceOriginKind.FILE:
            raise ValueError("Workbook originals require a file source snapshot")
        if any(not isinstance(cell, OriginalSourceCoordinate) for cell in self.cells):
            raise TypeError("Original source requests require typed physical cells")
        if any(not isinstance(target, OriginalSourceTarget) for target in self.targets):
            raise TypeError(
                "Original source requests require typed target authorization"
            )
        cohorts: dict[int, frozenset[OriginalSourceCoordinate]] = {}
        for target in self.targets:
            cohort_id = id(target.input_coordinates)
            if cohort_id not in cohorts:
                if any(
                    not isinstance(item, OriginalSourceCoordinate)
                    for item in target.input_coordinates
                ):
                    raise TypeError(
                        "Original source target requires typed input coordinates"
                    )
                components = frozenset(target.input_coordinates)
                if len(components) != len(target.input_coordinates):
                    raise ValueError(
                        "Original source targets require distinct input coordinates"
                    )
                cohorts[cohort_id] = components
            if target.coordinate not in cohorts[cohort_id]:
                raise ValueError(
                    "Original source target must belong to its complete cohort"
                )
        keys = {column.stable_key for column in self.snapshot.schema.columns}
        if not self.cells or len(set(self.cells)) != len(self.cells):
            raise ValueError("Original source requests require distinct physical cells")
        if any(
            cell.dataset_id != self.snapshot.dataset_id
            or cell.dataset != self.snapshot.dataset_name
            or cell.source_key not in keys
            for cell in self.cells
        ):
            raise ValueError(
                "Original source coordinates do not match the physical snapshot"
            )
        if (
            not self.targets
            or len(set(self.targets)) != len(self.targets)
            or {target.coordinate for target in self.targets} != set(self.cells)
        ):
            raise ValueError(
                "Original source requests require exact physical target inputs"
            )
