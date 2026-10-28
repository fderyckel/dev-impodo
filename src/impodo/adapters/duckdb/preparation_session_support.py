"""Shared bounded views and constants for preparation-session persistence."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, overload
from uuid import UUID

import duckdb

from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.domain.preparation.normalization import NormalizationEffect
from impodo.domain.preparation.staging_contracts import (
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingDisposition,
    StagingReconciliation,
)
from impodo.domain.workspace.errors import WorkspaceError

from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparationSessionSummary,
    PreparedCanonicalProjection,
    StoredCanonicalStagingRun,
)
from ...domain.staging.transformation_impact import (
    TransformationImpactRow,
)
from ...domain.target_numeric_precision import PreparedNumericField
from .constants import (
    DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
)

_STAGING_DISPOSITIONS = frozenset(item.value for item in StagingDisposition)

_PREPARATION_IMPACT_JSON_STRUCTURE = """[{
    "ordinal":"BIGINT",
    "dataset":"VARCHAR",
    "source_row":"BIGINT",
    "target_field":"VARCHAR",
    "outcome":"VARCHAR",
    "impact_json":"VARCHAR"
}]"""

_CANONICAL_STAGING_ROW_JSON_STRUCTURE = """[{
    "ordinal":"BIGINT",
    "row_id":"VARCHAR",
    "dataset":"VARCHAR",
    "source_row":"BIGINT",
    "target_model":"VARCHAR",
    "disposition":"VARCHAR",
    "record_label":"VARCHAR",
    "quality_identity_key":"VARCHAR",
    "row_json":"VARCHAR"
}]"""

_DIRECT_IDENTITY_JSON_STRUCTURE = """[{
    "ordinal":"BIGINT",
    "dataset":"VARCHAR",
    "identity_hash":"VARCHAR",
    "base_disposition":"VARCHAR",
    "finalized_duplicate":"BOOLEAN"
}]"""

_DIRECT_LINEAGE_JSON_STRUCTURE = """[{
    "dataset":"VARCHAR",
    "output_source_row":"BIGINT",
    "physical_dataset_id":"VARCHAR",
    "physical_source_row":"BIGINT"
}]"""

_DIRECT_PHYSICAL_ROW_JSON_STRUCTURE = """[{
    "physical_dataset_id":"VARCHAR",
    "source_row":"BIGINT"
}]"""

_DIRECT_RELATIONSHIP_JSON_STRUCTURE = """[{
    "child_ordinal":"BIGINT",
    "target_field":"VARCHAR",
    "item_ordinal":"INTEGER",
    "parent_dataset":"VARCHAR",
    "normalized_key_json":"VARCHAR",
    "parent_identity_hash":"VARCHAR"
}]"""

# A canonical row can legally be much larger than the normal JSON envelope.
# Route a conservative upper-bound estimate through one scalar insert instead
# of rejecting valid source evidence or creating an oversized copied payload.
_CANONICAL_ROW_SCALAR_FALLBACK_BYTES = DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES // 3


def canonical_preparation_session_id(session_id: str) -> str:
    """Validate and canonicalize one preparation-session UUID."""

    try:
        return str(UUID(session_id))
    except (ValueError, AttributeError) as error:
        raise WorkspaceError("Preparation session identifier is invalid") from error


class PreparationSessionViewRepository(Protocol):
    """Narrow callbacks used by bounded canonical-row and impact views."""

    def _load_canonical_row_range(
        self,
        workspace_id: str,
        session_id: str,
        start: int,
        stop: int,
    ) -> tuple[CanonicalRow, ...]: ...

    def _iter_canonical_rows(
        self,
        workspace_id: str,
        session_id: str,
    ) -> Iterator[CanonicalRow]: ...

    def iter_direct_encoded_batches(
        self,
        *args,
        **kwargs,
    ) -> Iterator[Sequence[Sequence[object]]]: ...

    def _bounded_quality_index(
        self,
        *args,
        **kwargs,
    ) -> dict[str, object] | None: ...

    def _bounded_relationship_findings(
        self,
        *args,
        **kwargs,
    ) -> tuple[tuple[object, ...], ...]: ...

    def _identity_group_findings(self, *args, **kwargs) -> Iterator[object]: ...

    def _iter_quality_index_batches(
        self,
        *args,
        **kwargs,
    ) -> Iterator[Sequence[Sequence[object]]]: ...

    def _iter_accounting_index_batches(
        self,
        *args,
        **kwargs,
    ) -> Iterator[Sequence[Sequence[object]]]: ...

    def _direct_index_contains_row_id(self, *args, **kwargs) -> bool: ...

    def _iter_impacts(
        self,
        workspace_id: str,
        session_id: str,
    ) -> Iterator[TransformationImpactRow]: ...

    def _prepare_normalization_facts(
        self,
        *args,
        **kwargs,
    ) -> dict[str, object]: ...

    def _copy_normalization_effects_to_run(self, *args, **kwargs) -> int: ...

    def _iter_normalization_effect_json_batches(
        self,
        *args,
        **kwargs,
    ) -> Iterator[tuple[str, ...]]: ...

    def _iter_prepared_normalization_effects(
        self,
        *args,
        **kwargs,
    ) -> Iterator[NormalizationEffect]: ...


class PreparationSessionRepositoryHost(PreparationSessionViewRepository, Protocol):
    """Dependencies composed by the concrete preparation-session facade."""

    @property
    def _artifacts(self) -> WorkspaceArtifactStore: ...

    def workspace_directory(self, workspace_id: str) -> Path: ...

    def _connect(
        self,
        path: Path,
    ) -> AbstractContextManager[duckdb.DuckDBPyConnection]: ...

    def _connect_prepared(
        self,
        path: Path,
    ) -> AbstractContextManager[duckdb.DuckDBPyConnection]: ...

    def _ensure_workspace_database_schema(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None: ...

    def bind_prepared_canonical_projection(
        self,
        workspace_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
        projection: PreparedCanonicalProjection,
    ) -> None: ...

    def get_session(
        self,
        workspace_id: str,
        session_id: str,
    ) -> PreparationSessionSummary: ...

    def load_stored_run(
        self,
        workspace_id: str,
        session_id: str,
    ) -> StoredCanonicalStagingRun: ...

    def _direct_rows_by_ordinal(
        self,
        workspace_id: str,
        run_id: str,
        ordinals: Sequence[int],
    ) -> dict[int, CanonicalRow]: ...

    def _restart_direct_finalization(
        self,
        workspace_id: str,
        session_id: str,
    ) -> None: ...

    def _update_direct_rows(
        self,
        workspace_id: str,
        run_id: str,
        values: Sequence[Sequence[object]],
    ) -> None: ...

    def _resolve_relationship_edges(
        self,
        workspace_id: str,
        session_id: str,
    ) -> None: ...

    def _reconciliation(
        self,
        workspace_id: str,
        session_id: str,
        evidence: Mapping[str, tuple[str, StagingDatasetRole, int, str]],
    ) -> tuple[StagingReconciliation, tuple[StagingDatasetReconciliation, ...]]: ...

    def _hash_direct_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        bindings: PreparationSessionBindings,
        datasets: tuple[StagingDatasetReconciliation, ...],
        issues: tuple[CanonicalIssue, ...],
        reconciliation: StagingReconciliation,
        control_totals: tuple[CanonicalControlTotal, ...],
    ) -> tuple[str, int]: ...

    def _require_status(
        self,
        connection: duckdb.DuckDBPyConnection,
        session_id: str,
        expected: PreparationSessionStatus,
    ) -> tuple[str, int, int]: ...


def _canonical_row_requires_scalar_transport(
    row: CanonicalPreparedSessionRow,
) -> bool:
    """Avoid copying a legally large canonical row into a JSON envelope."""

    estimated_bytes = sum(
        len(value.encode("utf-8"))
        for value in (
            row.row_id,
            row.dataset,
            row.target_model,
            row.disposition.value,
            row.row_json,
        )
    )
    return estimated_bytes > _CANONICAL_ROW_SCALAR_FALLBACK_BYTES


def _canonical_row_json_requires_scalar_transport(row_json: str) -> bool:
    """Keep large direct-row updates outside the copied JSON envelope."""

    return len(row_json.encode("utf-8")) > _CANONICAL_ROW_SCALAR_FALLBACK_BYTES


class _SessionCanonicalRows(Sequence[CanonicalRow]):
    """Read finalized session rows through bounded ordinal slices."""

    def __init__(
        self,
        repository: PreparationSessionViewRepository,
        workspace_id: str,
        session_id: str,
        row_count: int,
    ) -> None:
        self._repository = repository
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._row_count = row_count
        self.canonical_run_id = session_id

    def __len__(self) -> int:
        return self._row_count

    @overload
    def __getitem__(self, index: int) -> CanonicalRow: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[CanonicalRow, ...]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> CanonicalRow | tuple[CanonicalRow, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._row_count)
            if step != 1:
                raise ValueError("Canonical session slices must be contiguous")
            return self._repository._load_canonical_row_range(
                self._workspace_id,
                self._session_id,
                start,
                stop,
            )
        normalized = index + self._row_count if index < 0 else index
        if normalized < 0 or normalized >= self._row_count:
            raise IndexError(index)
        rows = self._repository._load_canonical_row_range(
            self._workspace_id,
            self._session_id,
            normalized,
            normalized + 1,
        )
        if not rows:
            raise IndexError(index)
        return rows[0]

    def __iter__(self) -> Iterator[CanonicalRow]:
        yield from self._repository._iter_canonical_rows(
            self._workspace_id,
            self._session_id,
        )

    def iter_encoded_batches(self, connection, batch_size: int):
        """Read exact stored JSON through the publication transaction."""

        yield from self._repository.iter_direct_encoded_batches(
            self._workspace_id,
            self._session_id,
            batch_size=batch_size,
            connection=connection,
        )

    def bounded_quality_index(
        self,
        physical_rows: Mapping[str, Sequence[int]],
        *,
        numeric_fields: tuple[PreparedNumericField, ...] = (),
    ):
        """Validate and summarize the direct quality index set-wise."""

        return self._repository._bounded_quality_index(
            self._workspace_id,
            self._session_id,
            physical_rows,
            numeric_fields=numeric_fields,
        )

    def bounded_relationship_findings(
        self,
        unsafe_row_ids: Sequence[str],
        propagating_datasets: Sequence[str],
    ):
        """Resolve and propagate relationship readiness set-wise."""

        return self._repository._bounded_relationship_findings(
            self._workspace_id,
            self._session_id,
            unsafe_row_ids,
            propagating_datasets,
        )

    def bounded_identity_group_findings(self, unsafe_row_ids, propagating_datasets):
        """Project symbolic identities and keep readiness propagation durable."""

        return self._repository._identity_group_findings(
            self._workspace_id,
            self._session_id,
            unsafe_row_ids,
            propagating_datasets,
        )

    def iter_quality_index_batches(self, connection, batch_size: int):
        """Yield narrow row decisions through the publisher transaction."""

        yield from self._repository._iter_quality_index_batches(
            self._workspace_id,
            self._session_id,
            batch_size=batch_size,
            connection=connection,
        )

    def iter_accounting_index_batches(self, connection, batch_size: int):
        """Yield one-to-one physical lineage in accounting order."""

        yield from self._repository._iter_accounting_index_batches(
            self._workspace_id,
            self._session_id,
            batch_size=batch_size,
            connection=connection,
        )

    def contains_row_id(self, row_id: str) -> bool:
        return self._repository._direct_index_contains_row_id(
            self._workspace_id,
            self._session_id,
            row_id,
        )


class _SessionImpacts:
    """Stream finalized transformation impacts without materializing them."""

    def __init__(
        self,
        repository: PreparationSessionViewRepository,
        workspace_id: str,
        session_id: str,
    ) -> None:
        self._repository = repository
        self._workspace_id = workspace_id
        self._session_id = session_id

    def __iter__(self) -> Iterator[TransformationImpactRow]:
        yield from self._repository._iter_impacts(
            self._workspace_id,
            self._session_id,
        )

    @property
    def normalization_run_id(self) -> str:
        """Use the session UUID for the construct-once normalization run."""

        return self._session_id

    def prepare_normalization_facts(
        self,
        *,
        effect_builder,
        finding_builder,
    ) -> dict[str, object]:
        """Construct and summarize normalization facts in one durable pass."""

        return self._repository._prepare_normalization_facts(
            self._workspace_id,
            self._session_id,
            effect_builder=effect_builder,
            finding_builder=finding_builder,
        )

    def copy_normalization_effects(self, connection, run_id: str) -> int:
        """Copy the exact prepared effect facts into an immutable run."""

        return self._repository._copy_normalization_effects_to_run(
            connection,
            self._session_id,
            run_id,
        )

    def iter_normalization_effect_json_batches(
        self,
        connection,
        batch_size: int,
    ) -> Iterator[tuple[str, ...]]:
        """Yield exact effect JSON in the public logical order."""

        yield from self._repository._iter_normalization_effect_json_batches(
            connection,
            self._session_id,
            batch_size,
        )

    def iter_normalization_effects(self) -> Iterator[NormalizationEffect]:
        """Decode durable facts for the bounded normalization reader."""

        yield from self._repository._iter_prepared_normalization_effects(
            self._workspace_id,
            self._session_id,
        )
