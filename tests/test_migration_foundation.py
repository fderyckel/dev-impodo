"""Verify the current Migration Project persistence foundation."""

from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb

from tests._database_probe import StatementCountingConnection

from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.schema.data_version_store import (
    DATA_VERSION_STORE_GENERATION,
    EXPECTED_DATA_VERSION_STORE_COLUMNS,
)
from impodo.adapters.duckdb.schema.migration_registry import (
    EXPECTED_REGISTRY_COLUMNS,
    MIGRATION_REGISTRY_GENERATION,
    MIGRATION_REGISTRY_VERSION,
)
from impodo.adapters.duckdb.schema.migration_workspace_store import (
    EXPECTED_WORKSPACE_STORE_COLUMNS,
    MIGRATION_WORKSPACE_GENERATION,
)
from impodo.application.data_version.service import DataVersionService
from impodo.web.composition.development_reset import (
    execute_development_reset,
    plan_development_reset,
)
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationIdentifierConfusionError,
    MigrationOperationReplayError,
    MigrationOperationState,
    MigrationStorageCompatibilityError,
)
from impodo.application.project.service import MigrationProjectService
from impodo.application.run.service import MigrationRunService
from impodo.application.workspace.service import MigrationWorkspaceService


ROOT = Path(__file__).resolve().parents[1]


class SimulatedCrash(RuntimeError):
    pass


def _crash_at(expected_stage: str):
    def crash(stage: str) -> None:
        if stage == expected_stage:
            raise SimulatedCrash(stage)

    return crash


class MigrationFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"migration-foundation-{uuid4()}"
        self.root.mkdir()
        self.database = MigrationFoundationDatabase(self.root)
        self.repository = MigrationFoundationRepository(self.database)
        authorization = CapabilityAuthorizationPolicy()
        self.projects = MigrationProjectService(self.repository, authorization)
        self.data_versions = DataVersionService(self.repository, authorization)
        self.runs = MigrationRunService(self.repository, authorization)
        self.workspaces = MigrationWorkspaceService(
            self.repository,
            authorization,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _project(self, *, operation_id: str | None = None):
        return self.projects.create(
            actor=LOCAL_ACTOR,
            display_name="Legacy ERP rollout",
            migration_purpose="Move governed master data to Odoo 19",
            source_system_identity="Fictional Legacy ERP",
            operation_id=operation_id,
        )

    def _data_version(
        self,
        project,
        *,
        operation_id: str | None = None,
        fault=None,
    ):
        return self.data_versions.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            purpose="AUTHORING",
            label="Representative export",
            operation_id=operation_id,
            fault=fault,
        )

    def _run(
        self,
        project,
        data_version,
        *,
        operation_id: str | None = None,
        fault=None,
    ):
        return self.runs.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            purpose="AUTHORING",
            label="Authoring run",
            operation_id=operation_id,
            fault=fault,
        )

    def _workspace(
        self,
        project,
        data_version,
        run,
        *,
        operation_id: str | None = None,
        fault=None,
    ):
        return self.workspaces.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Customer authoring",
            operation_id=operation_id,
            fault=fault,
        )

    def test_exact_registry_has_target_tables_and_no_recipe_first_shape(self) -> None:
        with self.database.connect(self.database.registry_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SHOW TABLES").fetchall()
            }
            generation = connection.execute(
                "SELECT generation, version FROM schema_version"
            ).fetchone()
            recipe_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('recipe')"
                ).fetchall()
            }
            data_version_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('data_version')"
                ).fetchall()
            }
        self.assertEqual(tables, set(EXPECTED_REGISTRY_COLUMNS))
        self.assertEqual(
            generation,
            (MIGRATION_REGISTRY_GENERATION, MIGRATION_REGISTRY_VERSION),
        )
        self.assertNotIn("project_registry", tables)
        self.assertNotIn("recipe_intent", tables)
        self.assertNotIn("current_data_version_id", recipe_columns)
        self.assertNotIn("cutover_candidate_id", recipe_columns)
        self.assertIn("project_id", data_version_columns)
        self.assertNotIn("recipe_id", data_version_columns)
        self.assertNotIn("workspace_project_id", data_version_columns)

    def test_roots_create_exact_relationships_and_distinct_identities(self) -> None:
        project = self._project()
        data_version = self._data_version(project)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run = self._run(project, data_version)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace = self._workspace(project, data_version, run)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)

        self.assertEqual(project.optimistic_revision, 4)
        self.assertEqual(
            len(
                {
                    project.project_id,
                    data_version.data_version_id,
                    run.migration_run_id,
                    workspace.workspace_id,
                }
            ),
            4,
        )
        self.assertEqual(data_version.project_id, project.project_id)
        self.assertEqual(run.data_version_id, data_version.data_version_id)
        self.assertEqual(workspace.migration_run_id, run.migration_run_id)
        self.assertEqual(workspace.data_version_id, data_version.data_version_id)
        self.assertIsNone(workspace.recipe_application_id)

        data_path = self.database.data_version_store_path(
            project.project_id,
            data_version.data_version_id,
        )
        workspace_path = self.database.workspace_store_path(
            project.project_id,
            workspace.workspace_id,
        )
        self.assertTrue(data_path.is_file())
        self.assertTrue(workspace_path.is_file())
        with self.database.connect(data_path) as connection:
            self.assertEqual(
                {
                    str(row[0])
                    for row in connection.execute("SHOW TABLES").fetchall()
                },
                set(EXPECTED_DATA_VERSION_STORE_COLUMNS),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT generation FROM schema_version"
                ).fetchone(),
                (DATA_VERSION_STORE_GENERATION,),
            )
        with self.database.connect(workspace_path) as connection:
            self.assertEqual(
                {
                    str(row[0])
                    for row in connection.execute("SHOW TABLES").fetchall()
                },
                set(EXPECTED_WORKSPACE_STORE_COLUMNS),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT generation FROM schema_version"
                ).fetchone(),
                (MIGRATION_WORKSPACE_GENERATION,),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT workspace_id, project_id, data_version_id,
                           migration_run_id, recipe_application_id
                      FROM workspace_linkage
                    """
                ).fetchone(),
                (
                    workspace.workspace_id,
                    project.project_id,
                    data_version.data_version_id,
                    run.migration_run_id,
                    None,
                ),
            )

        summary = self.projects.list(actor=LOCAL_ACTOR)[0]
        self.assertEqual(summary.data_version_count, 1)
        self.assertEqual(summary.run_count, 1)
        self.assertEqual(summary.workspace_count, 1)
        self.assertEqual(summary.recipe_count, 0)

    def test_project_list_is_registry_only_for_one_hundred_projects(self) -> None:
        for number in range(100):
            self.projects.create(
                actor=LOCAL_ACTOR,
                display_name=f"Project {number:03d}",
                migration_purpose="Bounded Project-list fixture",
                source_system_identity="Fictional ERP",
            )
        opened = []
        statements = []
        original_connect = self.database.connect

        def counted(path):
            opened.append(path)
            return StatementCountingConnection(original_connect(path), statements)

        with (
            patch.object(self.database, "connect", side_effect=counted),
            patch.object(
                self.database,
                "ensure_data_version_store",
                side_effect=AssertionError("Project list opened a DataVersion"),
            ),
            patch.object(
                self.database,
                "ensure_workspace_store",
                side_effect=AssertionError("Project list opened a workspace"),
            ),
        ):
            summaries = self.projects.list(actor=LOCAL_ACTOR)
        self.assertEqual(len(summaries), 100)
        self.assertTrue(all(item.recipe_count == 0 for item in summaries))
        self.assertEqual(opened, [self.database.registry_path])
        self.assertEqual(len(statements), 1)

    def test_authorization_rejects_each_root_before_creation(self) -> None:
        denied = Actor(
            identity=ActorIdentity(
                issuer="impodo.test",
                subject_id="denied",
                display_name="Denied actor",
            ),
            capabilities=frozenset(),
        )
        with self.assertRaises(AuthorizationError):
            self.projects.create(
                actor=denied,
                display_name="Denied",
                migration_purpose="Denied",
                source_system_identity="Denied",
            )
        project = self._project()
        with self.assertRaises(AuthorizationError):
            self.data_versions.create(
                project.project_id,
                actor=denied,
                expected_workspace_revision=project.optimistic_revision,
                purpose="AUTHORING",
                label="Denied",
            )
        data_version = self._data_version(project)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        with self.assertRaises(AuthorizationError):
            self.runs.create(
                project.project_id,
                actor=denied,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data_version.data_version_id,
                purpose="AUTHORING",
                label="Denied",
            )
        run = self._run(project, data_version)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        with self.assertRaises(AuthorizationError):
            self.workspaces.create(
                project.project_id,
                actor=denied,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data_version.data_version_id,
                migration_run_id=run.migration_run_id,
                display_name="Denied",
            )
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_project"
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM data_version").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM migration_run").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_workspace"
                ).fetchone(),
                (0,),
            )

    def test_optimistic_concurrency_is_enforced_for_every_root(self) -> None:
        project = self._project()
        data_version = self._data_version(project)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run = self._run(project, data_version)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace = self._workspace(project, data_version, run)
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)

        renamed_project = self.projects.rename(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.optimistic_revision,
            display_name="Renamed Project",
        )
        self.assertEqual(renamed_project.optimistic_revision, 5)
        with self.assertRaises(MigrationConflictError):
            self.projects.rename(
                project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=project.optimistic_revision,
                display_name="Stale Project",
            )

        renamed_data = self.data_versions.rename(
            data_version.data_version_id,
            actor=LOCAL_ACTOR,
            expected_revision=data_version.optimistic_revision,
            label="Renamed export",
        )
        self.assertEqual(renamed_data.optimistic_revision, 2)
        with self.assertRaises(MigrationConflictError):
            self.data_versions.rename(
                data_version.data_version_id,
                actor=LOCAL_ACTOR,
                expected_revision=data_version.optimistic_revision,
                label="Stale export",
            )

        renamed_run = self.runs.rename(
            run.migration_run_id,
            actor=LOCAL_ACTOR,
            expected_revision=run.optimistic_revision,
            label="Renamed run",
        )
        self.assertEqual(renamed_run.optimistic_revision, 2)
        with self.assertRaises(MigrationConflictError):
            self.runs.rename(
                run.migration_run_id,
                actor=LOCAL_ACTOR,
                expected_revision=run.optimistic_revision,
                label="Stale run",
            )

        closed = self.workspaces.close(
            workspace.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=workspace.optimistic_revision,
        )
        self.assertEqual(closed.optimistic_revision, 2)
        with self.assertRaises(MigrationConflictError):
            self.workspaces.close(
                workspace.workspace_id,
                actor=LOCAL_ACTOR,
                expected_revision=workspace.optimistic_revision,
            )

    def test_request_identity_is_idempotent_for_every_root(self) -> None:
        project_operation = str(uuid4())
        project = self._project(operation_id=project_operation)
        replayed_project = self._project(operation_id=project_operation)
        self.assertEqual(replayed_project.project_id, project.project_id)

        data_operation = str(uuid4())
        data_version = self._data_version(project, operation_id=data_operation)
        replayed_data = self._data_version(project, operation_id=data_operation)
        self.assertEqual(
            replayed_data.data_version_id,
            data_version.data_version_id,
        )

        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run_operation = str(uuid4())
        run = self._run(
            project,
            data_version,
            operation_id=run_operation,
        )
        replayed_run = self._run(
            project,
            data_version,
            operation_id=run_operation,
        )
        self.assertEqual(replayed_run.migration_run_id, run.migration_run_id)

        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace_operation = str(uuid4())
        workspace = self._workspace(
            project,
            data_version,
            run,
            operation_id=workspace_operation,
        )
        replayed_workspace = self._workspace(
            project,
            data_version,
            run,
            operation_id=workspace_operation,
        )
        self.assertEqual(replayed_workspace.workspace_id, workspace.workspace_id)

        with self.assertRaises(MigrationOperationReplayError):
            self.projects.create(
                actor=LOCAL_ACTOR,
                display_name="Different Project",
                migration_purpose="Different request meaning",
                source_system_identity="Different ERP",
                operation_id=project_operation,
            )
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_project"
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM data_version").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM migration_run").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_workspace"
                ).fetchone(),
                (1,),
            )

    def test_fault_injection_replays_each_root_without_duplicates(self) -> None:
        project_operation = str(uuid4())
        with self.assertRaises(SimulatedCrash):
            self.projects.create(
                actor=LOCAL_ACTOR,
                display_name="Legacy ERP rollout",
                migration_purpose="Move governed master data to Odoo 19",
                source_system_identity="Fictional Legacy ERP",
                operation_id=project_operation,
                fault=_crash_at("INTENT_RESERVED"),
            )
        self.assertEqual(
            self.repository.get_operation_intent(project_operation).state,
            MigrationOperationState.PENDING,
        )
        project = self._project(operation_id=project_operation)

        data_operation = str(uuid4())
        with self.assertRaises(SimulatedCrash):
            self._data_version(
                project,
                operation_id=data_operation,
                fault=_crash_at("REGISTRY_COMMITTED"),
            )
        self.assertEqual(
            self.repository.get_operation_intent(data_operation).state,
            MigrationOperationState.PENDING,
        )
        pending_data_id = self.repository.get_operation_intent(
            data_operation
        ).owner_id
        self.database.data_version_store_path(
            project.project_id,
            pending_data_id,
        ).parent.mkdir(parents=True)
        data_version = self._data_version(project, operation_id=data_operation)

        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run_operation = str(uuid4())
        with self.assertRaises(SimulatedCrash):
            self._run(
                project,
                data_version,
                operation_id=run_operation,
                fault=_crash_at("REGISTRY_COMMITTED"),
            )
        run = self._run(project, data_version, operation_id=run_operation)

        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace_operation = str(uuid4())
        with self.assertRaises(SimulatedCrash):
            self._workspace(
                project,
                data_version,
                run,
                operation_id=workspace_operation,
                fault=_crash_at("STORE_CREATED"),
            )
        workspace = self._workspace(
            project,
            data_version,
            run,
            operation_id=workspace_operation,
        )
        self.assertEqual(
            self.repository.get_operation_intent(workspace_operation).state,
            MigrationOperationState.COMMITTED,
        )
        self.assertTrue(
            self.database.workspace_store_path(
                project.project_id,
                workspace.workspace_id,
            ).is_file()
        )

    def test_identifier_namespaces_and_relationships_fail_closed(self) -> None:
        first = self._project()
        first_data = self._data_version(first)
        second = self.projects.create(
            actor=LOCAL_ACTOR,
            display_name="Second Project",
            migration_purpose="Separate governed migration",
            source_system_identity="Another ERP",
        )
        with self.assertRaises(MigrationIdentifierConfusionError):
            self.repository.get_data_version(first.project_id)
        with self.assertRaises(MigrationConflictError):
            self.runs.create(
                second.project_id,
                actor=LOCAL_ACTOR,
                expected_workspace_revision=second.optimistic_revision,
                data_version_id=first_data.data_version_id,
                purpose="AUTHORING",
                label="Cross-Project run",
            )


class MigrationFoundationResetTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.root = ROOT / ".tmp" / f"migration-foundation-reset-{uuid4()}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_retired_recipe_owned_registry_is_rejected_without_mutation(self) -> None:
        registry = self.root / "registry.duckdb"
        with duckdb.connect(str(registry)) as connection:
            connection.execute(
                "CREATE TABLE recipe (recipe_id VARCHAR PRIMARY KEY)"
            )
            connection.execute("INSERT INTO recipe VALUES ('obsolete-recipe')")
        with self.assertRaises(MigrationStorageCompatibilityError) as rejected:
            MigrationFoundationDatabase(self.root)
        self.assertEqual(rejected.exception.database_path, str(registry.resolve()))
        self.assertIn("reset-development-storage.py", rejected.exception.reset_command)
        with duckdb.connect(str(registry), read_only=True) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM recipe").fetchall(),
                [("obsolete-recipe",)],
            )
            self.assertEqual(
                connection.execute("SHOW TABLES").fetchall(),
                [("recipe",)],
            )
        self.assertFalse((self.root / "projects").exists())

        standalone_root = self.root / "standalone"
        obsolete_workspace = standalone_root / str(uuid4())
        obsolete_workspace.mkdir(parents=True)
        (obsolete_workspace / "workspace-engine.duckdb").write_bytes(
            b"retired-storage"
        )
        with self.assertRaises(MigrationStorageCompatibilityError):
            MigrationFoundationDatabase(standalone_root)
        self.assertFalse((standalone_root / "registry.duckdb").exists())
        self.assertTrue((obsolete_workspace / "workspace-engine.duckdb").is_file())

    def test_reset_requires_review_confirmation_and_development_mode(self) -> None:
        registry = self.root / "registry.duckdb"
        with duckdb.connect(str(registry)) as connection:
            connection.execute("CREATE TABLE recipe (recipe_id VARCHAR)")
        obsolete_workspace = self.root / str(uuid4())
        obsolete_workspace.mkdir()
        (obsolete_workspace / "workspace-engine.duckdb").write_bytes(b"fixture")

        plan = plan_development_reset(self.root)
        self.assertTrue(plan.can_execute)
        self.assertEqual(
            {item.name for item in plan.targets},
            {"registry.duckdb", obsolete_workspace.name},
        )
        with self.assertRaises(MigrationFoundationError):
            execute_development_reset(
                plan,
                confirmation_token=plan.confirmation_token,
                development_mode=False,
            )
        with self.assertRaises(MigrationFoundationError):
            execute_development_reset(
                plan,
                confirmation_token="RESET",
                development_mode=True,
            )
        self.assertTrue(registry.is_file())

        quarantine = execute_development_reset(
            plan,
            confirmation_token=plan.confirmation_token,
            development_mode=True,
        )
        self.assertFalse(registry.exists())
        self.assertTrue((quarantine / "registry.duckdb").is_file())
        self.assertTrue((quarantine / obsolete_workspace.name).is_dir())
        clean = MigrationFoundationDatabase(self.root)
        with clean.connect(clean.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT generation FROM schema_version"
                ).fetchone(),
                (MIGRATION_REGISTRY_GENERATION,),
            )

    def test_unknown_reset_entry_blocks_every_move(self) -> None:
        registry = self.root / "registry.duckdb"
        registry.write_bytes(b"retired-storage")
        unknown = self.root / "operator-notes.txt"
        unknown.write_text("Keep me", encoding="utf-8")
        plan = plan_development_reset(self.root)
        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.unknown_entries, (unknown.resolve(),))
        with self.assertRaises(MigrationFoundationError):
            execute_development_reset(
                plan,
                confirmation_token=plan.confirmation_token,
                development_mode=True,
            )
        self.assertTrue(registry.is_file())
        self.assertEqual(unknown.read_text(encoding="utf-8"), "Keep me")


if __name__ == "__main__":
    unittest.main()
