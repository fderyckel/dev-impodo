"""Verify delivery totals from Fresh data through saved compiler inputs."""

from dataclasses import replace
from datetime import UTC, datetime
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import MagicMock
from uuid import uuid4

from impodo.adapters.duckdb.test_run_repository import TestRunRepository
from impodo.adapters.duckdb.schema.migration_registry import (
    _create_test_run_parameter_values, ensure_migration_registry_schema,
)
from impodo.application.recipe_application_service import RecipeApplicationService
from impodo.application.run.fresh_data_setup import TestRunFreshDataUseCase
from impodo.application.run.test_setup_service import TestRunSetupService
from impodo.domain.data_version.models import DataVersionState
from impodo.domain.project.foundation import MigrationConflictError, MigrationFoundationError
from impodo.domain.recipe.control_values import normalize_recipe_control_values
from impodo.domain.recipe_applications import RecipeApplicationError, RecipeControlValues
from impodo.domain.run.contracts import RecipeRevisionSelection
from impodo.domain.run.test_setup import (
    RecipeRunControlValue, RecipeRunParameterValue, TestRunSetupBinding, TestRunValues,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.application.run import test_integrated_recipe_runs as fixtures


def _definition(name):
    return {
        "source_shape": {"datasets": [{
            "logical_dataset_id": "dataset:balances", "logical_name": "Balances", "columns": [],
        }]},
        "parameter_definitions": {"parameters": [{
            "logical_parameter_id": "parameter:batch", "label": "Batch", "type": "string",
            "required": True, "constraints": {},
        }]},
        "control_definitions": {"controls": [{
            "logical_control_id": "control:balance", "name": f"{name} balance",
            "dataset_id": "dataset:balances", "target_field": "amount", "calculation": "SUM",
            "unit": "EUR", "tolerance": "0.01", "invariant_expectation": False,
        }, {
            "logical_control_id": "control:zero", "name": "Clearing balance",
            "dataset_id": "dataset:balances", "target_field": "clearing", "calculation": "SUM",
            "unit": "EUR", "tolerance": "0", "invariant_expectation": True,
            "invariant_expected_total": "0",
        }]},
    }


class FreshDataControlTests(TestCase):
    def setUp(self):
        self.first, self.second = str(uuid4()), str(uuid4())
        self.definitions = {self.first: _definition("Opening"), self.second: _definition("Closing")}
        selections = tuple(RecipeRevisionSelection(
            recipe_id=recipe_id, recipe_revision=1, semantic_hash=content_hash(definition),
        ) for recipe_id, definition in self.definitions.items())
        self.binding = TestRunSetupBinding(
            test_run_setup_id=str(uuid4()), project_id=str(uuid4()), migration_run_id=str(uuid4()),
            data_version_id=str(uuid4()), setup_workspace_id=str(uuid4()), selected_revisions=selections,
            dependencies=(), state="SETUP", target_binding_id=None, created_at=datetime.now(UTC),
        )
        self.stored = None
        self.repository = MagicMock()
        self.repository.get.return_value = self.binding
        self.repository.get_run_values.side_effect = lambda run_id: self.stored
        self.repository.replace_run_values.side_effect = self._store
        self.recipes = MagicMock()
        self.recipes.read_revisions.return_value = {
            (selection.recipe_id, 1): NS(
                recipe=NS(recipe_id=selection.recipe_id, display_name=f"Recipe {index}", business_purpose="Balances"),
                envelope={"recipe": self.definitions[selection.recipe_id], "semantic_hash": selection.semantic_hash},
            ) for index, selection in enumerate(selections)
        }
        self.data_version = NS(state=DataVersionState.DRAFT, export_as_of="2026-08-24")
        self.data_versions = MagicMock()
        self.data_versions.get.return_value = self.data_version
        self.service = TestRunFreshDataUseCase(
            data_versions=self.data_versions, recipes=self.recipes, test_runs=self.repository,
            authorization=MagicMock(),
        )
        self.totals = {self.first: {"control:balance": "1250.50"}, self.second: {"control:balance": "0"}}

    def _store(self, values, **kwargs):
        self.stored = values
        return values

    def _save(self, *, totals=None, revision=None, batch="BATCH-1"):
        return self.service.replace_run_values(
            self.binding, {"parameter:batch": batch}, expected_revision=revision, actor=LOCAL_ACTOR,
            supplied_controls=self.totals if totals is None else totals,
        )

    def test_controls_stay_scoped_and_invariants_are_read_only(self):
        requirements = self.service.requirements(self.binding.migration_run_id, actor=LOCAL_ACTOR)
        plan = self.service.run_value_plan(self.binding, requirements, actor=LOCAL_ACTOR)
        self.assertEqual(len(plan.editable_values), 1)
        self.assertEqual(len(plan.editable_controls), 2)
        self.assertFalse(plan.ready_to_continue)
        self.assertEqual([item.supplied_value for item in plan.controls if item.automatic], ["0", "0"])
        self._save()
        ready = self.service.run_value_plan(self.binding, requirements, actor=LOCAL_ACTOR)
        self.assertTrue(ready.ready_to_continue)
        self.assertEqual(self.stored.controls_by_recipe, self.totals)
        self.assertEqual(self.stored.recipe_revisions, self.binding.selected_revisions)
        self.assertEqual(self.stored.contract_version, 2)

    def test_invalid_missing_and_undeclared_totals_do_not_save_parameters(self):
        for invalid in ("", "NaN", "Infinity", "-Infinity", "1e99999999", "wrong"):
            with self.subTest(invalid=invalid), self.assertRaises(MigrationFoundationError):
                self._save(totals={self.first: {"control:balance": invalid}, self.second: self.totals[self.second]})
        for invalid in (
            {}, {str(uuid4()): {"control:balance": "1"}},
            {self.first: {"control:zero": "5"}, self.second: self.totals[self.second]},
        ):
            with self.assertRaises(MigrationFoundationError):
                self._save(totals=invalid)
        self.repository.replace_run_values.assert_not_called()

    def test_stale_revision_and_frozen_changes_fail_but_retry_is_idempotent(self):
        saved = self._save()
        with self.assertRaises(MigrationConflictError):
            self._save(revision=None)
        self.data_version.state = DataVersionState.FROZEN
        self.assertIs(self._save(revision=1), saved)
        with self.assertRaises(MigrationConflictError):
            self._save(totals={self.first: {"control:balance": "100"}, self.second: self.totals[self.second]}, revision=1)
        self.assertIs(self.stored, saved)

    def test_legacy_frozen_answers_allow_missing_controls_once_without_changing_parameters(self):
        self._save()
        self.stored = replace(self.stored, contract_version=1, controls=(), recipe_revisions=())
        old_hash = self.stored.content_hash
        self.data_version.state = DataVersionState.FROZEN
        with self.assertRaises(MigrationConflictError):
            self._save(revision=1, batch="CHANGED")
        completed = self._save(revision=1)
        self.assertEqual(completed.revision, 2)
        self.assertNotEqual(completed.content_hash, old_hash)
        self.assertEqual(completed.contract_version, 2)

    def test_activation_reads_answers_once_and_passes_totals_into_compiled_controls(self):
        self._save()
        self.repository.get_run_values.reset_mock()
        values = self.service.activation_values(self.binding, "2026-08-24", actor=LOCAL_ACTOR)
        self.repository.get_run_values.assert_called_once()
        self.assertEqual(values.controls[self.first], {"control:balance": "1250.50", "control:zero": "0"})
        definition = {**self.definitions[self.first], "mapping": {"datasets": [{
            "logical_dataset_id": "dataset:balances", "target_model": "account.move", "mode": "create",
        }]}}
        compiler = RecipeApplicationService.__new__(RecipeApplicationService)
        mappings = compiler._mapping_datasets(
            definition, {"dataset:balances": "current-balances"}, None,
            RecipeControlValues(
                data_version_id=self.binding.data_version_id, values=values.controls[self.first],
                actor=LOCAL_ACTOR.identity, confirmed_at=datetime.now(UTC),
            ), None,
        )
        self.assertEqual({item.control_id: item.expected_total for item in mappings[0].control_expectations}, values.controls[self.first])
        planning = MagicMock()
        setup = TestRunSetupService.__new__(TestRunSetupService)
        setup.test_runs, setup.data_versions, setup.authorization = self.repository, self.data_versions, MagicMock()
        setup._fresh_data, setup.run_planning = self.service, planning
        setup.activate(self.binding.project_id, self.binding.migration_run_id,
            expected_workspace_revision=1, target_schema=object(), target_reference_bundle=None,
            credential_generation="credential", operation_id=str(uuid4()), actor=LOCAL_ACTOR)
        self.assertEqual(planning.activate_test_run.call_args.kwargs["control_values"], values.controls)

    def test_activation_refuses_missing_totals_and_different_recipe_evidence(self):
        with self.assertRaises(MigrationFoundationError):
            self.service.activation_values(self.binding, "2026-08-24", actor=LOCAL_ACTOR)
        self._save()
        revision = self.recipes.read_revisions.return_value[(self.first, 1)]
        revision.envelope["semantic_hash"] = content_hash("changed")
        with self.assertRaisesRegex(ValueError, "selected Recipe version has changed"):
            self.service.activation_values(self.binding, "2026-08-24", actor=LOCAL_ACTOR)

    def test_compiler_rejects_nonfinite_totals_and_changed_invariants(self):
        definitions = self.definitions[self.first]["control_definitions"]["controls"]
        for values in ({"control:balance": "NaN"}, {"control:zero": "10"}):
            with self.assertRaises(RecipeApplicationError):
                normalize_recipe_control_values(definitions, values)


class StoredRunControlTests(TestCase):
    def test_real_repository_preserves_legacy_hash_and_round_trips_new_contract(self):
        fixture = fixtures.IntegratedRecipeRunTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        repository = TestRunRepository(fixture.foundation)
        service = TestRunSetupService(
            projects=fixture.projects, data_versions=fixture.data_versions, runs=fixture.runs,
            migration_workspaces=fixture.workspaces, source_packages=fixture.packages,
            workspace_states=fixture.workspace_states, recipes=fixture.recipe_service,
            test_runs=repository, run_planning=fixture.planning, authorization=fixture.authorization,
        )
        project_id = fixture.bundle.project.project_id
        setup = service.start_setup(project_id,
            expected_workspace_revision=fixture.projects.get(project_id, actor=LOCAL_ACTOR).optimistic_revision,
            recipe_revisions=fixture._selected(), dependencies=(), label="Control persistence",
            export_as_of="2026-08-24", operation_id=str(uuid4()), actor=LOCAL_ACTOR)
        recipe_id = setup.binding.selected_revisions[0].recipe_id
        old = TestRunValues(
            test_run_setup_id=setup.binding.test_run_setup_id, project_id=project_id,
            migration_run_id=setup.run.migration_run_id, revision=1,
            values=(RecipeRunParameterValue(recipe_id, "parameter:batch", "BATCH"),),
            updated_by=LOCAL_ACTOR.identity, updated_at=datetime.now(UTC), contract_version=1,
        )
        self.assertEqual(repository.replace_run_values(old, expected_revision=None, actor=LOCAL_ACTOR), old)
        self.assertNotIn("controls", old.to_dict())
        with fixture.database.connect(fixture.database.registry_path) as connection:
            connection.execute("ALTER TABLE test_run_parameter_values RENAME TO run_values_current")
            _create_test_run_parameter_values(connection)
            connection.execute("INSERT INTO test_run_parameter_values SELECT * FROM run_values_current")
            connection.execute("DROP TABLE run_values_current")
            connection.execute("UPDATE schema_version SET version = 5")
            connection.execute("DROP TABLE production_run_values")
            ensure_migration_registry_schema(connection, fixture.database.registry_path)
        self.assertEqual(repository.get_run_values(old.migration_run_id).content_hash, old.content_hash)
        new = replace(old, revision=2, contract_version=2,
            recipe_revisions=setup.binding.selected_revisions,
            controls=(RecipeRunControlValue(recipe_id, "control:balance", "100.00"),))
        saved = repository.replace_run_values(new, expected_revision=1, actor=LOCAL_ACTOR)
        self.assertEqual(saved, new)
        with fixture.database.connect(fixture.database.registry_path) as connection:
            row = connection.execute("SELECT values_json FROM test_run_parameter_values WHERE migration_run_id = ?", [new.migration_run_id]).fetchone()
            self.assertEqual(json.loads(row[0])["controls"][0]["expected_total"], "100.00")
        with self.assertRaises(MigrationConflictError):
            repository.replace_run_values(replace(new, revision=3), expected_revision=1, actor=LOCAL_ACTOR)
        with self.assertRaisesRegex(MigrationConflictError, "selected Recipe revisions"):
            repository.replace_run_values(replace(new, revision=3, recipe_revisions=tuple(
                replace(item, semantic_hash=content_hash("another version")) for item in new.recipe_revisions
            )), expected_revision=2, actor=LOCAL_ACTOR)
        fixture.data_versions.repository.save_data_version(replace(
            setup.data_version, state=DataVersionState.FROZEN,
            source_package_hash=content_hash("delivery"), updated_at=datetime.now(UTC), frozen_at=datetime.now(UTC),
        ), expected_revision=setup.data_version.optimistic_revision, event_type="TEST_DATA_VERSION_FROZEN", actor=LOCAL_ACTOR)
        with self.assertRaisesRegex(MigrationConflictError, "accepted with this fresh data"):
            repository.replace_run_values(replace(new, revision=3,
                controls=(RecipeRunControlValue(recipe_id, "control:balance", "999"),),
            ), expected_revision=2, actor=LOCAL_ACTOR)
