"""DuckDB preflight repository implementation."""

from __future__ import annotations

from uuid import UUID


from ...access import Actor
from ...projects import ProjectNotFoundError
from ...domain.preflight.reports import ReadinessReport
from ...workspace import WorkspaceError







class PreflightRepositoryMixin:
    """Persistence operations for preflight repository."""

    def get_readiness_report(
        self,
        project_id: str,
        mapping_id: str,
        mapping_version: int,
        mapping_content_hash: str,
        staging_run_id: str,
        staging_content_hash: str,
        quality_run_id: str,
        quality_content_hash: str,
    ) -> ReadinessReport | None:
        values = self._read_json_rows(
            project_id,
            """
            SELECT report_json
              FROM readiness_run
             WHERE mapping_id = ?
               AND mapping_version = ?
               AND mapping_content_hash = ?
               AND staging_run_id = ?
               AND staging_content_hash = ?
               AND quality_run_id = ?
               AND quality_content_hash = ?
             ORDER BY checked_at DESC, run_id
            """,
            [
                mapping_id,
                mapping_version,
                mapping_content_hash,
                staging_run_id,
                staging_content_hash,
                quality_run_id,
                quality_content_hash,
            ],
        )
        return ReadinessReport.from_json(values[0]) if values else None
    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        actor: Actor,
    ) -> None:
        try:
            canonical_run_id = str(UUID(report.run_id))
            canonical_staging_run_id = str(UUID(report.staging_run_id))
            canonical_quality_run_id = str(UUID(report.quality_run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Readiness run identifier is invalid") from error
        if report.project_id != project_id:
            raise WorkspaceError("Readiness report belongs to another project")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
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
                    SELECT run.staging_run_id, run.quality_run_id
                      FROM normalization_current AS current
                      JOIN normalization_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.status = 'FROZEN'
                    """
                ).fetchone()
                if normalization is None or (
                    str(normalization[0]) != canonical_staging_run_id
                    or str(normalization[1]) != canonical_quality_run_id
                ):
                    raise WorkspaceError(
                        "Approve the prepared data before saving an Odoo comparison"
                    )
                revision = self._project_revision(connection)
                connection.execute(
                    """
                    INSERT INTO readiness_run (
                        run_id, mapping_id, mapping_version,
                        mapping_content_hash, target_hash, staging_run_id,
                        staging_content_hash, quality_run_id,
                        quality_content_hash, checked_at, checked_by,
                        report_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        report.to_json(),
                    ],
                )
                connection.execute(
                    """
                    UPDATE project
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
        updated_project = self.get(project_id)
        self._update_registry(updated_project)
        self._write_registration_manifest(updated_project)
