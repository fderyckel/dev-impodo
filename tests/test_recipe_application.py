"""Verify R3 Test TargetBinding and same-ish Recipe application behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.recipe_application_repository import (
    RecipeApplicationRepository,
)
from impodo.adapters.duckdb.recipe_repository import RecipeRepository
from impodo.application.recipe_application_service import RecipeApplicationService
from impodo.domain.recipe_applications import (
    RecipeApplicationDraft,
    RecipeApplicationError,
    RecipeApplicationState,
    RecipeControlValues,
    RecipeParameterValues,
    TargetBinding,
    TargetCredentialRole,
    TargetEnvironment,
    TargetProbeStatus,
)
from impodo.domain.mapping.validation.evidence import MappingValidationIssue
from impodo.models import target_identity_hash
from impodo.projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectService,
    ProjectStatus,
    SourceMode,
)
from impodo.quality import (
    QualityOutcomePolicy,
    QualityRuleFamily,
    manager_quality_rule,
)
from impodo.reference_keys import REFERENCE_POLICY_HASH
from impodo.recipes import DataVersionPurpose
from impodo.workspace_contracts import (
    MappingWorkingDraft,
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

from tests.test_recipe_authoring import _authoring_fixture, _file_binding


ROOT = Path(__file__).resolve().parents[1]


class _RecipeFacade:
    def __init__(self, recipe, data_version, envelope, candidate=None) -> None:
        self.recipe = recipe
        self.data_version = data_version
        self.envelope = envelope
        self.candidate = candidate
        self.projection = None

    def get(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return self.recipe

    def data_versions(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return (self.data_version,)

    def read_revision(self, recipe_id, version, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        assert version == 1
        return self.envelope

    def cutover_candidate(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return self.candidate

    def record_application_projection(self, **kwargs):
        self.projection = kwargs

    def update_data_version_parameter_values_hash(
        self,
        recipe_id,
        data_version_id,
        *,
        expected_hash,
        parameter_values_hash,
        actor,
    ):
        del actor
        assert recipe_id == self.recipe.recipe_id
        assert data_version_id == self.data_version.data_version_id
        assert expected_hash == self.data_version.parameter_values_hash
        self.data_version = replace(
            self.data_version,
            parameter_values_hash=parameter_values_hash,
        )
        return self.data_version


class _ApplicationState:
    def __init__(self, parameters, controls) -> None:
        self.parameters = parameters
        self.controls = controls
        self.target = None
        self.draft = None
        self.evidence = None
        self.quality_seed = None

    def get_target_binding(self, project_id):
        del project_id
        return self.target

    def save_target_binding(self, project_id, binding, *, actor):
        del project_id, actor
        self.target = binding

    def get_parameter_values(self, project_id):
        del project_id
        return self.parameters

    def save_parameter_values(self, project_id, values, *, actor):
        del project_id, actor
        self.parameters = values

    def get_control_values(self, project_id):
        del project_id
        return self.controls

    def save_control_values(self, project_id, values, *, actor):
        del project_id, actor
        self.controls = values

    def get_draft(self, project_id):
        del project_id
        return self.draft

    def save_draft(self, project_id, draft, *, expected_revision, actor):
        del project_id, actor
        actual = self.draft.revision if self.draft else None
        assert actual == expected_revision
        self.draft = draft

    def save_evidence_projection(self, project_id, **kwargs):
        del project_id
        self.evidence = kwargs

    def save_quality_seed(self, project_id, **kwargs):
        del project_id
        self.quality_seed = kwargs


class _MappingWorkspace:
    def __init__(self, selection, governance) -> None:
        self.mappings = self
        self.selection = selection
        self.governance = governance
        self.draft = None

    def get_mapping_working_draft(self, project_id):
        del project_id
        return self.draft

    def save_working_draft(self, project_id, *, datasets, expected_version, actor):
        del actor
        assert expected_version is None
        from impodo.domain.mapping.contracts import MappingDefinition

        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=self.selection.content_hash,
            schema_hash=self.governance.content_hash,
            datasets=tuple(datasets),
        )
        self.draft = MappingWorkingDraft(
            mapping_id=definition.mapping_id,
            version=1,
            project_id=project_id,
            base_mapping_version=None,
            definition=definition,
            updated_at=datetime.now(timezone.utc),
            updated_by="Manager",
        )
        return self.draft


class _ProtectedStore:
    def __init__(self) -> None:
        self.payload = None

    def put(self, recipe_id, *, payload, **kwargs):
        del recipe_id, kwargs
        self.payload = payload
        return SimpleNamespace(storage_key="protected/application.ipr")


class _PreparationState:
    def __init__(self) -> None:
        self.plan = None

    def get_derived_entity_plan(self, project_id):
        del project_id
        return self.plan

    def save_derived_entity_plan(
        self,
        project_id,
        plan,
        *,
        expected_parent_version,
        actor,
    ):
        del actor
        assert project_id == plan.project_id
        assert expected_parent_version is None
        self.plan = plan


def _service_fixture(
    *,
    renamed_name=False,
    readonly_name=False,
    categorical_issues=(),
    purpose=DataVersionPurpose.TEST,
    current_recipe_revision=1,
    recipe_parameters=(),
):
    authoring, authoring_facade, recipe = _authoring_fixture("6")
    for parameter in recipe_parameters:
        authoring.save_parameter_definition(
            recipe.recipe_id,
            name=parameter[0],
            label=parameter[1],
            value_type=parameter[2],
            required=parameter[3],
            actor=LOCAL_ACTOR,
        )
    authoring.publish_current(
        recipe.recipe_id,
        expected_recipe_revision=recipe.optimistic_revision,
        actor=LOCAL_ACTOR,
    )
    envelope = json.loads(authoring_facade.envelope)
    columns = (
        SourceDatasetColumn(1, "Customer code", "current:code", "string"),
        SourceDatasetColumn(
            2,
            "Full name" if renamed_name else "Name",
            "current:name",
            "string",
        ),
        SourceDatasetColumn(3, "Comment", "current:comment", "string"),
    )
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        project_id=authoring_facade.data_version.workspace_project_id,
        created_at=datetime.now(timezone.utc),
        created_by="Manager",
        datasets=(
            SourceDataset(
                dataset_id="current-customers",
                name="Customers",
                source=_file_binding("7"),
                row_count=4,
                columns=columns,
            ),
        ),
        content_hash="sha256:" + "7" * 64,
    )
    base_schema = authoring.schemas.schema
    base_model = base_schema.models[0]
    schema_fields = tuple(
        replace(field, readonly=True)
        if readonly_name and field.name == "name"
        else field
        for field in base_model.fields
    )
    project = MigrationProject(
        project_id=authoring_facade.data_version.workspace_project_id,
        name="Customer rehearsal",
        source_system="CSV export",
        source_mode=SourceMode.FILE,
        status=ProjectStatus.REGISTERED,
        odoo_connection_mode=OdooConnectionMode.REMOTE,
        odoo_base_url=(
            "https://production-odoo.example.invalid"
            if purpose is DataVersionPurpose.PRODUCTION
            else "https://test-odoo.example.invalid"
        ),
        odoo_database=(
            "customer_production"
            if purpose is DataVersionPurpose.PRODUCTION
            else "customer_test"
        ),
        intended_models=("res.partner",),
    )
    target_hash = target_identity_hash(
        connection_mode="REMOTE",
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )
    schema = replace(
        base_schema,
        project_id=project.project_id,
        database=project.odoo_database,
        connection_target_hash=target_hash,
        models=(replace(base_model, fields=schema_fields),),
    )
    governance = replace(
        authoring.schemas.governance,
        project_id=project.project_id,
        catalog_hash=schema.content_hash,
    )
    data_version = replace(
        authoring_facade.data_version,
        purpose=purpose,
        pinned_recipe_revision=1,
    )
    candidate = None
    if purpose is DataVersionPurpose.PRODUCTION:
        candidate = SimpleNamespace(
            cutover_candidate_id=str(uuid4()),
            recipe_revision=1,
        )
    recipe = replace(
        recipe,
        current_recipe_revision=current_recipe_revision,
        cutover_candidate_id=(
            candidate.cutover_candidate_id if candidate is not None else None
        ),
    )
    recipes = _RecipeFacade(recipe, data_version, envelope, candidate)
    sources = SimpleNamespace(
        get_source_selection=lambda project_id: selection,
        get_mapping_source_selection=lambda project_id: selection,
    )
    schemas = SimpleNamespace(
        schema=schema,
        governance=governance,
        get_odoo_schema_catalog=lambda project_id: schema,
        get_schema_governance=lambda project_id: governance,
    )
    parameters = RecipeParameterValues(
        data_version_id=data_version.data_version_id,
        values={"parameter:export_as_of_date": "2026-08-19"},
        source="DATA_MANAGER",
        reason="Recipe rehearsal",
        actor=LOCAL_ACTOR.identity,
        confirmed_at=datetime.now(timezone.utc),
    )
    data_version = replace(data_version, parameter_values_hash=parameters.content_hash)
    recipes.data_version = data_version
    controls = RecipeControlValues(
        data_version_id=data_version.data_version_id,
        values={},
        actor=LOCAL_ACTOR.identity,
        confirmed_at=datetime.now(timezone.utc),
    )
    applications = _ApplicationState(parameters, controls)
    mapping = _MappingWorkspace(selection, governance)
    store = _ProtectedStore()
    service = RecipeApplicationService(
        recipes=recipes,
        projects=None,
        project_reader=SimpleNamespace(get=lambda project_id: project),
        sources=sources,
        schemas=schemas,
        schema_workspace=SimpleNamespace(
            govern=lambda project_id, business_keys, actor: governance
        ),
        references=SimpleNamespace(get_reference_bundle=lambda project_id: None),
        preparation=_PreparationState(),
        applications=applications,
        mappings=mapping,
        categorical=SimpleNamespace(
            collect=lambda project_id, definition, selection, schema: SimpleNamespace(
                issues=tuple(categorical_issues)
            )
        ),
        store=store,
        authorization=CapabilityAuthorizationPolicy(),
    )
    return service, recipes, applications, mapping, store, schema


class RecipeApplicationTests(unittest.TestCase):
    def test_reviewed_country_reference_applies_with_or_without_model_capture(self):
        service, _recipes, _applications, _mapping, _store, schema = (
            _service_fixture()
        )
        partner = schema.models[0]
        country_relation = SchemaField(
            name="country_id",
            label="Country",
            type="many2one",
            required=False,
            readonly=False,
            relation="res.country",
            relation_field=None,
            selection=(),
        )
        schema_without_country = replace(
            schema,
            models=(
                replace(
                    partner,
                    fields=(*partner.fields, country_relation),
                ),
            ),
        )
        definition = {
            "contract_versions": {"odoo_target_contract": 2},
            "mapping": {"datasets": []},
            "odoo_target_contract": {
                "approved_write_fields": {},
                "business_keys": [],
                "models": [
                    {
                        "fields": [
                            {
                                "field_type": "char",
                                "name": "code",
                                "readonly": False,
                                "required": True,
                                "write_use": False,
                            }
                        ],
                        "model": "res.country",
                        "reference_evidence_kind": "REVIEWED_STANDARD",
                        "reference_paths": [
                            {
                                "key_fields": ["code"],
                                "parent_model": "res.partner",
                                "relationship_field": "country_id",
                                "relationship_type": "many2one",
                                "scope_fields": [],
                            }
                        ],
                    }
                ],
                "odoo_major_version": 19,
                "reference_policy_hash": REFERENCE_POLICY_HASH,
                "required_applications": [],
            },
        }

        absent_hash, absent_issues = service._target_assessment(
            definition,
            schema_without_country,
        )
        country = SchemaModel(
            name="res.country",
            label="Country",
            fields=(
                SchemaField(
                    name="code",
                    label="Code",
                    type="char",
                    required=True,
                    readonly=False,
                    relation=None,
                    relation_field=None,
                    selection=(),
                ),
            ),
        )
        captured_hash, captured_issues = service._target_assessment(
            definition,
            replace(
                schema_without_country,
                models=(*schema_without_country.models, country),
            ),
        )

        self.assertEqual(absent_issues, [])
        self.assertEqual(captured_issues, [])
        self.assertEqual(absent_hash, captured_hash)

    def test_same_shape_binds_exactly_and_ignores_new_unused_column(self):
        service, _recipes, _applications, _mapping, _store, schema = _service_fixture()
        review = service.review(
            service.recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )

        self.assertTrue(review.can_apply)
        self.assertEqual(
            review.source_bindings["dataset:customers"], "current-customers"
        )
        self.assertEqual(
            review.source_bindings["column:customers.name"], "current:name"
        )
        self.assertIn(
            "RECIPE_SOURCE_COLUMN_UNUSED", {item.code for item in review.issues}
        )
        self.assertIsNotNone(review.target_binding)
        self.assertNotIn("secret", review.target_binding.to_json().casefold())

    def test_renamed_used_column_requires_one_exact_application_override(self):
        service, _recipes, _applications, _mapping, _store, schema = _service_fixture(
            renamed_name=True
        )
        blocked = service.review(
            service.recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )
        self.assertFalse(blocked.can_apply)
        self.assertIn(
            "RECIPE_SOURCE_COLUMN_MISSING", {item.code for item in blocked.issues}
        )

        ready = service.review(
            service.recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            supplied_overrides={"column:customers.name": "current:name"},
            actor=LOCAL_ACTOR,
        )
        self.assertTrue(ready.can_apply)
        self.assertEqual(ready.source_bindings["column:customers.name"], "current:name")

    def test_target_and_credential_drift_block_only_the_application(self):
        service, recipes, _applications, _mapping, _store, schema = _service_fixture(
            readonly_name=True
        )
        stale = service.review(
            recipes.recipe.recipe_id,
            credential_generation="sha256:" + "9" * 64,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )
        codes = {item.code for item in stale.issues}
        self.assertIn("RECIPE_TARGET_FIELD_READONLY", codes)
        self.assertIn("TARGET_BINDING_STALE", codes)
        self.assertEqual(recipes.recipe.current_recipe_revision, 1)

    def test_production_uses_exact_selected_revision_and_production_binding(self):
        service, recipes, _applications, _mapping, _store, schema = _service_fixture(
            purpose=DataVersionPurpose.PRODUCTION,
            current_recipe_revision=2,
        )

        review = service.review(
            recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )

        self.assertTrue(review.can_apply)
        self.assertEqual(review.recipe_revision, 1)
        self.assertEqual(
            review.target_binding.environment, TargetEnvironment.PRODUCTION
        )

    def test_edited_parameters_remain_pinned_to_the_test_data_version(self):
        service, recipes, applications, _mapping, _store, _schema = _service_fixture()

        service.save_inputs(
            recipes.recipe.recipe_id,
            parameter_values={"parameter:export_as_of_date": "2026-08-18"},
            control_values={},
            overrides={},
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            recipes.data_version.parameter_values_hash,
            applications.parameters.content_hash,
        )

    def test_new_warehouse_value_changes_data_version_not_recipe_revision(self):
        service, recipes, applications, _mapping, _store, _schema = _service_fixture(
            recipe_parameters=(("warehouse", "Warehouse", "string", True),)
        )
        semantic_hash = service.recipes.envelope["semantic_hash"]

        service.save_inputs(
            recipes.recipe.recipe_id,
            parameter_values={
                "parameter:export_as_of_date": "2026-08-19",
                "parameter:warehouse": "WH-LUX",
            },
            control_values={},
            overrides={},
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(recipes.recipe.current_recipe_revision, 1)
        self.assertEqual(service.recipes.envelope["semantic_hash"], semantic_hash)
        self.assertEqual(
            applications.parameters.values["parameter:warehouse"],
            "WH-LUX",
        )

    def test_undeclared_stock_parameter_is_rejected_by_current_revision(self):
        service, recipes, _applications, _mapping, _store, _schema = _service_fixture(
            recipe_parameters=(("warehouse", "Warehouse", "string", True),)
        )

        with self.assertRaisesRegex(
            RecipeApplicationError,
            "parameter:warehouse_zone is not declared",
        ):
            service.save_inputs(
                recipes.recipe.recipe_id,
                parameter_values={
                    "parameter:export_as_of_date": "2026-08-19",
                    "parameter:warehouse": "WH-LUX",
                    "parameter:warehouse_zone": "ZONE-A",
                },
                control_values={},
                overrides={},
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(recipes.recipe.current_recipe_revision, 1)

    def test_uncovered_current_choices_remain_visible_after_blocked_apply(self):
        categorical_issue = MappingValidationIssue(
            code="MAPPING_CATEGORICAL_VALUE_UNCOVERED",
            severity="error",
            path="dataset:customers/name",
            message="Current value German is not covered by this Recipe.",
            remediation="Map German in a new Recipe revision and test it again.",
        )
        service, recipes, applications, mapping, _store, schema = _service_fixture(
            categorical_issues=(categorical_issue,),
        )

        evidence = service.apply(
            recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )
        review = service.review(
            recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(evidence.status, RecipeApplicationState.BLOCKED)
        self.assertIsNone(mapping.draft)
        self.assertEqual(applications.draft.state, RecipeApplicationState.BLOCKED)
        self.assertIn(
            "MAPPING_CATEGORICAL_VALUE_UNCOVERED",
            {item.code for item in review.issues},
        )
        self.assertFalse(review.can_apply)

    def test_apply_creates_fresh_mapping_and_protected_evidence(self):
        service, recipes, applications, mapping, store, schema = _service_fixture()
        evidence = service.apply(
            recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(evidence.status, RecipeApplicationState.APPLIED)
        self.assertIsNotNone(mapping.draft)
        self.assertEqual(
            mapping.draft.definition.datasets[0].dataset_id, "current-customers"
        )
        self.assertEqual(
            mapping.draft.definition.datasets[0].fields[0].source_column_key,
            "current:name",
        )
        self.assertIsNotNone(store.payload)
        self.assertEqual(
            recipes.projection["mapping_content_hash"], mapping.draft.content_hash
        )
        self.assertEqual(applications.draft.state, RecipeApplicationState.APPLIED)
        self.assertEqual(
            applications.quality_seed["mapping_content_hash"],
            mapping.draft.content_hash,
        )

    def test_apply_rebuilds_reusable_source_preparation_on_current_columns(self):
        service, recipes, _applications, _mapping, _store, schema = _service_fixture()
        service.recipes.envelope["recipe"]["source_preparation"] = {
            "rules": [
                {
                    "logical_rule_id": "preparation:lookup.languages",
                    "kind": "lookup",
                    "output_dataset_name": "languages",
                    "source_dataset_id": "dataset:customers",
                    "source_column_key": "column:customers.name",
                    "target_model": "res.lang",
                    "target_name_field": "name",
                    "external_id_namespace": "migration_lang",
                    "parent_separator": None,
                    "blank_policy": "block",
                }
            ]
        }

        evidence = service.apply(
            recipes.recipe.recipe_id,
            credential_generation=schema.read_credential_binding_hash,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(evidence.status, RecipeApplicationState.APPLIED)
        self.assertIsNotNone(service.preparation.plan)
        rule = service.preparation.plan.rules[0]
        self.assertEqual(rule.source_dataset_id, "current-customers")
        self.assertEqual(rule.source_column_key, "current:name")

    def test_duckdb_repository_round_trips_current_application_state(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            database = DuckDbDatabase(temporary)
            project_repository = ProjectRepository(database)
            project = ProjectService(
                project_repository,
                CapabilityAuthorizationPolicy(),
            ).create_project(
                actor=LOCAL_ACTOR,
                name="Customer rehearsal",
                source_system="CSV export",
            )
            recipe_repository = RecipeRepository(database)
            recipe = recipe_repository.list()[0]
            data_version = recipe_repository.data_versions(recipe.recipe_id)[0]
            repository = RecipeApplicationRepository(database)
            now = datetime.now(timezone.utc)
            parameters = RecipeParameterValues(
                data_version_id=data_version.data_version_id,
                values={"parameter:export_as_of_date": "2026-08-19"},
                source="DATA_MANAGER",
                reason="Repository round trip",
                actor=LOCAL_ACTOR.identity,
                confirmed_at=now,
            )
            controls = RecipeControlValues(
                data_version_id=data_version.data_version_id,
                values={},
                actor=LOCAL_ACTOR.identity,
                confirmed_at=now,
            )
            binding = TargetBinding(
                target_binding_id=str(uuid4()),
                environment=TargetEnvironment.TEST,
                endpoint="https://test-odoo.example.invalid",
                database="customer_test",
                connection_target_hash="sha256:" + "1" * 64,
                credential_role=TargetCredentialRole.READ,
                credential_generation="sha256:" + "2" * 64,
                credential_storage_class="SESSION",
                principal_hash="sha256:" + "3" * 64,
                permission_hash="sha256:" + "4" * 64,
                context_hash="sha256:" + "5" * 64,
                schema_dependency_hash="sha256:" + "6" * 64,
                reference_snapshot_hashes=(),
                probe_status=TargetProbeStatus.ACCEPTED,
                probed_at=now,
                captured_by=LOCAL_ACTOR.identity,
            )
            repository.save_parameter_values(
                project.project_id, parameters, actor=LOCAL_ACTOR
            )
            repository.save_control_values(
                project.project_id, controls, actor=LOCAL_ACTOR
            )
            repository.save_target_binding(
                project.project_id, binding, actor=LOCAL_ACTOR
            )
            quality_rule = manager_quality_rule(
                project_id=project.project_id,
                dataset="Customers",
                family=QualityRuleFamily.EQUALITY,
                name="Names agree",
                input_fields=("name", "display_name"),
                outcome=QualityOutcomePolicy.WARNING,
            )
            mapping_hash = "sha256:" + "a" * 64
            application_id = str(uuid4())
            repository.save_quality_seed(
                project.project_id,
                application_id=application_id,
                mapping_content_hash=mapping_hash,
                rules=(quality_rule,),
                actor=LOCAL_ACTOR,
            )
            draft = RecipeApplicationDraft(
                application_id=application_id,
                recipe_id=recipe.recipe_id,
                recipe_revision=1,
                data_version_id=data_version.data_version_id,
                workspace_project_id=project.project_id,
                target_binding_hash=binding.content_hash,
                source_selection_hash="sha256:" + "7" * 64,
                parameter_values_hash=parameters.content_hash,
                revision=1,
                state=RecipeApplicationState.READY,
                overrides={"column:customers.name": "physical:name"},
                issues=(),
                binding_hash="sha256:" + "8" * 64,
                target_assessment_hash="sha256:" + "9" * 64,
                updated_at=now,
                updated_by=LOCAL_ACTOR.identity,
            )
            repository.save_draft(
                project.project_id,
                draft,
                expected_revision=None,
                actor=LOCAL_ACTOR,
            )

            self.assertEqual(
                repository.get_parameter_values(project.project_id), parameters
            )
            self.assertEqual(
                repository.get_control_values(project.project_id), controls
            )
            self.assertEqual(repository.get_target_binding(project.project_id), binding)
            self.assertEqual(repository.get_draft(project.project_id), draft)
            self.assertEqual(
                repository.get_quality_seed(project.project_id, mapping_hash),
                (quality_rule,),
            )
            self.assertEqual(
                repository.get_quality_seed(
                    project.project_id,
                    "sha256:" + "b" * 64,
                ),
                (),
            )


if __name__ == "__main__":
    unittest.main()
