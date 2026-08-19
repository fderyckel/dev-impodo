"""Verify Recipe-native creation and portable authoring publication."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from uuid import uuid4

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
from impodo.projects import ProjectStatus
from impodo.quality import default_quality_ruleset
from impodo.recipes import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
    Recipe,
    RecipeDraftState,
    RecipeState,
    SetupHydrationState,
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
    ) -> None:
        self.selection = selection
        self.revision = revision
        self.submission = submission
        self.schema = schema
        self.governance = governance
        self.ruleset = ruleset

    def get_mapping_source_selection(self, project_id):
        del project_id
        return self.selection

    def get_source_selection(self, project_id):
        del project_id
        return self.selection

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
        return None

    def get_reference_bundle(self, project_id):
        del project_id
        return None


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
                    SchemaField(
                        "name", "Name", "char", True, False, None, None, ()
                    ),
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
        state=RecipeState.ACTIVE,
        data_classification="INTERNAL",
        retention_days=90,
        current_recipe_revision=None,
        current_data_version_id=data_version_id,
        pending_data_version_id=None,
        cutover_candidate_id=None,
        setup_hydration_state=SetupHydrationState.READY,
        setup_hydration_hash="sha256:" + "0" * 64,
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
        intake_status="READY",
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


if __name__ == "__main__":
    unittest.main()
