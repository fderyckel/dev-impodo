"""Verify Recipe-native creation and portable authoring publication."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from uuid import uuid4

import duckdb
from fastapi.testclient import TestClient

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.recipe_authoring_service import RecipeAuthoringService
from impodo.application.recipe_service import RecipeService
from impodo.domain.mapping.artifacts import MappingRevision, MappingSubmission
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.recipe_parameters import (
    RecipeParameterDefinition,
    RecipeParameterDefinitions,
    RecipeParameterType,
)
from impodo.projects import ProjectStatus
from impodo.quality import default_quality_ruleset
from impodo.recipes import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
    Recipe,
    RecipeConflictError,
    RecipeDraftState,
)
from impodo.secrets import MemorySecretStore
from impodo.web.app import create_local_app
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


ROOT = Path(__file__).resolve().parents[1]
POST_HEADERS = {
    "Origin": "http://testserver",
    "Sec-Fetch-Site": "same-origin",
}
UUID_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class _RecipeFacade:
    def __init__(self, recipe, data_version) -> None:
        self.recipe = recipe
        self.data_version = data_version
        self.envelope: bytes | None = None

    def get(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return self.recipe

    def data_versions(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return (self.data_version,)

    def publish_revision(self, recipe_id, *, envelope_bytes, **kwargs):
        del kwargs
        assert recipe_id == self.recipe.recipe_id
        self.envelope = envelope_bytes
        return object()


class _Evidence:
    def __init__(
        self,
        *,
        selection,
        revision,
        submission,
        schema,
        governance,
        ruleset,
        parameter_definitions=RecipeParameterDefinitions(),
        base_selection=None,
        preparation=None,
        reference_bundle=None,
    ) -> None:
        self.selection = selection
        self.revision = revision
        self.submission = submission
        self.schema = schema
        self.governance = governance
        self.ruleset = ruleset
        self.parameter_definitions = parameter_definitions
        self.base_selection = base_selection or selection
        self.preparation = preparation
        self.reference_bundle = reference_bundle

    def get_mapping_source_selection(self, project_id):
        del project_id
        return self.selection

    def get_source_selection(self, project_id):
        del project_id
        return self.base_selection

    def get_mapping_revision(self, project_id, version=None):
        del project_id, version
        return self.revision

    def get_mapping_submission(self, project_id, version=None):
        del project_id, version
        return self.submission

    def get_odoo_schema_catalog(self, project_id):
        del project_id
        return self.schema

    def get_schema_governance(self, project_id):
        del project_id
        return self.governance

    def get_current_quality_ruleset(self, project_id):
        del project_id
        return self.ruleset

    def get_derived_entity_plan(self, project_id):
        del project_id
        return self.preparation

    def get_reference_bundle(self, project_id):
        del project_id
        return self.reference_bundle

    def get_parameter_definitions(self, project_id):
        del project_id
        return self.parameter_definitions

    def save_parameter_definitions(self, project_id, definitions, *, actor):
        del project_id, actor
        self.parameter_definitions = definitions


def _authoring_fixture(marker: str, *, name_required: bool = True):
    now = datetime.now(timezone.utc)
    project_id = str(uuid4())
    recipe_id = str(uuid4())
    data_version_id = str(uuid4())
    dataset_id = f"physical-dataset-{marker}-{uuid4()}"
    source_hash = "sha256:" + marker * 64
    schema_hash = "sha256:" + chr(ord(marker) + 1) * 64
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        project_id=project_id,
        created_at=now,
        created_by="Manager",
        datasets=(
            SourceDataset(
                dataset_id=dataset_id,
                name="Customers",
                source=_file_binding(marker),
                row_count=3,
                columns=(
                    SourceDatasetColumn(1, "Customer code", "physical:code", "string"),
                    SourceDatasetColumn(2, "Name", "physical:name", "string"),
                ),
            ),
        ),
        content_hash=source_hash,
    )
    governance = SchemaGovernance(
        governance_id=str(uuid4()),
        version=1,
        project_id=project_id,
        catalog_hash=schema_hash,
        permitted_models=("res.partner",),
        business_keys=(
            BusinessKeyDefinition(
                key_id="partner-ref",
                model="res.partner",
                key_fields=("ref",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
        recorded_at=now,
        recorded_by="Manager",
    )
    mapping_id = str(uuid4())
    definition = MappingDefinition(
        mapping_id=mapping_id,
        source_selection_hash=source_hash,
        schema_hash=governance.content_hash,
        datasets=(
            DatasetMapping(
                dataset_id=dataset_id,
                target_model="res.partner",
                source_identity_column_keys=("physical:code",),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=("physical:code",),
                        target_fields=("ref",),
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        target_field="name",
                        source_column_key="physical:name",
                        required=name_required,
                    ),
                ),
                approved_write_fields=("name", "ref"),
            ),
        ),
    )
    revision = MappingRevision(mapping_id, 1, None, definition, now, "Manager")
    submission = MappingSubmission(
        str(uuid4()),
        mapping_id,
        1,
        definition.content_hash,
        "sha256:" + "f" * 64,
        (),
        now,
        "Manager",
    )
    schema = OdooSchemaCatalog(
        project_id=project_id,
        policy_hash="sha256:" + "a" * 64,
        captured_at=now,
        captured_by="Manager",
        connection_mode="REMOTE",
        database=f"physical_database_{marker}",
        odoo_version="19.0",
        models=(
            SchemaModel(
                "res.partner",
                "Contact",
                (
                    SchemaField(
                        "ref", "Reference", "char", False, False, None, None, ()
                    ),
                    SchemaField("name", "Name", "char", True, False, None, None, ()),
                ),
            ),
        ),
        content_hash=schema_hash,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "b" * 64,
        read_principal_hash="sha256:" + "c" * 64,
        read_permission_hash="sha256:" + "d" * 64,
        read_context_hash="sha256:" + "e" * 64,
        connection_target_hash="sha256:" + marker * 64,
    )
    ruleset = default_quality_ruleset(
        project_id=project_id,
        mapping_hash=definition.content_hash,
        schema_hash=governance.content_hash,
        datasets=("Customers",),
    )
    recipe = Recipe(
        recipe_id=recipe_id,
        display_name="Customer migration",
        business_purpose="Customer migration",
        data_classification="INTERNAL",
        retention_days=90,
        current_recipe_revision=None,
        current_data_version_id=data_version_id,
        cutover_candidate_id=None,
        optimistic_revision=2,
        created_at=now,
        updated_at=now,
    )
    data_version = DataVersion(
        data_version_id=data_version_id,
        recipe_id=recipe_id,
        version_number=1,
        workspace_project_id=project_id,
        parent_data_version_id=None,
        purpose=DataVersionPurpose.AUTHORING,
        state=DataVersionState.ACTIVE,
        pinned_recipe_revision=None,
        label="Authoring data",
        export_as_of_date=None,
        parameter_values_hash=None,
        created_at=now,
        sealed_at=None,
    )
    evidence = _Evidence(
        selection=selection,
        revision=revision,
        submission=submission,
        schema=schema,
        governance=governance,
        ruleset=ruleset,
    )
    facade = _RecipeFacade(recipe, data_version)
    service = RecipeAuthoringService(
        facade,
        None,
        evidence,
        evidence,
        evidence,
        evidence,
        evidence,
        evidence,
        CapabilityAuthorizationPolicy(),
        evidence,
    )
    return service, facade, recipe


def _file_binding(marker: str):
    from impodo.domain.source_binding import FileSourceBinding

    return FileSourceBinding(
        file_id=f"physical-file-{uuid4()}",
        table_key="Customers",
        source_sha256="sha256:" + marker * 64,
        catalog_hash="sha256:" + marker * 64,
        encoding="utf-8",
        delimiter=",",
        header_row=1,
    )


class RecipeAuthoringTests(unittest.TestCase):
    def test_custom_parameter_declaration_is_reusable_recipe_meaning(self):
        service, facade, recipe = _authoring_fixture("7")
        before = service.draft(recipe.recipe_id, actor=LOCAL_ACTOR)

        saved = service.save_parameter_definition(
            recipe.recipe_id,
            name="warehouse",
            label="Warehouse",
            value_type=RecipeParameterType.STRING,
            required=True,
            actor=LOCAL_ACTOR,
        )
        after = service.draft(recipe.recipe_id, actor=LOCAL_ACTOR)

        self.assertNotEqual(before.semantic_hash, after.semantic_hash)
        self.assertEqual(
            saved.definitions[0].logical_parameter_id, "parameter:warehouse"
        )
        service.publish_current(
            recipe.recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        envelope = json.loads(facade.envelope)
        parameters = envelope["recipe"]["parameter_definitions"]["parameters"]
        self.assertEqual(
            [item["logical_parameter_id"] for item in parameters],
            ["parameter:export_as_of_date", "parameter:warehouse"],
        )
        self.assertEqual(parameters[1]["allowed_use_sites"], ["controls", "provenance"])

    def test_parameter_definitions_are_sorted_hash_checked_and_removable(self):
        definitions = RecipeParameterDefinitions(
            definitions=(
                RecipeParameterDefinition("warehouse", "Warehouse", "string"),
                RecipeParameterDefinition("company", "Company", "integer"),
            )
        )
        restored = RecipeParameterDefinitions.from_json(definitions.to_json())

        self.assertEqual(
            [item.name for item in restored.definitions],
            ["company", "warehouse"],
        )

        service, _facade, recipe = _authoring_fixture("6")
        service.save_parameter_definition(
            recipe.recipe_id,
            name="warehouse",
            label="Warehouse",
            value_type="string",
            required=True,
            actor=LOCAL_ACTOR,
        )
        result = service.remove_parameter_definition(
            recipe.recipe_id,
            name="warehouse",
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(result.definitions, ())

    def test_application_data_version_cannot_publish_new_recipe_meaning(self):
        service, facade, recipe = _authoring_fixture("8")
        facade.recipe = replace(recipe, current_recipe_revision=1)
        facade.data_version = replace(
            facade.data_version,
            purpose=DataVersionPurpose.PRODUCTION,
            pinned_recipe_revision=1,
        )

        draft = service.draft(recipe.recipe_id, actor=LOCAL_ACTOR)

        self.assertEqual(draft.state, RecipeDraftState.BLOCKED)
        self.assertEqual(draft.issues[0].code, "CURRENT_DATA_VERSION_NOT_AUTHORING")
        with self.assertRaisesRegex(RecipeConflictError, "Only an Authoring"):
            service.publish_current(
                recipe.recipe_id,
                expected_recipe_revision=recipe.optimistic_revision,
                actor=LOCAL_ACTOR,
            )

    def test_physical_workspace_identity_does_not_change_recipe_semantics(self):
        first, first_facade, first_recipe = _authoring_fixture("1")
        second, second_facade, second_recipe = _authoring_fixture("2")
        changed, _changed_facade, changed_recipe = _authoring_fixture(
            "3",
            name_required=False,
        )

        first_draft = first.draft(first_recipe.recipe_id, actor=LOCAL_ACTOR)
        second_draft = second.draft(second_recipe.recipe_id, actor=LOCAL_ACTOR)
        self.assertEqual(first_draft.state, RecipeDraftState.READY)
        self.assertEqual(first_draft.semantic_hash, second_draft.semantic_hash)
        self.assertNotEqual(
            first_draft.semantic_hash,
            changed.draft(changed_recipe.recipe_id, actor=LOCAL_ACTOR).semantic_hash,
        )

        first.publish_current(
            first_recipe.recipe_id,
            expected_recipe_revision=first_recipe.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        second.publish_current(
            second_recipe.recipe_id,
            expected_recipe_revision=second_recipe.optimistic_revision,
            actor=LOCAL_ACTOR,
        )
        first_envelope = json.loads(first_facade.envelope)
        second_envelope = json.loads(second_facade.envelope)
        RecipeService._validated_envelope(first_facade.envelope)
        RecipeService._validated_envelope(second_facade.envelope)
        self.assertEqual(
            first_envelope["semantic_hash"],
            second_envelope["semantic_hash"],
        )
        semantic_text = json.dumps(first_envelope["recipe"], sort_keys=True)
        self.assertIsNone(UUID_TEXT.search(semantic_text))
        self.assertNotIn("physical_database", semantic_text)
        self.assertNotIn("connection_target_hash", semantic_text)

    def test_recipe_native_create_keeps_project_setup_compatibility(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            app = create_local_app(
                temporary,
                launch_token="launch-secret",
                session_secret="session-secret",
                secret_store=MemorySecretStore(),
            )
            with TestClient(app) as client:
                client.get("/launch?token=launch-secret")
                page = client.get("/recipes")
                csrf = re.search(
                    r'name="csrf_token" value="([^"]+)"',
                    page.text,
                ).group(1)
                created = client.post(
                    "/recipes/new",
                    data={
                        "csrf_token": csrf,
                        "name": "Customer migration",
                        "source_system": "CSV export",
                        "source_mode": "FILE",
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(created.status_code, 303)
                self.assertRegex(created.headers["location"], r"/recipes/[0-9a-f-]+")
                overview = client.get(created.headers["location"])
                self.assertEqual(overview.status_code, 200)
                self.assertIn("Complete Recipe setup", overview.text)
                recipes = app.state.context.recipes.list(actor=LOCAL_ACTOR)
                self.assertEqual(len(recipes), 1)
                self.assertEqual(recipes[0].display_name, "Customer migration")
                project = app.state.context.queries.get(
                    recipes[0].current_workspace_project_id
                )
                self.assertEqual(project.status, ProjectStatus.DRAFT)

    def test_parameter_definitions_persist_in_the_authoring_workspace(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            app = create_local_app(
                temporary,
                launch_token="launch-secret",
                session_secret="session-secret",
                secret_store=MemorySecretStore(),
            )
            recipe, _project = app.state.context.recipe_authoring.create(
                name="Opening stock",
                source_system="CSV export",
                source_mode="FILE",
                actor=LOCAL_ACTOR,
            )
            app.state.context.recipe_authoring.save_parameter_definition(
                recipe.recipe_id,
                name="warehouse",
                label="Warehouse",
                value_type="string",
                required=True,
                actor=LOCAL_ACTOR,
            )

            reopened = create_local_app(
                temporary,
                launch_token="other-launch-secret",
                session_secret="other-session-secret",
                secret_store=MemorySecretStore(),
            )
            definitions = reopened.state.context.recipe_authoring.parameter_definitions(
                recipe.recipe_id,
                actor=LOCAL_ACTOR,
            )

            self.assertEqual(len(definitions), 1)
            self.assertEqual(definitions[0].logical_parameter_id, "parameter:warehouse")

    def test_recipe_parameter_editor_is_accessible_and_uses_existing_page_style(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            app = create_local_app(
                temporary,
                launch_token="launch-secret",
                session_secret="session-secret",
                secret_store=MemorySecretStore(),
            )
            recipe, project = app.state.context.recipe_authoring.create(
                name="Opening stock",
                source_system="CSV export",
                source_mode="FILE",
                actor=LOCAL_ACTOR,
            )
            with duckdb.connect(
                str(Path(temporary) / project.project_id / "project.duckdb")
            ) as connection:
                connection.execute(
                    "UPDATE project SET status = 'REGISTERED' WHERE project_id = ?",
                    [project.project_id],
                )

            with TestClient(app) as client:
                client.get("/launch?token=launch-secret")
                overview = client.get(f"/recipes/{recipe.recipe_id}")
                self.assertEqual(overview.status_code, 200)
                self.assertIn("Inputs for each data version", overview.text)
                self.assertIn('aria-labelledby="recipe-inputs-title"', overview.text)
                csrf = re.search(
                    r'name="csrf_token" value="([^"]+)"',
                    overview.text,
                ).group(1)
                saved = client.post(
                    f"/recipes/{recipe.recipe_id}/parameters",
                    data={
                        "csrf_token": csrf,
                        "name": "warehouse",
                        "label": "Warehouse",
                        "value_type": "string",
                        "required": "yes",
                    },
                    headers=POST_HEADERS,
                    follow_redirects=False,
                )
                self.assertEqual(saved.status_code, 303)
                updated = client.get(saved.headers["location"])
                self.assertIn("Warehouse", updated.text)
                self.assertIn("warehouse", updated.text)


if __name__ == "__main__":
    unittest.main()
