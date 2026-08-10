"""Publish and read immutable Parquet projections of governed source tables.

The existing strict CSV/XLSX reader remains authoritative.  This adapter
encodes its accepted Python scalars into the tagged physical schema defined by
``domain.source_snapshot``, writes bounded Parquet fragments, compacts them
with Polars streaming execution, validates the completed artifact, and only
then asks the artifact store to atomically publish it.

Preparation routes supported direct datasets to the native columnar adapter.
Preview and unsupported mappings retain the bounded ``SourceRow`` compatibility
adapter as the Python semantic oracle.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Iterator

from .columnar_runtime import configure_columnar_runtime


configure_columnar_runtime()

import polars as pl

from .artifacts import ArtifactStore
from .domain.source_snapshot import (
    EncodedSourceCell,
    SOURCE_KIND_PHYSICAL_TYPE,
    SOURCE_ROW_COLUMN,
    SOURCE_VALUE_PHYSICAL_TYPE,
    SourceCellKind,
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotContractError,
    SourceSnapshotSchema,
)
from .inspection import SourceFileCatalog, SourceTableCatalog
from .projects import MigrationProject, SourceFile
from .source import (
    SelectedSourceBatchStream,
    SourceLoadError,
    SourceRow,
    SourceTable,
    open_selected_source_batches,
)
from .workspace_contracts import SourceDataset, SourceSelection


SOURCE_SNAPSHOT_TARGET_BATCH_ROWS = 5_000
SOURCE_SNAPSHOT_MAX_BATCH_CELLS = 100_000
SOURCE_SNAPSHOT_PARQUET_COMPRESSION = "zstd"
SOURCE_SNAPSHOT_MAX_COMPACTION_INPUTS = 128
SOURCE_SNAPSHOT_COMPATIBILITY_BATCH_ROWS = 1_000


@dataclass(frozen=True, slots=True)
class SourceSnapshotPublication:
    """One published manifest plus bounded-ingestion accounting."""

    snapshot: SourceSnapshot
    input_batch_rows: int
    fragment_count: int


class SourceSnapshotPublisher:
    """Convert one exact frozen physical dataset through the strict reader."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def publish(
        self,
        project: MigrationProject,
        selection: SourceSelection,
        dataset: SourceDataset,
        catalog: SourceFileCatalog,
        source_file: SourceFile,
    ) -> SourceSnapshotPublication:
        """Write, validate, and atomically publish one immutable snapshot."""

        _validate_snapshot_bindings(project, selection, dataset, catalog, source_file)
        table = _selected_table(catalog, dataset)
        schema = source_snapshot_schema(dataset)
        batch_rows = source_snapshot_batch_rows(len(schema.columns))
        expected_headers = tuple(item.source_name for item in dataset.columns)
        expected_source_hash = _canonical_hash(dataset.source_sha256)
        named_range = (
            table.named_tables[0].cell_range
            if table.kind == "NAMED_TABLE" and table.named_tables
            else None
        )

        with self.artifacts.materialize_source(
            project.project_id,
            source_file.stored_name,
        ) as source_path:
            with open_selected_source_batches(
                source_path,
                dataset=dataset.name,
                table_key=dataset.table_key,
                encoding=dataset.encoding,
                delimiter=dataset.delimiter,
                header_row=dataset.header_row,
                named_table_range=named_range,
                source_display_name=source_file.display_name,
                batch_size=batch_rows,
            ) as source:
                if source.content_hash != expected_source_hash:
                    raise SourceLoadError(
                        "Stored source content changed after dataset freezing"
                    )
                if source.headers != expected_headers:
                    raise SourceLoadError(
                        "Stored source columns changed after dataset freezing"
                    )
                with self.artifacts.prepare_source_snapshot(
                    project.project_id
                ) as workspace:
                    candidate = workspace / "snapshot.parquet"
                    source_data_hash, row_count, fragment_count = (
                        _write_snapshot_candidate(
                            source,
                            schema,
                            workspace,
                            candidate,
                        )
                    )
                    if row_count != dataset.row_count:
                        raise SourceLoadError(
                            "Stored source row count changed after dataset freezing"
                        )
                    output_data_hash = _validate_snapshot_candidate(
                        candidate,
                        schema,
                        expected_row_count=row_count,
                        batch_rows=batch_rows,
                    )
                    if output_data_hash != source_data_hash:
                        raise SourceLoadError(
                            "Parquet snapshot changed source row semantics"
                        )
                    parquet_hash = _file_hash(candidate)
                    snapshot = SourceSnapshot.create(
                        project_id=project.project_id,
                        dataset_id=dataset.dataset_id,
                        dataset_name=dataset.name,
                        file_id=dataset.file_id,
                        table_key=dataset.table_key,
                        source_sha256=expected_source_hash,
                        catalog_hash=dataset.catalog_hash,
                        physical_selection_hash=selection.content_hash,
                        schema=schema,
                        row_count=row_count,
                        parquet_sha256=parquet_hash,
                        created_at=selection.created_at,
                    )
                    self.artifacts.publish_source_snapshot(
                        project.project_id,
                        candidate,
                        snapshot.parquet_storage_key,
                        expected_sha256=parquet_hash,
                    )
        return SourceSnapshotPublication(
            snapshot=snapshot,
            input_batch_rows=batch_rows,
            fragment_count=fragment_count,
        )


def source_snapshot_schema(dataset: SourceDataset) -> SourceSnapshotSchema:
    """Derive the exact safe physical schema from stable dataset columns."""

    return SourceSnapshotSchema.create(
        SourceSnapshotColumn.create(
            ordinal=column.ordinal,
            stable_key=column.stable_key,
            source_name=column.source_name,
            candidate_type=column.candidate_type,
        )
        for column in dataset.columns
    )


def source_snapshot_batch_rows(column_count: int) -> int:
    """Cap each ingestion batch by rows and cells to bound wide-file memory."""

    if column_count < 1:
        raise SourceSnapshotContractError(
            "A source snapshot requires at least one source column"
        )
    return max(
        1,
        min(
            SOURCE_SNAPSHOT_TARGET_BATCH_ROWS,
            SOURCE_SNAPSHOT_MAX_BATCH_CELLS // column_count,
        ),
    )


@contextmanager
def open_source_snapshot_batches(
    path: str | Path,
    snapshot: SourceSnapshot,
    *,
    batch_size: int = SOURCE_SNAPSHOT_COMPATIBILITY_BATCH_ROWS,
) -> Iterator[SelectedSourceBatchStream]:
    """Expose one verified Parquet snapshot through the bounded row adapter."""

    if batch_size < 1:
        raise ValueError("Source batch size must be positive")
    snapshot_path = Path(path).resolve()
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise SourceLoadError("stored source snapshot is unavailable")
    _validate_physical_schema(snapshot_path, snapshot.schema)
    rows = _snapshot_rows(
        snapshot_path,
        snapshot,
        batch_size=min(batch_size, SOURCE_SNAPSHOT_COMPATIBILITY_BATCH_ROWS),
    )
    yield SelectedSourceBatchStream(
        dataset=snapshot.dataset_name,
        path=snapshot_path,
        headers=tuple(item.source_name for item in snapshot.schema.columns),
        content_hash=snapshot.source_sha256,
        batch_size=batch_size,
        _rows=rows,
    )


def load_source_snapshot_table(
    path: str | Path,
    snapshot: SourceSnapshot,
) -> SourceTable:
    """Materialize a snapshot only for the existing bounded legacy evaluator."""

    with open_source_snapshot_batches(path, snapshot) as source:
        rows = tuple(row for batch in source.iter_batches() for row in batch)
        return SourceTable(
            dataset=source.dataset,
            path=source.path,
            headers=source.headers,
            rows=rows,
            content_hash=source.content_hash,
        )


def validate_snapshot_for_dataset(
    selection: SourceSelection,
    dataset: SourceDataset,
    snapshot: SourceSnapshot,
) -> None:
    """Reject a manifest that is not the current exact physical dataset."""

    if (
        snapshot.project_id != selection.project_id
        or snapshot.dataset_id != dataset.dataset_id
        or snapshot.dataset_name != dataset.name
        or snapshot.file_id != dataset.file_id
        or snapshot.table_key != dataset.table_key
        or snapshot.source_sha256 != _canonical_hash(dataset.source_sha256)
        or snapshot.catalog_hash != dataset.catalog_hash
        or snapshot.physical_selection_hash != selection.content_hash
        or snapshot.row_count != dataset.row_count
        or snapshot.schema != source_snapshot_schema(dataset)
    ):
        raise SourceLoadError(
            "Current source snapshot does not match the frozen dataset"
        )


def validate_source_snapshot_path(
    path: str | Path,
    snapshot: SourceSnapshot,
) -> Path:
    """Validate one materialized snapshot's contained physical schema."""

    snapshot_path = Path(path).resolve()
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise SourceLoadError("stored source snapshot is unavailable")
    _validate_physical_schema(snapshot_path, snapshot.schema)
    return snapshot_path


def _write_snapshot_candidate(
    source: SelectedSourceBatchStream,
    schema: SourceSnapshotSchema,
    workspace: Path,
    candidate: Path,
) -> tuple[str, int, int]:
    parts = workspace / "parts"
    parts.mkdir()
    data_hasher = _SnapshotDataHasher()
    row_count = 0
    previous_row = 0
    part_paths: list[Path] = []
    for index, batch in enumerate(source.iter_batches()):
        physical = _physical_batch(batch, schema, data_hasher)
        for row in batch:
            if row.number <= previous_row:
                raise SourceLoadError("Source row order is not strictly increasing")
            previous_row = row.number
        row_count += len(batch)
        part = parts / f"part-{index:08d}.parquet"
        physical.write_parquet(
            part,
            compression=SOURCE_SNAPSHOT_PARQUET_COMPRESSION,
            statistics=True,
            row_group_size=len(batch),
        )
        part_paths.append(part)

    if not part_paths:
        pl.DataFrame(schema=_polars_schema(schema)).write_parquet(
            candidate,
            compression=SOURCE_SNAPSHOT_PARQUET_COMPRESSION,
            statistics=True,
        )
    else:
        _compact_parquet_parts(
            part_paths,
            candidate,
            workspace,
            row_group_size=source.batch_size,
        )
    return data_hasher.hexdigest(), row_count, len(part_paths)


def _compact_parquet_parts(
    paths: list[Path],
    candidate: Path,
    workspace: Path,
    *,
    row_group_size: int,
) -> None:
    """Merge fragments hierarchically so Parquet metadata memory stays bounded."""

    current = list(paths)
    generation = 0
    while len(current) > SOURCE_SNAPSHOT_MAX_COMPACTION_INPUTS:
        merged: list[Path] = []
        generation_directory = workspace / f"merge-{generation:04d}"
        generation_directory.mkdir()
        for group_index, start in enumerate(
            range(0, len(current), SOURCE_SNAPSHOT_MAX_COMPACTION_INPUTS)
        ):
            group = current[start : start + SOURCE_SNAPSHOT_MAX_COMPACTION_INPUTS]
            target = generation_directory / f"part-{group_index:08d}.parquet"
            _sink_parquet_group(group, target, row_group_size=row_group_size)
            merged.append(target)
        for path in current:
            path.unlink(missing_ok=True)
        current = merged
        generation += 1
    if len(current) == 1:
        current[0].replace(candidate)
    else:
        _sink_parquet_group(current, candidate, row_group_size=row_group_size)


def _sink_parquet_group(
    paths: list[Path],
    target: Path,
    *,
    row_group_size: int,
) -> None:
    pl.scan_parquet(
        paths,
        glob=False,
        low_memory=True,
        rechunk=False,
    ).sink_parquet(
        target,
        compression=SOURCE_SNAPSHOT_PARQUET_COMPRESSION,
        statistics=True,
        row_group_size=row_group_size,
        maintain_order=True,
        engine="streaming",
    )


def _physical_batch(
    batch: tuple[SourceRow, ...],
    schema: SourceSnapshotSchema,
    data_hasher: "_SnapshotDataHasher",
) -> pl.DataFrame:
    data: dict[str, list[object]] = {
        SOURCE_ROW_COLUMN: [row.number for row in batch]
    }
    for column in schema.columns:
        texts: list[str | None] = []
        kinds: list[int] = []
        for row in batch:
            encoded = EncodedSourceCell.from_python(
                row.values.get(column.source_name)
            )
            texts.append(encoded.text)
            kinds.append(int(encoded.kind))
        data[column.value_column] = texts
        data[column.kind_column] = kinds
    for row in batch:
        data_hasher.add(row, schema)
    return pl.DataFrame(data, schema=_polars_schema(schema), strict=True)


def _validate_snapshot_candidate(
    path: Path,
    schema: SourceSnapshotSchema,
    *,
    expected_row_count: int,
    batch_rows: int,
) -> str:
    _validate_physical_schema(path, schema)
    hasher = _SnapshotDataHasher()
    count = 0
    previous_row = 0
    for frame in pl.scan_parquet(
        path,
        glob=False,
        low_memory=True,
        rechunk=False,
    ).collect_batches(
        chunk_size=batch_rows,
        maintain_order=True,
        engine="streaming",
    ):
        for row in _frame_source_rows(frame, schema):
            if row.number <= previous_row:
                raise SourceLoadError("Parquet snapshot row order is invalid")
            previous_row = row.number
            hasher.add(row, schema)
            count += 1
    if count != expected_row_count:
        raise SourceLoadError("Parquet snapshot row count is invalid")
    return hasher.hexdigest()


def _snapshot_rows(
    path: Path,
    snapshot: SourceSnapshot,
    *,
    batch_size: int,
) -> Iterator[SourceRow]:
    count = 0
    previous_row = 0
    for frame in pl.scan_parquet(
        path,
        glob=False,
        low_memory=True,
        rechunk=False,
    ).collect_batches(
        chunk_size=batch_size,
        maintain_order=True,
        engine="streaming",
    ):
        for row in _frame_source_rows(frame, snapshot.schema):
            if row.number <= previous_row:
                raise SourceLoadError("Parquet snapshot row order is invalid")
            previous_row = row.number
            count += 1
            if count > snapshot.row_count:
                raise SourceLoadError("Parquet snapshot row count is invalid")
            yield row
    if count != snapshot.row_count:
        raise SourceLoadError("Parquet snapshot row count is invalid")


def _frame_source_rows(
    frame: pl.DataFrame,
    schema: SourceSnapshotSchema,
) -> Iterator[SourceRow]:
    for physical in frame.iter_rows(named=False):
        number = int(physical[0])
        values: dict[str, object] = {}
        offset = 1
        for column in schema.columns:
            text = physical[offset]
            kind = physical[offset + 1]
            try:
                encoded = EncodedSourceCell.from_portable_dict(
                    {"kind": int(kind), "text": text}
                )
            except (TypeError, ValueError, SourceSnapshotContractError) as error:
                raise SourceLoadError(
                    "Parquet snapshot contains an invalid source value"
                ) from error
            values[column.source_name] = encoded.to_python()
            offset += 2
        yield SourceRow(number=number, values=values)


def _polars_schema(schema: SourceSnapshotSchema) -> dict[str, pl.DataType]:
    physical: dict[str, pl.DataType] = {SOURCE_ROW_COLUMN: pl.Int64}
    for column in schema.columns:
        physical[column.value_column] = pl.String
        physical[column.kind_column] = pl.UInt8
    return physical


def _validate_physical_schema(path: Path, schema: SourceSnapshotSchema) -> None:
    physical = pl.read_parquet_schema(path)
    if dict(physical) != _polars_schema(schema):
        raise SourceLoadError("Parquet snapshot schema is invalid")
    if (
        SOURCE_VALUE_PHYSICAL_TYPE != "utf8"
        or SOURCE_KIND_PHYSICAL_TYPE != "uint8"
    ):
        raise SourceSnapshotContractError(
            "Unsupported source snapshot physical contract"
        )


class _SnapshotDataHasher:
    def __init__(self) -> None:
        self._digest = sha256(b"impodo-source-snapshot-data-v1\0")

    def add(self, row: SourceRow, schema: SourceSnapshotSchema) -> None:
        _hash_bytes(self._digest, str(row.number).encode("ascii"))
        for column in schema.columns:
            encoded = EncodedSourceCell.from_python(
                row.values.get(column.source_name)
            )
            self._digest.update(bytes((int(encoded.kind),)))
            if encoded.text is None:
                self._digest.update(b"\xff" * 8)
            else:
                _hash_bytes(self._digest, encoded.text.encode("utf-8"))

    def hexdigest(self) -> str:
        return f"sha256:{self._digest.hexdigest()}"


def _hash_bytes(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _validate_snapshot_bindings(
    project: MigrationProject,
    selection: SourceSelection,
    dataset: SourceDataset,
    catalog: SourceFileCatalog,
    source_file: SourceFile,
) -> None:
    if selection.project_id != project.project_id or dataset not in selection.datasets:
        raise SourceLoadError("Source snapshot belongs to another selection")
    if (
        source_file.file_id != dataset.file_id
        or catalog.file_id != dataset.file_id
        or _canonical_hash(source_file.sha256) != _canonical_hash(dataset.source_sha256)
        or _canonical_hash(catalog.source_sha256)
        != _canonical_hash(dataset.source_sha256)
        or catalog.content_hash != dataset.catalog_hash
    ):
        raise SourceLoadError("Frozen source evidence is incomplete")


def _selected_table(
    catalog: SourceFileCatalog,
    dataset: SourceDataset,
) -> SourceTableCatalog:
    try:
        return next(item for item in catalog.tables if item.table_key == dataset.table_key)
    except StopIteration as error:
        raise SourceLoadError("Frozen source table is unavailable") from error


def _canonical_hash(value: str) -> str:
    digest = value.removeprefix("sha256:").casefold()
    if len(digest) != 64:
        raise SourceLoadError("Source hash evidence is invalid")
    try:
        int(digest, 16)
    except ValueError as error:
        raise SourceLoadError("Source hash evidence is invalid") from error
    return f"sha256:{digest}"


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
