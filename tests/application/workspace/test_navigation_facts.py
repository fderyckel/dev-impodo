"""Application boundaries for bounded workspace navigation facts."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.workspace.execution.navigation import (
    ExecutionNavigationState,
    ExecutionPreviewSummary,
)
from impodo.application.workspace.execution.service import ExecutionService
from impodo.application.workspace.navigation import (
    WorkspaceNavigationFacts,
    WorkspaceNavigationQueryService,
)
from impodo.application.workspace.views import WorkspaceOwnerViewService
from impodo.domain.shared.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
)


HASH = "sha256:" + "1" * 64


class WorkspaceNavigationQueryServiceTests(unittest.TestCase):
    def test_combines_one_durable_read_with_bounded_runtime_facts(self) -> None:
        workspace = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Products",
            source_system="CSV",
            source_mode=SourceMode.FILE,
        )
        state = ExecutionNavigationState(
            summary=ExecutionPreviewSummary(
                preflight_run_id=str(uuid4()),
                snapshot_hash=HASH,
                snapshot_root_hash=HASH,
                comparison_status="READY",
                create_count=3,
                update_count=2,
                unchanged_count=1,
                blocked_count=0,
                ambiguous_count=0,
                relationship_blocker_count=0,
                target_hash=HASH,
                target_odoo_version="19.0",
                read_credential_binding_hash="",
                read_principal_hash="",
                read_permission_hash="",
                read_context_hash="",
                execution_shape_ready=True,
            )
        )
        facts = WorkspaceNavigationFacts(
            workspace_id=workspace.workspace_id,
            execution_state=state,
        )
        repository = Mock()
        repository.get.return_value = facts
        execution = Mock()
        preview = SimpleNamespace(write_count=5)
        execution.navigation_preview.return_value = preview
        preparation_jobs = Mock()
        preparation_jobs.active.return_value = SimpleNamespace(job_id="prepare-1")
        load_jobs = Mock()
        load_jobs.active.return_value = SimpleNamespace(job_id="load-1")
        service = WorkspaceNavigationQueryService(
            repository,
            execution,
            preparation_jobs=preparation_jobs,
            load_jobs=load_jobs,
        )

        result = service.get(workspace)

        repository.get.assert_called_once_with(workspace.workspace_id)
        execution.navigation_preview.assert_called_once_with(
            state,
            workspace_state=workspace,
        )
        self.assertIs(result.execution_preview, preview)
        self.assertEqual(result.active_preparation_job_id, "prepare-1")
        self.assertEqual(result.active_load_job_id, "load-1")


class ExecutionNavigationPreviewTests(unittest.TestCase):
    def test_validates_compact_target_binding_without_loading_a_snapshot(self) -> None:
        workspace = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Products",
            source_system="CSV",
            source_mode=SourceMode.FILE,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="product_migration",
        )
        state = ExecutionNavigationState(
            summary=ExecutionPreviewSummary(
                preflight_run_id=str(uuid4()),
                snapshot_hash=HASH,
                snapshot_root_hash=HASH,
                comparison_status="READY",
                create_count=1,
                update_count=1,
                unchanged_count=0,
                blocked_count=0,
                ambiguous_count=0,
                relationship_blocker_count=0,
                target_hash=target_identity_hash(
                    connection_mode="LOCAL",
                    base_url=workspace.odoo_base_url,
                    database=workspace.odoo_database,
                ),
                target_odoo_version="19.0",
                read_credential_binding_hash="",
                read_principal_hash="",
                read_permission_hash="",
                read_context_hash="",
                execution_shape_ready=True,
            )
        )
        service = ExecutionService(
            Mock(),
            Mock(),
            Mock(),
            CapabilityAuthorizationPolicy(),
        )

        preview = service.navigation_preview(state, workspace_state=workspace)

        self.assertTrue(preview.can_load)
        self.assertEqual(preview.write_count, 2)
        self.assertEqual(preview.scope_error, "")

        changed_workspace = WorkspaceState(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            source_system=workspace.source_system,
            source_mode=workspace.source_mode,
            odoo_connection_mode=workspace.odoo_connection_mode,
            odoo_base_url=workspace.odoo_base_url,
            odoo_database="different_database",
        )
        stale = service.navigation_preview(
            state,
            workspace_state=changed_workspace,
        )
        self.assertFalse(stale.can_load)
        self.assertIn("target changed", stale.scope_error)


class WorkspaceOwnerViewServiceTests(unittest.TestCase):
    def test_uses_one_batched_owner_read_including_target_setup(self) -> None:
        project_id = str(uuid4())
        workspace_id = str(uuid4())
        data_version_id = str(uuid4())
        run_id = str(uuid4())
        context = SimpleNamespace(
            project_id=project_id,
            workspace_id=workspace_id,
            data_version_id=data_version_id,
            migration_run_id=run_id,
        )
        project = SimpleNamespace(project_id=project_id)
        workspace = SimpleNamespace(
            project_id=project_id,
            workspace_id=workspace_id,
            data_version_id=data_version_id,
            migration_run_id=run_id,
        )
        data_version = SimpleNamespace(
            project_id=project_id,
            data_version_id=data_version_id,
        )
        run = SimpleNamespace(
            project_id=project_id,
            migration_run_id=run_id,
            data_version_id=data_version_id,
        )
        target = SimpleNamespace(project_id=project_id, migration_run_id=run_id)
        repository = Mock()
        repository.get_workspace_owner_records.return_value = (
            project,
            workspace,
            data_version,
            run,
            target,
        )
        access = Mock()
        access.resolve.return_value = context
        service = WorkspaceOwnerViewService(repository, access)

        view = service.get(workspace_id, actor=LOCAL_ACTOR)

        repository.get_workspace_owner_records.assert_called_once_with(context)
        self.assertIs(view.target_setup, target)
        self.assertFalse(repository.get_source_package.called)
        self.assertFalse(repository.get_migration_run_target_setup.called)


if __name__ == "__main__":
    unittest.main()
