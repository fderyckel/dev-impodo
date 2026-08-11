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

from ...access import Actor
from ...domain.serialization import CanonicalJsonObjectHasher
from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparationSessionSummary,
    PreparedSessionRow,
    StoredCanonicalStagingRun,
    prepared_record_from_portable_dict,
    prepared_record_to_portable_dict,
    transformation_impact_from_portable_dict,
    transformation_impact_to_portable_dict,
    transformation_report_to_portable_dict,
)
from ...domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
)
from ...models import Issue, PreparedRecord, canonical_json_bytes, portable_value
from ...projects import ProjectNotFoundError
from ...staging import StagingRunStatus
from ...staging_contracts import (
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingDisposition,
    StagingReconciliation,
    canonical_row_from_prepared,
    validate_canonical_row_bindings,
)
from ...workspace_errors import WorkspaceError
from .constants import (
    DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
    DUCKDB_JSON_BATCH_MAX_BYTES,
    PREPARATION_SESSION_MEMORY_LIMIT,
    PREPARATION_SESSION_ROW_BATCH_SIZE,
)
from .repository import DuckDbRepository
from .serialization import (
    _canonical_json,
    _columnar_parameters,
    iter_encoded_json_batches,
)


_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

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

# A canonical row can legally be much larger than the normal JSON envelope.
# Route a conservative upper-bound estimate through one scalar insert instead
# of rejecting valid source evidence or creating an oversized copied payload.
_CANONICAL_ROW_SCALAR_FALLBACK_BYTES = (
    DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES // 3
)


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
        *,
        direct: bool = False,
    ) -> None:
        self._repository = repository
        self._project_id = project_id
        self._session_id = session_id
        self._row_count = row_count
        self._direct = direct
        self.canonical_run_id = session_id if direct else None

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
            return self._repository._load_final_row_range(
                self._project_id,
                self._session_id,
                start,
                stop,
                direct=self._direct,
            )
        normalized = index + self._row_count if index < 0 else index
        if normalized < 0 or normalized >= self._row_count:
            raise IndexError(index)
        rows = self._repository._load_final_row_range(
            self._project_id,
            self._session_id,
            normalized,
            normalized + 1,
            direct=self._direct,
        )
        if not rows:
            raise IndexError(index)
        return rows[0]

    def __iter__(self) -> Iterator[CanonicalRow]:
        yield from self._repository._iter_final_rows(
            self._project_id,
            self._session_id,
            direct=self._direct,
        )

    def iter_encoded_batches(self, connection, batch_size: int):
        """Read exact stored JSON through the publication transaction."""

        table = (
            "canonical_staging_row"
            if self._direct
            else "preparation_final_row"
        )
        id_column = "run_id" if self._direct else "session_id"
        start = 0
        while batch := connection.execute(
            f"""
            SELECT ordinal, row_id, dataset, source_row, target_model,
                   disposition, row_json
              FROM {table}
             WHERE {id_column} = ?
               AND ordinal >= ?
             ORDER BY ordinal
             LIMIT ?
            """,
            [self._session_id, start, batch_size],
        ).fetchall():
            yield batch
            start += len(batch)


class _SessionImpacts:
    """Replay finalized transformation impacts without materializing them."""

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

    def iter_bound_rows(
        self,
        *,
        connection=None,
        batch_size: int = PREPARATION_SESSION_ROW_BATCH_SIZE,
    ):
        """Yield each impact with its unique canonical row ID."""

        yield from self._repository._iter_bound_impacts(
            self._project_id,
            self._session_id,
            connection=connection,
            batch_size=batch_size,
        )


class PreparationSessionRepository(DuckDbRepository):
    """Persist provisional rows and expose a bounded canonical publication."""

    def _connect(self, path):
        """Use a smaller hardened buffer allowance for bounded session work."""

        return self._database.connection_factory.connect(
            path,
            memory_limit=PREPARATION_SESSION_MEMORY_LIMIT,
            threads="1",
            preserve_insertion_order=False,
        )

    def begin_session(
        self,
        project_id: str,
        bindings: PreparationSessionBindings,
    ) -> PreparationSessionSummary:
        """Create an unpublished session bound to immutable preparation inputs."""

        return self._begin_session(project_id, bindings, actor=None)

    def begin_direct_session(
        self,
        project_id: str,
        bindings: PreparationSessionBindings,
        *,
        actor: Actor,
    ) -> PreparationSessionSummary:
        """Create a session whose UUID is also a pending canonical run ID."""

        return self._begin_session(project_id, bindings, actor=actor)

    def _begin_session(
        self,
        project_id: str,
        bindings: PreparationSessionBindings,
        *,
        actor: Actor | None,
    ) -> PreparationSessionSummary:
        """Create session metadata and, for direct runs, its pending header."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
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
                    impact_report_json, provisional_row_count,
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
                if actor is not None:
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

    def append_provisional_rows(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[PreparedSessionRow],
    ) -> None:
        """Append one bounded row batch and its normalized lineage facts."""

        self.append_session_batch(project_id, session_id, rows, ())

    def find_prepared_snapshot(
        self,
        project_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> PreparedSnapshot | None:
        """Find one historical exact prepared artifact for safe reuse."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
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
            self._validate_project_database_schema(connection)
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
            self._validate_project_database_schema(connection)
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
            self._validate_project_database_schema(connection)
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
            self._validate_project_database_schema(connection)
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
                    raise WorkspaceError(
                        "Direct preparation run is not pending"
                    )
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
                            target_model, disposition, row_json
                        )
                        SELECT
                            ?, item.ordinal, item.row_id, item.dataset,
                            item.source_row, item.target_model,
                            item.disposition, item.row_json
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
                            target_model, disposition, row_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            canonical_session_id,
                            item.ordinal,
                            item.row_id,
                            item.dataset,
                            item.source_row,
                            item.target_model,
                            item.disposition.value,
                            item.row_json,
                        ],
                    )
                    canonical_row_count += 1
                if canonical_row_count != len(rows):
                    raise WorkspaceError(
                        "Prepared canonical row batch is incomplete"
                    )
                identity_rows = (
                    {
                        "ordinal": item.ordinal,
                        "dataset": item.dataset,
                        "identity_hash": "sha256:" + sha256(
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
                if identity_count != len(rows):
                    raise WorkspaceError(
                        "Prepared identity fact batch is incomplete"
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
                    raise WorkspaceError(
                        "Prepared lineage fact batch is incomplete"
                    )

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
                       SET provisional_row_count = provisional_row_count + ?,
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

    def append_impacts(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[TransformationImpactRow],
    ) -> None:
        """Append one bounded impact batch with deterministic ordinals."""

        self.append_session_batch(project_id, session_id, (), rows)

    def append_session_batch(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[PreparedSessionRow | CanonicalPreparedSessionRow],
        impacts: Sequence[TransformationImpactRow],
    ) -> None:
        """Append prepared rows and their impacts in one bounded transaction."""

        if not rows and not impacts:
            return
        provisional_values: list[list[object]] = []
        lineage_values: list[list[object]] = []
        physical_values: list[list[object]] = []
        for item in rows:
            canonical = (
                item if isinstance(item, CanonicalPreparedSessionRow) else None
            )
            record = item.record if isinstance(item, PreparedSessionRow) else None
            dataset = canonical.dataset if canonical is not None else record.dataset
            source_row_number = (
                canonical.source_row if canonical is not None else record.source_row
            )
            target_model = (
                canonical.target_model
                if canonical is not None
                else record.target_model
            )
            source_identity = (
                canonical.source_identity
                if canonical is not None
                else record.source_identity
            )
            if not item.physical_sources:
                raise ValueError("Prepared session rows require physical lineage")
            identity_hash = "sha256:" + sha256(
                canonical_json_bytes(
                    {
                        "dataset": dataset,
                        "source_identity": portable_value(source_identity),
                    }
                )
            ).hexdigest()
            provisional_values.append(
                [
                    session_id,
                    canonical.ordinal if canonical is not None else None,
                    dataset,
                    source_row_number,
                    target_model,
                    identity_hash,
                    "CANONICAL" if canonical is not None else "PREPARED",
                    canonical.row_id if canonical is not None else None,
                    canonical.disposition.value if canonical is not None else None,
                    (
                        canonical.row_json
                        if canonical is not None
                        else _canonical_json(prepared_record_to_portable_dict(record))
                    ),
                ]
            )
            for physical_dataset_id, source_rows in sorted(
                item.physical_sources.items()
            ):
                ordered = tuple(sorted(set(source_rows)))
                if not physical_dataset_id or ordered != tuple(source_rows):
                    raise ValueError("Prepared session lineage is invalid")
                for source_row in ordered:
                    if source_row < 1:
                        raise ValueError("Prepared session source rows must be positive")
                    lineage_values.append(
                        [
                            session_id,
                            dataset,
                            source_row_number,
                            physical_dataset_id,
                            source_row,
                        ]
                    )
                    physical_values.append(
                        [session_id, physical_dataset_id, source_row]
                    )

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            connection.begin()
            try:
                current = self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.BUILDING,
                )
                start = int(current[1])
                for offset, values in enumerate(provisional_values):
                    if values[1] is None:
                        values[1] = start + offset
                if provisional_values:
                    connection.execute(
                        """
                        INSERT INTO preparation_provisional_row (
                            session_id, ordinal, dataset, source_row,
                            target_model,
                            identity_hash, payload_kind, row_id,
                            disposition, record_json
                        )
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?),
                               unnest(?), unnest(?), unnest(?), unnest(?),
                               unnest(?), unnest(?)
                        """,
                        _columnar_parameters(provisional_values),
                    )
                    connection.execute(
                        """
                        INSERT INTO preparation_lineage (
                            session_id, dataset, output_source_row,
                            physical_dataset_id, physical_source_row
                        )
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(lineage_values),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO preparation_physical_row (
                            session_id, physical_dataset_id, source_row
                        )
                        SELECT unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(physical_values),
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
                    for offset, row in enumerate(impacts)
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
                if impact_count != len(impacts):
                    raise WorkspaceError(
                        "Preparation impact batch is incomplete"
                    )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET provisional_row_count = provisional_row_count + ?,
                           impact_row_count = impact_row_count + ?,
                           updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        len(provisional_values),
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
                for dataset, identity_hash, identity_count
                in connection.execute(
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
                        collision_counts[
                            (str(dataset), str(identity_hash))
                        ],
                    )
                    for ordinal, dataset, identity_hash, finalized
                    in identity_batch
                    if not bool(finalized)
                    and (
                        str(dataset),
                        str(identity_hash),
                    ) in collision_counts
                )

        canonical_issues = [
            CanonicalIssue.from_issue(item) for item in run_issues
        ]
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
            with self._connect(database_path) as connection:
                batch = connection.execute(
                    """
                    SELECT ordinal, row_json
                      FROM canonical_staging_row
                     WHERE run_id = ?
                       AND ordinal IN (SELECT unnest(?))
                     ORDER BY ordinal
                    """,
                    [canonical_session_id, ordinals],
                ).fetchall()
            if len(batch) != len(ordinals):
                raise WorkspaceError(
                    "Direct duplicate rows are incomplete"
                )
            values: list[list[object]] = []
            for ordinal, row_text in batch:
                identity_count = duplicate_counts_by_ordinal[int(ordinal)]
                try:
                    row = CanonicalRow.from_dict(json.loads(str(row_text)))
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Stored direct preparation row is invalid"
                    ) from error
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

        reconciliation, datasets = self._reconciliation(
            project_id,
            canonical_session_id,
            dataset_evidence,
            row_table="canonical_staging_row",
            id_column="run_id",
        )
        controls = tuple(
            sorted(control_totals, key=lambda item: item.control_id)
        )
        content_hash, row_count = self._hash_direct_run(
            project_id=project_id,
            run_id=canonical_session_id,
            bindings=bindings,
            datasets=datasets,
            issues=tuple(canonical_issues),
            reconciliation=reconciliation,
            control_totals=controls,
        )
        if row_count != summary.provisional_row_count:
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
                    raise WorkspaceError(
                        "Direct preparation rows are incomplete"
                    )
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
                            [
                                item.to_portable_dict()
                                for item in canonical_issues
                            ]
                        ),
                        _canonical_json(
                            [item.to_portable_dict() for item in controls]
                        ),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json(
                            [item.to_portable_dict() for item in datasets]
                        ),
                        _canonical_json(
                            transformation_report_to_portable_dict(
                                impact_report
                            )
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
                            [
                                item.to_portable_dict()
                                for item in canonical_issues
                            ]
                        ),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json(
                            [item.to_portable_dict() for item in datasets]
                        ),
                        _canonical_json(
                            [item.to_portable_dict() for item in controls]
                        ),
                        canonical_session_id,
                        StagingRunStatus.PENDING.value,
                    ],
                ).fetchone()
                if updated is None:
                    raise WorkspaceError(
                        "Direct preparation run is not pending"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.load_stored_run(project_id, canonical_session_id)

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
            with self._connect(database_path) as connection:
                batch = connection.execute(
                    """
                    SELECT ordinal, row_json
                      FROM canonical_staging_row
                     WHERE run_id = ?
                       AND ordinal IN (SELECT unnest(?))
                     ORDER BY ordinal
                    """,
                    [session_id, ordinals],
                ).fetchall()
            if len(batch) != len(ordinals):
                raise WorkspaceError(
                    "Direct duplicate rows are incomplete"
                )
            values: list[list[object]] = []
            for ordinal, row_text in batch:
                try:
                    row = CanonicalRow.from_dict(json.loads(str(row_text)))
                    row = replace(
                        row,
                        disposition=StagingDisposition(
                            base_by_ordinal[int(ordinal)]
                        ),
                        issues=tuple(
                            issue
                            for issue in row.issues
                            if issue.code != "SOURCE_IDENTITY_DUPLICATE"
                        ),
                    )
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Stored direct preparation row is invalid"
                    ) from error
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
                    raise WorkspaceError(
                        "Preparation session cannot be finalized"
                    )
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
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                connection.execute(
                    """
                    UPDATE canonical_staging_row AS target
                       SET disposition = updates.disposition,
                           row_json = updates.row_json
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

    def finalize_session(
        self,
        project_id: str,
        session_id: str,
        *,
        modes: Mapping[str, str],
        field_sources: Mapping[str, Mapping[str, tuple[str, ...]]],
        dataset_evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
        run_issues: Sequence[Issue],
        control_totals: Sequence[CanonicalControlTotal],
        impact_report: TransformationImpactReport,
    ) -> StoredCanonicalStagingRun:
        """Resolve duplicates and build exact final rows through bounded batches."""

        summary = self.get_session(project_id, session_id)
        if summary.status not in {
            PreparationSessionStatus.BUILDING,
            PreparationSessionStatus.FINALIZING,
        }:
            raise WorkspaceError("Preparation session cannot be finalized")
        self._restart_finalization(project_id, session_id)
        bindings = summary.bindings
        canonical_issues = [CanonicalIssue.from_issue(item) for item in run_issues]
        last_ordinal = -1
        expected_row_count = summary.provisional_row_count
        database_path = self.project_directory(project_id) / "project.duckdb"

        while True:
            with self._connect(database_path) as connection:
                batch = connection.execute(
                    """
                    SELECT ordinal, payload_kind, record_json, identity_count,
                           physical_dataset_ids_json,
                           physical_source_rows_json
                      FROM preparation_finalization_row
                     WHERE session_id = ?
                       AND ordinal > ?
                     ORDER BY ordinal
                     LIMIT ?
                    """,
                    [
                        session_id,
                        last_ordinal,
                        PREPARATION_SESSION_ROW_BATCH_SIZE,
                    ],
                ).fetchall()
            if not batch:
                break

            final_values = self._finalization_values(
                session_id,
                batch,
                bindings=bindings,
                modes=modes,
                field_sources=field_sources,
                canonical_issues=canonical_issues,
            )
            last_ordinal = int(batch[-1][0])
            with self._connect(database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO preparation_final_row (
                        session_id, ordinal, row_id, dataset, source_row,
                        target_model, disposition, row_json
                    )
                    SELECT unnest(?), unnest(?), unnest(?), unnest(?),
                           unnest(?), unnest(?), unnest(?), unnest(?)
                    """,
                    _columnar_parameters(final_values),
                )

        reconciliation, datasets = self._reconciliation(
            project_id,
            session_id,
            dataset_evidence,
        )
        controls = tuple(sorted(control_totals, key=lambda item: item.control_id))
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.FINALIZING,
                )
                stored = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM preparation_final_row
                     WHERE session_id = ?
                    """,
                    [session_id],
                ).fetchone()
                if stored is None or int(stored[0]) != expected_row_count:
                    raise WorkspaceError(
                        "Preparation-session rows were not finalized completely"
                    )
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
                        expected_row_count,
                        _canonical_json(
                            [item.to_portable_dict() for item in canonical_issues]
                        ),
                        _canonical_json(
                            [item.to_portable_dict() for item in controls]
                        ),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json(
                            [item.to_portable_dict() for item in datasets]
                        ),
                        _canonical_json(
                            transformation_report_to_portable_dict(impact_report)
                        ),
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.execute(
                    "DELETE FROM preparation_provisional_row WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_identity_group WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_finalization_row WHERE session_id = ?",
                    [session_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.load_stored_run(project_id, session_id)

    @staticmethod
    def _finalization_values(
        session_id: str,
        batch: Sequence[Sequence[object]],
        *,
        bindings: PreparationSessionBindings,
        modes: Mapping[str, str],
        field_sources: Mapping[str, Mapping[str, tuple[str, ...]]],
        canonical_issues: list[CanonicalIssue],
    ) -> list[list[object]]:
        """Convert one durable provisional batch to exact canonical row text."""

        final_values: list[list[object]] = []
        for (
            ordinal,
            payload_kind,
            record_text,
            identity_count,
            physical_dataset_ids_text,
            physical_source_rows_text,
        ) in batch:
            duplicate_count = int(identity_count)
            if str(payload_kind) == "CANONICAL":
                try:
                    row = CanonicalRow.from_dict(json.loads(str(record_text)))
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Stored preparation-session row is invalid"
                    ) from error
                if duplicate_count <= 1:
                    raise WorkspaceError(
                        "Canonical session row required unexpected finalization"
                    )
                duplicate = Issue(
                    code="SOURCE_IDENTITY_DUPLICATE",
                    message=(
                        f"source identity {row.source_identity!r} occurs "
                        f"{duplicate_count} times"
                    ),
                    dataset=row.dataset,
                    row=row.source_row,
                    affected_count=duplicate_count,
                )
                canonical_duplicate = CanonicalIssue.from_issue(duplicate)
                row = replace(
                    row,
                    disposition=StagingDisposition.BLOCKED,
                    issues=(*row.issues, canonical_duplicate),
                )
                canonical_issues.append(canonical_duplicate)
                validate_canonical_row_bindings(
                    row,
                    source_selection_hash=bindings.source_selection_hash,
                    mapping_hash=bindings.mapping_hash,
                    schema_hash=bindings.schema_hash,
                    derived_plan_hash=bindings.derived_plan_hash,
                )
            else:
                try:
                    record = prepared_record_from_portable_dict(
                        json.loads(str(record_text))
                    )
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Stored preparation-session row is invalid"
                    ) from error
                if duplicate_count > 1:
                    duplicate = Issue(
                        code="SOURCE_IDENTITY_DUPLICATE",
                        message=(
                            f"source identity {record.source_identity!r} occurs "
                            f"{duplicate_count} times"
                        ),
                        dataset=record.dataset,
                        row=record.source_row,
                        affected_count=duplicate_count,
                    )
                    record = replace(record, issues=(*record.issues, duplicate))
                    canonical_issues.append(CanonicalIssue.from_issue(duplicate))

            try:
                physical_dataset_ids = json.loads(
                    str(physical_dataset_ids_text)
                )
                physical_source_rows = json.loads(
                    str(physical_source_rows_text)
                )
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Stored preparation-session lineage is invalid"
                ) from error
            physical_sources: dict[str, list[int]] = {}
            for physical_dataset_id, physical_source_row in zip(
                physical_dataset_ids,
                physical_source_rows,
                strict=True,
            ):
                physical_sources.setdefault(
                    str(physical_dataset_id),
                    [],
                ).append(int(physical_source_row))
            normalized_sources = {
                physical_dataset_id: tuple(source_rows)
                for physical_dataset_id, source_rows in sorted(
                    physical_sources.items()
                )
            }
            if not normalized_sources:
                raise WorkspaceError(
                    "Stored preparation-session lineage is incomplete"
                )
            primary_dataset_id = next(iter(normalized_sources))
            if str(payload_kind) == "CANONICAL":
                if dict(row.lineage.physical_sources) != normalized_sources:
                    raise WorkspaceError(
                        "Stored preparation-session lineage is inconsistent"
                    )
            else:
                try:
                    row = canonical_row_from_prepared(
                        record,
                        mode=modes[record.dataset],
                        source_hash=bindings.source_hashes[record.dataset],
                        source_selection_hash=bindings.source_selection_hash,
                        mapping_hash=bindings.mapping_hash,
                        schema_hash=bindings.schema_hash,
                        derived_plan_hash=bindings.derived_plan_hash,
                        field_sources=field_sources.get(record.dataset, {}),
                        physical_dataset_id=primary_dataset_id,
                        physical_source_rows=normalized_sources[primary_dataset_id],
                        physical_sources=normalized_sources,
                    )
                    validate_canonical_row_bindings(
                        row,
                        source_selection_hash=bindings.source_selection_hash,
                        mapping_hash=bindings.mapping_hash,
                        schema_hash=bindings.schema_hash,
                        derived_plan_hash=bindings.derived_plan_hash,
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Preparation-session canonical row is invalid"
                    ) from error
            final_values.append(
                [
                    session_id,
                    int(ordinal),
                    row.row_id,
                    row.dataset,
                    row.source_row,
                    row.target_model,
                    row.disposition.value,
                    _canonical_json(row.to_portable_dict()),
                ]
            )
        return final_values

    def load_stored_run(
        self,
        project_id: str,
        session_id: str,
    ) -> StoredCanonicalStagingRun:
        """Return a READY header backed by bounded durable row slices."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
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
        if PreparationSessionStatus(str(row[0])) is not PreparationSessionStatus.READY:
            raise WorkspaceError("Preparation session is not ready")
        try:
            issues = tuple(
                CanonicalIssue.from_dict(item)
                for item in json.loads(str(row[10]))
            )
            reconciliation = StagingReconciliation.from_dict(
                json.loads(str(row[11]))
            )
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
                direct=direct is not None,
            ),
            issues=issues,
            reconciliation=reconciliation,
            compiled_plan_hash=str(row[7]),
            control_totals=controls,
            evaluator_version=int(row[9]),
            contract_version=int(row[8]),
            validated_content_hash=(
                str(direct[0]) if direct is not None else None
            ),
        )

    def get_session(
        self,
        project_id: str,
        session_id: str,
    ) -> PreparationSessionSummary:
        """Return one value-free session status projection."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT status, mapping_id, mapping_version,
                       physical_selection_hash, source_selection_hash,
                       mapping_hash, schema_hash, derived_plan_hash,
                       compiled_plan_hash, contract_version,
                       evaluator_version, source_hashes_json,
                       provisional_row_count, canonical_row_count,
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
                provisional_row_count=int(row[12]),
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
            self._validate_project_database_schema(connection)
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
        return {
            dataset_id: tuple(rows)
            for dataset_id, rows in grouped.items()
        }

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
            self._validate_project_database_schema(connection)
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

    def _iter_bound_impacts(
        self,
        project_id: str,
        session_id: str,
        *,
        connection=None,
        batch_size: int,
    ):
        """Join impact coordinates to one canonical row in bounded pages."""

        if connection is None:
            database_path = self.project_directory(project_id) / "project.duckdb"
            with self._connect(database_path) as owned_connection:
                self._validate_project_database_schema(owned_connection)
                yield from self._iter_bound_impacts(
                    project_id,
                    session_id,
                    connection=owned_connection,
                    batch_size=batch_size,
                )
            return

        canonical_session_id = self._session_id(session_id)
        direct = connection.execute(
            "SELECT 1 FROM canonical_staging_run WHERE run_id = ?",
            [canonical_session_id],
        ).fetchone() is not None
        canonical_table = (
            "canonical_staging_row" if direct else "preparation_final_row"
        )
        canonical_id = "run_id" if direct else "session_id"
        next_ordinal = 0
        while batch := connection.execute(
            f"""
            SELECT impact.ordinal, impact.impact_json, canonical.row_id
              FROM preparation_impact_row AS impact
              JOIN {canonical_table} AS canonical
                ON canonical.{canonical_id} = impact.session_id
               AND canonical.dataset = impact.dataset
               AND canonical.source_row = impact.source_row
             WHERE impact.session_id = ?
               AND impact.ordinal >= ?
             ORDER BY impact.ordinal
             LIMIT ?
            """,
            [canonical_session_id, next_ordinal, batch_size],
        ).fetchall():
            for ordinal, row_text, row_id in batch:
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
                yield impact, str(row_id)

    def mark_published(self, project_id: str, session_id: str) -> None:
        """Retain value-free status metadata and remove all temporary evidence."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
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
                self._delete_session_rows(connection, session_id)
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
            self._validate_project_database_schema(connection)
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

    def _restart_finalization(self, project_id: str, session_id: str) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._validate_project_database_schema(connection)
            connection.begin()
            try:
                status = connection.execute(
                    "SELECT status FROM preparation_session WHERE session_id = ?",
                    [self._session_id(session_id)],
                ).fetchone()
                if status is None or str(status[0]) not in {
                    PreparationSessionStatus.BUILDING.value,
                    PreparationSessionStatus.FINALIZING.value,
                }:
                    raise WorkspaceError("Preparation session cannot be finalized")
                connection.execute(
                    "DELETE FROM preparation_final_row WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_identity_group WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    "DELETE FROM preparation_finalization_row WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    """
                    INSERT INTO preparation_identity_group (
                        session_id, dataset, identity_hash, identity_count
                    )
                    SELECT session_id, dataset, identity_hash, COUNT(*)
                      FROM preparation_provisional_row
                     WHERE session_id = ?
                     GROUP BY session_id, dataset, identity_hash
                    """,
                    [session_id],
                )
                connection.execute(
                    """
                    INSERT INTO preparation_final_row (
                        session_id, ordinal, row_id, dataset, source_row,
                        target_model, disposition, row_json
                    )
                    SELECT session_id, ordinal, row_id, dataset, source_row,
                           target_model, disposition, record_json
                      FROM (
                          SELECT provisional.session_id,
                                 provisional.ordinal,
                                 provisional.row_id,
                                 provisional.dataset,
                                 provisional.source_row,
                                 provisional.target_model,
                                 provisional.disposition,
                                 provisional.record_json,
                                 identities.identity_count,
                                 COALESCE(
                                     provisional.payload_kind,
                                     'PREPARED'
                                 ) AS payload_kind
                            FROM preparation_provisional_row AS provisional
                            JOIN preparation_identity_group AS identities
                              ON identities.session_id = provisional.session_id
                             AND identities.dataset = provisional.dataset
                             AND identities.identity_hash =
                                 provisional.identity_hash
                           WHERE provisional.session_id = ?
                      ) AS ordered
                     WHERE payload_kind = 'CANONICAL'
                       AND identity_count = 1
                       AND row_id IS NOT NULL
                       AND disposition IS NOT NULL
                    """,
                    [session_id],
                )
                connection.execute(
                    """
                    INSERT INTO preparation_finalization_row (
                        session_id, ordinal, payload_kind, record_json,
                        identity_count,
                        physical_dataset_ids_json,
                        physical_source_rows_json
                    )
                    SELECT session_id, ordinal, payload_kind,
                           record_json,
                           identity_count,
                           to_json(physical_dataset_ids),
                           to_json(physical_source_rows)
                      FROM (
                          SELECT ordered.session_id,
                                 ordered.ordinal,
                                 ordered.dataset,
                                 ordered.source_row,
                                 ordered.payload_kind,
                                 ordered.record_json,
                                 ordered.identity_count,
                                 list(lineage.physical_dataset_id ORDER BY
                                      lineage.physical_dataset_id,
                                      lineage.physical_source_row)
                                     AS physical_dataset_ids,
                                 list(lineage.physical_source_row ORDER BY
                                      lineage.physical_dataset_id,
                                      lineage.physical_source_row)
                                     AS physical_source_rows
                            FROM (
                                SELECT provisional.session_id,
                                       provisional.ordinal,
                                       provisional.dataset,
                                       provisional.source_row,
                                       COALESCE(
                                           provisional.payload_kind,
                                           'PREPARED'
                                       ) AS payload_kind,
                                       provisional.record_json,
                                       identities.identity_count
                                  FROM preparation_provisional_row
                                       AS provisional
                                  JOIN preparation_identity_group AS identities
                                    ON identities.session_id =
                                       provisional.session_id
                                   AND identities.dataset = provisional.dataset
                                   AND identities.identity_hash =
                                       provisional.identity_hash
                                 WHERE provisional.session_id = ?
                            ) AS ordered
                            JOIN preparation_lineage AS lineage
                              ON lineage.session_id = ordered.session_id
                             AND lineage.dataset = ordered.dataset
                             AND lineage.output_source_row =
                                 ordered.source_row
                           WHERE ordered.payload_kind != 'CANONICAL'
                              OR ordered.identity_count != 1
                           GROUP BY ordered.session_id,
                                    ordered.ordinal,
                                    ordered.dataset,
                                    ordered.source_row,
                                    ordered.payload_kind,
                                    ordered.record_json,
                                    ordered.identity_count
                      ) AS grouped
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

    def _reconciliation(
        self,
        project_id: str,
        session_id: str,
        evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
        *,
        row_table: str = "preparation_final_row",
        id_column: str = "session_id",
    ) -> tuple[StagingReconciliation, tuple[StagingDatasetReconciliation, ...]]:
        if (row_table, id_column) not in {
            ("preparation_final_row", "session_id"),
            ("canonical_staging_row", "run_id"),
        }:
            raise ValueError("Preparation reconciliation source is invalid")
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            disposition_rows = connection.execute(
                f"""
                SELECT dataset, disposition, COUNT(*)
                  FROM {row_table}
                 WHERE {id_column} = ?
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
        database_path = self.project_directory(project_id) / "project.duckdb"
        expected_ordinal = 0
        with self._connect(database_path) as connection:
            while batch := connection.execute(
                """
                SELECT ordinal, row_id, dataset, source_row, target_model,
                       disposition, row_json
                  FROM canonical_staging_row
                 WHERE run_id = ? AND ordinal >= ?
                 ORDER BY ordinal
                 LIMIT ?
                """,
                [
                    run_id,
                    expected_ordinal,
                    PREPARATION_SESSION_ROW_BATCH_SIZE,
                ],
            ).fetchall():
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
                    encoded = str(row_text)
                    try:
                        row = CanonicalRow.from_dict(json.loads(encoded))
                        validate_canonical_row_bindings(
                            row,
                            source_selection_hash=(
                                bindings.source_selection_hash
                            ),
                            mapping_hash=bindings.mapping_hash,
                            schema_hash=bindings.schema_hash,
                            derived_plan_hash=bindings.derived_plan_hash,
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored direct preparation row is invalid"
                        ) from error
                    if (
                        row.row_id != str(row_id)
                        or row.dataset != str(dataset)
                        or row.source_row != int(source_row)
                        or row.target_model != str(target_model)
                        or row.disposition.value != str(disposition)
                    ):
                        raise WorkspaceError(
                            "Stored direct preparation row metadata is inconsistent"
                        )
                    hasher.add_encoded_array_item(encoded)
                    expected_ordinal += 1
        hasher.end_array()
        hasher.add_value("schema_hash", bindings.schema_hash)
        hasher.add_value(
            "source_selection_hash",
            bindings.source_selection_hash,
        )
        return hasher.finish(), expected_ordinal

    def _load_final_row_range(
        self,
        project_id: str,
        session_id: str,
        start: int,
        stop: int,
        *,
        direct: bool = False,
    ) -> tuple[CanonicalRow, ...]:
        if stop <= start:
            return ()
        database_path = self.project_directory(project_id) / "project.duckdb"
        table = (
            "canonical_staging_row" if direct else "preparation_final_row"
        )
        id_column = "run_id" if direct else "session_id"
        with self._connect(database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT row_json
                  FROM {table}
                 WHERE {id_column} = ?
                   AND ordinal >= ?
                   AND ordinal < ?
                 ORDER BY ordinal
                """,
                [self._session_id(session_id), start, stop],
            ).fetchall()
        return tuple(self._canonical_row(str(row[0])) for row in rows)

    def _iter_final_rows(
        self,
        project_id: str,
        session_id: str,
        *,
        direct: bool = False,
    ) -> Iterator[CanonicalRow]:
        database_path = self.project_directory(project_id) / "project.duckdb"
        table = (
            "canonical_staging_row" if direct else "preparation_final_row"
        )
        id_column = "run_id" if direct else "session_id"
        with self._connect(database_path) as connection:
            canonical_session_id = self._session_id(session_id)
            next_ordinal = 0
            while batch := connection.execute(
                f"""
                SELECT ordinal, row_json
                  FROM {table}
                 WHERE {id_column} = ?
                   AND ordinal >= ?
                 ORDER BY ordinal
                 LIMIT ?
                """,
                [
                    canonical_session_id,
                    next_ordinal,
                    PREPARATION_SESSION_ROW_BATCH_SIZE,
                ],
            ).fetchall():
                for ordinal, row_text in batch:
                    if int(ordinal) != next_ordinal:
                        raise WorkspaceError(
                            "Stored preparation final rows are not contiguous"
                        )
                    next_ordinal += 1
                    yield self._canonical_row(str(row_text))

    @staticmethod
    def _canonical_row(row_text: str) -> CanonicalRow:
        try:
            return CanonicalRow.from_dict(json.loads(row_text))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored preparation final row is invalid") from error

    @staticmethod
    def _require_status(
        connection,
        session_id: str,
        expected: PreparationSessionStatus,
    ):
        row = connection.execute(
            """
            SELECT status, provisional_row_count, impact_row_count
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
    def _delete_session_rows(connection, session_id: str) -> None:
        canonical = PreparationSessionRepository._session_id(session_id)
        for table in (
            "preparation_final_row",
            "preparation_impact_row",
            "preparation_physical_row",
            "preparation_lineage",
            "preparation_finalization_row",
            "preparation_identity_group",
            "preparation_direct_identity",
            "preparation_provisional_row",
            "preparation_session_snapshot",
        ):
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
