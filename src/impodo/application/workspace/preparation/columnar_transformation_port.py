"""Application port for bounded native transformation execution.

Preparation owns the decision to transform a verified source snapshot.  This
port keeps the optional Polars implementation outside that application
decision while preserving the bounded batch and immutable-artifact contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from impodo.domain.compiler.columnar_transformation import ColumnarTransformationProgram
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.staging.transformation_impact import (
    TransformationImpactCounts,
    TransformationImpactRow,
    TransformationRuleImpact,
)
from impodo.domain.shared.models import Issue, PreparedRecord


DEFAULT_COLUMNAR_TRANSFORMATION_BATCH_ROWS = 1_000


@dataclass(frozen=True, slots=True)
class ColumnarTransformationBatch:
    """One bounded native result adapted to canonical preparation evidence."""

    records: tuple[PreparedRecord, ...]
    source_identities: tuple[tuple[Any, ...], ...]
    target_identities: tuple[tuple[Any, ...], ...]
    target_scopes: tuple[tuple[Any, ...], ...]
    scalar_values: tuple[Mapping[str, Any], ...]
    references: tuple[Mapping[str, Any], ...]
    issues: tuple[tuple[Issue, ...], ...]
    impacts: tuple[TransformationImpactRow, ...]
    impact_counts: TransformationImpactCounts
    rule_impacts: tuple[TransformationRuleImpact, ...]
    source_rows: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ColumnarPreparedSnapshotCandidate:
    """Validated physical evidence produced before artifact publication."""

    row_count: int
    physical_schema_hash: str
    parquet_sha256: str


class ColumnarTransformationPort(Protocol):
    """Run one approved native transformation without owning preparation state."""

    batch_rows: int

    def write_prepared_snapshot(
        self,
        source_path: str | Path,
        source_snapshot: SourceSnapshot,
        program: ColumnarTransformationProgram,
        destination: str | Path,
    ) -> ColumnarPreparedSnapshotCandidate: ...

    def iter_prepared_batches(
        self,
        path: str | Path,
        prepared_snapshot: PreparedSnapshot,
        source_snapshot: SourceSnapshot | None,
        program: ColumnarTransformationProgram,
        *,
        batch_size: int,
        materialize_records: bool,
        collect_impacts: bool = True,
    ) -> Iterator[ColumnarTransformationBatch]: ...

    def summarize_rule_impacts(
        self,
        path: str | Path,
        prepared_snapshot: PreparedSnapshot,
        program: ColumnarTransformationProgram,
    ) -> tuple[TransformationRuleImpact, ...]: ...
