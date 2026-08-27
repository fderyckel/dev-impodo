"""Persist Stage H reports and protected Odoo snapshot evidence in DuckDB.

Layer: adapter. ``PreflightRepository`` is called by ``PreflightService`` after
the shared engine has produced deterministic decisions. It returns a report
only when every supplied upstream evidence binding still matches and publishes
the report, row projections, snapshots, current pointer, and audit event in one
database transaction.

See ``docs/architecture/python-code-map.md``,
``docs/developer/contracts/preflight.md``, and
``tests/application/workspace/review/test_preflight.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import datetime, timezone
from itertools import islice
import json
from uuid import UUID

from impodo.domain.shared.access import Actor
from impodo.domain.odoo.contracts import (
    MetadataSnapshot,
    RecordSnapshot,
    metadata_snapshot_payload,
    record_snapshot_json,
)
from ...domain.preflight.reports import (
    ReadinessReport,
    ReadinessRow,
    ReadinessRowPage,
)
from impodo.domain.shared.models import canonical_json_text, target_identity_hash
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError
from impodo.domain.workspace.errors import WorkspaceError
from .constants import PREFLIGHT_ROW_BATCH_SIZE
from .database import DuckDbWorkspaceDatabase
from .workspace_state_repository import WorkspaceStateRepository
from .repository import DuckDbRepository


class PreflightRepository(DuckDbRepository):
    """Own immutable readiness runs and the current compatible report pointer.

    Numeric Odoo IDs are permitted only in the protected target snapshots
    stored here. Portable reports and row projections remain bound to business
    identities and source trace IDs.
    """

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        workspaces: WorkspaceStateRepository,
    ) -> None:
        super().__init__(database)
        self._workspaces = workspaces

    def get_readiness_report(
        self,
        workspace_id: str,
        mapping_id: str,
        mapping_version: int,
        mapping_content_hash: str,
        staging_run_id: str,
        staging_content_hash: str,
        quality_run_id: str,
        quality_content_hash: str,
        normalization_run_id: str,
        normalization_content_hash: str,
        normalization_lifecycle_version: int,
        eligible_dataset_hash: str,
    ) -> ReadinessReport | None:
        """Return the current report only when all upstream bindings match."""

        values = self._read_json_rows(
            workspace_id,
            """
            SELECT run.report_json
              FROM preflight_current AS current
              JOIN readiness_run AS run ON run.run_id = current.run_id
             WHERE mapping_id = ?
               AND mapping_version = ?
               AND mapping_content_hash = ?
               AND staging_run_id = ?
               AND staging_content_hash = ?
               AND quality_run_id = ?
               AND quality_content_hash = ?
               AND normalization_run_id = ?
               AND normalization_content_hash = ?
               AND normalization_lifecycle_version = ?
               AND eligible_dataset_hash = ?
               AND current.singleton_id = 1
            """,
            [
                mapping_id,
                mapping_version,
                mapping_content_hash,
                staging_run_id,
                staging_content_hash,
                quality_run_id,
                quality_content_hash,
                normalization_run_id,
                normalization_content_hash,
                normalization_lifecycle_version,
                eligible_dataset_hash,
            ],
        )
        return ReadinessReport.from_json(values[0]) if values else None

    def save_readiness_report(
        self,
        workspace_id: str,
        report: ReadinessReport,
        *,
        decision_rows: Iterable[ReadinessRow],
        decision_count: int,
        metadata_snapshot: MetadataSnapshot,
        record_snapshot: RecordSnapshot,
        actor: Actor,
    ) -> None:
        """Atomically publish a report, rows, snapshots, pointer, and audit event.

        The report must match the current project target, submitted mapping,
        staging, quality, and frozen normalization evidence. Validation or any
        write failure rolls back the database transaction.
        """
        try:
            canonical_run_id = str(UUID(report.run_id))
            canonical_staging_run_id = str(UUID(report.staging_run_id))
            canonical_quality_run_id = str(UUID(report.quality_run_id))
            canonical_normalization_run_id = str(UUID(report.normalization_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Readiness run identifier is invalid") from error
        if report.workspace_id != workspace_id:
            raise WorkspaceError("Readiness report belongs to another workspace")
        if (
            metadata_snapshot.content_hash != report.metadata_snapshot_hash
            or record_snapshot.content_hash != report.record_snapshot_hash
            or metadata_snapshot.fingerprint != record_snapshot.fingerprint
            or metadata_snapshot.fingerprint.target_hash != report.target_hash
            or report.rows
            or decision_count < 0
        ):
            raise WorkspaceError("Readiness snapshot evidence is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                target = connection.execute(
                    """
                    SELECT odoo_connection_mode, odoo_base_url, odoo_database
                      FROM workspace_projection_cache
                    """
                ).fetchone()
                if target is None or target_identity_hash(
                    connection_mode=str(target[0] or ""),
                    base_url=str(target[1] or ""),
                    database=str(target[2] or ""),
                ) != report.target_hash:
                    raise WorkspaceError(
                        "Readiness report does not match the current Odoo target"
                    )
                submission = connection.execute(
                    """
                    SELECT submission_id
                      FROM mapping_submission
                     WHERE mapping_id = ?
                       AND version = ?
                       AND content_hash = ?
                     ORDER BY submitted_at DESC
                     LIMIT 1
                    """,
                    [
                        report.mapping_id,
                        report.mapping_version,
                        report.mapping_content_hash,
                    ],
                ).fetchone()
                if submission is None:
                    raise WorkspaceError(
                        "Readiness report does not match a submitted mapping"
                    )
                staging = connection.execute(
                    """
                    SELECT run.content_hash, run.mapping_id,
                           run.mapping_version, run.mapping_hash
                      FROM canonical_staging_current AS current
                      JOIN canonical_staging_run AS run
                        ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.run_id = ?
                       AND run.status = 'PUBLISHED'
                    """,
                    [canonical_staging_run_id],
                ).fetchone()
                if (
                    staging is None
                    or str(staging[0]) != report.staging_content_hash
                    or str(staging[1]) != report.mapping_id
                    or int(staging[2]) != report.mapping_version
                    or str(staging[3]) != report.mapping_content_hash
                ):
                    raise WorkspaceError(
                        "Readiness report does not match the current prepared data"
                    )
                quality = connection.execute(
                    """
                    SELECT run.content_hash, run.staging_run_id,
                           run.staging_content_hash
                      FROM quality_current AS current
                      JOIN quality_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.run_id = ?
                       AND run.status = 'PUBLISHED'
                    """,
                    [canonical_quality_run_id],
                ).fetchone()
                if quality is None or (
                    str(quality[0]) != report.quality_content_hash
                    or str(quality[1]) != canonical_staging_run_id
                    or str(quality[2]) != report.staging_content_hash
                ):
                    raise WorkspaceError(
                        "Readiness report does not match the current data checks"
                    )
                normalization = connection.execute(
                    """
                    SELECT run.content_hash, run.staging_run_id,
                           run.staging_content_hash, run.quality_run_id,
                           run.quality_content_hash,
                           run.eligible_dataset_hash,
                           run.lifecycle_version
                      FROM normalization_current AS current
                      JOIN normalization_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.run_id = ?
                       AND run.status = 'FROZEN'
                    """,
                    [canonical_normalization_run_id],
                ).fetchone()
                if normalization is None or (
                    str(normalization[0]) != report.normalization_content_hash
                    or str(normalization[1]) != canonical_staging_run_id
                    or str(normalization[2]) != report.staging_content_hash
                    or str(normalization[3]) != canonical_quality_run_id
                    or str(normalization[4]) != report.quality_content_hash
                    or str(normalization[5]) != report.eligible_dataset_hash
                    or int(normalization[6]) != report.normalization_lifecycle_version
                ):
                    raise WorkspaceError(
                        "Approve the prepared data before saving an Odoo comparison"
                    )
                revision = self._workspace_revision(connection)
                connection.execute(
                    """
                    INSERT INTO readiness_run (
                        run_id, mapping_id, mapping_version,
                        mapping_content_hash, target_hash, staging_run_id,
                        staging_content_hash, quality_run_id,
                        quality_content_hash, checked_at, checked_by,
                        report_json, normalization_run_id,
                        normalization_content_hash,
                        normalization_lifecycle_version,
                        eligible_dataset_hash, frozen_input_hash,
                        requirement_plan_hash, metadata_snapshot_hash,
                        record_snapshot_hash, result_hash, manifest_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_run_id,
                        report.mapping_id,
                        report.mapping_version,
                        report.mapping_content_hash,
                        report.target_hash,
                        canonical_staging_run_id,
                        report.staging_content_hash,
                        canonical_quality_run_id,
                        report.quality_content_hash,
                        report.checked_at.isoformat(),
                        report.checked_by,
                        replace(report, rows=()).to_json(),
                        canonical_normalization_run_id,
                        report.normalization_content_hash,
                        report.normalization_lifecycle_version,
                        report.eligible_dataset_hash,
                        report.frozen_input_hash,
                        report.requirement_plan_hash,
                        report.metadata_snapshot_hash,
                        report.record_snapshot_hash,
                        report.result_hash,
                        report.manifest_hash,
                    ],
                )
                dataset_values = [
                        [
                            canonical_run_id,
                            ordinal,
                            item.dataset,
                            canonical_json_text(asdict(item)),
                        ]
                        for ordinal, item in enumerate(report.datasets)
                    ]
                if dataset_values:
                    connection.executemany(
                        """
                        INSERT INTO preflight_dataset
                        VALUES (?, ?, ?, ?)
                        """,
                        dataset_values,
                    )
                row_iterator = iter(decision_rows)
                inserted_decisions = 0
                while batch := tuple(
                    islice(row_iterator, PREFLIGHT_ROW_BATCH_SIZE)
                ):
                    connection.executemany(
                        """
                        INSERT INTO preflight_decision (
                            run_id, ordinal, source_trace_id, dataset,
                            source_row, status, decision_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            [
                                canonical_run_id,
                                inserted_decisions + offset,
                                item.source_trace_id,
                                item.dataset,
                                item.source_row,
                                item.status,
                                canonical_json_text(asdict(item)),
                            ]
                            for offset, item in enumerate(batch)
                        ],
                    )
                    inserted_decisions += len(batch)
                if inserted_decisions != decision_count:
                    raise WorkspaceError(
                        "Readiness decision count changed during publication"
                    )
                connection.executemany(
                    """
                    INSERT INTO preflight_target_snapshot
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        [
                            canonical_run_id,
                            "metadata",
                            report.metadata_snapshot_hash,
                            canonical_json_text(
                                metadata_snapshot_payload(metadata_snapshot)
                            ),
                        ],
                        [
                            canonical_run_id,
                            "records",
                            report.record_snapshot_hash,
                            record_snapshot_json(record_snapshot),
                        ],
                    ],
                )
                stored = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM preflight_dataset WHERE run_id = ?),
                        (SELECT COUNT(*) FROM preflight_decision WHERE run_id = ?),
                        (SELECT COUNT(*) FROM preflight_target_snapshot WHERE run_id = ?)
                    """,
                    [canonical_run_id, canonical_run_id, canonical_run_id],
                ).fetchone()
                if stored is None or tuple(int(item) for item in stored) != (
                    len(report.datasets),
                    decision_count,
                    2,
                ):
                    raise WorkspaceError("Readiness evidence was not stored completely")
                previous = connection.execute(
                    "SELECT run_id FROM preflight_current WHERE singleton_id = 1"
                ).fetchone()
                if previous is not None:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO preflight_transition
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            str(previous[0]),
                            "SUPERSEDED",
                            datetime.now(timezone.utc).isoformat(),
                            actor.identity.display_name,
                            canonical_run_id,
                        ],
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO preflight_current VALUES (1, ?)
                    """,
                    [canonical_run_id],
                )
                connection.execute(
                    """
                    INSERT INTO preflight_transition VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_run_id,
                        "COMPLETED",
                        datetime.now(timezone.utc).isoformat(),
                        actor.identity.display_name,
                        report.result_hash,
                    ],
                )
                connection.execute(
                    """
                    UPDATE workspace_projection_cache
                       SET current_run_id = ?,
                           approval_status = 'REVIEW_REQUIRED'
                    """,
                    [canonical_run_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="READINESS_CHECK_COMPLETED",
                    detail=(
                        f"run {canonical_run_id}: {report.status}; "
                        f"{report.total_count} row(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._workspaces.synchronize_registration_artifacts(workspace_id)

    def get_readiness_rows(
        self,
        workspace_id: str,
        run_id: str,
        *,
        status: str = "",
        dataset: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> ReadinessRowPage:
        """Return one filtered page without loading the full decision set.

        Run IDs and page bounds are validated first. SQL applies dataset/status
        filters and stable ordinal ordering, preserving the engine's decision
        order while keeping browser memory bounded.
        """

        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Readiness run identifier is invalid") from error
        if page < 1 or page_size < 1 or page_size > 500:
            raise WorkspaceError("Readiness page request is invalid")
        clauses = ["run_id = ?"]
        parameters: list[object] = [canonical_run_id]
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if dataset:
            clauses.append("dataset = ?")
            parameters.append(dataset)
        where = " AND ".join(clauses)
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            count_row = connection.execute(
                f"SELECT COUNT(*) FROM preflight_decision WHERE {where}",
                parameters,
            ).fetchone()
            matching_count = int(count_row[0]) if count_row else 0
            page_count = max(1, (matching_count + page_size - 1) // page_size)
            bounded_page = min(page, page_count)
            rows = connection.execute(
                f"""
                SELECT decision_json
                  FROM preflight_decision
                 WHERE {where}
                 ORDER BY ordinal
                 LIMIT ? OFFSET ?
                """,
                [
                    *parameters,
                    page_size,
                    (bounded_page - 1) * page_size,
                ],
            ).fetchall()
        return ReadinessRowPage(
            items=tuple(
                ReadinessRow(**json.loads(str(row[0]))) for row in rows
            ),
            matching_count=matching_count,
            page=bounded_page,
            page_count=page_count,
        )
