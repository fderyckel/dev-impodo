"""Write and publish immutable derived/grouped value artifacts in bounded pages.

This adapter is deliberately independent of preparation admission.  A future
set-based derived executor may feed ordered value pages into this writer, but
the current materialized route is not redirected until its lineage and logical
projector are integrated with the pending preparation session.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import shutil

from impodo.application.shared.columnar_runtime import configure_columnar_runtime


configure_columnar_runtime()

import polars as pl

from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.domain.derived_value_artifact import (
    DERIVED_VALUE_ORDINAL_COLUMN,
    DerivedValueArtifact,
    DerivedValueInput,
    DerivedValueKind,
)
from impodo.domain.serialization import content_hash


DERIVED_VALUE_TARGET_BATCH_ROWS = 5_000
DERIVED_VALUE_MAX_COMPACTION_INPUTS = 128
DERIVED_VALUE_PARQUET_COMPRESSION = "zstd"


class DerivedValueArtifactWriteError(RuntimeError):
    """Raised when bounded derived-value creation or validation fails."""


@dataclass(frozen=True, slots=True)
class DerivedValuePage:
    """One ordered, bounded page supplied by a future set-based executor."""

    first_ordinal: int
    values_by_name: Mapping[str, Sequence[object]]


@dataclass(frozen=True, slots=True)
class DerivedValueArtifactCandidate:
    """Validated physical evidence awaiting immutable store publication."""

    path: Path
    row_count: int
    physical_schema_hash: str
    parquet_sha256: str
    size_bytes: int
    fragment_count: int


@dataclass(frozen=True, slots=True)
class DerivedValueArtifactPublication:
    """One verified manifest plus bounded-writer accounting."""

    artifact: DerivedValueArtifact
    input_batch_rows: int
    fragment_count: int
    size_bytes: int


class DerivedValueArtifactCandidateWriter:
    """Append typed value pages without retaining the complete output."""

    def __init__(
        self,
        workspace: Path,
        value_schema: Mapping[str, pl.DataType],
        *,
        batch_rows: int = DERIVED_VALUE_TARGET_BATCH_ROWS,
        maximum_artifact_bytes: int | None = None,
        maximum_temporary_bytes: int | None = None,
    ) -> None:
        if batch_rows < 1:
            raise DerivedValueArtifactWriteError(
                "Derived-value batch size must be positive"
            )
        if maximum_artifact_bytes is not None and maximum_artifact_bytes < 1:
            raise DerivedValueArtifactWriteError(
                "Derived-value artifact byte limit must be positive"
            )
        if maximum_temporary_bytes is not None and maximum_temporary_bytes < 1:
            raise DerivedValueArtifactWriteError(
                "Derived-value temporary byte limit must be positive"
            )
        candidate_workspace = Path(workspace)
        if candidate_workspace.is_symlink() or not candidate_workspace.is_dir():
            raise DerivedValueArtifactWriteError(
                "Derived-value workspace is invalid"
            )
        schema_items = tuple(value_schema.items())
        if not schema_items:
            raise DerivedValueArtifactWriteError(
                "Derived-value artifacts require at least one value column"
            )
        for name, _data_type in schema_items:
            _validate_value_column_name(name)
        self.workspace = candidate_workspace.resolve()
        self.value_schema = dict(schema_items)
        self.physical_schema = {
            DERIVED_VALUE_ORDINAL_COLUMN: pl.Int64,
            **self.value_schema,
        }
        self.batch_rows = batch_rows
        self.maximum_artifact_bytes = maximum_artifact_bytes
        self.maximum_temporary_bytes = maximum_temporary_bytes
        self.candidate = self.workspace / "derived-values.parquet"
        self.parts = self.workspace / "parts"
        self.parts.mkdir()
        self._row_count = 0
        self._fragment_count = 0
        self._temporary_bytes = 0
        self._finalized = False
        self._failed = False

    def append_columnar_page(self, page: DerivedValuePage) -> None:
        """Encode one exact page and assign contiguous artifact ordinals."""

        self._require_open()
        if page.first_ordinal != self._row_count:
            raise DerivedValueArtifactWriteError(
                "Derived-value page ordinals are not contiguous"
            )
        if set(page.values_by_name) != set(self.value_schema):
            raise DerivedValueArtifactWriteError(
                "Derived-value page projection is invalid"
            )
        lengths = {len(page.values_by_name[name]) for name in self.value_schema}
        if len(lengths) != 1:
            raise DerivedValueArtifactWriteError(
                "Derived-value page columns have different lengths"
            )
        count = next(iter(lengths), 0)
        if not count or count > self.batch_rows:
            raise DerivedValueArtifactWriteError(
                "Derived-value page size is invalid"
            )
        data: dict[str, Sequence[object] | range] = {
            DERIVED_VALUE_ORDINAL_COLUMN: range(
                page.first_ordinal,
                page.first_ordinal + count,
            )
        }
        data.update(
            (name, page.values_by_name[name]) for name in self.value_schema
        )
        part = self.parts / f"part-{self._fragment_count:08d}.parquet"
        try:
            frame = pl.DataFrame(data, schema=self.physical_schema, strict=True)
            frame.write_parquet(
                part,
                compression=DERIVED_VALUE_PARQUET_COMPRESSION,
                statistics=True,
                row_group_size=count,
            )
        except (OSError, TypeError, ValueError, pl.exceptions.PolarsError) as error:
            self._failed = True
            raise DerivedValueArtifactWriteError(
                "Derived-value page could not be written"
            ) from error
        self._temporary_bytes += part.stat().st_size
        if (
            self.maximum_temporary_bytes is not None
            and self._temporary_bytes > self.maximum_temporary_bytes
        ):
            self._failed = True
            raise DerivedValueArtifactWriteError(
                "Derived-value output exceeds its temporary byte limit"
            )
        self._fragment_count += 1
        self._row_count += count

    def finalize(self) -> DerivedValueArtifactCandidate:
        """Compact, validate, and hash one completed candidate exactly once."""

        self._require_open()
        self._finalized = True
        paths = [
            self.parts / f"part-{index:08d}.parquet"
            for index in range(self._fragment_count)
        ]
        try:
            if not paths:
                pl.DataFrame(schema=self.physical_schema).write_parquet(
                    self.candidate,
                    compression=DERIVED_VALUE_PARQUET_COMPRESSION,
                    statistics=True,
                )
            else:
                _compact_parquet_parts(
                    paths,
                    self.candidate,
                    self.workspace,
                    row_group_size=self.batch_rows,
                    maximum_temporary_bytes=self.maximum_temporary_bytes,
                )
            size_bytes = self.candidate.stat().st_size
            if (
                self.maximum_artifact_bytes is not None
                and size_bytes > self.maximum_artifact_bytes
            ):
                raise DerivedValueArtifactWriteError(
                    "Derived-value output exceeds its artifact byte limit"
                )
            if (
                self.maximum_temporary_bytes is not None
                and _workspace_regular_file_bytes(self.workspace)
                > self.maximum_temporary_bytes
            ):
                raise DerivedValueArtifactWriteError(
                    "Derived-value output exceeds its temporary byte limit"
                )
            shutil.rmtree(self.parts, ignore_errors=True)
            physical_schema_hash = _physical_schema_hash(
                self.candidate,
                expected_schema=self.physical_schema,
            )
            _validate_ordinal_sequence(
                self.candidate,
                expected_row_count=self._row_count,
                batch_rows=self.batch_rows,
            )
            return DerivedValueArtifactCandidate(
                path=self.candidate,
                row_count=self._row_count,
                physical_schema_hash=physical_schema_hash,
                parquet_sha256=_file_hash(self.candidate),
                size_bytes=size_bytes,
                fragment_count=self._fragment_count,
            )
        except DerivedValueArtifactWriteError:
            self._failed = True
            raise
        except (OSError, pl.exceptions.PolarsError) as error:
            self._failed = True
            raise DerivedValueArtifactWriteError(
                "Derived-value artifact could not be finalized"
            ) from error

    def _require_open(self) -> None:
        if self._failed:
            raise DerivedValueArtifactWriteError(
                "Derived-value candidate writer has failed"
            )
        if self._finalized:
            raise DerivedValueArtifactWriteError(
                "Derived-value candidate is already finalized"
            )


class DerivedValueArtifactPublisher:
    """Run bounded writing, immutable publication, and verified read-back."""

    def __init__(
        self,
        artifacts: WorkspaceArtifactStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self,
        *,
        workspace_id: str,
        dataset_id: str,
        dataset_name: str,
        derivation_kind: DerivedValueKind,
        input_evidence: Iterable[DerivedValueInput],
        physical_selection_hash: str,
        source_selection_hash: str,
        derived_plan_hash: str,
        derivation_rule_hash: str,
        mapping_hash: str,
        schema_hash: str,
        transformation_program_hash: str,
        lineage_hash: str,
        value_schema: Mapping[str, pl.DataType],
        pages: Iterable[DerivedValuePage],
        batch_rows: int = DERIVED_VALUE_TARGET_BATCH_ROWS,
        maximum_artifact_bytes: int | None = None,
        maximum_temporary_bytes: int | None = None,
    ) -> DerivedValueArtifactPublication:
        """Publish one artifact while retaining only one supplied page at a time."""

        with self.artifacts.prepare_derived_value_artifact(workspace_id) as workspace:
            writer = DerivedValueArtifactCandidateWriter(
                workspace,
                value_schema,
                batch_rows=batch_rows,
                maximum_artifact_bytes=maximum_artifact_bytes,
                maximum_temporary_bytes=maximum_temporary_bytes,
            )
            for page in pages:
                writer.append_columnar_page(page)
            candidate = writer.finalize()
            artifact = DerivedValueArtifact.create(
                workspace_id=workspace_id,
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                derivation_kind=derivation_kind,
                input_evidence=input_evidence,
                physical_selection_hash=physical_selection_hash,
                source_selection_hash=source_selection_hash,
                derived_plan_hash=derived_plan_hash,
                derivation_rule_hash=derivation_rule_hash,
                mapping_hash=mapping_hash,
                schema_hash=schema_hash,
                transformation_program_hash=transformation_program_hash,
                lineage_hash=lineage_hash,
                row_count=candidate.row_count,
                physical_schema_hash=candidate.physical_schema_hash,
                parquet_sha256=candidate.parquet_sha256,
                created_at=self.clock(),
            )
            published_new = False
            try:
                published_new = self.artifacts.publish_derived_value_artifact(
                    workspace_id,
                    candidate.path,
                    artifact.parquet_storage_key,
                    expected_sha256=artifact.parquet_sha256,
                )
                with self.artifacts.materialize_derived_value_artifact(
                    workspace_id,
                    artifact.parquet_storage_key,
                    expected_sha256=artifact.parquet_sha256,
                ) as stored_path:
                    validate_derived_value_artifact(
                        stored_path,
                        artifact,
                        batch_rows=batch_rows,
                        verify_file_hash=False,
                    )
            except Exception:
                if published_new:
                    try:
                        self.artifacts.delete_derived_value_artifact(
                            workspace_id,
                            artifact.parquet_storage_key,
                        )
                    except Exception:
                        pass
                raise
        return DerivedValueArtifactPublication(
            artifact=artifact,
            input_batch_rows=batch_rows,
            fragment_count=candidate.fragment_count,
            size_bytes=candidate.size_bytes,
        )


def validate_derived_value_artifact(
    path: str | Path,
    artifact: DerivedValueArtifact,
    *,
    batch_rows: int = DERIVED_VALUE_TARGET_BATCH_ROWS,
    verify_file_hash: bool = True,
) -> None:
    """Verify schema, byte identity, row count, and exact ordinal sequence."""

    if batch_rows < 1:
        raise DerivedValueArtifactWriteError(
            "Derived-value validation batch size must be positive"
        )
    candidate = Path(path)
    if candidate.is_symlink():
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact must not be a symbolic link"
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact is unavailable"
        )
    if verify_file_hash and _file_hash(resolved) != artifact.parquet_sha256:
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact failed hash verification"
        )
    if _physical_schema_hash(resolved) != artifact.physical_schema_hash:
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact physical schema changed"
        )
    _validate_ordinal_sequence(
        resolved,
        expected_row_count=artifact.row_count,
        batch_rows=batch_rows,
    )


def _compact_parquet_parts(
    paths: list[Path],
    candidate: Path,
    workspace: Path,
    *,
    row_group_size: int,
    maximum_temporary_bytes: int | None,
) -> None:
    """Merge bounded fragments without a whole-output dataframe."""

    current = list(paths)
    generation = 0
    while len(current) > DERIVED_VALUE_MAX_COMPACTION_INPUTS:
        merged: list[Path] = []
        generation_directory = workspace / f"merge-{generation:04d}"
        generation_directory.mkdir()
        for group_index, start in enumerate(
            range(0, len(current), DERIVED_VALUE_MAX_COMPACTION_INPUTS)
        ):
            group = current[start : start + DERIVED_VALUE_MAX_COMPACTION_INPUTS]
            target = generation_directory / f"part-{group_index:08d}.parquet"
            _sink_parquet_group(group, target, row_group_size=row_group_size)
            _require_temporary_capacity(workspace, maximum_temporary_bytes)
            merged.append(target)
        for path in current:
            path.unlink(missing_ok=True)
        current = merged
        generation += 1
    if len(current) == 1:
        current[0].replace(candidate)
    else:
        _sink_parquet_group(current, candidate, row_group_size=row_group_size)
        _require_temporary_capacity(workspace, maximum_temporary_bytes)


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
        cache=False,
        parallel="none",
    ).sink_parquet(
        target,
        compression=DERIVED_VALUE_PARQUET_COMPRESSION,
        statistics=True,
        row_group_size=row_group_size,
        maintain_order=True,
        engine="streaming",
    )


def _physical_schema_hash(
    path: Path,
    *,
    expected_schema: Mapping[str, pl.DataType] | None = None,
) -> str:
    try:
        schema = pl.read_parquet_schema(path)
    except Exception as error:
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact schema is unreadable"
        ) from error
    if (
        DERIVED_VALUE_ORDINAL_COLUMN not in schema
        or schema[DERIVED_VALUE_ORDINAL_COLUMN] != pl.Int64
        or (expected_schema is not None and dict(schema) != dict(expected_schema))
    ):
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact schema is invalid"
        )
    return content_hash(
        {
            "columns": [
                {"name": name, "type": str(data_type)}
                for name, data_type in schema.items()
            ]
        }
    )


def _validate_ordinal_sequence(
    path: Path,
    *,
    expected_row_count: int,
    batch_rows: int,
) -> None:
    observed = 0
    try:
        frames = (
            pl.scan_parquet(
                path,
                glob=False,
                low_memory=True,
                rechunk=False,
                cache=False,
                parallel="none",
            )
            .select(DERIVED_VALUE_ORDINAL_COLUMN)
            .collect_batches(
                chunk_size=batch_rows,
                maintain_order=True,
                engine="streaming",
            )
        )
        for frame in frames:
            if not frame.height or frame.height > batch_rows:
                raise DerivedValueArtifactWriteError(
                    "Derived-value validation batch exceeded its bound"
                )
            expected = pl.Series(
                DERIVED_VALUE_ORDINAL_COLUMN,
                range(observed, observed + frame.height),
                dtype=pl.Int64,
            )
            if not frame.get_column(DERIVED_VALUE_ORDINAL_COLUMN).equals(expected):
                raise DerivedValueArtifactWriteError(
                    "Derived-value artifact ordinal sequence is invalid"
                )
            observed += frame.height
            if observed > expected_row_count:
                raise DerivedValueArtifactWriteError(
                    "Derived-value artifact row count is invalid"
                )
    except DerivedValueArtifactWriteError:
        raise
    except (OSError, pl.exceptions.PolarsError) as error:
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact values are unreadable"
        ) from error
    if observed != expected_row_count:
        raise DerivedValueArtifactWriteError(
            "Derived-value artifact row count is invalid"
        )


def _validate_value_column_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 200
        or any(ord(character) < 32 for character in name)
        or name == DERIVED_VALUE_ORDINAL_COLUMN
    ):
        raise DerivedValueArtifactWriteError(
            "Derived-value column name is invalid"
        )


def _workspace_regular_file_bytes(workspace: Path) -> int:
    return sum(
        item.stat().st_size
        for item in workspace.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def _require_temporary_capacity(
    workspace: Path,
    maximum_temporary_bytes: int | None,
) -> None:
    if (
        maximum_temporary_bytes is not None
        and _workspace_regular_file_bytes(workspace) > maximum_temporary_bytes
    ):
        raise DerivedValueArtifactWriteError(
            "Derived-value output exceeds its temporary byte limit"
        )


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


__all__ = [
    "DERIVED_VALUE_MAX_COMPACTION_INPUTS",
    "DERIVED_VALUE_PARQUET_COMPRESSION",
    "DERIVED_VALUE_TARGET_BATCH_ROWS",
    "DerivedValueArtifactCandidate",
    "DerivedValueArtifactCandidateWriter",
    "DerivedValueArtifactPublication",
    "DerivedValueArtifactPublisher",
    "DerivedValueArtifactWriteError",
    "DerivedValuePage",
    "validate_derived_value_artifact",
]
