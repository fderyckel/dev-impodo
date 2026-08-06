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
    PREPARATION_SESSION_MEMORY_LIMIT,
    PREPARATION_SESSION_ROW_BATCH_SIZE,
)
from .repository import DuckDbRepository
from .serialization import _canonical_json, _columnar_parameters


_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


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
            )
        normalized = index + self._row_count if index < 0 else index
        if normalized < 0 or normalized >= self._row_count:
            raise IndexError(index)
        rows = self._repository._load_final_row_range(
            self._project_id,
            self._session_id,
            normalized,
            normalized + 1,
        )
        if not rows:
            raise IndexError(index)
        return rows[0]

    def __iter__(self) -> Iterator[CanonicalRow]:
        yield from self._repository._iter_final_rows(
            self._project_id,
            self._session_id,
        )

    def iter_encoded_batches(self, connection, batch_size: int):
        """Read exact stored JSON through the publication transaction."""

        start = 0
        while batch := connection.execute(
            """
            SELECT ordinal, row_id, dataset, source_row, target_model,
                   disposition, row_json
              FROM preparation_final_row
             WHERE session_id = ?
               AND ordinal >= ?
             ORDER BY ordinal
             LIMIT ?
            """,
            [self._session_id, start, batch_size],
        ).fetchall():
            yield batch
            start += len(batch)


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

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
            self._migrate_project_database(connection)
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
                impact_values = [
                    [
                        session_id,
                        int(current[2]) + offset,
                        row.dataset,
                        row.source_row,
                        row.target_field,
                        row.outcome,
                        _canonical_json(
                            transformation_impact_to_portable_dict(row)
                        ),
                    ]
                    for offset, row in enumerate(impacts)
                ]
                if impact_values:
                    connection.execute(
                        """
                        INSERT INTO preparation_impact_row (
                            session_id, ordinal, dataset, source_row,
                            target_field, outcome, impact_json
                        )
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?),
                               unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(impact_values),
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
                        len(impact_values),
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
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
            self._migrate_project_database(connection)
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
            rows=_SessionCanonicalRows(self, project_id, session_id, row_count),
            issues=issues,
            reconciliation=reconciliation,
            compiled_plan_hash=str(row[7]),
            control_totals=controls,
            evaluator_version=int(row[9]),
            contract_version=int(row[8]),
        )

    def get_session(
        self,
        project_id: str,
        session_id: str,
    ) -> PreparationSessionSummary:
        """Return one value-free session status projection."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
            self._migrate_project_database(connection)
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
    ) -> Iterator[TransformationImpactRow]:
        """Yield persisted impacts in original deterministic emission order."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            cursor = connection.execute(
                """
                SELECT impact_json
                  FROM preparation_impact_row
                 WHERE session_id = ?
                 ORDER BY ordinal
                """,
                [self._session_id(session_id)],
            )
            while batch := cursor.fetchmany(PREPARATION_SESSION_ROW_BATCH_SIZE):
                for (row_text,) in batch:
                    try:
                        yield transformation_impact_from_portable_dict(
                            json.loads(str(row_text))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored preparation impact is invalid"
                        ) from error

    def mark_published(self, project_id: str, session_id: str) -> None:
        """Retain value-free status metadata and remove all temporary evidence."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.READY,
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
            self._migrate_project_database(connection)
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
            self._migrate_project_database(connection)
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
    ) -> tuple[StagingReconciliation, tuple[StagingDatasetReconciliation, ...]]:
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            disposition_rows = connection.execute(
                """
                SELECT dataset, disposition, COUNT(*)
                  FROM preparation_final_row
                 WHERE session_id = ?
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

    def _load_final_row_range(
        self,
        project_id: str,
        session_id: str,
        start: int,
        stop: int,
    ) -> tuple[CanonicalRow, ...]:
        if stop <= start:
            return ()
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            rows = connection.execute(
                """
                SELECT row_json
                  FROM preparation_final_row
                 WHERE session_id = ?
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
    ) -> Iterator[CanonicalRow]:
        database_path = self.project_directory(project_id) / "project.duckdb"
        with self._connect(database_path) as connection:
            cursor = connection.execute(
                """
                SELECT row_json
                  FROM preparation_final_row
                 WHERE session_id = ?
                 ORDER BY ordinal
                """,
                [self._session_id(session_id)],
            )
            while batch := cursor.fetchmany(PREPARATION_SESSION_ROW_BATCH_SIZE):
                for (row_text,) in batch:
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
            "preparation_provisional_row",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE session_id = ?",
                [canonical],
            )

    @staticmethod
    def _session_id(session_id: str) -> str:
        try:
            return str(UUID(session_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Preparation session identifier is invalid") from error
