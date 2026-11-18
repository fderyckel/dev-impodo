"""Hydrate bounded workbook originals from verified immutable file snapshots.

Requests come from the comparison-bound workbook projection. This adapter
reads only those source cells and never reopens an uploaded file or database.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import polars as pl

from impodo.application.data_version.source_snapshots import (
    validate_source_snapshot_path,
)
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.domain.errors import ReadinessError
from impodo.domain.preflight.fields_set_aside import FieldSetAside
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalSourceOccurrence,
    OriginalSourceRequest,
    OriginalSourceValue,
)
from impodo.domain.preflight.reports import ReviewWorkbookEvidence
from impodo.domain.source_snapshot import (
    SOURCE_ROW_COLUMN,
    EncodedSourceCell,
    SourceSnapshot,
    SourceSnapshotContractError,
)

type _PhysicalCohort = tuple[OriginalSourceCoordinate, ...]

_ORIGINAL_BATCH_ROWS = 1_000
_RECOVERY = (
    "Original source cells could not be verified. Prepare the data again "
    "and run the Odoo comparison again."
)


def populate_workbook_original_values(
    evidence: ReviewWorkbookEvidence | None,
    artifacts: DataVersionSourceArtifactStore,
) -> ReviewWorkbookEvidence | None:
    """Add exact requested originals, then discard their materialization requests."""

    if evidence is None:
        return None
    fields = evidence.fields_set_aside
    if fields is None:
        return replace(evidence, original_source_requests=())
    if fields.source_values_restricted:
        return replace(
            evidence,
            fields_set_aside=replace(
                fields,
                findings=tuple(
                    replace(finding, source_inputs=(), source_inputs_known=False)
                    for finding in fields.findings
                ),
            ),
            original_source_requests=(),
        )
    if not evidence.original_source_requests:
        return evidence

    cohorts = _authorized_cohorts(evidence.original_source_requests)
    originals: dict[OriginalSourceCoordinate, OriginalSourceValue] = {}
    for snapshot, requested in _group_requests(evidence.original_source_requests):
        try:
            with artifacts.materialize_source_snapshot(
                snapshot.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path:
                snapshot_path = validate_source_snapshot_path(path, snapshot)
                originals.update(
                    _read_requested_cells(snapshot_path, snapshot, requested)
                )
        except (OSError, TypeError, ValueError, pl.exceptions.PolarsError) as error:
            raise ReadinessError(_RECOVERY) from error

    return replace(
        evidence,
        fields_set_aside=replace(
            fields,
            findings=tuple(
                _hydrate_finding(finding, cohorts, originals)
                for finding in fields.findings
            ),
        ),
        original_source_requests=(),
    )


def _authorized_cohorts(
    requests: tuple[OriginalSourceRequest, ...],
) -> dict[OriginalSourceOccurrence, tuple[_PhysicalCohort, ...]]:
    """Validate each complete cross-snapshot cohort once before source reads.

    A cohort tuple is shared by all its target cells. Hash it once per tuple
    object and retain exact equality checks for distinct tuples or collisions.
    This keeps grouped-row authorization linear in the supplied evidence.
    """

    hashes: dict[int, int] = {}
    groups: dict[
        tuple[OriginalSourceOccurrence, int],
        list[tuple[_PhysicalCohort, set[OriginalSourceCoordinate]]],
    ] = {}
    for request in requests:
        for target in request.targets:
            coordinates = target.input_coordinates
            identity = id(coordinates)
            if identity not in hashes:
                hashes[identity] = hash(coordinates)
            candidates = groups.setdefault(
                (target.occurrence, hashes[identity]), []
            )
            for cohort, authorized in candidates:
                if cohort is coordinates or cohort == coordinates:
                    authorized.add(target.coordinate)
                    break
            else:
                candidates.append((coordinates, {target.coordinate}))
    result: dict[OriginalSourceOccurrence, list[_PhysicalCohort]] = {}
    for (occurrence, _digest), candidates in groups.items():
        for cohort, authorized in candidates:
            if set(cohort) != authorized:
                raise ReadinessError(_RECOVERY)
            result.setdefault(occurrence, []).append(cohort)
    return {occurrence: tuple(cohorts) for occurrence, cohorts in result.items()}


def _hydrate_finding(
    finding: FieldSetAside,
    cohorts: dict[OriginalSourceOccurrence, tuple[_PhysicalCohort, ...]],
    originals: dict[OriginalSourceCoordinate, OriginalSourceValue],
) -> FieldSetAside:
    if (
        finding.source_row is None
        or not finding.target_field
        or not finding.source_inputs
    ):
        return finding
    coordinates = tuple(item.coordinate for item in finding.source_inputs)
    occurrence = OriginalSourceOccurrence(
        finding.dataset,
        finding.source_row,
        finding.target_field,
        finding.source_keys,
        finding.source_trace_id,
    )
    if not any(
        coordinates == allowed for allowed in cohorts.get(occurrence, ())
    ) or any(coordinate not in originals for coordinate in coordinates):
        return finding
    return replace(
        finding,
        source_inputs=tuple(
            replace(item, value=originals[item.coordinate])
            for item in finding.source_inputs
        ),
    )


def _group_requests(
    requests: tuple[OriginalSourceRequest, ...],
) -> tuple[tuple[SourceSnapshot, set[OriginalSourceCoordinate]], ...]:
    groups: dict[str, tuple[SourceSnapshot, set[OriginalSourceCoordinate]]] = {}
    dataset_hashes: dict[str, str] = {}
    artifact_hashes: dict[tuple[str, str], str] = {}
    for request in requests:
        snapshot = request.snapshot
        identity = snapshot.content_hash
        artifact = (snapshot.data_version_id, snapshot.parquet_storage_key)
        if (
            dataset_hashes.setdefault(snapshot.dataset_id, identity) != identity
            or artifact_hashes.setdefault(artifact, identity) != identity
        ):
            raise ReadinessError(_RECOVERY)
        group = groups.setdefault(identity, (snapshot, set()))
        group[1].update(request.cells)
    return tuple(groups.values())


def _read_requested_cells(
    path: Path,
    snapshot: SourceSnapshot,
    requested: set[OriginalSourceCoordinate],
) -> dict[OriginalSourceCoordinate, OriginalSourceValue]:
    requested_by_row: dict[int, set[str]] = {}
    for coordinate in requested:
        requested_by_row.setdefault(coordinate.source_row, set()).add(coordinate.source_key)
    keys = {coordinate.source_key for coordinate in requested}
    columns = tuple(
        column for column in snapshot.schema.columns if column.stable_key in keys
    )
    positions = {
        column.stable_key: (index * 2 + 1, index * 2 + 2)
        for index, column in enumerate(columns)
    }
    query = (
        pl.scan_parquet(path, glob=False, low_memory=True, rechunk=False)
        .filter(pl.col(SOURCE_ROW_COLUMN).is_in(sorted(requested_by_row)))
        .select(
            SOURCE_ROW_COLUMN,
            *(
                name
                for column in columns
                for name in (column.value_column, column.kind_column)
            ),
        )
    )
    seen: set[int] = set()
    originals: dict[OriginalSourceCoordinate, OriginalSourceValue] = {}
    for batch in query.collect_batches(
        chunk_size=_ORIGINAL_BATCH_ROWS, maintain_order=True, engine="streaming"
    ):
        for values in batch.iter_rows():
            row = values[0]
            if type(row) is not int or row not in requested_by_row or row in seen:
                raise SourceSnapshotContractError(
                    "Requested source rows are not distinct"
                )
            seen.add(row)
            for key in sorted(requested_by_row[row]):
                value_index, kind_index = positions[key]
                cell = EncodedSourceCell.from_portable_dict(
                    {"kind": values[kind_index], "text": values[value_index]}
                )
                coordinate = OriginalSourceCoordinate(
                    snapshot.dataset_id, snapshot.dataset_name, row, key
                )
                originals[coordinate] = OriginalSourceValue.from_cell(cell)
    if seen != requested_by_row.keys():
        raise SourceSnapshotContractError("Requested source rows are missing")
    return originals
