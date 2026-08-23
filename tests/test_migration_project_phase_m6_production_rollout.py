"""Verify M6 latest-data rollout without reusing Test authority or evidence."""

from __future__ import annotations

from dataclasses import replace
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.access import LOCAL_ACTOR
from impodo.adapters.duckdb.production_run_repository import (
    ProductionRunRepository,
)
from impodo.application.production_cutover_service import (
    ProductionCutoverService,
)
from impodo.data_versions import DataVersionPurpose, DataVersionState
from impodo.domain.serialization import content_hash
from impodo.migration_production import (
    ProductionRunBindingState,
    ProductionRunError,
)
from impodo.models import OdooReadIdentity, OdooWriteIdentity
from impodo.web.app import create_local_app
from tests import test_migration_project_phase_m4_multi_recipe_runs as m4
from tests import test_migration_project_phase_m5_cutover_qualification as m5


class MigrationProjectPhaseM6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.m5_fixture = m5.MigrationProjectPhaseM5Tests(
            methodName="test_plan_qualification_and_selection_pin_exact_test_evidence"
        )
        self.m5_fixture.setUp()
        self.fixture = self.m5_fixture.fixture
        self.test_bundle = self.fixture._start()
        review = self.m5_fixture.service.review(
            self.test_bundle.run.project_id,
            self.test_bundle.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )
        project = self.fixture.projects.get(
            self.test_bundle.run.project_id,
            actor=LOCAL_ACTOR,
        )
        self.qualification = self.m5_fixture.service.qualify(
            self.test_bundle.run.project_id,
            self.test_bundle.run.migration_run_id,
            expected_project_revision=project.optimistic_revision,
            expected_evidence_hash=str(review.integrated_evidence_hash),
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        project = self.fixture.projects.get(
            self.test_bundle.run.project_id,
            actor=LOCAL_ACTOR,
        )
        self.selection = self.m5_fixture.service.select(
            self.test_bundle.run.project_id,
            self.qualification.qualification_id,
            expected_project_revision=project.optimistic_revision,
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        self.production_repository = ProductionRunRepository(
            self.fixture.foundation
        )
        self.production = ProductionCutoverService(
            projects=self.fixture.projects,
            data_versions=self.fixture.data_versions,
            runs=self.fixture.runs,
            migration_workspaces=self.fixture.workspaces,
            source_packages=self.fixture.packages,
            workspace_states=self.fixture.workspace_states,
            cutover_plans=self.fixture.cutover_repository,
            production_runs=self.production_repository,
            run_planning=self.fixture.planning,
            authorization=self.fixture.authorization,
        )

    def tearDown(self) -> None:
        self.m5_fixture.tearDown()

    def _start_setup(self, *, operation_id: str | None = None):
        project = self.fixture.projects.get(
            self.test_bundle.run.project_id,
            actor=LOCAL_ACTOR,
        )
        return self.production.start_setup(
            project.project_id,
            expected_project_revision=project.optimistic_revision,
            cutover_selection_id=self.selection.cutover_selection_id,
            label="Production rollout 2026-08-23",
            export_as_of="2026-08-23T00:00:00Z",
            operation_id=operation_id or str(uuid4()),
            actor=LOCAL_ACTOR,
        )

    def _accept_latest_data(self, setup):
        return self.fixture._replace_and_freeze(
            setup.data_version,
            expected_package_revision=1,
        )

    def _production_schema(self, setup):
        read_generation = content_hash("production-read-generation")
        target_hash = content_hash("production-target")
        schema = replace(
            self.fixture.schema,
            project_id=setup.setup_workspace.workspace_id,
            database="production_2026",
            connection_target_hash=target_hash,
            read_credential_binding_hash=read_generation,
            read_principal_hash=content_hash("production-read-principal"),
            read_permission_hash=content_hash("production-read-permissions"),
            read_context_hash=content_hash("production-context"),
            content_hash=content_hash("production-schema"),
        )
        state = self.fixture.workspace_states.repository.get(
            setup.setup_workspace.workspace_id
        )
        self.fixture.workspace_states.update_target(
            state.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://production.example.test",
            odoo_database=schema.database,
            intended_applications=("base",),
            intended_models=("res.partner", "product.template"),
        )
        return schema

    @staticmethod
    def _write_identity(schema):
        return OdooWriteIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=content_hash("production-write-principal"),
            permission_hash=content_hash("production-write-permissions"),
            context_hash=schema.read_context_hash,
            readable_models=("product.template", "res.partner"),
            writable_models=("product.template", "res.partner"),
            observed_at="2026-08-23T00:05:00+00:00",
        )

    @staticmethod
    def _read_identity(schema):
        return OdooReadIdentity(
            target_hash=schema.connection_target_hash,
            principal_hash=schema.read_principal_hash,
            permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash,
            readable_models=("product.template", "res.partner"),
            observed_at="2026-08-23T00:04:00+00:00",
        )

    def _activate(
        self,
        setup,
        schema,
        *,
        operation_id: str | None = None,
        fault=None,
    ):
        project = self.fixture.projects.get(
            setup.run.project_id,
            actor=LOCAL_ACTOR,
        )
        return self.production.activate(
            setup.run.project_id,
            setup.run.migration_run_id,
            expected_project_revision=project.optimistic_revision,
            target_schema=schema,
            target_reference_bundle=None,
            read_credential_generation=schema.read_credential_binding_hash,
            write_identity=self._write_identity(schema),
            write_credential_generation=content_hash(
                "production-write-generation"
            ),
            parameter_values={},
            control_values={},
            operation_id=operation_id or str(uuid4()),
            actor=LOCAL_ACTOR,
            fault=fault,
        )

    def test_setup_creates_fresh_latest_data_and_no_write_authority(self):
        operation_id = str(uuid4())
        setup = self._start_setup(operation_id=operation_id)
        replay = self._start_setup(operation_id=operation_id)

        self.assertEqual(
            replay.binding.production_run_binding_id,
            setup.binding.production_run_binding_id,
        )
        self.assertEqual(setup.data_version.purpose, DataVersionPurpose.PRODUCTION)
        self.assertEqual(setup.data_version.state, DataVersionState.DRAFT)
        self.assertEqual(
            setup.data_version.parent_data_version_id,
            self.test_bundle.run.data_version_id,
        )
        self.assertNotEqual(
            setup.data_version.data_version_id,
            self.test_bundle.run.data_version_id,
        )
        self.assertNotEqual(
            setup.setup_workspace.workspace_id,
            next(iter(self.test_bundle.workspaces)).workspace_id,
        )
        self.assertEqual(setup.binding.state, ProductionRunBindingState.SETUP)
        self.assertIsNone(setup.binding.write_credential_generation)
        self.assertEqual(
            setup.binding.cutover_selection_id,
            self.selection.cutover_selection_id,
        )

    def test_latest_package_runs_on_a_different_target_with_fresh_authority(self):
        setup = self._start_setup()
        frozen = self._accept_latest_data(setup)
        schema = self._production_schema(setup)
        result = self._activate(setup, schema)
        binding = self.production_repository.get(setup.run.migration_run_id)

        production_data_version = self.fixture.data_versions.get(
            setup.data_version.data_version_id,
            actor=LOCAL_ACTOR,
        )
        test_package = self.fixture.packages.repository.get_source_package(
            self.test_bundle.run.data_version_id
        )
        self.assertEqual(production_data_version.state, DataVersionState.FROZEN)
        self.assertIsNotNone(test_package)
        self.assertNotEqual(
            frozen.content_hash,
            test_package.content_hash,
        )
        self.assertEqual(result.run.purpose.value, "PRODUCTION")
        self.assertEqual(binding.state, ProductionRunBindingState.ACTIVE)
        self.assertEqual(result.target_binding.environment, "PRODUCTION")
        self.assertNotEqual(
            result.target_binding.connection_target_hash,
            self.test_bundle.target_binding.connection_target_hash,
        )
        self.assertEqual(
            tuple(
                (item.recipe_id, item.recipe_revision)
                for item in result.requirement_plan.selected_revisions
            ),
            tuple(
                (item.recipe_id, item.recipe_revision)
                for item in self.fixture.cutover_repository.get_revision(
                    binding.cutover_plan_id,
                    binding.cutover_plan_revision,
                ).selected_revisions
            ),
        )
        self.assertEqual(len(result.applications), 2)
        self.assertEqual(len({item.workspace_id for item in result.applications}), 2)
        self.assertNotIn(
            setup.setup_workspace.workspace_id,
            {item.workspace_id for item in result.applications},
        )
        first_workspace = result.applications[0].workspace_id
        self.production.assert_execution_authority(
            first_workspace,
            read_identity=self._read_identity(schema),
            read_credential_generation=schema.read_credential_binding_hash,
            expected_read_credential_generation=(
                schema.read_credential_binding_hash
            ),
            write_identity=self._write_identity(schema),
            write_credential_generation=content_hash(
                "production-write-generation"
            ),
            actor=LOCAL_ACTOR,
        )
        with self.assertRaises(ProductionRunError):
            self.production.assert_execution_authority(
                first_workspace,
                read_identity=self._read_identity(schema),
                read_credential_generation=content_hash("rotated-read-key"),
                expected_read_credential_generation=(
                    schema.read_credential_binding_hash
                ),
                write_identity=self._write_identity(schema),
                write_credential_generation=content_hash(
                    "production-write-generation"
                ),
                actor=LOCAL_ACTOR,
            )
        rotated_generation = content_hash("rotated-read-key")
        self.production.assert_execution_authority(
            first_workspace,
            read_identity=self._read_identity(schema),
            read_credential_generation=rotated_generation,
            expected_read_credential_generation=rotated_generation,
            write_identity=self._write_identity(schema),
            write_credential_generation=content_hash(
                "rotated-production-write-generation"
            ),
            actor=LOCAL_ACTOR,
        )

    def test_target_reuse_is_blocked_and_browser_explains_separation(self):
        setup = self._start_setup()
        self._accept_latest_data(setup)
        reused_test_schema = replace(
            self.fixture.schema,
            project_id=setup.setup_workspace.workspace_id,
        )
        with self.assertRaises(ProductionRunError) as caught:
            self._activate(setup, reused_test_schema)
        self.assertIn("same Odoo target", str(caught.exception))
        self.assertEqual(
            self.production_repository.get(setup.run.migration_run_id).state,
            ProductionRunBindingState.SETUP,
        )

        app = create_local_app(
            self.fixture.root,
            launch_token="m6-launch",
            session_secret="m6-session",
            secret_store=self.fixture.secret_store,
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        with TestClient(app) as client:
            self.assertEqual(
                client.get(
                    "/launch?token=m6-launch",
                    follow_redirects=False,
                ).status_code,
                303,
            )
            response = client.get(f"/projects/{setup.run.project_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Production rollout", response.text)
        self.assertIn("separate", response.text.casefold())

    def test_activation_recovers_after_registry_commit_before_workspace_stores(self):
        setup = self._start_setup()
        self._accept_latest_data(setup)
        schema = self._production_schema(setup)
        operation_id = str(uuid4())

        def fault(stage):
            if stage == "REGISTRY_COMMITTED":
                raise m4.SimulatedCrash(stage)

        with self.assertRaises(m4.SimulatedCrash):
            self._activate(
                setup,
                schema,
                operation_id=operation_id,
                fault=fault,
            )
        recovered = self._activate(
            setup,
            schema,
            operation_id=operation_id,
        )

        self.assertEqual(
            {item.status.value for item in recovered.applications},
            {"READY"},
        )
        self.assertEqual(
            self.fixture.foundation.get_operation_intent(operation_id).state.value,
            "COMMITTED",
        )


if __name__ == "__main__":
    unittest.main()
