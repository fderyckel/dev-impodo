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
                    replace(finding, source_inputs=()) for finding in fields.findings
                ),
            ),
            original_source_requests=(),
        )
    if not evidence.original_source_requests:
        return evidence

    targets = {
        (
            request.dataset,
            target.source_row,
            target.source_key,
            target.target_field,
            target.input_keys,
        )
        for request in evidence.original_source_requests
        for target in request.targets
    }
    originals: dict[tuple[str, int, str], OriginalSourceValue] = {}
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
                _hydrate_finding(finding, targets, originals)
                for finding in fields.findings
            ),
        ),
        original_source_requests=(),
    )


def _hydrate_finding(
    finding: FieldSetAside,
    targets: set[tuple[str, int, str, str, tuple[str, ...]]],
    originals: dict[tuple[str, int, str], OriginalSourceValue],
) -> FieldSetAside:
    if finding.source_row is None or not finding.source_inputs:
        return finding
    cells = tuple(
        (finding.dataset, finding.source_row, item.source_key)
        for item in finding.source_inputs
    )
    if any(
        (*cell, finding.target_field, finding.source_keys) not in targets
        or cell not in originals
        for cell in cells
    ):
        return finding
    return replace(
        finding,
        source_inputs=tuple(
            replace(item, value=originals[cell])
            for item, cell in zip(finding.source_inputs, cells, strict=True)
        ),
    )


def _group_requests(
    requests: tuple[OriginalSourceRequest, ...],
) -> tuple[tuple[SourceSnapshot, set[tuple[int, str]]], ...]:
    groups: dict[str, tuple[SourceSnapshot, set[tuple[int, str]]]] = {}
    dataset_hashes: dict[str, str] = {}
    artifact_hashes: dict[tuple[str, str], str] = {}
    for request in requests:
        snapshot = request.snapshot
        identity = snapshot.content_hash
        artifact = (snapshot.data_version_id, snapshot.parquet_storage_key)
        if (
            dataset_hashes.setdefault(request.dataset, identity) != identity
            or artifact_hashes.setdefault(artifact, identity) != identity
        ):
            raise ReadinessError(_RECOVERY)
        group = groups.setdefault(identity, (snapshot, set()))
        group[1].update(request.cells)
    return tuple(groups.values())


def _read_requested_cells(
    path: Path,
    snapshot: SourceSnapshot,
    requested: set[tuple[int, str]],
) -> dict[tuple[str, int, str], OriginalSourceValue]:
    requested_by_row: dict[int, set[str]] = {}
    for row, key in requested:
        requested_by_row.setdefault(row, set()).add(key)
    keys = {key for _, key in requested}
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
    originals: dict[tuple[str, int, str], OriginalSourceValue] = {}
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
                originals[(snapshot.dataset_name, row, key)] = (
                    OriginalSourceValue.from_cell(cell)
                )
    if seen != requested_by_row.keys():
        raise SourceSnapshotContractError("Requested source rows are missing")
    return originals
