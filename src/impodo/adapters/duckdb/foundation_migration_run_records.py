"""Persist MigrationRun roots and target configuration records."""

from __future__ import annotations

from datetime import datetime
import json

from ...access import Actor
from ...domain.serialization import canonical_json
from ...migration_foundation import (
    MigrationConflictError,
    require_revision,
    require_uuid,
)
from ...domain.run.models import MigrationRun
from ...migration_run_setup import MigrationRunTargetSetup, OdooConnectionMode


class FoundationMigrationRunRecords:
    """Own MigrationRun reads, revisions, and target-setup persistence."""

    def next_run_number(self, project_id: str) -> int:
        return self._next_number(project_id, "migration_run", "run_number")

    def get_migration_run(self, migration_run_id: str) -> MigrationRun:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            row = self._exact_row(
                connection,
                table="migration_run",
                id_column="migration_run_id",
                identity=migration_run_id,
                expected_kind="MIGRATION_RUN",
            )
        return self._run_from_row(row)

    def migration_run_project_id(self, migration_run_id: str) -> str:
        return self.get_migration_run(migration_run_id).project_id

    def get_migration_run_target_setup(
        self,
        migration_run_id: str,
    ) -> MigrationRunTargetSetup | None:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            self._exact_row(
                connection,
                table="migration_run",
                id_column="migration_run_id",
                identity=migration_run_id,
                expected_kind="MIGRATION_RUN",
            )
            row = connection.execute(
                "SELECT * FROM migration_run_target_setup "
                "WHERE migration_run_id = ?",
                [migration_run_id],
            ).fetchone()
            columns = (
                [item[0] for item in connection.description]
                if row is not None
                else []
            )
        if row is None:
            return None
        value = dict(zip(columns, row, strict=True))
        return MigrationRunTargetSetup(
            migration_run_id=str(value["migration_run_id"]),
            project_id=str(value["project_id"]),
            revision=int(value["revision"]),
            connection_mode=OdooConnectionMode(str(value["connection_mode"])),
            base_url=str(value["base_url"]),
            database=str(value["database"]),
            intended_applications=tuple(
                str(item)
                for item in json.loads(str(value["intended_applications_json"]))
            ),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )

    def replace_migration_run_target_setup(
        self,
        setup: MigrationRunTargetSetup,
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> MigrationRunTargetSetup:
        with self._registry_transactions.transaction() as connection:
            run = connection.execute(
                "SELECT project_id FROM migration_run "
                "WHERE migration_run_id = ?",
                [setup.migration_run_id],
            ).fetchone()
            if run != (setup.project_id,):
                raise MigrationConflictError(
                    "MigrationRun target setup has inconsistent ownership"
                )
            current = connection.execute(
                "SELECT revision FROM migration_run_target_setup "
                "WHERE migration_run_id = ?",
                [setup.migration_run_id],
            ).fetchone()
            if current is None:
                if expected_revision is not None or setup.revision != 1:
                    raise MigrationConflictError(
                        "MigrationRun target setup changed; reload and retry"
                    )
                connection.execute(
                    "INSERT INTO migration_run_target_setup "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        setup.migration_run_id,
                        setup.project_id,
                        setup.revision,
                        setup.connection_mode.value,
                        setup.base_url,
                        setup.database,
                        canonical_json(list(setup.intended_applications)),
                        setup.updated_at.isoformat(),
                    ],
                )
            else:
                expected = require_revision(
                    expected_revision,
                    "expected_target_setup_revision",
                )
                if current != (expected,) or setup.revision != expected + 1:
                    raise MigrationConflictError(
                        "MigrationRun target setup changed; reload and retry"
                    )
                updated = connection.execute(
                    """
                    UPDATE migration_run_target_setup
                       SET revision = ?, connection_mode = ?, base_url = ?,
                           database = ?, intended_applications_json = ?,
                           updated_at = ?
                     WHERE migration_run_id = ? AND revision = ?
                     RETURNING migration_run_id
                    """,
                    [
                        setup.revision,
                        setup.connection_mode.value,
                        setup.base_url,
                        setup.database,
                        canonical_json(list(setup.intended_applications)),
                        setup.updated_at.isoformat(),
                        setup.migration_run_id,
                        expected,
                    ],
                ).fetchone()
                if updated is None:
                    raise MigrationConflictError(
                        "MigrationRun target setup changed; reload and retry"
                    )
            self._insert_event(
                connection,
                project_id=setup.project_id,
                aggregate_kind="MIGRATION_RUN_TARGET_SETUP",
                aggregate_id=setup.migration_run_id,
                aggregate_revision=setup.revision,
                event_type="MIGRATION_RUN_TARGET_SETUP_REPLACED",
                detail={},
                actor=actor,
                occurred_at=setup.updated_at,
            )
        saved = self.get_migration_run_target_setup(setup.migration_run_id)
        if saved is None:
            raise MigrationConflictError("MigrationRun target setup was not saved")
        return saved

    def list_migration_runs(self, project_id: str) -> tuple[MigrationRun, ...]:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            self._require_project(connection, project_id)
            rows = self._rows(
                connection,
                "SELECT * FROM migration_run WHERE project_id = ? "
                "ORDER BY run_number",
                [project_id],
            )
        return tuple(self._run_from_row(row) for row in rows)

    def save_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationRun:
        expected_revision = require_revision(expected_revision)
        if run.optimistic_revision != expected_revision:
            raise MigrationConflictError("MigrationRun revision is stale")
        new_revision = expected_revision + 1
        with self._registry_transactions.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE migration_run
                   SET label = ?, state = ?, target_binding_id = ?,
                       cutover_selection_id = ?, optimistic_revision = ?,
                       updated_at = ?, closed_at = ?
                 WHERE migration_run_id = ? AND optimistic_revision = ?
                 RETURNING migration_run_id
                """,
                [
                    run.label,
                    run.state.value,
                    run.target_binding_id,
                    run.cutover_selection_id,
                    new_revision,
                    run.updated_at.isoformat(),
                    self._time(run.closed_at),
                    run.migration_run_id,
                    expected_revision,
                ],
            ).fetchone()
            if updated is None:
                raise MigrationConflictError(
                    "MigrationRun changed; reload and retry"
                )
            self._insert_event(
                connection,
                project_id=run.project_id,
                aggregate_kind="MIGRATION_RUN",
                aggregate_id=run.migration_run_id,
                aggregate_revision=new_revision,
                event_type=event_type,
                detail={},
                actor=actor,
                occurred_at=run.updated_at,
            )
        return self.get_migration_run(run.migration_run_id)
