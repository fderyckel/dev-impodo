"""Prove canonical ownership across registry and workbench stores."""

from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from io import BytesIO
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.domain.run.setup import OdooConnectionMode
from impodo.domain.workspace.models import MigrationWorkspaceSetupState
from impodo.web.app import create_local_app
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStatus


ROOT = REPOSITORY_ROOT


class CanonicalOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".tmp" / f"canonical-ownership-{uuid4()}"
        self.root.mkdir()
        self.app = create_local_app(self.root)
        self.context = self.app.state.context
        self.bundle = self.context.project_authoring.create(
            actor=self.context.actor,
            display_name="Thailand customer rollout",
            source_mode=SourceMode.FILE,
            creation_request_id=str(uuid4()),
            migration_purpose="Prepare customer data for Odoo 19",
            source_system_identity="Legacy ERP",
            data_classification="CONFIDENTIAL",
            retention_days=180,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_project_and_data_version_values_override_workbench_cache(self) -> None:
        workspace_id = self.bundle.workspace.workspace_id
        current = self.context.queries.get(workspace_id)
        with patch("impodo.application.data_version.intake.validate_source_file_isolated"):
            self.context.intake.accept(
                workspace_id,
                actor=self.context.actor,
                expected_revision=current.revision,
                display_name="customers.csv",
                stream=BytesIO(b"code,name\nC1,Customer One\n"),
            )
        package = self.context.data_versions.repository.get_source_package(
            self.bundle.data_version.data_version_id
        )
        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(len(package.files), 1)

        database_path = (
            self.context.workspace_states.repository.workspace_directory(
                workspace_id
            )
            / "workspace-engine.duckdb"
        )
        with duckdb.connect(str(database_path)) as connection:
            connection.execute(
                "UPDATE workspace_projection_cache SET name = ?, source_system = ?, "
                "data_classification = ?, retention_days = ?",
                ["Wrong workspace project", "Wrong ERP", "INTERNAL", 1],
            )
            connection.execute(
                "UPDATE source_file SET display_name = ?",
                ["wrong.csv"],
            )

        view = self.context.queries.get(workspace_id)
        self.assertEqual(view.name, self.bundle.workspace.display_name)
        self.assertEqual(view.source_system, "Legacy ERP")
        self.assertEqual(view.data_classification.value, "CONFIDENTIAL")
        self.assertEqual(view.retention_days, 180)
        self.assertEqual(view.source_files[0].display_name, "customers.csv")

    def test_registration_advances_only_the_workspace_setup_root(self) -> None:
        workspace_id = self.bundle.workspace.workspace_id
        current = self.context.queries.get(workspace_id)
        with patch("impodo.application.data_version.intake.validate_source_file_isolated"):
            self.context.intake.accept(
                workspace_id,
                actor=self.context.actor,
                expected_revision=current.revision,
                display_name="customers.csv",
                stream=BytesIO(b"code,name\nC1,Customer One\n"),
            )
        current = self.context.queries.get(workspace_id)
        registered = self.context.workspace_states.register(
            workspace_id,
            actor=self.context.actor,
            expected_revision=current.revision,
        )

        root = self.context.migration_workspaces.get(
            workspace_id,
            actor=self.context.actor,
        )
        self.assertEqual(root.setup_state, MigrationWorkspaceSetupState.READY)
        self.assertIsNotNone(root.setup_completed_at)
        self.assertEqual(registered.status, WorkspaceStatus.REGISTERED)
        self.assertEqual(
            self.context.queries.get(workspace_id).status,
            WorkspaceStatus.REGISTERED,
        )

    def test_one_run_target_setup_is_shared_by_all_its_workspaces(self) -> None:
        first_id = self.bundle.workspace.workspace_id
        first = self.context.queries.get(first_id)
        self.context.workspace_states.update_target(
            first_id,
            actor=self.context.actor,
            expected_revision=first.revision,
            odoo_connection_mode=OdooConnectionMode.REMOTE.value,
            odoo_base_url="https://odoo.example.test",
            odoo_database="rollout",
            intended_applications=("Contacts", "Sales"),
        )

        project = self.context.migration_projects.get(
            self.bundle.project.project_id,
            actor=self.context.actor,
        )
        second_root = self.context.migration_workspaces.create(
            project.project_id,
            actor=self.context.actor,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=self.bundle.data_version.data_version_id,
            migration_run_id=self.bundle.run.migration_run_id,
            display_name="Customer matching review",
        )
        self.context.workspace_states.provision_migration_workspace(
            second_root.workspace_id,
            actor=self.context.actor,
            name=second_root.display_name,
            source_system=project.source_system_identity,
            source_mode=SourceMode.FILE,
            data_classification=project.data_classification.value,
            retention_days=project.retention_days,
        )

        second = self.context.queries.get(second_root.workspace_id)
        self.assertEqual(second.odoo_base_url, "https://odoo.example.test")
        self.assertEqual(second.odoo_database, "rollout")
        self.assertEqual(second.intended_applications, ("Contacts", "Sales"))
        setup = self.context.migration_runs.repository.get_migration_run_target_setup(
            self.bundle.run.migration_run_id
        )
        self.assertIsNotNone(setup)
        assert setup is not None
        self.assertEqual(setup.project_id, self.bundle.project.project_id)
        self.assertEqual(setup.revision, 1)


if __name__ == "__main__":
    unittest.main()
