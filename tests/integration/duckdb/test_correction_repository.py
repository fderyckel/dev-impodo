"""Verify atomic correction binding publication and current-pointer invalidation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from impodo.adapters.duckdb.correction_repository import CorrectionRepository
from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.migration_workspace_engine_database import (
    MigrationWorkspaceEngineDatabase,
)
from impodo.adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from impodo.application.correction_orchestration import CorrectionBinding
from impodo.application.data_version.service import DataVersionService
from impodo.application.project.service import MigrationProjectService
from impodo.application.run.service import MigrationRunService
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.domain.correction_origin import ProtectedCorrectionArtifactReference
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.run.models import MigrationRunState
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.workspace.models import MigrationWorkspaceState
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateService


IDS = tuple(f"{value:08d}-0000-4000-8000-000000000000" for value in range(1, 8))
HASHES = tuple("sha256:" + character * 64 for character in "123456789")
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)


class SimulatedCrash(RuntimeError):
    pass


class CorrectionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = MigrationFoundationDatabase(Path(self.temporary.name))
        self.foundation = MigrationFoundationRepository(database)
        authorization = CapabilityAuthorizationPolicy()
        self.projects = MigrationProjectService(self.foundation, authorization)
        self.data_versions = DataVersionService(self.foundation, authorization)
        self.runs = MigrationRunService(self.foundation, authorization)
        self.workspaces = MigrationWorkspaceService(self.foundation, authorization)
        self.corrections = CorrectionRepository(self.foundation)
        self.workspace_states = WorkspaceStateService(
            MigrationWorkspaceStateRepository(
                MigrationWorkspaceEngineDatabase(database),
                self.foundation,
            ),
            authorization,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _roots(self):
        project = self.projects.create(
            actor=LOCAL_ACTOR,
            display_name="Correction fixture",
            migration_purpose="Verify completed-load correction",
            source_system_identity="Fixture ERP",
        )
        data_version = self.data_versions.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            purpose="AUTHORING",
            label="Accepted source",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run = self.runs.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            purpose="AUTHORING",
            label="Completed load",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace = self.workspaces.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Completed mapping",
        )
        return project, data_version, run, workspace

    @staticmethod
    def _reference(artifact_id: str, logical_hash: str, name: str):
        return ProtectedCorrectionArtifactReference(
            artifact_id=artifact_id,
            logical_hash=logical_hash,
            storage_key=f"project/{name}/{artifact_id}.ipe",
            artifact_hash=HASHES[8],
        )

    def _binding(self, project, data_version, run, workspace):
        return CorrectionBinding(
            correction_binding_id=IDS[0],
            project_id=project.project_id,
            data_version_id=data_version.data_version_id,
            completed_migration_run_id=run.migration_run_id,
            completed_workspace_id=workspace.workspace_id,
            origin=self._reference(IDS[1], HASHES[0], "origin"),
            target_index=self._reference(IDS[2], HASHES[1], "index"),
            successor_migration_run_id=None,
            successor_workspace_id=None,
            current_mapping_hash=None,
            current_prepared_hash=None,
            current_plan=None,
            current_confirmation=None,
            optimistic_revision=1,
            created_at=NOW,
            updated_at=NOW,
        )

    def test_origin_visibility_closes_run_and_workspace_in_one_transaction(self) -> None:
        project, data_version, run, workspace = self._roots()
        self.workspace_states.provision_migration_workspace(
            workspace.workspace_id,
            actor=LOCAL_ACTOR,
            name=workspace.display_name,
            source_system=project.source_system_identity,
            source_mode="FILE",
            data_classification=project.data_classification.value,
            retention_days=project.retention_days,
        )
        candidate = self._binding(project, data_version, run, workspace)

        published = self.corrections.seal_completed_origin(
            candidate,
            expected_run_revision=run.optimistic_revision,
            expected_workspace_revision=workspace.optimistic_revision,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(published.origin, candidate.origin)
        self.assertEqual(
            self.runs.get(run.migration_run_id, actor=LOCAL_ACTOR).state,
            MigrationRunState.COMPLETED,
        )
        self.assertEqual(
            self.workspaces.get(workspace.workspace_id, actor=LOCAL_ACTOR).state,
            MigrationWorkspaceState.CLOSED,
        )
        with self.assertRaisesRegex(WorkspaceError, "closed and read-only"):
            self.workspace_states.repository._database.assert_workspace_mutable(
                workspace.workspace_id
            )
        replay = self.corrections.seal_completed_origin(
            candidate,
            expected_run_revision=run.optimistic_revision,
            expected_workspace_revision=workspace.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(replay, published)
        with self.assertRaisesRegex(MigrationFoundationError, "historical evidence"):
            self.runs.rename(
                run.migration_run_id,
                actor=LOCAL_ACTOR,
                expected_revision=run.optimistic_revision + 1,
                label="Rewritten history",
            )

    def test_fault_before_registry_commit_leaves_all_owners_open(self) -> None:
        project, data_version, run, workspace = self._roots()
        candidate = self._binding(project, data_version, run, workspace)

        def crash(stage: str) -> None:
            if stage == "BEFORE_REGISTRY_COMMIT":
                raise SimulatedCrash(stage)

        with self.assertRaises(SimulatedCrash):
            self.corrections.seal_completed_origin(
                candidate,
                expected_run_revision=run.optimistic_revision,
                expected_workspace_revision=workspace.optimistic_revision,
                actor=LOCAL_ACTOR,
                fault=crash,
            )

        self.assertIsNone(
            self.corrections.get_for_completed_workspace(workspace.workspace_id)
        )
        self.assertEqual(
            self.runs.get(run.migration_run_id, actor=LOCAL_ACTOR).state,
            MigrationRunState.DRAFT,
        )
        self.assertEqual(
            self.workspaces.get(workspace.workspace_id, actor=LOCAL_ACTOR).state,
            MigrationWorkspaceState.OPEN,
        )

    def test_successor_plan_pointer_is_replaced_by_invalidation_not_a_state(self) -> None:
        project, data_version, run, workspace = self._roots()
        current = self.corrections.seal_completed_origin(
            self._binding(project, data_version, run, workspace),
            expected_run_revision=run.optimistic_revision,
            expected_workspace_revision=workspace.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        successor_run = self.runs.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            purpose="AUTHORING",
            label="Correction successor",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        successor_workspace = self.workspaces.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=successor_run.migration_run_id,
            display_name="Correction mapping",
        )
        current = self.corrections.attach_successor(
            workspace.workspace_id,
            successor_migration_run_id=successor_run.migration_run_id,
            successor_workspace_id=successor_workspace.workspace_id,
            expected_revision=current.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        plan = self._reference(IDS[3], HASHES[2], "plan")
        current = self.corrections.publish_plan(
            workspace.workspace_id,
            successor_workspace_id=successor_workspace.workspace_id,
            mapping_hash=HASHES[3],
            prepared_hash=HASHES[4],
            plan=plan,
            expected_revision=current.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(current.current_plan, plan)

        invalidated = self.corrections.invalidate_plan(
            workspace.workspace_id,
            current_mapping_hash=HASHES[5],
            current_prepared_hash=None,
            expected_revision=current.optimistic_revision,
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(invalidated.current_plan)
        self.assertEqual(invalidated.current_mapping_hash, HASHES[5])
        self.assertEqual(
            self.runs.get(successor_run.migration_run_id, actor=LOCAL_ACTOR).state,
            MigrationRunState.DRAFT,
        )


if __name__ == "__main__":
    unittest.main()
