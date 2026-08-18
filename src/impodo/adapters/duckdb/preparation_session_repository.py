"""DuckDB adapter for bounded, unpublished Stage-E preparation sessions."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import overload
from uuid import UUID, uuid4

import duckdb

from ...access import Actor
from ..polars_transformation import iter_polars_prepared_batches
from ...artifacts import ArtifactStore, ArtifactStoreError, LocalArtifactStore
from ...domain.staging.canonical_projection import canonical_prepared_session_row
from ...domain.serialization import CanonicalJsonObjectHasher
from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparedCanonicalProjection,
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparationSessionSummary,
    StoredCanonicalStagingRun,
    transformation_impact_from_portable_dict,
    transformation_impact_to_portable_dict,
    transformation_report_to_portable_dict,
)
from ...domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
)
from ...models import Issue, LogicalReference, canonical_json_bytes, portable_value
from ...normalization import NormalizationEffect
from ...projects import ProjectNotFoundError
from ...quality import QualityIssue
from ...staging import StagingRunStatus
from ...staging_contracts import (
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingDisposition,
    StagingReconciliation,
    validate_canonical_row_bindings,
)
from ...workspace_errors import WorkspaceError
from .constants import (
    DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
    DUCKDB_JSON_BATCH_MAX_BYTES,
    NATIVE_PREPARED_PROJECTION_MEMORY_LIMIT,
    PREPARATION_SESSION_MEMORY_LIMIT,
    PREPARATION_SESSION_ROW_BATCH_SIZE,
    PREPARED_VALUE_PROJECTOR_MEMORY_LIMIT,
    STAGING_ROW_BATCH_SIZE,
)
from .native_prepared_projection import (
    NativePreparedProjectionResult,
    append_clean_native_projection,
    projected_encoded_rows_sql,
    supports_clean_native_projection,
)
from .repository import DuckDbRepository
from .serialization import (
    _canonical_json,
    _columnar_parameters,
    iter_encoded_json_batches,
)


_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
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


class _SessionCanonicalRows(Sequence[CanonicalRow]):
    """Read finalized session rows through bounded ordinal slices."""

    def __init__(
        self,
        repository: "PreparationSessionRepository",
        project_id: str,
        session_id: str,
        row_count: int,
    ) -> None:
        self._repository = repository
        self._project_id = project_id
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
                self._project_id,
                self._session_id,
                start,
                stop,
            )
        normalized = index + self._row_count if index < 0 else index
        if normalized < 0 or normalized >= self._row_count:
            raise IndexError(index)
        rows = self._repository._load_canonical_row_range(
            self._project_id,
            self._session_id,
            normalized,
            normalized + 1,
        )
        if not rows:
            raise IndexError(index)
        return rows[0]

    def __iter__(self) -> Iterator[CanonicalRow]:
        yield from self._repository._iter_canonical_rows(
            self._project_id,
            self._session_id,
        )

    def iter_encoded_batches(self, connection, batch_size: int):
        """Read exact stored JSON through the publication transaction."""

        yield from self._repository._iter_direct_encoded_batches(
            self._project_id,
            self._session_id,
            batch_size=batch_size,
            connection=connection,
        )

    def bounded_quality_index(self, physical_rows: Mapping[str, Sequence[int]]):
        """Validate and summarize the direct Stage-F index set-wise."""

        return self._repository._bounded_quality_index(
            self._project_id,
            self._session_id,
            physical_rows,
        )

    def bounded_relationship_findings(
        self,
        unsafe_row_ids: Sequence[str],
        propagating_datasets: Sequence[str],
    ):
        """Resolve and propagate relationship readiness set-wise."""

        return self._repository._bounded_relationship_findings(
            self._project_id,
            self._session_id,
            unsafe_row_ids,
            propagating_datasets,
        )

    def iter_quality_index_batches(self, connection, batch_size: int):
        """Yield narrow row decisions through the publisher transaction."""

        yield from self._repository._iter_quality_index_batches(
            self._project_id,
            self._session_id,
            batch_size=batch_size,
            connection=connection,
        )

    def iter_accounting_index_batches(self, connection, batch_size: int):
        """Yield one-to-one physical lineage in accounting order."""

        yield from self._repository._iter_accounting_index_batches(
            self._project_id,
            self._session_id,
            batch_size=batch_size,
            connection=connection,
        )

    def contains_row_id(self, row_id: str) -> bool:
        return self._repository._direct_index_contains_row_id(
            self._project_id,
            self._session_id,
            row_id,
        )


class _SessionImpacts:
    """Stream finalized transformation impacts without materializing them."""

    def __init__(
        self,
        repository: "PreparationSessionRepository",
        project_id: str,
        session_id: str,
    ) -> None:
        self._repository = repository
        self._project_id = project_id
        self._session_id = session_id

    def __iter__(self) -> Iterator[TransformationImpactRow]:
        yield from self._repository._iter_impacts(
            self._project_id,
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
    ):
        """Construct and summarize Stage-G facts in one durable pass."""

        return self._repository._prepare_normalization_facts(
            self._project_id,
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
    ):
        """Yield exact effect JSON in the public logical order."""

        yield from self._repository._iter_normalization_effect_json_batches(
            connection,
            self._session_id,
            batch_size,
        )

    def iter_normalization_effects(self):
        """Decode durable facts for the bounded normalization reader."""

        yield from self._repository._iter_prepared_normalization_effects(
            self._project_id,
            self._session_id,
        )


class PreparationSessionRepository(DuckDbRepository):
    """Persist a bounded canonical preparation until publication succeeds."""

    def __init__(
        self,
        database,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        super().__init__(database)
        self._artifacts = artifacts or LocalArtifactStore(database.root)

    def _connect(self, path):
        """Use a smaller hardened buffer allowance for bounded session work."""

        return self._database.connection_factory.connect(
            path,
            memory_limit=PREPARATION_SESSION_MEMORY_LIMIT,
            threads="1",
            preserve_insertion_order=False,
        )

    def _connect_prepared(self, path):
        """Allow only internal hash-verified prepared Parquet scans."""

        return self._database.connection_factory.connect(
            path,
            memory_limit=NATIVE_PREPARED_PROJECTION_MEMORY_LIMIT,
            threads="1",
            preserve_insertion_order=False,
            enable_external_access=True,
        )

    def begin_direct_session(
        self,
        project_id: str,
        bindings: PreparationSessionBindings,
        *,
        actor: Actor,
    ) -> PreparationSessionSummary:
        """Create a session whose UUID is also a pending canonical run ID."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                INSERT INTO preparation_session (
                    session_id, status, mapping_id, mapping_version,
                    physical_selection_hash, source_selection_hash,
                    mapping_hash, schema_hash, derived_plan_hash,
                    compiled_plan_hash, contract_version, evaluator_version,
                    source_hashes_json, run_issues_json, control_totals_json,
                    reconciliation_json, dataset_reconciliation_json,
                    impact_report_json, staged_row_count,
                    canonical_row_count, impact_row_count, created_at,
                    updated_at, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]',
                          NULL, NULL, NULL, 0, 0, 0, ?, ?, NULL)
                    """,
                    [
                        session_id,
                        PreparationSessionStatus.BUILDING.value,
                        bindings.mapping_id,
                        bindings.mapping_version,
                        bindings.physical_selection_hash,
                        bindings.source_selection_hash,
                        bindings.mapping_hash,
                        bindings.schema_hash,
                        bindings.derived_plan_hash,
                        bindings.compiled_plan_hash,
                        bindings.contract_version,
                        bindings.evaluator_version,
                        _canonical_json(dict(sorted(bindings.source_hashes.items()))),
                        now,
                        now,
                    ],
                )
                zero_hash = "sha256:" + "0" * 64
                connection.execute(
                    """
                        INSERT INTO canonical_staging_run (
                            run_id, content_hash, mapping_id, mapping_version,
                            physical_selection_hash, source_selection_hash,
                            mapping_hash, schema_hash, derived_plan_hash,
                            compiled_plan_hash, contract_version,
                            evaluator_version, status, published_at,
                            published_by, row_count, run_issues_json,
                            reconciliation_json,
                            dataset_reconciliation_json,
                            control_totals_json, retired_at,
                            retired_reason, successor_run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  0, '[]', '{}', '[]', '[]', NULL, NULL, NULL)
                    """,
                    [
                        session_id,
                        zero_hash,
                        bindings.mapping_id,
                        bindings.mapping_version,
                        bindings.physical_selection_hash,
                        bindings.source_selection_hash,
                        bindings.mapping_hash,
                        bindings.schema_hash,
                        bindings.derived_plan_hash,
                        bindings.compiled_plan_hash,
                        bindings.contract_version,
                        bindings.evaluator_version,
                        StagingRunStatus.PENDING.value,
                        now,
                        actor.identity.display_name,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PreparationSessionSummary(
            session_id=session_id,
            status=PreparationSessionStatus.BUILDING,
            bindings=bindings,
        )

    def find_prepared_snapshot(
        self,
        project_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> PreparedSnapshot | None:
        """Find one historical exact prepared artifact for safe reuse."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT manifest_json
                  FROM prepared_snapshot_manifest
                 WHERE dataset_id = ? AND logical_hash = ?
                 ORDER BY created_at DESC, content_hash
                 LIMIT 1
                """,
                [dataset_id, logical_hash],
            ).fetchone()
        return PreparedSnapshot.from_json(str(row[0])) if row is not None else None

    def current_prepared_snapshots(
        self,
        project_id: str,
    ) -> tuple[PreparedSnapshot, ...]:
        """Load snapshots advanced only by a fully published preparation."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            rows = connection.execute(
                """
                SELECT manifest.manifest_json
                  FROM prepared_snapshot_current AS current
                  JOIN prepared_snapshot_manifest AS manifest
                    ON manifest.content_hash = current.content_hash
                 ORDER BY current.dataset_id
                """
            ).fetchall()
        return tuple(PreparedSnapshot.from_json(str(row[0])) for row in rows)

    def prepared_snapshot_storage_keys(self, project_id: str) -> frozenset[str]:
        """Return immutable prepared files referenced by any manifest."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            rows = connection.execute(
                """
                SELECT parquet_storage_key
                  FROM prepared_snapshot_manifest
                 ORDER BY parquet_storage_key
                """
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def bind_prepared_snapshot(
        self,
        project_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
    ) -> None:
        """Register a verified manifest and bind it to one building session."""

        if snapshot.project_id != project_id:
            raise WorkspaceError("Prepared snapshot belongs to another project")
        canonical_session_id = self._session_id(session_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                bindings = connection.execute(
                    """
                    SELECT mapping_hash, schema_hash
                      FROM preparation_session
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if bindings != (snapshot.mapping_hash, snapshot.schema_hash):
                    raise WorkspaceError(
                        "Prepared snapshot does not match the preparation session"
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO prepared_snapshot_manifest
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        snapshot.content_hash,
                        snapshot.dataset_id,
                        snapshot.logical_hash,
                        snapshot.source_snapshot_hash,
                        snapshot.mapping_hash,
                        snapshot.schema_hash,
                        snapshot.transformation_program_hash,
                        snapshot.row_count,
                        snapshot.parquet_sha256,
                        snapshot.parquet_storage_key,
                        snapshot.created_at.isoformat(),
                        snapshot.to_json(),
                    ],
                )
                registered = connection.execute(
                    """
                    SELECT dataset_id, logical_hash, parquet_sha256,
                           parquet_storage_key
                      FROM prepared_snapshot_manifest
                     WHERE content_hash = ?
                    """,
                    [snapshot.content_hash],
                ).fetchone()
                if registered != (
                    snapshot.dataset_id,
                    snapshot.logical_hash,
                    snapshot.parquet_sha256,
                    snapshot.parquet_storage_key,
                ):
                    raise WorkspaceError(
                        "Stored prepared snapshot manifest is inconsistent"
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO preparation_session_snapshot
                    VALUES (?, ?, ?)
                    """,
                    [
                        canonical_session_id,
                        snapshot.dataset_id,
                        snapshot.content_hash,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def bind_prepared_canonical_projection(
        self,
        project_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
        projection: PreparedCanonicalProjection,
    ) -> None:
        """Bind one native direct dataset to its immutable value carrier."""

        if (
            snapshot.project_id != project_id
            or snapshot.dataset_id != projection.dataset_id
            or snapshot.row_count != projection.row_count
            or snapshot.transformation_program_hash != projection.program.content_hash
        ):
            raise WorkspaceError(
                "Prepared canonical projection does not match its snapshot"
            )
        canonical_session_id = self._session_id(session_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        encoded = _canonical_json(projection.to_portable_dict())
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                bindings = connection.execute(
                    """
                    SELECT source_selection_hash, mapping_hash, schema_hash,
                           source_hashes_json
                      FROM preparation_session
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if bindings is None:
                    raise WorkspaceError("Preparation session was not found")
                try:
                    source_hashes = json.loads(str(bindings[3]))
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Preparation session source bindings are invalid"
                    ) from error
                if (
                    projection.program.source_selection_hash != str(bindings[0])
                    or projection.program.mapping_content_hash != str(bindings[1])
                    or projection.program.schema_hash != str(bindings[2])
                    or not isinstance(source_hashes, dict)
                    or source_hashes.get(projection.dataset) != projection.source_hash
                ):
                    raise WorkspaceError(
                        "Prepared canonical projection bindings changed"
                    )
                snapshot_binding = connection.execute(
                    """
                    SELECT 1
                      FROM preparation_session_snapshot
                     WHERE session_id = ? AND dataset_id = ?
                       AND content_hash = ?
                    """,
                    [
                        canonical_session_id,
                        snapshot.dataset_id,
                        snapshot.content_hash,
                    ],
                ).fetchone()
                if snapshot_binding is None:
                    raise WorkspaceError(
                        "Prepared snapshot is not bound to the session"
                    )
                overlap = connection.execute(
                    """
                    SELECT 1
                      FROM canonical_prepared_projection
                     WHERE run_id = ?
                       AND ordinal_start < ?
                       AND ordinal_start + row_count > ?
                    """,
                    [
                        canonical_session_id,
                        projection.ordinal_start + projection.row_count,
                        projection.ordinal_start,
                    ],
                ).fetchone()
                if overlap is not None:
                    raise WorkspaceError(
                        "Prepared canonical projection ordinals overlap"
                    )
                connection.execute(
                    """
                    INSERT INTO canonical_prepared_projection
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_session_id,
                        projection.dataset_id,
                        projection.dataset,
                        projection.ordinal_start,
                        projection.row_count,
                        snapshot.content_hash,
                        encoded,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def append_direct_rows(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[CanonicalPreparedSessionRow],
    ) -> None:
        """Write one encoded batch directly into its pending canonical run."""

        if not rows:
            return
        canonical_session_id = self._session_id(session_id)
        expected_lineage_count = 0
        for item in rows:
            if not item.physical_sources:
                raise ValueError("Prepared session rows require physical lineage")
            for physical_dataset_id, source_rows in sorted(
                item.physical_sources.items()
            ):
                ordered = tuple(sorted(set(source_rows)))
                if not physical_dataset_id or ordered != tuple(source_rows):
                    raise ValueError("Prepared session lineage is invalid")
                for source_row in ordered:
                    if source_row < 1:
                        raise ValueError(
                            "Prepared session source rows must be positive"
                        )
                    expected_lineage_count += 1

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                pending = connection.execute(
                    """
                    SELECT 1
                      FROM canonical_staging_run
                     WHERE run_id = ? AND status = ?
                    """,
                    [canonical_session_id, StagingRunStatus.PENDING.value],
                ).fetchone()
                if pending is None:
                    raise WorkspaceError("Direct preparation run is not pending")
                json_rows = []
                scalar_rows = []
                for item in rows:
                    destination = (
                        scalar_rows
                        if _canonical_row_requires_scalar_transport(item)
                        else json_rows
                    )
                    destination.append(item)
                canonical_row_count = 0
                transport_rows = (
                    {
                        "ordinal": item.ordinal,
                        "row_id": item.row_id,
                        "dataset": item.dataset,
                        "source_row": item.source_row,
                        "target_model": item.target_model,
                        "disposition": item.disposition.value,
                        "record_label": item.record_label,
                        "quality_identity_key": item.quality_identity_key,
                        "row_json": item.row_json,
                    }
                    for item in json_rows
                )
                for encoded_batch in iter_encoded_json_batches(
                    transport_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row (
                            run_id, ordinal, row_id, dataset, source_row,
                            target_model, disposition, record_label,
                            quality_identity_key, row_json
                        )
                        SELECT
                            ?, item.ordinal, item.row_id, item.dataset,
                            item.source_row, item.target_model,
                            item.disposition, item.record_label,
                            item.quality_identity_key, item.row_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _CANONICAL_STAGING_ROW_JSON_STRUCTURE,
                        ],
                    )
                    canonical_row_count += encoded_batch.row_count
                for item in scalar_rows:
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row (
                            run_id, ordinal, row_id, dataset, source_row,
                            target_model, disposition, record_label,
                            quality_identity_key, row_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            canonical_session_id,
                            item.ordinal,
                            item.row_id,
                            item.dataset,
                            item.source_row,
                            item.target_model,
                            item.disposition.value,
                            item.record_label,
                            item.quality_identity_key,
                            item.row_json,
                        ],
                    )
                    canonical_row_count += 1
                if canonical_row_count != len(rows):
                    raise WorkspaceError("Prepared canonical row batch is incomplete")
                issue_rows = [
                    [
                        canonical_session_id,
                        item.ordinal,
                        issue_ordinal,
                        _canonical_json(issue.to_portable_dict()),
                    ]
                    for item in rows
                    for issue_ordinal, issue in enumerate(item.issues, start=1)
                ]
                if issue_rows:
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row_issue
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(issue_rows),
                    )
                identity_rows = (
                    {
                        "ordinal": item.ordinal,
                        "dataset": item.dataset,
                        "identity_hash": "sha256:"
                        + sha256(
                            canonical_json_bytes(
                                {
                                    "dataset": item.dataset,
                                    "source_identity": portable_value(
                                        item.source_identity
                                    ),
                                }
                            )
                        ).hexdigest(),
                        "base_disposition": item.disposition.value,
                        "finalized_duplicate": False,
                    }
                    for item in rows
                    if item.source_identity
                )
                identity_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    identity_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_direct_identity (
                            session_id, ordinal, dataset, identity_hash,
                            base_disposition, finalized_duplicate
                        )
                        SELECT
                            ?, item.ordinal, item.dataset,
                            item.identity_hash, item.base_disposition,
                            item.finalized_duplicate
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_IDENTITY_JSON_STRUCTURE,
                        ],
                    )
                    identity_count += encoded_batch.row_count
                if identity_count != sum(
                    bool(item.source_identity) for item in rows
                ):
                    raise WorkspaceError("Prepared identity fact batch is incomplete")

                relationship_rows = (
                    {
                        "child_ordinal": item.ordinal,
                        "target_field": target_field,
                        "item_ordinal": item_ordinal,
                        "parent_dataset": reference.dataset,
                        "normalized_key_json": _canonical_json(
                            portable_value(reference.key)
                        ),
                        "parent_identity_hash": "sha256:"
                        + sha256(
                            canonical_json_bytes(
                                {
                                    "dataset": reference.dataset,
                                    "source_identity": portable_value(reference.key),
                                }
                            )
                        ).hexdigest(),
                    }
                    for item in rows
                    for target_field, raw_reference in sorted(item.references.items())
                    for references in (
                        raw_reference
                        if isinstance(raw_reference, tuple)
                        else (raw_reference,),
                    )
                    for item_ordinal, reference in enumerate(references)
                    if isinstance(reference, LogicalReference)
                    and reference.origin == "incoming"
                    and reference.dataset
                )
                for encoded_batch in iter_encoded_json_batches(
                    relationship_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_relationship_edge (
                            session_id, child_ordinal, target_field,
                            item_ordinal, parent_dataset,
                            normalized_key_json, parent_identity_hash,
                            match_state, resolution_state, match_count,
                            resolved_parent_ordinal
                        )
                        SELECT
                            ?, item.child_ordinal, item.target_field,
                            item.item_ordinal,
                            item.parent_dataset, item.normalized_key_json,
                            item.parent_identity_hash, 'PENDING', 'PENDING',
                            0, NULL
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_RELATIONSHIP_JSON_STRUCTURE,
                        ],
                    )

                lineage_rows = (
                    {
                        "dataset": item.dataset,
                        "output_source_row": item.source_row,
                        "physical_dataset_id": physical_dataset_id,
                        "physical_source_row": source_row,
                    }
                    for item in rows
                    for physical_dataset_id, source_rows in sorted(
                        item.physical_sources.items()
                    )
                    for source_row in source_rows
                )
                lineage_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    lineage_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_lineage (
                            session_id, dataset, output_source_row,
                            physical_dataset_id, physical_source_row
                        )
                        SELECT
                            ?, item.dataset, item.output_source_row,
                            item.physical_dataset_id,
                            item.physical_source_row
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_LINEAGE_JSON_STRUCTURE,
                        ],
                    )
                    lineage_count += encoded_batch.row_count
                if lineage_count != expected_lineage_count:
                    raise WorkspaceError("Prepared lineage fact batch is incomplete")

                physical_rows = (
                    {
                        "physical_dataset_id": physical_dataset_id,
                        "source_row": source_row,
                    }
                    for item in rows
                    for physical_dataset_id, source_rows in sorted(
                        item.physical_sources.items()
                    )
                    for source_row in source_rows
                )
                physical_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    physical_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO preparation_physical_row (
                            session_id, physical_dataset_id, source_row
                        )
                        SELECT
                            ?, item.physical_dataset_id, item.source_row
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_PHYSICAL_ROW_JSON_STRUCTURE,
                        ],
                    )
                    physical_count += encoded_batch.row_count
                if physical_count != expected_lineage_count:
                    raise WorkspaceError(
                        "Prepared physical-row fact batch is incomplete"
                    )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET staged_row_count = staged_row_count + ?,
                           updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        canonical_row_count,
                        datetime.now(timezone.utc).isoformat(),
                        canonical_session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def append_native_prepared_projection(
        self,
        project_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
        projection: PreparedCanonicalProjection,
        path,
        control_fields: tuple[str, ...] = (),
    ) -> NativePreparedProjectionResult | None:
        """Project one complete clean native dataset without Python row objects."""

        if not projection.set_based_projection:
            raise ValueError("Native projection route metadata is required")
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect_prepared(database_path) as connection:
            self._ensure_project_database_schema(connection)
            if not supports_clean_native_projection(
                connection,
                path,
                projection.program,
                control_fields,
            ):
                return None

        self.bind_prepared_canonical_projection(
            project_id,
            session_id,
            snapshot,
            projection,
        )
        canonical_session_id = self._session_id(session_id)
        with self._connect_prepared(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                pending = connection.execute(
                    """
                    SELECT 1
                      FROM canonical_staging_run
                     WHERE run_id = ? AND status = ?
                    """,
                    [canonical_session_id, StagingRunStatus.PENDING.value],
                ).fetchone()
                if pending is None:
                    raise WorkspaceError("Direct preparation run is not pending")
                result = append_clean_native_projection(
                    connection,
                    path=path,
                    session_id=canonical_session_id,
                    projection=projection,
                    control_fields=control_fields,
                )
                observed = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM canonical_staging_row
                     WHERE run_id = ? AND ordinal >= ? AND ordinal < ?
                    """,
                    [
                        canonical_session_id,
                        projection.ordinal_start,
                        projection.ordinal_start + projection.row_count,
                    ],
                ).fetchone()
                if observed is None or int(observed[0]) != projection.row_count:
                    raise WorkspaceError("Native canonical row index is incomplete")
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def append_impacts(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[TransformationImpactRow],
    ) -> None:
        """Append one bounded impact batch with deterministic ordinals."""

        if not rows:
            return
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                current = self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.BUILDING,
                )
                impact_start = int(current[2])
                impact_rows = (
                    {
                        "ordinal": impact_start + offset,
                        "dataset": row.dataset,
                        "source_row": row.source_row,
                        "target_field": row.target_field,
                        "outcome": row.outcome,
                        "impact_json": _canonical_json(
                            transformation_impact_to_portable_dict(row)
                        ),
                    }
                    for offset, row in enumerate(rows)
                )
                impact_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    impact_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_impact_row (
                            session_id, ordinal, dataset, source_row,
                            target_field, outcome, impact_json
                        )
                        SELECT
                            ?, item.ordinal, item.dataset, item.source_row,
                            item.target_field, item.outcome, item.impact_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            session_id,
                            encoded_batch.payload,
                            _PREPARATION_IMPACT_JSON_STRUCTURE,
                        ],
                    )
                    impact_count += encoded_batch.row_count
                if impact_count != len(rows):
                    raise WorkspaceError("Preparation impact batch is incomplete")
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET impact_row_count = impact_row_count + ?,
                           updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        impact_count,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def finalize_direct_session(
        self,
        project_id: str,
        session_id: str,
        *,
        dataset_evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
        run_issues: Sequence[Issue],
        control_totals: Sequence[CanonicalControlTotal],
        impact_report: TransformationImpactReport,
    ) -> StoredCanonicalStagingRun:
        """Finalize rows already stored under a pending canonical run ID."""

        summary = self.get_session(project_id, session_id)
        if summary.status not in {
            PreparationSessionStatus.BUILDING,
            PreparationSessionStatus.FINALIZING,
        }:
            raise WorkspaceError("Preparation session cannot be finalized")
        canonical_session_id = self._session_id(session_id)
        self._restart_direct_finalization(project_id, canonical_session_id)
        bindings = summary.bindings
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            collision_counts = {
                (str(dataset), str(identity_hash)): int(identity_count)
                for dataset, identity_hash, identity_count in connection.execute(
                    """
                    SELECT dataset, identity_hash, identity_count
                      FROM preparation_identity_group
                     WHERE session_id = ? AND identity_count > 1
                    """,
                    [canonical_session_id],
                ).fetchall()
            }

        duplicate_rows: list[tuple[int, int]] = []
        with self._connect(database_path) as connection:
            cursor = connection.execute(
                """
                SELECT ordinal, dataset, identity_hash,
                       finalized_duplicate
                  FROM preparation_direct_identity
                 WHERE session_id = ?
                 ORDER BY ordinal
                """,
                [canonical_session_id],
            )
            while identity_batch := cursor.fetchmany(
                PREPARATION_SESSION_ROW_BATCH_SIZE
            ):
                duplicate_rows.extend(
                    (
                        int(ordinal),
                        collision_counts[(str(dataset), str(identity_hash))],
                    )
                    for ordinal, dataset, identity_hash, finalized in identity_batch
                    if not bool(finalized)
                    and (
                        str(dataset),
                        str(identity_hash),
                    )
                    in collision_counts
                )

        canonical_issues = [CanonicalIssue.from_issue(item) for item in run_issues]
        for start in range(
            0,
            len(duplicate_rows),
            PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            duplicate_batch = duplicate_rows[
                start : start + PREPARATION_SESSION_ROW_BATCH_SIZE
            ]
            duplicate_counts_by_ordinal = dict(duplicate_batch)
            ordinals = [item[0] for item in duplicate_batch]
            rows_by_ordinal = self._direct_rows_by_ordinal(
                project_id,
                canonical_session_id,
                ordinals,
            )
            if len(rows_by_ordinal) != len(ordinals):
                raise WorkspaceError("Direct duplicate rows are incomplete")
            values: list[list[object]] = []
            for ordinal in ordinals:
                identity_count = duplicate_counts_by_ordinal[ordinal]
                row = rows_by_ordinal[ordinal]
                duplicate = Issue(
                    code="SOURCE_IDENTITY_DUPLICATE",
                    message=(
                        f"source identity {row.source_identity!r} occurs "
                        f"{int(identity_count)} times"
                    ),
                    dataset=row.dataset,
                    row=row.source_row,
                    affected_count=int(identity_count),
                )
                canonical_duplicate = CanonicalIssue.from_issue(duplicate)
                canonical_issues.append(canonical_duplicate)
                row = replace(
                    row,
                    disposition=StagingDisposition.BLOCKED,
                    issues=(*row.issues, canonical_duplicate),
                )
                validate_canonical_row_bindings(
                    row,
                    source_selection_hash=bindings.source_selection_hash,
                    mapping_hash=bindings.mapping_hash,
                    schema_hash=bindings.schema_hash,
                    derived_plan_hash=bindings.derived_plan_hash,
                )
                values.append(
                    [
                        int(ordinal),
                        row.disposition.value,
                        _canonical_json(row.to_portable_dict()),
                        True,
                    ]
                )
            self._update_direct_rows(
                project_id,
                canonical_session_id,
                values,
            )

        self._resolve_relationship_edges(
            project_id,
            canonical_session_id,
        )

        reconciliation, datasets = self._reconciliation(
            project_id,
            canonical_session_id,
            dataset_evidence,
        )
        controls = tuple(sorted(control_totals, key=lambda item: item.control_id))
        content_hash, row_count = self._hash_direct_run(
            project_id=project_id,
            run_id=canonical_session_id,
            bindings=bindings,
            datasets=datasets,
            issues=tuple(canonical_issues),
            reconciliation=reconciliation,
            control_totals=controls,
        )
        if row_count != summary.staged_row_count:
            raise WorkspaceError(
                "Direct preparation rows were not finalized completely"
            )
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.FINALIZING,
                )
                pending = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM canonical_staging_row
                     WHERE run_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if pending is None or int(pending[0]) != row_count:
                    raise WorkspaceError("Direct preparation rows are incomplete")
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, canonical_row_count = ?,
                           run_issues_json = ?, control_totals_json = ?,
                           reconciliation_json = ?,
                           dataset_reconciliation_json = ?,
                           impact_report_json = ?, updated_at = ?,
                           failure_code = NULL
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.READY.value,
                        row_count,
                        _canonical_json(
                            [item.to_portable_dict() for item in canonical_issues]
                        ),
                        _canonical_json([item.to_portable_dict() for item in controls]),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json([item.to_portable_dict() for item in datasets]),
                        _canonical_json(
                            transformation_report_to_portable_dict(impact_report)
                        ),
                        now,
                        canonical_session_id,
                    ],
                )
                updated = connection.execute(
                    """
                    UPDATE canonical_staging_run
                       SET content_hash = ?, row_count = ?,
                           run_issues_json = ?, reconciliation_json = ?,
                           dataset_reconciliation_json = ?,
                           control_totals_json = ?
                     WHERE run_id = ? AND status = ?
                    RETURNING run_id
                    """,
                    [
                        content_hash,
                        row_count,
                        _canonical_json(
                            [item.to_portable_dict() for item in canonical_issues]
                        ),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json([item.to_portable_dict() for item in datasets]),
                        _canonical_json([item.to_portable_dict() for item in controls]),
                        canonical_session_id,
                        StagingRunStatus.PENDING.value,
                    ],
                ).fetchone()
                if updated is None:
                    raise WorkspaceError("Direct preparation run is not pending")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.load_stored_run(project_id, canonical_session_id)

    def _resolve_relationship_edges(
        self,
        project_id: str,
        session_id: str,
    ) -> None:
        """Classify every incoming reference with one set-based parent join."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.FINALIZING,
                )
                connection.execute(
                    """
                    WITH matches AS (
                        SELECT edge.child_ordinal, edge.target_field,
                               edge.item_ordinal,
                               COUNT(parent_identity.ordinal) AS match_count,
                               MIN(parent_identity.ordinal) AS parent_ordinal,
                               MIN(parent.disposition) AS parent_disposition
                          FROM preparation_relationship_edge AS edge
                          LEFT JOIN preparation_direct_identity AS parent_identity
                            ON parent_identity.session_id = edge.session_id
                           AND parent_identity.dataset = edge.parent_dataset
                           AND parent_identity.identity_hash =
                               edge.parent_identity_hash
                          LEFT JOIN canonical_staging_row AS parent
                            ON parent.run_id = parent_identity.session_id
                           AND parent.ordinal = parent_identity.ordinal
                         WHERE edge.session_id = ?
                         GROUP BY edge.child_ordinal, edge.target_field,
                                  edge.item_ordinal
                    )
                    UPDATE preparation_relationship_edge AS edge
                       SET match_count = matches.match_count,
                           match_state = CASE
                               WHEN matches.match_count = 0 THEN 'MISSING'
                               WHEN matches.match_count = 1 THEN 'UNIQUE'
                               ELSE 'DUPLICATE'
                           END,
                           resolution_state = CASE
                               WHEN matches.match_count = 0 THEN 'MISSING'
                               WHEN matches.match_count > 1 THEN 'AMBIGUOUS'
                               WHEN matches.parent_disposition IN
                                    ('CANDIDATE', 'REFERENCE')
                                   THEN 'RESOLVED'
                               ELSE 'UNSAFE_PARENT'
                           END,
                           resolved_parent_ordinal = CASE
                               WHEN matches.match_count = 1
                                   THEN matches.parent_ordinal
                               ELSE NULL
                           END
                      FROM matches
                     WHERE edge.session_id = ?
                       AND edge.child_ordinal = matches.child_ordinal
                       AND edge.target_field = matches.target_field
                       AND edge.item_ordinal = matches.item_ordinal
                    """,
                    [session_id, session_id],
                )
                invalid = connection.execute(
                    """
                    SELECT 1
                      FROM preparation_relationship_edge AS edge
                      LEFT JOIN canonical_staging_row AS child
                        ON child.run_id = edge.session_id
                       AND child.ordinal = edge.child_ordinal
                     WHERE edge.session_id = ?
                       AND (
                           child.row_id IS NULL
                           OR edge.match_state = 'PENDING'
                           OR edge.resolution_state = 'PENDING'
                           OR (edge.match_state = 'UNIQUE') !=
                              (edge.resolved_parent_ordinal IS NOT NULL)
                       )
                     LIMIT 1
                    """,
                    [session_id],
                ).fetchone()
                if invalid is not None:
                    raise WorkspaceError("Prepared relationship facts are incomplete")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _restart_direct_finalization(
        self,
        project_id: str,
        session_id: str,
    ) -> None:
        """Rebuild compact identity groups and safely resume duplicate edits."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        while True:
            with self._connect(database_path) as connection:
                identity_batch = connection.execute(
                    """
                    SELECT ordinal, base_disposition
                      FROM preparation_direct_identity
                     WHERE session_id = ? AND finalized_duplicate
                     ORDER BY ordinal
                     LIMIT ?
                    """,
                    [session_id, PREPARATION_SESSION_ROW_BATCH_SIZE],
                ).fetchall()
            if not identity_batch:
                break
            base_by_ordinal = {
                int(ordinal): str(base_disposition)
                for ordinal, base_disposition in identity_batch
            }
            ordinals = list(base_by_ordinal)
            rows_by_ordinal = self._direct_rows_by_ordinal(
                project_id,
                session_id,
                ordinals,
            )
            if len(rows_by_ordinal) != len(ordinals):
                raise WorkspaceError("Direct duplicate rows are incomplete")
            values: list[list[object]] = []
            for ordinal in ordinals:
                row = replace(
                    rows_by_ordinal[ordinal],
                    disposition=StagingDisposition(base_by_ordinal[ordinal]),
                    issues=tuple(
                        issue
                        for issue in rows_by_ordinal[ordinal].issues
                        if issue.code != "SOURCE_IDENTITY_DUPLICATE"
                    ),
                )
                values.append(
                    [
                        int(ordinal),
                        row.disposition.value,
                        _canonical_json(row.to_portable_dict()),
                        False,
                    ]
                )
            self._update_direct_rows(project_id, session_id, values)

        with self._connect(database_path) as connection:
            connection.begin()
            try:
                status = connection.execute(
                    "SELECT status FROM preparation_session WHERE session_id = ?",
                    [session_id],
                ).fetchone()
                if status is None or str(status[0]) not in {
                    PreparationSessionStatus.BUILDING.value,
                    PreparationSessionStatus.FINALIZING.value,
                }:
                    raise WorkspaceError("Preparation session cannot be finalized")
                connection.execute(
                    "DELETE FROM preparation_identity_group WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    """
                    INSERT INTO preparation_identity_group (
                        session_id, dataset, identity_hash, identity_count
                    )
                    SELECT session_id, dataset, identity_hash, COUNT(*)
                      FROM preparation_direct_identity
                     WHERE session_id = ?
                     GROUP BY session_id, dataset, identity_hash
                    """,
                    [session_id],
                )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, canonical_row_count = 0,
                           updated_at = ?, failure_code = NULL
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.FINALIZING.value,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _update_direct_rows(
        self,
        project_id: str,
        run_id: str,
        values: Sequence[Sequence[object]],
    ) -> None:
        if not values:
            return
        overlay_rows: list[list[object]] = []
        ordinals = [int(item[0]) for item in values]
        for ordinal, _disposition, row_text, finalized in values:
            if not bool(finalized):
                continue
            try:
                row = CanonicalRow.from_dict(json.loads(str(row_text)))
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Stored direct preparation row is invalid"
                ) from error
            duplicate_issues = tuple(
                issue
                for issue in row.issues
                if issue.code == "SOURCE_IDENTITY_DUPLICATE"
            )
            if len(duplicate_issues) != 1:
                raise WorkspaceError("Direct duplicate issue evidence is invalid")
            overlay_rows.append(
                [
                    run_id,
                    int(ordinal),
                    0,
                    _canonical_json(duplicate_issues[0].to_portable_dict()),
                ]
            )
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                connection.execute(
                    """
                    UPDATE canonical_staging_row AS target
                       SET disposition = updates.disposition,
                           row_json = CASE
                               WHEN target.row_json = '' THEN ''
                               ELSE updates.row_json
                           END
                      FROM (
                          SELECT unnest(?) AS ordinal,
                                 unnest(?) AS disposition,
                                 unnest(?) AS row_json,
                                 unnest(?) AS finalized_duplicate
                      ) AS updates
                     WHERE target.run_id = ?
                       AND target.ordinal = updates.ordinal
                    """,
                    [*_columnar_parameters(values), run_id],
                )
                connection.execute(
                    """
                    DELETE FROM canonical_staging_row_issue
                     WHERE run_id = ? AND issue_ordinal = 0
                       AND ordinal IN (SELECT unnest(?))
                    """,
                    [run_id, ordinals],
                )
                if overlay_rows:
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row_issue
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(overlay_rows),
                    )
                connection.execute(
                    """
                    UPDATE preparation_direct_identity AS target
                       SET finalized_duplicate = updates.finalized_duplicate
                      FROM (
                          SELECT unnest(?) AS ordinal,
                                 unnest(?) AS disposition,
                                 unnest(?) AS row_json,
                                 unnest(?) AS finalized_duplicate
                      ) AS updates
                     WHERE target.session_id = ?
                       AND target.ordinal = updates.ordinal
                    """,
                    [*_columnar_parameters(values), run_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def load_stored_run(
        self,
        project_id: str,
        session_id: str,
    ) -> StoredCanonicalStagingRun:
        """Return a READY header backed by bounded durable row slices."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT status, mapping_id, physical_selection_hash,
                       source_selection_hash, mapping_hash, schema_hash,
                       derived_plan_hash, compiled_plan_hash,
                       contract_version, evaluator_version,
                       run_issues_json, reconciliation_json,
                       dataset_reconciliation_json, control_totals_json,
                       canonical_row_count
                  FROM preparation_session
                 WHERE session_id = ?
                """,
                [self._session_id(session_id)],
            ).fetchone()
            direct = connection.execute(
                """
                SELECT content_hash
                  FROM canonical_staging_run
                 WHERE run_id = ?
                """,
                [self._session_id(session_id)],
            ).fetchone()
        if row is None:
            raise WorkspaceError("Preparation session was not found")
        if direct is None:
            raise WorkspaceError("Preparation session has no canonical run")
        if PreparationSessionStatus(str(row[0])) is not PreparationSessionStatus.READY:
            raise WorkspaceError("Preparation session is not ready")
        try:
            issues = tuple(
                CanonicalIssue.from_dict(item) for item in json.loads(str(row[10]))
            )
            reconciliation = StagingReconciliation.from_dict(json.loads(str(row[11])))
            datasets = tuple(
                StagingDatasetReconciliation.from_dict(item)
                for item in json.loads(str(row[12]))
            )
            controls = tuple(
                CanonicalControlTotal.from_dict(item)
                for item in json.loads(str(row[13]))
            )
            row_count = int(row[14])
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Preparation session header is invalid") from error
        return StoredCanonicalStagingRun(
            project_id=project_id,
            mapping_id=str(row[1]),
            physical_selection_hash=str(row[2]),
            source_selection_hash=str(row[3]),
            mapping_hash=str(row[4]),
            schema_hash=str(row[5]),
            derived_plan_hash=str(row[6]) if row[6] else None,
            datasets=datasets,
            rows=_SessionCanonicalRows(
                self,
                project_id,
                session_id,
                row_count,
            ),
            issues=issues,
            reconciliation=reconciliation,
            compiled_plan_hash=str(row[7]),
            control_totals=controls,
            evaluator_version=int(row[9]),
            contract_version=int(row[8]),
            validated_content_hash=str(direct[0]),
        )

    def get_session(
        self,
        project_id: str,
        session_id: str,
    ) -> PreparationSessionSummary:
        """Return one value-free session status projection."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT status, mapping_id, mapping_version,
                       physical_selection_hash, source_selection_hash,
                       mapping_hash, schema_hash, derived_plan_hash,
                       compiled_plan_hash, contract_version,
                       evaluator_version, source_hashes_json,
                       staged_row_count, canonical_row_count,
                       impact_row_count, failure_code
                  FROM preparation_session
                 WHERE session_id = ?
                """,
                [self._session_id(session_id)],
            ).fetchone()
        if row is None:
            raise WorkspaceError("Preparation session was not found")
        try:
            bindings = PreparationSessionBindings(
                mapping_id=str(row[1]),
                mapping_version=int(row[2]),
                physical_selection_hash=str(row[3]),
                source_selection_hash=str(row[4]),
                mapping_hash=str(row[5]),
                schema_hash=str(row[6]),
                derived_plan_hash=str(row[7]) if row[7] else None,
                compiled_plan_hash=str(row[8]),
                contract_version=int(row[9]),
                evaluator_version=int(row[10]),
                source_hashes={
                    str(key): str(value)
                    for key, value in json.loads(str(row[11])).items()
                },
            )
            return PreparationSessionSummary(
                session_id=session_id,
                status=PreparationSessionStatus(str(row[0])),
                bindings=bindings,
                staged_row_count=int(row[12]),
                canonical_row_count=int(row[13]),
                impact_row_count=int(row[14]),
                failure_code=str(row[15]) if row[15] else None,
            )
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Preparation session header is invalid") from error

    def physical_rows(
        self,
        project_id: str,
        session_id: str,
    ) -> dict[str, tuple[int, ...]]:
        """Load compact source-row coordinates required by current quality APIs."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            cursor = connection.execute(
                """
                SELECT physical_dataset_id, source_row
                  FROM preparation_physical_row
                 WHERE session_id = ?
                 ORDER BY physical_dataset_id, source_row
                """,
                [self._session_id(session_id)],
            )
            grouped: dict[str, list[int]] = {}
            while batch := cursor.fetchmany(PREPARATION_SESSION_ROW_BATCH_SIZE):
                for physical_dataset_id, source_row in batch:
                    grouped.setdefault(str(physical_dataset_id), []).append(
                        int(source_row)
                    )
        return {dataset_id: tuple(rows) for dataset_id, rows in grouped.items()}

    def _bounded_quality_index(
        self,
        project_id: str,
        session_id: str,
        physical_rows: Mapping[str, Sequence[int]],
    ) -> dict[str, object] | None:
        """Validate a direct prepared run using set-based narrow relations."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            header = connection.execute(
                """
                SELECT COUNT(*), COUNT(*) FILTER (WHERE row_json = ''),
                       COUNT(DISTINCT row_id),
                       COUNT(*) FILTER (WHERE disposition = 'CANDIDATE'),
                       COUNT(*) FILTER (WHERE disposition = 'REFERENCE'),
                       COUNT(*) FILTER (WHERE disposition = 'BLOCKED'),
                       COUNT(*) FILTER (WHERE disposition = 'QUARANTINED'),
                       COUNT(*) FILTER (WHERE disposition = 'EXCLUDED')
                  FROM canonical_staging_row
                 WHERE run_id = ?
                """,
                [session_id],
            ).fetchone()
            if header is None:
                return None
            row_count = int(header[0])
            if (
                row_count == 0
                or int(header[1]) not in {0, row_count}
                or int(header[2]) != row_count
            ):
                return None
            invalid_order = connection.execute(
                """
                SELECT 1
                  FROM (
                    SELECT ordinal, dataset, source_row, row_id,
                           LAG(ordinal) OVER (ORDER BY ordinal) AS prior_ordinal,
                           LAG(dataset) OVER (ORDER BY ordinal) AS prior_dataset,
                           LAG(source_row) OVER (ORDER BY ordinal) AS prior_source,
                           LAG(row_id) OVER (ORDER BY ordinal) AS prior_row_id
                      FROM canonical_staging_row
                     WHERE run_id = ?
                  ) AS ordered
                 WHERE ordinal != COALESCE(prior_ordinal + 1, 0)
                    OR (prior_ordinal IS NOT NULL AND
                        (dataset, source_row, row_id) <
                        (prior_dataset, prior_source, prior_row_id))
                 LIMIT 1
                """,
                [session_id],
            ).fetchone()
            if invalid_order is not None:
                return None
            lineage_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM preparation_physical_row
                      WHERE session_id = ?),
                    (SELECT COUNT(*) FROM preparation_lineage
                      WHERE session_id = ?),
                    (SELECT COUNT(*)
                       FROM canonical_staging_row AS row
                       JOIN preparation_lineage AS lineage
                         ON lineage.session_id = row.run_id
                        AND lineage.dataset = row.dataset
                        AND lineage.output_source_row = row.source_row
                      WHERE row.run_id = ?),
                    (SELECT COUNT(*) FROM (
                         SELECT dataset, output_source_row
                           FROM preparation_lineage WHERE session_id = ?
                          GROUP BY dataset, output_source_row
                         HAVING COUNT(*) != 1)),
                    (SELECT COUNT(*) FROM (
                         SELECT physical_dataset_id, physical_source_row
                           FROM preparation_lineage WHERE session_id = ?
                          GROUP BY physical_dataset_id, physical_source_row
                         HAVING COUNT(*) != 1))
                """,
                [session_id, session_id, session_id, session_id, session_id],
            ).fetchone()
            expected_physical = sum(len(rows) for rows in physical_rows.values())
            stored_datasets = {
                str(item[0]): int(item[1])
                for item in connection.execute(
                    """
                    SELECT physical_dataset_id, COUNT(*)
                      FROM preparation_physical_row
                     WHERE session_id = ?
                     GROUP BY physical_dataset_id
                    """,
                    [session_id],
                ).fetchall()
            }
            expected_datasets = {
                dataset_id: len(rows) for dataset_id, rows in physical_rows.items()
            }
            if (
                lineage_counts is None
                or expected_physical != row_count
                or stored_datasets != expected_datasets
                or any(int(value) != row_count for value in lineage_counts[:3])
                or int(lineage_counts[3])
                or int(lineage_counts[4])
            ):
                return None
            issue_rows = connection.execute(
                """
                SELECT issue.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, issue.issue_json,
                       lineage.physical_dataset_id,
                       lineage.physical_source_row
                  FROM canonical_staging_row_issue AS issue
                  JOIN canonical_staging_row AS row
                   ON row.run_id = issue.run_id
                   AND row.ordinal = issue.ordinal
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id
                   AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE issue.run_id = ?
                 ORDER BY issue.ordinal, issue.issue_ordinal
                """,
                [session_id],
            ).fetchall()
            collisions = connection.execute(
                """
                WITH collision AS (
                    SELECT quality_identity_key, COUNT(*) AS identity_count
                      FROM canonical_staging_row
                     WHERE run_id = ? AND quality_identity_key IS NOT NULL
                     GROUP BY quality_identity_key
                    HAVING COUNT(*) > 1
                )
                SELECT row.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, collision.identity_count,
                       lineage.physical_dataset_id,
                       lineage.physical_source_row
                  FROM canonical_staging_row AS row
                  JOIN collision USING (quality_identity_key)
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id
                   AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE row.run_id = ?
                 ORDER BY row.ordinal
                """,
                [session_id, session_id],
            ).fetchall()
            exception_rows = connection.execute(
                """
                SELECT row.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, lineage.physical_dataset_id,
                       lineage.physical_source_row
                  FROM canonical_staging_row AS row
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id
                   AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE row.run_id = ?
                   AND row.disposition NOT IN ('CANDIDATE', 'REFERENCE')
                 ORDER BY row.ordinal
                """,
                [session_id],
            ).fetchall()
            return {
                "row_count": row_count,
                "disposition_counts": {
                    "CANDIDATE": int(header[3]),
                    "REFERENCE": int(header[4]),
                    "BLOCKED": int(header[5]),
                    "QUARANTINED": int(header[6]),
                    "EXCLUDED": int(header[7]),
                },
                "issue_rows": tuple(issue_rows),
                "collisions": tuple(collisions),
                "exception_rows": tuple(exception_rows),
            }

    def _bounded_relationship_findings(
        self,
        project_id: str,
        session_id: str,
        unsafe_row_ids: Sequence[str],
        propagating_datasets: Sequence[str],
    ) -> tuple[tuple[object, ...], ...]:
        """Return only affected children after one recursive set operation."""

        canonical_session_id = self._session_id(session_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            relationship_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM preparation_relationship_edge
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()[0]
            )
            if relationship_count == 0:
                return ()
            connection.begin()
            try:
                connection.execute(
                    """
                    CREATE TEMP TABLE relationship_initial_unsafe (
                        row_id VARCHAR PRIMARY KEY
                    )
                    """
                )
                if unsafe_row_ids:
                    connection.execute(
                        """
                        INSERT INTO relationship_initial_unsafe
                        SELECT DISTINCT CAST(unnest(?) AS VARCHAR)
                        """,
                        [list(unsafe_row_ids)],
                    )
                connection.execute(
                    """
                    CREATE TEMP TABLE relationship_propagating_dataset (
                        dataset VARCHAR PRIMARY KEY
                    )
                    """
                )
                if propagating_datasets:
                    connection.execute(
                        """
                        INSERT INTO relationship_propagating_dataset
                        SELECT DISTINCT CAST(unnest(?) AS VARCHAR)
                        """,
                        [list(propagating_datasets)],
                    )
                connection.execute(
                    """
                    UPDATE preparation_relationship_edge
                       SET resolution_state = CASE
                           WHEN match_state = 'MISSING' THEN 'MISSING'
                           WHEN match_state = 'DUPLICATE' THEN 'AMBIGUOUS'
                           WHEN match_state = 'UNIQUE' THEN 'RESOLVED'
                           ELSE 'PENDING'
                       END
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                )
                connection.execute(
                    """
                    CREATE TEMP TABLE relationship_unsafe AS
                    WITH RECURSIVE unsafe(ordinal) AS (
                        SELECT row.ordinal
                          FROM canonical_staging_row AS row
                          JOIN relationship_initial_unsafe AS initial
                            ON initial.row_id = row.row_id
                         WHERE row.run_id = ?
                        UNION
                        SELECT edge.child_ordinal
                          FROM preparation_relationship_edge AS edge
                          JOIN canonical_staging_row AS child
                            ON child.run_id = edge.session_id
                           AND child.ordinal = edge.child_ordinal
                          JOIN relationship_propagating_dataset AS allowed
                            ON allowed.dataset = child.dataset
                         WHERE edge.session_id = ?
                           AND edge.resolution_state IN ('MISSING', 'AMBIGUOUS')
                        UNION
                        SELECT edge.child_ordinal
                          FROM preparation_relationship_edge AS edge
                          JOIN unsafe AS parent
                            ON parent.ordinal = edge.resolved_parent_ordinal
                          JOIN canonical_staging_row AS child
                            ON child.run_id = edge.session_id
                           AND child.ordinal = edge.child_ordinal
                         WHERE edge.session_id = ?
                           AND edge.match_state = 'UNIQUE'
                           AND child.dataset IN (
                               SELECT dataset
                                 FROM relationship_propagating_dataset
                           )
                    )
                    SELECT DISTINCT ordinal FROM unsafe
                    """,
                    [
                        canonical_session_id,
                        canonical_session_id,
                        canonical_session_id,
                    ],
                )
                connection.execute(
                    """
                    UPDATE preparation_relationship_edge AS edge
                       SET resolution_state = CASE
                           WHEN edge.match_state = 'MISSING' THEN 'MISSING'
                           WHEN edge.match_state = 'DUPLICATE' THEN 'AMBIGUOUS'
                           WHEN parent.ordinal IS NOT NULL THEN 'UNSAFE_PARENT'
                           ELSE 'RESOLVED'
                       END
                      FROM (
                          SELECT ordinal FROM relationship_unsafe
                      ) AS parent
                     WHERE edge.session_id = ?
                       AND edge.match_state = 'UNIQUE'
                       AND edge.resolved_parent_ordinal = parent.ordinal
                    """,
                    [canonical_session_id],
                )
                findings = tuple(
                    connection.execute(
                        """
                        SELECT child.ordinal, child.row_id, child.dataset,
                               child.source_row, child.disposition,
                               lineage.physical_dataset_id,
                               lineage.physical_source_row,
                               MIN(edge.resolution_state) AS resolution_state
                          FROM preparation_relationship_edge AS edge
                          JOIN canonical_staging_row AS child
                            ON child.run_id = edge.session_id
                           AND child.ordinal = edge.child_ordinal
                          JOIN preparation_lineage AS lineage
                            ON lineage.session_id = child.run_id
                           AND lineage.dataset = child.dataset
                           AND lineage.output_source_row = child.source_row
                         WHERE edge.session_id = ?
                           AND edge.resolution_state != 'RESOLVED'
                         GROUP BY child.ordinal, child.row_id, child.dataset,
                                  child.source_row, child.disposition,
                                  lineage.physical_dataset_id,
                                  lineage.physical_source_row
                         ORDER BY child.ordinal
                        """,
                        [canonical_session_id],
                    ).fetchall()
                )
                connection.commit()
                return findings
            except Exception:
                connection.rollback()
                raise

    def _iter_quality_index_batches(
        self,
        project_id: str,
        session_id: str,
        *,
        batch_size: int,
        connection=None,
    ):
        if connection is None:
            database_path = self.project_directory(project_id) / "project.duckdb"
            with self._connect(database_path) as owned:
                yield from self._iter_quality_index_batches(
                    project_id,
                    session_id,
                    batch_size=batch_size,
                    connection=owned,
                )
            return
        next_ordinal = 0
        while batch := connection.execute(
            """
            SELECT ordinal, row_id, dataset, source_row, record_label,
                   disposition
              FROM canonical_staging_row
             WHERE run_id = ? AND ordinal >= ?
             ORDER BY ordinal LIMIT ?
            """,
            [session_id, next_ordinal, batch_size],
        ).fetchall():
            yield batch
            next_ordinal += len(batch)

    def _iter_accounting_index_batches(
        self,
        project_id: str,
        session_id: str,
        *,
        batch_size: int,
        connection=None,
    ):
        if connection is None:
            database_path = self.project_directory(project_id) / "project.duckdb"
            with self._connect(database_path) as owned:
                yield from self._iter_accounting_index_batches(
                    project_id,
                    session_id,
                    batch_size=batch_size,
                    connection=owned,
                )
            return
        offset = 0
        while batch := connection.execute(
            """
            SELECT lineage.physical_dataset_id,
                   lineage.physical_source_row, row.row_id
              FROM preparation_lineage AS lineage
              JOIN canonical_staging_row AS row
                ON row.run_id = lineage.session_id
               AND row.dataset = lineage.dataset
               AND row.source_row = lineage.output_source_row
             WHERE lineage.session_id = ?
             ORDER BY lineage.physical_dataset_id,
                      lineage.physical_source_row
             LIMIT ? OFFSET ?
            """,
            [session_id, batch_size, offset],
        ).fetchall():
            yield batch
            offset += len(batch)

    def _direct_index_contains_row_id(
        self,
        project_id: str,
        session_id: str,
        row_id: str,
    ) -> bool:
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            return (
                connection.execute(
                    """
                SELECT 1 FROM canonical_staging_row
                 WHERE run_id = ? AND row_id = ? LIMIT 1
                """,
                    [session_id, row_id],
                ).fetchone()
                is not None
            )

    def iter_impacts(
        self,
        project_id: str,
        session_id: str,
    ) -> _SessionImpacts:
        """Return a replayable bounded view of persisted transformation impacts."""

        return _SessionImpacts(self, project_id, self._session_id(session_id))

    def _iter_impacts(
        self,
        project_id: str,
        session_id: str,
    ) -> Iterator[TransformationImpactRow]:
        """Yield persisted impacts in deterministic bounded ordinal pages."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            next_ordinal = 0
            while batch := connection.execute(
                """
                SELECT ordinal, impact_json
                  FROM preparation_impact_row
                 WHERE session_id = ?
                   AND ordinal >= ?
                 ORDER BY ordinal
                 LIMIT ?
                """,
                [
                    self._session_id(session_id),
                    next_ordinal,
                    PREPARATION_SESSION_ROW_BATCH_SIZE,
                ],
            ).fetchall():
                for ordinal, row_text in batch:
                    if int(ordinal) != next_ordinal:
                        raise WorkspaceError(
                            "Stored preparation impacts are not contiguous"
                        )
                    next_ordinal += 1
                    try:
                        yield transformation_impact_from_portable_dict(
                            json.loads(str(row_text))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored preparation impact is invalid"
                        ) from error

    def _prepare_normalization_facts(
        self,
        project_id: str,
        session_id: str,
        *,
        effect_builder,
        finding_builder,
    ) -> dict[str, object]:
        """Construct every effect once and summarize the durable fact ledger."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        canonical_session_id = self._session_id(session_id)
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.READY,
                )
                quality_binding = connection.execute(
                    """
                    SELECT quality.run_id,
                           projection.run_id IS NOT NULL
                      FROM quality_current AS current
                      JOIN quality_run AS quality
                        ON quality.run_id = current.run_id
                       AND quality.status = 'PUBLISHED'
                      LEFT JOIN quality_evidence_projection AS projection
                        ON projection.run_id = quality.run_id
                     WHERE current.singleton_id = 1
                       AND quality.staging_content_hash = (
                           SELECT content_hash
                             FROM canonical_staging_run
                            WHERE run_id = ?
                       )
                    """,
                    [canonical_session_id],
                ).fetchone()
                if quality_binding is None:
                    raise WorkspaceError(
                        "Prepared changes do not match the current data checks"
                    )
                quality_run_id = str(quality_binding[0])
                sparse_quality = bool(quality_binding[1])
                connection.execute(
                    """
                    DELETE FROM normalization_effect
                     WHERE run_id = ?
                       AND NOT EXISTS (
                           SELECT 1 FROM normalization_run WHERE run_id = ?
                       )
                    """,
                    [canonical_session_id, canonical_session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_normalization_group_seed WHERE session_id = ?",
                    [canonical_session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_normalization_finding WHERE session_id = ?",
                    [canonical_session_id],
                )
                connection.execute(
                    """
                    CREATE TEMP TABLE normalization_pending_effect (
                        run_id VARCHAR NOT NULL,
                        ordinal BIGINT NOT NULL,
                        effect_id VARCHAR PRIMARY KEY,
                        group_id VARCHAR NOT NULL,
                        row_id VARCHAR NOT NULL,
                        dataset VARCHAR NOT NULL,
                        source_row BIGINT NOT NULL,
                        target_field VARCHAR NOT NULL,
                        eligible BOOLEAN NOT NULL,
                        effect_json VARCHAR NOT NULL
                    )
                    """
                )
                construction_ordinal = 0
                pending_effects: list[list[object]] = []
                pending_group_seeds: list[list[object]] = []
                bound_rows = self._iter_bound_impacts_with_eligibility(
                    connection,
                    canonical_session_id,
                    quality_run_id,
                    sparse_quality=sparse_quality,
                    batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
                )
                for effect, metadata in effect_builder(bound_rows):
                    kind = getattr(metadata["kind"], "value", metadata["kind"])
                    outcome = getattr(
                        metadata["outcome"],
                        "value",
                        metadata["outcome"],
                    )
                    metadata_values = {
                        "dataset": str(metadata["dataset"]),
                        "explanation": str(metadata["explanation"]),
                        "kind": str(kind),
                        "name": str(metadata["name"]),
                        "outcome": str(outcome),
                        "owner_label": str(metadata["owner_label"]),
                        "rule_id": str(metadata["rule_id"]),
                        "target_field": str(metadata["target_field"]),
                    }
                    metadata_hash = (
                        "sha256:"
                        + sha256(
                            _canonical_json(metadata_values).encode("utf-8")
                        ).hexdigest()
                    )
                    pending_effects.append(
                        [
                            canonical_session_id,
                            construction_ordinal,
                            effect.effect_id,
                            effect.group_id,
                            effect.row_id,
                            effect.dataset,
                            effect.source_row,
                            effect.target_field,
                            effect.eligible,
                            _canonical_json(effect.to_portable_dict()),
                        ]
                    )
                    pending_group_seeds.append(
                        [
                            canonical_session_id,
                            effect.group_id,
                            metadata_hash,
                            metadata_values["dataset"],
                            metadata_values["target_field"],
                            metadata_values["rule_id"],
                            metadata_values["kind"],
                            metadata_values["outcome"],
                            metadata_values["name"],
                            metadata_values["explanation"],
                            metadata_values["owner_label"],
                        ]
                    )
                    construction_ordinal += 1
                    if len(pending_effects) >= PREPARATION_SESSION_ROW_BATCH_SIZE:
                        self._insert_prepared_normalization_effects(
                            connection,
                            pending_effects,
                        )
                        self._insert_prepared_normalization_group_seeds(
                            connection,
                            pending_group_seeds,
                        )
                        pending_effects.clear()
                        pending_group_seeds.clear()
                if pending_effects:
                    self._insert_prepared_normalization_effects(
                        connection,
                        pending_effects,
                    )
                    self._insert_prepared_normalization_group_seeds(
                        connection,
                        pending_group_seeds,
                    )

                pending_findings: list[list[object]] = []
                warning_issues = self._iter_eligible_warning_issues(
                    connection,
                    canonical_session_id,
                    quality_run_id,
                    sparse_quality=sparse_quality,
                    batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
                )
                for group_id, issue_id, row_id, metadata in finding_builder(
                    warning_issues
                ):
                    kind = getattr(metadata["kind"], "value", metadata["kind"])
                    outcome = getattr(
                        metadata["outcome"],
                        "value",
                        metadata["outcome"],
                    )
                    pending_findings.append(
                        [
                            canonical_session_id,
                            issue_id,
                            group_id,
                            row_id,
                            str(metadata["rule_id"]),
                            str(kind),
                            str(outcome),
                            str(metadata["dataset"]),
                            str(metadata["target_field"]),
                            str(metadata["name"]),
                            str(metadata["explanation"]),
                            str(metadata["owner_label"]),
                        ]
                    )
                    if len(pending_findings) >= PREPARATION_SESSION_ROW_BATCH_SIZE:
                        self._insert_prepared_normalization_findings(
                            connection,
                            pending_findings,
                        )
                        pending_findings.clear()
                if pending_findings:
                    self._insert_prepared_normalization_findings(
                        connection,
                        pending_findings,
                    )

                conflict = connection.execute(
                    """
                    SELECT group_id
                      FROM preparation_normalization_group_seed
                     WHERE session_id = ?
                     GROUP BY group_id
                    HAVING COUNT(*) > 1
                     LIMIT 1
                    """,
                    [canonical_session_id],
                ).fetchone()
                if conflict is not None:
                    raise WorkspaceError(
                        "Prepared normalization group metadata is inconsistent"
                    )

                totals = connection.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(DISTINCT row_id) FILTER (WHERE eligible),
                           (
                               SELECT COUNT(DISTINCT rule_id)
                                 FROM preparation_normalization_group_seed
                                WHERE session_id = ?
                           ),
                           COUNT(DISTINCT group_id),
                           COUNT(DISTINCT (dataset, source_row))
                      FROM normalization_pending_effect
                     WHERE run_id = ?
                    """,
                    [canonical_session_id, canonical_session_id],
                ).fetchone()
                effect_groups = connection.execute(
                    """
                    SELECT seed.group_id, seed.rule_id, seed.kind,
                           seed.outcome, seed.dataset, seed.target_field,
                           seed.name, seed.explanation, seed.owner_label,
                           COUNT(*) FILTER (WHERE effect.eligible),
                           COUNT(*) FILTER (WHERE NOT effect.eligible)
                      FROM preparation_normalization_group_seed AS seed
                      JOIN normalization_pending_effect AS effect
                        ON effect.run_id = seed.session_id
                       AND effect.group_id = seed.group_id
                     WHERE seed.session_id = ?
                     GROUP BY seed.group_id, seed.rule_id, seed.kind,
                              seed.outcome, seed.dataset, seed.target_field,
                              seed.name, seed.explanation, seed.owner_label
                     ORDER BY seed.group_id
                    """,
                    [canonical_session_id],
                ).fetchall()
                example_rows = connection.execute(
                    """
                    WITH top_effect AS (
                        SELECT run_id, group_id,
                               UNNEST(
                                   arg_min(
                                       effect_id,
                                       struct_pack(
                                           source_row := source_row,
                                           row_id := row_id,
                                           effect_id := effect_id
                                       ),
                                       5
                                   )
                               ) AS effect_id
                          FROM normalization_pending_effect
                         WHERE run_id = ? AND eligible
                         GROUP BY run_id, group_id
                    )
                    SELECT effect.group_id, effect.source_row,
                           json_extract_string(effect.effect_json, '$.before'),
                           json_extract_string(effect.effect_json, '$.after')
                      FROM top_effect
                      JOIN normalization_pending_effect AS effect
                        ON effect.run_id = top_effect.run_id
                       AND effect.group_id = top_effect.group_id
                       AND effect.effect_id = top_effect.effect_id
                     ORDER BY effect.group_id, effect.source_row,
                              effect.row_id, effect.effect_id
                    """,
                    [canonical_session_id],
                ).fetchall()
                finding_groups = connection.execute(
                    """
                    SELECT group_id,
                           arg_min(rule_id, issue_id),
                           arg_min(kind, issue_id),
                           arg_min(outcome, issue_id),
                           arg_min(dataset, issue_id),
                           arg_min(target_field, issue_id),
                           arg_min(name, issue_id),
                           arg_min(explanation, issue_id),
                           arg_min(owner_label, issue_id),
                           COUNT(*)
                      FROM preparation_normalization_finding
                     WHERE session_id = ?
                     GROUP BY group_id
                     ORDER BY group_id
                    """,
                    [canonical_session_id],
                ).fetchall()
                connection.execute(
                    """
                    INSERT INTO normalization_effect
                    SELECT run_id, ordinal, effect_id, group_id, row_id,
                           dataset, source_row, target_field, eligible,
                           effect_json
                      FROM normalization_pending_effect
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        examples: dict[str, list[tuple[int, str, str]]] = {}
        for group_id, source_row, before, after in example_rows:
            examples.setdefault(str(group_id), []).append(
                (int(source_row), str(before), str(after))
            )
        return {
            "effect_count": int(totals[0]),
            "changed_record_count": int(totals[1]),
            "distinct_rule_count": int(totals[2]),
            "distinct_group_count": int(totals[3]),
            "distinct_source_count": int(totals[4]),
            "effect_groups": tuple(effect_groups),
            "finding_groups": tuple(finding_groups),
            "examples": {
                group_id: tuple(items) for group_id, items in examples.items()
            },
        }

    @staticmethod
    def _insert_prepared_normalization_effects(connection, rows) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO normalization_pending_effect
            SELECT
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS BIGINT), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS BOOLEAN), CAST(unnest(?) AS VARCHAR)
            """,
            _columnar_parameters(rows),
        )

    @staticmethod
    def _insert_prepared_normalization_group_seeds(connection, rows) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO preparation_normalization_group_seed
            SELECT
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR)
            """,
            _columnar_parameters(rows),
        )

    @staticmethod
    def _insert_prepared_normalization_findings(connection, rows) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO preparation_normalization_finding
            SELECT
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR)
            """,
            _columnar_parameters(rows),
        )

    def _iter_bound_impacts_with_eligibility(
        self,
        connection,
        session_id: str,
        quality_run_id: str,
        *,
        sparse_quality: bool,
        batch_size: int,
    ):
        """Yield impact, row ID, and eligibility from one set-based join."""

        if sparse_quality:
            disposition = (
                "COALESCE(exception.effective_disposition, canonical.disposition)"
            )
            quality_join = """
                LEFT JOIN quality_row_result AS exception
                  ON exception.run_id = ? AND exception.row_id = canonical.row_id
            """
        else:
            disposition = "quality.effective_disposition"
            quality_join = """
                JOIN quality_row_result AS quality
                  ON quality.run_id = ? AND quality.row_id = canonical.row_id
            """
        next_ordinal = 0
        while batch := connection.execute(
            f"""
            SELECT impact.ordinal, impact.impact_json, canonical.row_id,
                   {disposition} IN ('CANDIDATE', 'REFERENCE') AS eligible
              FROM preparation_impact_row AS impact
              JOIN canonical_staging_row AS canonical
                ON canonical.run_id = impact.session_id
               AND canonical.dataset = impact.dataset
               AND canonical.source_row = impact.source_row
              {quality_join}
             WHERE impact.session_id = ?
               AND impact.ordinal >= ?
             ORDER BY impact.ordinal
             LIMIT ?
            """,
            [quality_run_id, session_id, next_ordinal, batch_size],
        ).fetchall():
            for ordinal, row_text, row_id, eligible in batch:
                if int(ordinal) != next_ordinal:
                    raise WorkspaceError(
                        "Prepared changes do not match one canonical row"
                    )
                next_ordinal += 1
                try:
                    impact = transformation_impact_from_portable_dict(
                        json.loads(str(row_text))
                    )
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Stored preparation impact is invalid"
                    ) from error
                yield impact, str(row_id), bool(eligible)

    def _iter_eligible_warning_issues(
        self,
        connection,
        session_id: str,
        quality_run_id: str,
        *,
        sparse_quality: bool,
        batch_size: int,
    ):
        """Yield eligible row-level warnings without building a row-ID set."""

        if sparse_quality:
            disposition = (
                "COALESCE(exception.effective_disposition, canonical.disposition)"
            )
            quality_join = """
                LEFT JOIN quality_row_result AS exception
                  ON exception.run_id = ? AND exception.row_id = canonical.row_id
            """
        else:
            disposition = "quality.effective_disposition"
            quality_join = """
                JOIN quality_row_result AS quality
                  ON quality.run_id = ? AND quality.row_id = canonical.row_id
            """
        last_issue_id = ""
        while batch := connection.execute(
            f"""
            SELECT issue.issue_id, issue.issue_json
              FROM quality_issue AS issue
              JOIN canonical_staging_row AS canonical
                ON canonical.run_id = ? AND canonical.row_id = issue.row_id
              {quality_join}
             WHERE issue.run_id = ?
               AND issue.policy = 'WARNING'
               AND issue.row_id IS NOT NULL
               AND {disposition} IN ('CANDIDATE', 'REFERENCE')
               AND issue.issue_id > ?
             ORDER BY issue.issue_id
             LIMIT ?
            """,
            [
                session_id,
                quality_run_id,
                quality_run_id,
                last_issue_id,
                batch_size,
            ],
        ).fetchall():
            for issue_id, issue_json in batch:
                last_issue_id = str(issue_id)
                try:
                    yield QualityIssue.from_dict(json.loads(str(issue_json)))
                except (TypeError, ValueError) as error:
                    raise WorkspaceError("Stored quality finding is invalid") from error

    @staticmethod
    def _copy_normalization_effects_to_run(
        connection,
        session_id: str,
        run_id: str,
    ) -> int:
        """Verify publication reuses the already-constructed run facts."""

        if run_id != session_id:
            raise WorkspaceError(
                "Prepared normalization facts use a different run identifier"
            )
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM normalization_effect WHERE run_id = ?",
                [run_id],
            ).fetchone()[0]
        )

    @staticmethod
    def _iter_normalization_effect_json_batches(
        connection,
        session_id: str,
        batch_size: int,
    ):
        cursor = connection.execute(
            """
            SELECT effect_json
              FROM normalization_effect
             WHERE run_id = ?
             ORDER BY effect_id
            """,
            [session_id],
        )
        while batch := cursor.fetchmany(batch_size):
            yield tuple(str(item[0]) for item in batch)

    def _iter_prepared_normalization_effects(
        self,
        project_id: str,
        session_id: str,
    ):
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            last_effect_id = ""
            while batch := connection.execute(
                """
                SELECT effect_id, effect_json
                  FROM normalization_effect
                 WHERE run_id = ? AND effect_id > ?
                 ORDER BY effect_id
                 LIMIT ?
                """,
                [session_id, last_effect_id, PREPARATION_SESSION_ROW_BATCH_SIZE],
            ).fetchall():
                for effect_id, effect_json in batch:
                    last_effect_id = str(effect_id)
                    try:
                        yield NormalizationEffect.from_dict(
                            json.loads(str(effect_json))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored normalization effect is invalid"
                        ) from error

    def mark_published(self, project_id: str, session_id: str) -> None:
        """Retain value-free status metadata and remove all temporary evidence."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.READY,
                )
                bound_snapshots = connection.execute(
                    """
                    SELECT binding.dataset_id, binding.content_hash
                      FROM preparation_session_snapshot AS binding
                      JOIN prepared_snapshot_manifest AS manifest
                        ON manifest.content_hash = binding.content_hash
                       AND manifest.dataset_id = binding.dataset_id
                     WHERE binding.session_id = ?
                     ORDER BY binding.dataset_id
                    """,
                    [self._session_id(session_id)],
                ).fetchall()
                for dataset_id, content_hash in bound_snapshots:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO prepared_snapshot_current
                        VALUES (?, ?)
                        """,
                        [str(dataset_id), str(content_hash)],
                    )
                canonical_status = connection.execute(
                    """
                    SELECT status
                      FROM canonical_staging_run
                     WHERE run_id = ?
                    """,
                    [self._session_id(session_id)],
                ).fetchone()
                self._delete_session_rows(
                    connection,
                    session_id,
                    retain_relationships=(
                        canonical_status is not None
                        and str(canonical_status[0]) != StagingRunStatus.PENDING.value
                    ),
                )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.PUBLISHED.value,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def fail_session(
        self,
        project_id: str,
        session_id: str,
        failure_code: str,
    ) -> None:
        """Fail closed with a non-sensitive code and remove temporary values."""

        if _FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("Preparation failure code is invalid")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            return
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                exists = connection.execute(
                    "SELECT 1 FROM preparation_session WHERE session_id = ?",
                    [self._session_id(session_id)],
                ).fetchone()
                if exists is None:
                    connection.rollback()
                    return
                self._delete_session_rows(connection, session_id)
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, failure_code = ?, updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.FAILED.value,
                        failure_code,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _reconciliation(
        self,
        project_id: str,
        session_id: str,
        evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
    ) -> tuple[StagingReconciliation, tuple[StagingDatasetReconciliation, ...]]:
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            disposition_rows = connection.execute(
                """
                SELECT dataset, disposition, COUNT(*)
                  FROM canonical_staging_row
                 WHERE run_id = ?
                 GROUP BY dataset, disposition
                """,
                [session_id],
            ).fetchall()
            lineage_rows = connection.execute(
                """
                SELECT dataset,
                       COUNT(*),
                       COUNT(DISTINCT (physical_dataset_id, physical_source_row))
                  FROM preparation_lineage
                 WHERE session_id = ?
                 GROUP BY dataset
                """,
                [session_id],
            ).fetchall()
        dispositions: dict[str, dict[StagingDisposition, int]] = {}
        for dataset, disposition, count in disposition_rows:
            dispositions.setdefault(str(dataset), {})[
                StagingDisposition(str(disposition))
            ] = int(count)
        lineage = {
            str(dataset): (int(link_count), int(used_count))
            for dataset, link_count, used_count in lineage_rows
        }
        datasets: list[StagingDatasetReconciliation] = []
        run_counts = dict.fromkeys(StagingDisposition, 0)
        for dataset, (
            physical_dataset_id,
            role,
            input_rows,
            target_model,
        ) in sorted(evidence.items()):
            counts = dispositions.get(dataset, {})
            output_rows = sum(counts.values())
            lineage_links, input_rows_used = lineage.get(dataset, (0, 0))
            for disposition in StagingDisposition:
                run_counts[disposition] += counts.get(disposition, 0)
            datasets.append(
                StagingDatasetReconciliation(
                    dataset=dataset,
                    target_model=target_model,
                    physical_dataset_id=physical_dataset_id,
                    role=role,
                    input_rows=input_rows,
                    input_rows_used=input_rows_used,
                    output_rows=output_rows,
                    lineage_links=lineage_links,
                    created_rows=max(output_rows - input_rows_used, 0),
                    combined_rows=max(lineage_links - output_rows, 0),
                    unrepresented_rows=input_rows - input_rows_used,
                    candidate_rows=counts.get(StagingDisposition.CANDIDATE, 0),
                    reference_rows=counts.get(StagingDisposition.REFERENCE, 0),
                    blocked_rows=counts.get(StagingDisposition.BLOCKED, 0),
                    quarantined_rows=counts.get(
                        StagingDisposition.QUARANTINED,
                        0,
                    ),
                    excluded_rows=counts.get(StagingDisposition.EXCLUDED, 0),
                )
            )
        total_rows = sum(run_counts.values())
        return (
            StagingReconciliation(
                total_rows=total_rows,
                candidate_rows=run_counts[StagingDisposition.CANDIDATE],
                reference_rows=run_counts[StagingDisposition.REFERENCE],
                blocked_rows=run_counts[StagingDisposition.BLOCKED],
                quarantined_rows=run_counts[StagingDisposition.QUARANTINED],
                excluded_rows=run_counts[StagingDisposition.EXCLUDED],
            ),
            tuple(datasets),
        )

    def _hash_direct_run(
        self,
        *,
        project_id: str,
        run_id: str,
        bindings: PreparationSessionBindings,
        datasets: tuple[StagingDatasetReconciliation, ...],
        issues: tuple[CanonicalIssue, ...],
        reconciliation: StagingReconciliation,
        control_totals: tuple[CanonicalControlTotal, ...],
    ) -> tuple[str, int]:
        """Validate and hash pending rows without copying their JSON payloads."""

        hasher = CanonicalJsonObjectHasher()
        hasher.add_value("compiled_plan_hash", bindings.compiled_plan_hash)
        hasher.add_value("contract_version", bindings.contract_version)
        hasher.add_value(
            "control_totals",
            [item.to_portable_dict() for item in control_totals],
        )
        hasher.add_value(
            "datasets",
            [item.to_portable_dict() for item in datasets],
        )
        hasher.add_value("derived_plan_hash", bindings.derived_plan_hash)
        hasher.add_value("evaluator_version", bindings.evaluator_version)
        hasher.add_value(
            "issues",
            [item.to_portable_dict() for item in issues],
        )
        hasher.add_value("mapping_hash", bindings.mapping_hash)
        hasher.add_value("mapping_id", bindings.mapping_id)
        hasher.add_value(
            "physical_selection_hash",
            bindings.physical_selection_hash,
        )
        hasher.add_value("project_id", project_id)
        hasher.add_value(
            "reconciliation",
            reconciliation.to_portable_dict(),
        )
        hasher.start_array("rows")
        expected_ordinal = 0
        for batch in self._iter_direct_encoded_batches(
            project_id,
            run_id,
            batch_size=STAGING_ROW_BATCH_SIZE,
        ):
            for (
                ordinal,
                row_id,
                dataset,
                source_row,
                target_model,
                disposition,
                row_text,
            ) in batch:
                if int(ordinal) != expected_ordinal:
                    raise WorkspaceError(
                        "Stored direct preparation rows are not contiguous"
                    )
                if (
                    not str(row_id)
                    or not str(dataset)
                    or int(source_row) < 1
                    or not str(target_model)
                    or str(disposition) not in _STAGING_DISPOSITIONS
                ):
                    raise WorkspaceError(
                        "Stored direct preparation row metadata is inconsistent"
                    )
                encoded = str(row_text)
                if not encoded:
                    raise WorkspaceError("Stored direct preparation row is invalid")
                hasher.add_encoded_array_item(encoded)
                expected_ordinal += 1
        hasher.end_array()
        hasher.add_value("schema_hash", bindings.schema_hash)
        hasher.add_value(
            "source_selection_hash",
            bindings.source_selection_hash,
        )
        return hasher.finish(), expected_ordinal

    def _iter_direct_encoded_batches(
        self,
        project_id: str,
        run_id: str,
        *,
        batch_size: int,
        connection=None,
    ):
        """Yield stored or prepared-backed canonical JSON in ordinal order."""

        if connection is None:
            database_path = self.project_directory(project_id) / "project.duckdb"
            with self._connect(database_path) as owned_connection:
                self._ensure_project_database_schema(owned_connection)
                yield from self._iter_direct_encoded_batches(
                    project_id,
                    run_id,
                    batch_size=batch_size,
                    connection=owned_connection,
                )
            return
        canonical_run_id = self._session_id(run_id)
        raw_projections = connection.execute(
            """
            SELECT projection.ordinal_start, projection.row_count,
                   projection.projection_json, manifest.manifest_json
              FROM canonical_prepared_projection AS projection
              JOIN prepared_snapshot_manifest AS manifest
                ON manifest.content_hash = projection.prepared_snapshot_hash
               AND manifest.dataset_id = projection.dataset_id
             WHERE projection.run_id = ?
             ORDER BY projection.ordinal_start
            """,
            [canonical_run_id],
        ).fetchall()
        projections: list[
            tuple[int, int, PreparedCanonicalProjection, PreparedSnapshot]
        ] = []
        for ordinal_start, row_count, projection_text, manifest_text in raw_projections:
            try:
                projection = PreparedCanonicalProjection.from_portable_dict(
                    json.loads(str(projection_text))
                )
                snapshot = PreparedSnapshot.from_json(str(manifest_text))
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Prepared canonical projection is invalid"
                ) from error
            if (
                projection.ordinal_start != int(ordinal_start)
                or projection.row_count != int(row_count)
                or projection.dataset_id != snapshot.dataset_id
                or projection.row_count != snapshot.row_count
                or projection.program.content_hash
                != snapshot.transformation_program_hash
                or projection.program.mapping_content_hash != snapshot.mapping_hash
                or projection.program.schema_hash != snapshot.schema_hash
            ):
                raise WorkspaceError("Prepared canonical projection metadata changed")
            projections.append(
                (
                    int(ordinal_start),
                    int(row_count),
                    projection,
                    snapshot,
                )
            )

        next_ordinal = 0
        for ordinal_start, row_count, projection, snapshot in projections:
            if ordinal_start < next_ordinal:
                raise WorkspaceError("Prepared canonical projection ordinals overlap")
            yield from self._iter_stored_direct_encoded_batches(
                connection,
                canonical_run_id,
                start=next_ordinal,
                stop=ordinal_start,
                batch_size=batch_size,
            )
            projection_end = ordinal_start + row_count
            counts = connection.execute(
                """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE row_json = '')
                  FROM canonical_staging_row
                 WHERE run_id = ?
                   AND ordinal >= ? AND ordinal < ?
                """,
                [canonical_run_id, ordinal_start, projection_end],
            ).fetchone()
            if counts is None or int(counts[0]) != row_count:
                raise WorkspaceError("Prepared canonical row index is incomplete")
            empty_count = int(counts[1])
            if empty_count == 0:
                yield from self._iter_stored_direct_encoded_batches(
                    connection,
                    canonical_run_id,
                    start=ordinal_start,
                    stop=projection_end,
                    batch_size=batch_size,
                )
            elif empty_count == row_count:
                yield from self._iter_projected_dataset_encoded_batches(
                    project_id,
                    canonical_run_id,
                    projection,
                    snapshot,
                    batch_size=batch_size,
                    connection=connection,
                )
            else:
                raise WorkspaceError("Prepared canonical value storage is inconsistent")
            next_ordinal = projection_end
        yield from self._iter_stored_direct_encoded_batches(
            connection,
            canonical_run_id,
            start=next_ordinal,
            stop=None,
            batch_size=batch_size,
        )

    @staticmethod
    def _iter_stored_direct_encoded_batches(
        connection,
        run_id: str,
        *,
        start: int,
        stop: int | None,
        batch_size: int,
    ):
        next_ordinal = start
        while True:
            if stop is not None and next_ordinal >= stop:
                return
            limit = batch_size if stop is None else min(batch_size, stop - next_ordinal)
            batch = connection.execute(
                """
                SELECT ordinal, row_id, dataset, source_row, target_model,
                       disposition, row_json
                  FROM canonical_staging_row
                 WHERE run_id = ? AND ordinal >= ?
                   AND (? IS NULL OR ordinal < ?)
                 ORDER BY ordinal
                 LIMIT ?
                """,
                [run_id, next_ordinal, stop, stop, limit],
            ).fetchall()
            if not batch:
                return
            yield batch
            next_ordinal += len(batch)

    def _iter_projected_dataset_encoded_batches(
        self,
        project_id: str,
        run_id: str,
        projection: PreparedCanonicalProjection,
        snapshot: PreparedSnapshot,
        *,
        batch_size: int,
        connection,
    ):
        try:
            artifact_context = self._artifacts.materialize_prepared_snapshot(
                project_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            )
            with artifact_context as path:
                if projection.set_based_projection:
                    base_disposition = (
                        StagingDisposition.REFERENCE.value
                        if projection.mode == "reference"
                        else StagingDisposition.CANDIDATE.value
                    )
                    exception = connection.execute(
                        """
                        SELECT 1
                          FROM canonical_staging_row AS row
                          LEFT JOIN canonical_staging_row_issue AS issue
                            ON issue.run_id = row.run_id
                           AND issue.ordinal = row.ordinal
                         WHERE row.run_id = ?
                           AND row.ordinal >= ? AND row.ordinal < ?
                           AND (row.disposition <> ? OR issue.ordinal IS NOT NULL)
                         LIMIT 1
                        """,
                        [
                            run_id,
                            projection.ordinal_start,
                            projection.ordinal_start + projection.row_count,
                            base_disposition,
                        ],
                    ).fetchone()
                    if exception is None:
                        query = projected_encoded_rows_sql(
                            projection,
                        )
                        projection_connection = duckdb.connect(
                            ":memory:",
                            config={
                                "allow_community_extensions": "false",
                                "autoinstall_known_extensions": "false",
                                "autoload_known_extensions": "false",
                                "enable_external_access": "true",
                                "lock_configuration": "true",
                                "memory_limit": PREPARED_VALUE_PROJECTOR_MEMORY_LIMIT,
                                "threads": "1",
                            },
                        )
                        try:
                            local_offset = 0
                            while local_offset < projection.row_count:
                                local_stop = min(
                                    local_offset + batch_size,
                                    projection.row_count,
                                )
                                start = projection.ordinal_start + local_offset
                                stop = projection.ordinal_start + local_stop
                                batch = projection_connection.execute(
                                    query,
                                    [str(path), local_offset, local_stop],
                                ).fetchall()
                                metadata = connection.execute(
                                    """
                                    SELECT ordinal, row_id, dataset, source_row,
                                           target_model, disposition
                                      FROM canonical_staging_row
                                     WHERE run_id = ?
                                       AND ordinal >= ? AND ordinal < ?
                                     ORDER BY ordinal
                                    """,
                                    [run_id, start, stop],
                                ).fetchall()
                                if len(batch) != local_stop - local_offset or tuple(
                                    row[:6] for row in batch
                                ) != tuple(metadata):
                                    raise WorkspaceError(
                                        "Prepared canonical row index changed"
                                    )
                                yield batch
                                local_offset = local_stop
                            return
                        finally:
                            projection_connection.close()

                local_offset = 0
                for native_batch in iter_polars_prepared_batches(
                    path,
                    snapshot,
                    None,
                    projection.program,
                    batch_size=batch_size,
                    materialize_records=False,
                    collect_impacts=False,
                ):
                    start = projection.ordinal_start + local_offset
                    stop = start + len(native_batch.source_rows)
                    metadata = connection.execute(
                        """
                        SELECT ordinal, row_id, dataset, source_row,
                               target_model, disposition
                          FROM canonical_staging_row
                         WHERE run_id = ?
                           AND ordinal >= ? AND ordinal < ?
                         ORDER BY ordinal
                        """,
                        [run_id, start, stop],
                    ).fetchall()
                    if len(metadata) != len(native_batch.source_rows):
                        raise WorkspaceError(
                            "Prepared canonical row index is incomplete"
                        )
                    issue_rows = connection.execute(
                        """
                        SELECT ordinal, issue_ordinal, issue_json
                          FROM canonical_staging_row_issue
                         WHERE run_id = ?
                           AND ordinal >= ? AND ordinal < ?
                           AND issue_ordinal = 0
                         ORDER BY ordinal, issue_ordinal
                        """,
                        [run_id, start, stop],
                    ).fetchall()
                    overlays: dict[int, list[CanonicalIssue]] = {}
                    for ordinal, _issue_ordinal, issue_text in issue_rows:
                        try:
                            issue = CanonicalIssue.from_dict(
                                json.loads(str(issue_text))
                            )
                        except (TypeError, ValueError) as error:
                            raise WorkspaceError(
                                "Prepared canonical row issue is invalid"
                            ) from error
                        overlays.setdefault(int(ordinal), []).append(issue)
                    encoded_batch = []
                    values = zip(
                        metadata,
                        native_batch.source_rows,
                        native_batch.source_identities,
                        native_batch.target_identities,
                        native_batch.target_scopes,
                        native_batch.scalar_values,
                        native_batch.references,
                        native_batch.issues,
                        strict=True,
                    )
                    for (
                        metadata_row,
                        source_row,
                        source_identity,
                        target_identity,
                        target_scope,
                        scalar_values,
                        references,
                        issues,
                    ) in values:
                        ordinal = int(metadata_row[0])
                        projected = canonical_prepared_session_row(
                            dataset=projection.dataset,
                            source_row=source_row,
                            target_model=projection.program.target_model,
                            source_identity=source_identity,
                            target_identity=target_identity,
                            target_scope=target_scope,
                            scalar_values=scalar_values,
                            references=references,
                            issues=issues,
                            ordinal=ordinal,
                            mode=projection.mode,
                            source_hash=projection.source_hash,
                            source_selection_hash=(
                                projection.program.source_selection_hash
                            ),
                            mapping_hash=(projection.program.mapping_content_hash),
                            schema_hash=projection.program.schema_hash,
                            field_sources=projection.field_sources,
                            physical_dataset_id=(projection.physical_dataset_id),
                        )
                        try:
                            row = CanonicalRow.from_dict(json.loads(projected.row_json))
                        except (TypeError, ValueError) as error:
                            raise WorkspaceError(
                                "Projected canonical row is invalid"
                            ) from error
                        overlay = tuple(overlays.get(ordinal, ()))
                        stored_disposition = StagingDisposition(str(metadata_row[5]))
                        if overlay or row.disposition is not stored_disposition:
                            row = replace(
                                row,
                                disposition=stored_disposition,
                                issues=(*row.issues, *overlay),
                            )
                        if (
                            row.row_id != str(metadata_row[1])
                            or row.dataset != str(metadata_row[2])
                            or row.source_row != int(metadata_row[3])
                            or row.target_model != str(metadata_row[4])
                        ):
                            raise WorkspaceError("Prepared canonical row index changed")
                        encoded_batch.append(
                            (
                                ordinal,
                                row.row_id,
                                row.dataset,
                                row.source_row,
                                row.target_model,
                                row.disposition.value,
                                _canonical_json(row.to_portable_dict()),
                            )
                        )
                    yield encoded_batch
                    local_offset += len(native_batch.source_rows)
                if local_offset != projection.row_count:
                    raise WorkspaceError(
                        "Prepared canonical projection row count changed"
                    )
        except ArtifactStoreError as error:
            raise WorkspaceError(
                "Prepared canonical value artifact could not be verified"
            ) from error

    def _direct_rows_by_ordinal(
        self,
        project_id: str,
        run_id: str,
        ordinals: Sequence[int],
    ) -> dict[int, CanonicalRow]:
        wanted = {int(item) for item in ordinals}
        if not wanted:
            return {}
        rows: dict[int, CanonicalRow] = {}
        maximum = max(wanted)
        for batch in self._iter_direct_encoded_batches(
            project_id,
            run_id,
            batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            for ordinal, *_metadata, row_text in batch:
                observed = int(ordinal)
                if observed in wanted:
                    try:
                        rows[observed] = CanonicalRow.from_dict(
                            json.loads(str(row_text))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored direct preparation row is invalid"
                        ) from error
                if observed >= maximum:
                    return rows
        return rows

    def _load_canonical_row_range(
        self,
        project_id: str,
        session_id: str,
        start: int,
        stop: int,
    ) -> tuple[CanonicalRow, ...]:
        if stop <= start:
            return ()
        rows: list[CanonicalRow] = []
        for batch in self._iter_direct_encoded_batches(
            project_id,
            session_id,
            batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            for ordinal, *_metadata, row_text in batch:
                observed = int(ordinal)
                if observed >= stop:
                    return tuple(rows)
                if observed >= start:
                    rows.append(self._canonical_row(str(row_text)))
        return tuple(rows)

    def _iter_canonical_rows(
        self,
        project_id: str,
        session_id: str,
    ) -> Iterator[CanonicalRow]:
        next_ordinal = 0
        for batch in self._iter_direct_encoded_batches(
            project_id,
            session_id,
            batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            for ordinal, *_metadata, row_text in batch:
                if int(ordinal) != next_ordinal:
                    raise WorkspaceError(
                        "Stored preparation canonical rows are not contiguous"
                    )
                next_ordinal += 1
                yield self._canonical_row(str(row_text))

    @staticmethod
    def _canonical_row(row_text: str) -> CanonicalRow:
        try:
            return CanonicalRow.from_dict(json.loads(row_text))
        except (TypeError, ValueError) as error:
            raise WorkspaceError(
                "Stored preparation canonical row is invalid"
            ) from error

    @staticmethod
    def _require_status(
        connection,
        session_id: str,
        expected: PreparationSessionStatus,
    ):
        row = connection.execute(
            """
            SELECT status, staged_row_count, impact_row_count
              FROM preparation_session
             WHERE session_id = ?
            """,
            [PreparationSessionRepository._session_id(session_id)],
        ).fetchone()
        if row is None:
            raise WorkspaceError("Preparation session was not found")
        if PreparationSessionStatus(str(row[0])) is not expected:
            raise WorkspaceError("Preparation session is in the wrong state")
        return row

    @staticmethod
    def _delete_session_rows(
        connection,
        session_id: str,
        *,
        retain_relationships: bool = False,
    ) -> None:
        canonical = PreparationSessionRepository._session_id(session_id)
        connection.execute(
            """
            DELETE FROM normalization_effect
             WHERE run_id = ?
               AND NOT EXISTS (
                   SELECT 1 FROM normalization_run WHERE run_id = ?
               )
            """,
            [canonical, canonical],
        )
        session_tables = (
            "preparation_normalization_finding",
            "preparation_normalization_group_seed",
            "preparation_impact_row",
            "preparation_physical_row",
            "preparation_lineage",
            "preparation_identity_group",
            "preparation_relationship_edge",
            "preparation_direct_identity",
            "preparation_session_snapshot",
        )
        available_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = current_schema()
                   AND table_name = ANY(?)
                """,
                [list(session_tables)],
            ).fetchall()
        }
        for table in session_tables:
            if table not in available_tables:
                continue
            if retain_relationships and table == "preparation_relationship_edge":
                continue
            connection.execute(
                f"DELETE FROM {table} WHERE session_id = ?",
                [canonical],
            )
        pending = connection.execute(
            """
            SELECT 1
              FROM canonical_staging_run
             WHERE run_id = ? AND status = ?
            """,
            [canonical, StagingRunStatus.PENDING.value],
        ).fetchone()
        if pending is not None:
            connection.execute(
                "DELETE FROM canonical_staging_row_issue WHERE run_id = ?",
                [canonical],
            )
            connection.execute(
                "DELETE FROM canonical_prepared_projection WHERE run_id = ?",
                [canonical],
            )
            connection.execute(
                "DELETE FROM canonical_staging_row WHERE run_id = ?",
                [canonical],
            )
            connection.execute(
                "DELETE FROM canonical_staging_run WHERE run_id = ?",
                [canonical],
            )

    @staticmethod
    def _session_id(session_id: str) -> str:
        try:
            return str(UUID(session_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Preparation session identifier is invalid") from error
