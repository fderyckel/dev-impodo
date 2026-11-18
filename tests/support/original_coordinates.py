"""Build typed physical coordinates for small original-evidence fixtures."""

from collections.abc import Iterable

from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalSourceOccurrence,
    OriginalSourceTarget,
)


def source_coordinate(
    row: int,
    key: str,
    *,
    dataset_id: str = "dataset:" + "1" * 24,
    dataset: str = "Customers",
) -> OriginalSourceCoordinate:
    return OriginalSourceCoordinate(dataset_id, dataset, row, key)


def source_coordinates(
    cells: Iterable[tuple[int, str]],
) -> tuple[OriginalSourceCoordinate, ...]:
    return tuple(source_coordinate(row, key) for row, key in cells)


def source_target(
    row: int,
    key: str,
    target: str,
    input_keys: tuple[str, ...] | None = None,
    *,
    source_keys: tuple[str, ...] | None = None,
    source_trace_id: str = "",
) -> OriginalSourceTarget:
    keys = (key,) if input_keys is None else input_keys
    return OriginalSourceTarget(
        OriginalSourceOccurrence(
            "Customers",
            row,
            target,
            keys if source_keys is None else source_keys,
            source_trace_id,
        ),
        source_coordinate(row, key),
        tuple(source_coordinate(row, item) for item in keys),
    )
