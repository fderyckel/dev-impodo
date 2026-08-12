"""Publish and read immutable Parquet projections of governed source tables.

The existing strict CSV/XLSX reader remains authoritative.  This adapter
encodes its accepted Python scalars into the tagged physical schema defined by
``domain.source_snapshot``, writes bounded Parquet fragments, compacts them
with Polars streaming execution, validates the completed artifact, and only
then asks the artifact store to atomically publish it.

Preparation routes supported direct datasets to the native columnar adapter.
Preview and mappings outside the columnar capability set use the bounded
``SourceRow`` adapter as the Python semantic oracle.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import shutil
from typing import Iterator, Mapping, Sequence

from .columnar_runtime import configure_columnar_runtime


configure_columnar_runtime()

import polars as pl

from .artifacts import ArtifactStore
from .domain.source_snapshot import (
    EncodedSourceCell,
    SOURCE_KIND_PHYSICAL_TYPE,
    SOURCE_ROW_COLUMN,
    SOURCE_VALUE_PHYSICAL_TYPE,
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotContractError,
    SourceSnapshotSchema,
)
from .domain.source_binding import require_file_source
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


@dataclass(frozen=True, slots=True)
class SourceSnapshotCandidate:
    """Verified immutable Parquet candidate before artifact publication."""

    path: Path
    data_logical_hash: str
    parquet_sha256: str
    size_bytes: int
    row_count: int
    fragment_count: int


class SourceSnapshotCandidateWriter:
    """Append bounded rows/pages and encode every accepted cell exactly once."""

    def __init__(
        self,
        workspace: Path,
        schema: SourceSnapshotSchema,
        *,
        batch_rows: int,
        maximum_snapshot_bytes: int | None = None,
        maximum_temporary_bytes: int | None = None,
    ) -> None:
        if batch_rows < 1:
            raise SourceLoadError("Source snapshot batch size must be positive")
        if maximum_snapshot_bytes is not None and maximum_snapshot_bytes < 1:
            raise SourceLoadError("Source snapshot byte limit must be positive")
        if maximum_temporary_bytes is not None and maximum_temporary_bytes < 1:
            raise SourceLoadError("Source snapshot temporary limit must be positive")
        self.workspace = workspace
        self.schema = schema
        self.batch_rows = batch_rows
        self.maximum_snapshot_bytes = maximum_snapshot_bytes
        self.maximum_temporary_bytes = maximum_temporary_bytes
        self.candidate = workspace / "snapshot.parquet"
        self.parts = workspace / "parts"
        self.parts.mkdir()
        self._hasher = _SnapshotDataHasher()
        self._row_count = 0
        self._fragment_count = 0
        self._previous_row = 0
        self._temporary_bytes = 0
        self._finalized = False

    def append_source_rows(self, batch: tuple[SourceRow, ...]) -> None:
        """Append one strict-reader batch through the tagged columnar schema."""

        if not batch:
            return
        self._append(
            row_numbers=tuple(row.number for row in batch),
            values_by_name={
                column.source_name: tuple(
                    row.values.get(column.source_name) for row in batch
                )
                for column in self.schema.columns
            },
        )

    def append_columnar_page(
        self,
        *,
        first_row_ordinal: int,
        values_by_name: Mapping[str, Sequence[object]],
    ) -> None:
        """Append one already bounded typed page without constructing row dicts."""

        expected_names = {column.source_name for column in self.schema.columns}
        if set(values_by_name) != expected_names:
            raise SourceLoadError("Source snapshot page projection is invalid")
        lengths = {len(values) for values in values_by_name.values()}
        if len(lengths) != 1:
            raise SourceLoadError("Source snapshot page columns have different lengths")
        count = next(iter(lengths), 0)
        if not count or count > self.batch_rows:
            raise SourceLoadError("Source snapshot page size is invalid")
        if first_row_ordinal != self._previous_row + 1:
            raise SourceLoadError("Source snapshot page ordinals are not contiguous")
        self._append(
            row_numbers=tuple(range(first_row_ordinal, first_row_ordinal + count)),
            values_by_name=values_by_name,
        )

    def finalize(self) -> SourceSnapshotCandidate:
        """Compact, validate, and hash one complete candidate once."""

        if self._finalized:
            raise SourceLoadError("Source snapshot candidate is already finalized")
        self._finalized = True
        paths = [
            self.parts / f"part-{index:08d}.parquet"
            for index in range(self._fragment_count)
        ]
        if not paths:
            pl.DataFrame(schema=_polars_schema(self.schema)).write_parquet(
                self.candidate,
                compression=SOURCE_SNAPSHOT_PARQUET_COMPRESSION,
                statistics=True,
            )
        else:
            _compact_parquet_parts(
                paths,
                self.candidate,
                self.workspace,
                row_group_size=self.batch_rows,
            )
        size_bytes = self.candidate.stat().st_size
        if (
            self.maximum_snapshot_bytes is not None
            and size_bytes > self.maximum_snapshot_bytes
        ):
            raise SourceLoadError("Source snapshot exceeds its artifact byte limit")
        if (
            self.maximum_temporary_bytes is not None
            and _workspace_regular_file_bytes(self.workspace)
            > self.maximum_temporary_bytes
        ):
            raise SourceLoadError("Source snapshot exceeds its temporary byte limit")
        shutil.rmtree(self.parts, ignore_errors=True)
        data_logical_hash = self._hasher.hexdigest()
        _validate_snapshot_candidate(
            self.candidate,
            self.schema,
            expected_row_count=self._row_count,
            batch_rows=self.batch_rows,
        )
        return SourceSnapshotCandidate(
            path=self.candidate,
            data_logical_hash=data_logical_hash,
            parquet_sha256=_file_hash(self.candidate),
            size_bytes=size_bytes,
            row_count=self._row_count,
            fragment_count=self._fragment_count,
        )

    def _append(
        self,
        *,
        row_numbers: tuple[int, ...],
        values_by_name: Mapping[str, Sequence[object]],
    ) -> None:
        if self._finalized:
            raise SourceLoadError("Source snapshot candidate is already finalized")
        if (
            not row_numbers
            or row_numbers[0] <= self._previous_row
            or any(
                current <= previous
                for previous, current in zip(row_numbers, row_numbers[1:])
            )
        ):
            raise SourceLoadError("Source row order is not strictly increasing")
        physical = _physical_columnar_batch(
            row_numbers,
            values_by_name,
            self.schema,
            self._hasher,
        )
        part = self.parts / f"part-{self._fragment_count:08d}.parquet"
        physical.write_parquet(
            part,
            compression=SOURCE_SNAPSHOT_PARQUET_COMPRESSION,
            statistics=True,
            row_group_size=len(row_numbers),
        )
        self._temporary_bytes += part.stat().st_size
        if (
            self.maximum_temporary_bytes is not None
            and self._temporary_bytes > self.maximum_temporary_bytes
        ):
            raise SourceLoadError("Source snapshot exceeds its temporary byte limit")
        self._fragment_count += 1
        self._row_count += len(row_numbers)
        self._previous_row = row_numbers[-1]


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

        binding = require_file_source(dataset.source)
        _validate_snapshot_bindings(project, selection, dataset, catalog, source_file)
        table = _selected_table(catalog, dataset)
        schema = source_snapshot_schema(dataset)
        batch_rows = source_snapshot_batch_rows(len(schema.columns))
        expected_headers = tuple(item.source_name for item in dataset.columns)
        expected_source_hash = _canonical_hash(binding.source_sha256)
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
                table_key=binding.table_key,
                encoding=binding.encoding,
                delimiter=binding.delimiter,
                header_row=binding.header_row,
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
                    publication = _write_snapshot_candidate(
                        source,
                        schema,
                        workspace,
                        candidate,
                    )
                    if publication.row_count != dataset.row_count:
                        raise SourceLoadError(
                            "Stored source row count changed after dataset freezing"
                        )
                    snapshot = SourceSnapshot.create(
                        project_id=project.project_id,
                        dataset_id=dataset.dataset_id,
                        dataset_name=dataset.name,
                        source=dataset.source,
                        physical_selection_hash=selection.content_hash,
                        schema=schema,
                        row_count=publication.row_count,
                        data_logical_hash=publication.data_logical_hash,
                        parquet_sha256=publication.parquet_sha256,
                        created_at=selection.created_at,
                    )
                    self.artifacts.publish_source_snapshot(
                        project.project_id,
                        candidate,
                        snapshot.parquet_storage_key,
                        expected_sha256=publication.parquet_sha256,
                    )
        return SourceSnapshotPublication(
            snapshot=snapshot,
            input_batch_rows=batch_rows,
            fragment_count=publication.fragment_count,
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
        content_hash=snapshot.source.source_evidence_hash,
        batch_size=batch_size,
        _rows=rows,
    )


def load_source_snapshot_table(
    path: str | Path,
    snapshot: SourceSnapshot,
) -> SourceTable:
    """Materialize a snapshot for the current bounded Python evaluator."""

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
        or snapshot.source != dataset.source
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
) -> SourceSnapshotCandidate:
    writer = SourceSnapshotCandidateWriter(
        workspace,
        schema,
        batch_rows=source.batch_size,
    )
    for batch in source.iter_batches():
        writer.append_source_rows(batch)
    publication = writer.finalize()
    if publication.path != candidate:
        raise SourceLoadError("Source snapshot candidate path is invalid")
    return publication


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


def _physical_columnar_batch(
    row_numbers: tuple[int, ...],
    values_by_name: Mapping[str, Sequence[object]],
    schema: SourceSnapshotSchema,
    data_hasher: "_SnapshotDataHasher",
) -> pl.DataFrame:
    data: dict[str, list[object]] = {SOURCE_ROW_COLUMN: list(row_numbers)}
    encoded_by_column: list[tuple[list[str | None], list[int]]] = []
    for column in schema.columns:
        texts: list[str | None] = []
        kinds: list[int] = []
        raw_values = values_by_name[column.source_name]
        if len(raw_values) != len(row_numbers):
            raise SourceLoadError("Source snapshot page columns have different lengths")
        for raw_value in raw_values:
            encoded = EncodedSourceCell.from_python(raw_value)
            texts.append(encoded.text)
            kinds.append(int(encoded.kind))
        data[column.value_column] = texts
        data[column.kind_column] = kinds
        encoded_by_column.append((texts, kinds))
    for row_index, row_number in enumerate(row_numbers):
        data_hasher.add_encoded(
            row_number,
            tuple(
                (kinds[row_index], texts[row_index])
                for texts, kinds in encoded_by_column
            ),
        )
    return pl.DataFrame(data, schema=_polars_schema(schema), strict=True)


def _validate_snapshot_candidate(
    path: Path,
    schema: SourceSnapshotSchema,
    *,
    expected_row_count: int,
    batch_rows: int,
) -> None:
    _validate_physical_schema(path, schema)
    count = 0
    previous_row = 0
    for frame in (
        pl.scan_parquet(
            path,
            glob=False,
            low_memory=True,
            rechunk=False,
        )
        .select(
            SOURCE_ROW_COLUMN,
        )
        .collect_batches(
            chunk_size=batch_rows,
            maintain_order=True,
            engine="streaming",
        )
    ):
        for (raw_number,) in frame.iter_rows(named=False):
            number = int(raw_number)
            if number <= previous_row:
                raise SourceLoadError("Parquet snapshot row order is invalid")
            previous_row = number
            count += 1
    if count != expected_row_count:
        raise SourceLoadError("Parquet snapshot row count is invalid")


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
    if SOURCE_VALUE_PHYSICAL_TYPE != "utf8" or SOURCE_KIND_PHYSICAL_TYPE != "uint8":
        raise SourceSnapshotContractError(
            "Unsupported source snapshot physical contract"
        )


class _SnapshotDataHasher:
    def __init__(self) -> None:
        self._digest = sha256(b"impodo-source-snapshot-data-v1\0")

    def add_encoded(
        self,
        row_number: int,
        values: tuple[tuple[int, str | None], ...],
    ) -> None:
        _hash_bytes(self._digest, str(row_number).encode("ascii"))
        for kind, text in values:
            self._digest.update(bytes((kind,)))
            if text is None:
                self._digest.update(b"\xff" * 8)
            else:
                _hash_bytes(self._digest, text.encode("utf-8"))

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
    binding = require_file_source(dataset.source)
    if selection.project_id != project.project_id or dataset not in selection.datasets:
        raise SourceLoadError("Source snapshot belongs to another selection")
    if (
        source_file.file_id != binding.file_id
        or catalog.file_id != binding.file_id
        or _canonical_hash(source_file.sha256) != _canonical_hash(binding.source_sha256)
        or _canonical_hash(catalog.source_sha256)
        != _canonical_hash(binding.source_sha256)
        or catalog.content_hash != binding.catalog_hash
    ):
        raise SourceLoadError("Frozen source evidence is incomplete")


def _selected_table(
    catalog: SourceFileCatalog,
    dataset: SourceDataset,
) -> SourceTableCatalog:
    binding = require_file_source(dataset.source)
    try:
        return next(
            item for item in catalog.tables if item.table_key == binding.table_key
        )
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


def _workspace_regular_file_bytes(workspace: Path) -> int:
    """Measure bounded temporary storage using metadata, never content hashing."""

    return sum(
        path.stat().st_size
        for path in workspace.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
