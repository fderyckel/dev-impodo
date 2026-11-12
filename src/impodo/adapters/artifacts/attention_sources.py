"""Read only affected original file rows from verified immutable snapshots."""

from collections import defaultdict

import polars as pl

from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.domain.preflight.attention import AttentionExport
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.source_snapshot import SOURCE_ROW_COLUMN


def read_attention_sources(
    export: AttentionExport,
    artifacts: DataVersionSourceArtifactStore,
) -> dict[tuple[str, int], dict[str, str | None]]:
    """Project each affected physical source once without reopening databases."""

    if export.restricted:
        return {}
    requested: dict[str, set[int]] = defaultdict(set)
    needed_keys = {
        key
        for record in export.records
        for keys in record.field_sources.values()
        for key in keys
    }
    needed_keys.update(
        field
        for finding in export.findings
        for field in finding.fields
        if field in export.source_columns
    )
    for record in export.records:
        for dataset, rows in record.locations.items():
            requested[dataset].update(rows)
    result = {}
    for snapshot in export.snapshots:
        rows = requested.get(snapshot.dataset_id)
        if not rows or snapshot.source.origin is not SourceOriginKind.FILE:
            continue
        columns = tuple(
            column
            for column in snapshot.schema.columns
            if column.stable_key in needed_keys
        )
        if not columns:
            continue
        with artifacts.materialize_source_snapshot(
            snapshot.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            frame = (
                pl.scan_parquet(path)
                .filter(pl.col(SOURCE_ROW_COLUMN).is_in(sorted(rows)))
                .select(
                    [SOURCE_ROW_COLUMN, *(column.value_column for column in columns)]
                )
                .collect(engine="streaming")
            )
        if frame.height != len(rows):
            raise ValueError(
                "Some affected source rows could not be verified. Prepare the data again."
            )
        for values in frame.iter_rows():
            result[(snapshot.dataset_id, int(values[0]))] = {
                column.stable_key: value
                for column, value in zip(columns, values[1:], strict=True)
            }
    return result
