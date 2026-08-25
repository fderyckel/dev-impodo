from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.access import LOCAL_ACTOR
from impodo.connectors import MetadataSnapshot, RecordSnapshot
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ScalarFieldMapping,
)
from impodo.domain.mapping.artifacts import MappingSubmission
from impodo.domain.mapping.validation.evidence import (
    MappingValidationStatus,
    mapping_issue_fingerprint,
)
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.mapping_field_catalog_repository import (
    MappingFieldCatalogRepository,
)
from impodo.adapters.duckdb.mapping_repository import MappingRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.schema_repository import SchemaRepository
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.workspace_state import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStatus,
    SourceMode,
    SourceFile,
)
from impodo.application.mapping_workspace_service import MappingWorkspaceService
from impodo.application.categorical_coverage_service import (
    CategoricalCoverageService,
)
from impodo.application.schema_workspace_service import SchemaWorkspaceService
from impodo.application.source_workspace_service import SourceWorkspaceService
from impodo.derived_entities import (
    DerivedEntityPlan,
    RelatedDatasetRule,
)
from impodo.workspace_contracts import (
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.workspace_errors import WorkspaceError
from tests.workspace_access_helpers import workspace_access_service


ROOT = Path(__file__).resolve().parents[1]
READ_CREDENTIAL_BINDING_HASH = "sha256:" + "9" * 64


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.database = database
        self.workspace_state_repository = WorkspaceStateRepository(database)
        derived_entity_repository = DerivedEntityRepository(database)
        self.derived_entity_repository = derived_entity_repository
        self.source_repository = SourceRepository(
            database,
            derived_entity_repository,
        )
        self.schema_repository = SchemaRepository(database)
        self.mapping_repository = MappingRepository(database)
        self.mapping_field_catalog_repository = MappingFieldCatalogRepository(
            database
        )
        now = datetime.now(timezone.utc)
        self.source = SourceFile(
            file_id=str(uuid4()),
            display_name="customers.csv",
            stored_name="source.csv",
            size_bytes=42,
            sha256="a" * 64,
            received_at=now,
        )
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Customer migration",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_applications=("Contacts",),
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspace_state_repository.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
        self.workspace_state = replace(
            self.workspace_state,
            source_files=(self.source,),
            revision=2,
            updated_at=now,
        )
        self.workspace_state_repository.add_source_file(
            self.workspace_state,
            self.source,
            expected_revision=1,
            actor=LOCAL_ACTOR,
        )
        self.authorization = workspace_access_service()
        self.sources = SourceWorkspaceService(
            self.workspace_state_repository,
            self.source_repository,
            self.authorization,
            schemas=self.schema_repository,
        )
        self.schemas = SchemaWorkspaceService(
            self.workspace_state_repository,
            self.source_repository,
            self.schema_repository,
            self.authorization,
        )
        self.mappings = MappingWorkspaceService(
            self.source_repository,
            self.schema_repository,
            self.mapping_repository,
            self.authorization,
            CategoricalCoverageService(self.source_repository, object()),
        )
        self.catalog = _catalog(self.source, now)
        self.source_repository.save_source_catalogs(
            self.workspace_state.workspace_id,
            (self.catalog,),
            actor=LOCAL_ACTOR,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _capture_authenticated_schema(self, snapshot=None):
        self.schemas.discover_models(
            self.workspace_state.workspace_id,
            _model_catalog_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            read_identity=_read_identity(("ir.model",)),
            actor=LOCAL_ACTOR,
        )
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        return self.schemas.capture(
            self.workspace_state.workspace_id,
            snapshot or _metadata_snapshot(),
            read_credential_binding_hash="sha256:" + "8" * 64,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

    def test_model_discovery_filters_nonpersistent_models_and_is_persisted(
        self,
    ) -> None:
        catalog = self.schemas.discover_models(
            self.workspace_state.workspace_id,
            _model_catalog_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            tuple(model.name for model in catalog.models),
            ("res.partner",),
        )
        self.assertEqual(catalog.models[0].label, "Contact")
        self.assertEqual(catalog.models[0].modules, ("base", "contacts"))
        self.assertEqual(
            self.schema_repository.get_odoo_model_catalog(self.workspace_state.workspace_id),
            catalog,
        )

    def test_mapping_field_catalog_snapshot_uses_one_connection(self) -> None:
        schema = self._capture_authenticated_schema()
        selection = self.source_repository.get_source_selection(
            self.workspace_state.workspace_id
        )

        with patch.object(
            self.database.connection_factory,
            "connect",
            wraps=self.database.connection_factory.connect,
        ) as connect:
            snapshot = (
                self.mapping_field_catalog_repository
                .get_mapping_field_catalog_snapshot(self.workspace_state.workspace_id)
            )

        self.assertEqual(connect.call_count, 1)
        self.assertEqual(snapshot.physical_selection, selection)
        self.assertIsNone(snapshot.preparation_plan)
        self.assertEqual(snapshot.source_catalogs, ())
        self.assertEqual(snapshot.schema, schema)
        self.assertIsNone(snapshot.governance)
        self.assertIsNone(snapshot.revision)
        self.assertIsNone(snapshot.working_draft)

    def test_mapping_field_catalog_snapshot_loads_required_source_catalogs(
        self,
    ) -> None:
        self._capture_authenticated_schema()
        selection = self.source_repository.get_source_selection(
            self.workspace_state.workspace_id
        )
        assert selection is not None
        dataset = selection.datasets[0]
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=self.workspace_state.workspace_id,
            source_selection_hash=selection.content_hash,
            rules=(
                RelatedDatasetRule(
                    rule_id=str(uuid4()),
                    source_dataset_id=dataset.dataset_id,
                    parent_dataset_name="customer_groups",
                    child_dataset_name="customers",
                    parent_key_column_key=dataset.columns[1].stable_key,
                    child_key_column_key=dataset.columns[0].stable_key,
                ),
            ),
            updated_at=datetime.now(timezone.utc),
            updated_by=LOCAL_ACTOR.identity.display_name,
        )
        self.derived_entity_repository.save_derived_entity_plan(
            self.workspace_state.workspace_id,
            plan,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )

        snapshot = (
            self.mapping_field_catalog_repository
            .get_mapping_field_catalog_snapshot(self.workspace_state.workspace_id)
        )

        self.assertEqual(snapshot.preparation_plan, plan)
        self.assertEqual(snapshot.source_catalogs, (self.catalog,))

    def test_schema_capture_rejects_a_different_read_credential_generation(
        self,
    ) -> None:
        self.schemas.discover_models(
            self.workspace_state.workspace_id,
            _model_catalog_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(WorkspaceError, "credential changed"):
            self.schemas.capture(
                self.workspace_state.workspace_id,
                _metadata_snapshot(),
                read_credential_binding_hash="sha256:" + "8" * 64,
                actor=LOCAL_ACTOR,
            )

    def test_authenticated_key_rotation_preserves_the_same_principal_identity(
        self,
    ) -> None:
        self.schemas.discover_models(
            self.workspace_state.workspace_id,
            _model_catalog_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            read_identity=_read_identity(("ir.model",)),
            actor=LOCAL_ACTOR,
        )
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )

        schema = self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash="sha256:" + "8" * 64,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(schema.read_principal_hash, "sha256:" + "1" * 64)
        self.assertEqual(schema.read_context_hash, "sha256:" + "4" * 64)

    def test_equivalent_schema_access_rebind_preserves_governance(self) -> None:
        schema = self._capture_authenticated_schema()
        governance = self.schemas.govern(
            self.workspace_state.workspace_id,
            business_keys=(
                BusinessKeyDefinition(
                    key_id="partner-name",
                    model="res.partner",
                    key_fields=("name",),
                    description="Unique contact name",
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            actor=LOCAL_ACTOR,
        )

        rebound = self.schemas.rebind_current_access(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash="sha256:" + "7" * 64,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(rebound.content_hash, schema.content_hash)
        self.assertEqual(
            rebound.read_credential_binding_hash,
            "sha256:" + "7" * 64,
        )
        self.assertEqual(
            self.schema_repository.get_schema_governance(
                self.workspace_state.workspace_id
            ),
            governance,
        )
        database_path = (
            self.schema_repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.schema_repository._connect(database_path) as connection:
            event = connection.execute(
                """
                SELECT event_type
                  FROM audit_event
                 ORDER BY event_id DESC
                 LIMIT 1
                """
            ).fetchone()
        self.assertEqual(event, ("ODOO_SCHEMA_ACCESS_REBOUND",))

    def test_schema_access_rebind_ignores_display_label_only_changes(self) -> None:
        schema = self._capture_authenticated_schema()
        snapshot = _metadata_snapshot()
        partner = snapshot.models["res.partner"]
        relabeled = replace(
            snapshot,
            models={
                "res.partner": replace(
                    partner,
                    fields={
                        **partner.fields,
                        "name": replace(
                            partner.fields["name"],
                            label="Legal name",
                        ),
                    },
                )
            },
        )

        rebound = self.schemas.rebind_current_access(
            self.workspace_state.workspace_id,
            relabeled,
            read_credential_binding_hash="sha256:" + "7" * 64,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(rebound.content_hash, schema.content_hash)
        self.assertEqual(rebound.models, schema.models)

    def test_schema_capture_keeps_only_usable_required_scalar_defaults(
        self,
    ) -> None:
        snapshot = _metadata_snapshot(
            create_defaults={
                "res.partner": {
                    "name": "New contact",
                    "display_name": "Ignored readonly value",
                    "active": True,
                }
            }
        )

        schema = self._capture_authenticated_schema(snapshot)
        fields = {field.name: field for field in schema.models[0].fields}

        self.assertTrue(fields["name"].create_default_present)
        self.assertEqual(fields["name"].create_default_value, "New contact")
        self.assertFalse(fields["display_name"].create_default_present)
        self.assertFalse(fields["active"].create_default_present)
        restored = type(schema).from_json(schema.to_json())
        restored_fields = {
            field.name: field for field in restored.models[0].fields
        }
        self.assertEqual(
            restored_fields["name"].create_default_value,
            "New contact",
        )

    def test_schema_access_rebind_blocks_field_drift_without_invalidation(
        self,
    ) -> None:
        schema = self._capture_authenticated_schema()
        governance = self.schemas.govern(
            self.workspace_state.workspace_id,
            business_keys=(
                BusinessKeyDefinition(
                    key_id="partner-name",
                    model="res.partner",
                    key_fields=("name",),
                    description="Unique contact name",
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            actor=LOCAL_ACTOR,
        )
        snapshot = _metadata_snapshot()
        partner = snapshot.models["res.partner"]
        changed = replace(
            snapshot,
            models={
                "res.partner": replace(
                    partner,
                    fields={
                        **partner.fields,
                        "name": replace(
                            partner.fields["name"],
                            required=False,
                        ),
                    },
                )
            },
        )

        with self.assertRaisesRegex(WorkspaceError, "available Odoo fields"):
            self.schemas.rebind_current_access(
                self.workspace_state.workspace_id,
                changed,
                read_credential_binding_hash="sha256:" + "7" * 64,
                read_identity=_read_identity(("res.partner",)),
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(
            self.schema_repository.get_odoo_schema_catalog(
                self.workspace_state.workspace_id
            ),
            schema,
        )
        self.assertEqual(
            self.schema_repository.get_schema_governance(
                self.workspace_state.workspace_id
            ),
            governance,
        )

    def test_schema_capture_rejects_a_changed_authenticated_principal(self) -> None:
        self.schemas.discover_models(
            self.workspace_state.workspace_id,
            _model_catalog_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            read_identity=_read_identity(("ir.model",)),
            actor=LOCAL_ACTOR,
        )
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(WorkspaceError, "principal or context changed"):
            self.schemas.capture(
                self.workspace_state.workspace_id,
                _metadata_snapshot(),
                read_credential_binding_hash="sha256:" + "8" * 64,
                read_identity=_read_identity(
                    ("res.partner",),
                    principal_digit="7",
                ),
                actor=LOCAL_ACTOR,
            )

    def test_odoo_source_captures_eligibility_schema_before_source_freeze(
        self,
    ) -> None:
        with self.assertRaisesRegex(WorkspaceError, "Freeze source datasets"):
            self.schemas.capture(
                self.workspace_state.workspace_id,
                _metadata_snapshot(),
                read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
                actor=LOCAL_ACTOR,
            )

        odoo_project = replace(
            self.workspace_state,
            source_mode=SourceMode.ODOO,
            revision=self.workspace_state.revision + 1,
        )
        self.workspace_state_repository.save(
            odoo_project,
            expected_revision=self.workspace_state.revision,
            event_type="TEST_ODOO_SOURCE_MODE",
            event_detail="",
            actor=LOCAL_ACTOR,
        )
        self.workspace_state = odoo_project

        schema = self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(schema.models[0].name, "res.partner")
        with self.assertRaisesRegex(
            WorkspaceError,
            "Freeze the selected Odoo source records",
        ):
            self.schemas.govern(
                self.workspace_state.workspace_id,
                business_keys=(
                    BusinessKeyDefinition(
                        key_id="partner-name",
                        model="res.partner",
                        key_fields=("name",),
                        description="Unique contact name",
                        status=BusinessKeyStatus.CONFIRMED,
                    ),
                ),
                actor=LOCAL_ACTOR,
            )

    def test_odoo_capture_plan_survives_check_until_schema_change_is_confirmed(
        self,
    ) -> None:
        odoo_project = replace(
            self.workspace_state,
            source_mode=SourceMode.ODOO,
            revision=self.workspace_state.revision + 1,
        )
        self.workspace_state_repository.save(
            odoo_project,
            expected_revision=self.workspace_state.revision,
            event_type="TEST_ODOO_SOURCE_MODE",
            event_detail="",
            actor=LOCAL_ACTOR,
        )
        self.workspace_state = odoo_project
        schema = self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

        first = self.sources.define_odoo_capture_selection(
            self.workspace_state.workspace_id,
            dataset_name="odoo_contacts",
            model="res.partner",
            field_names=("name", "active", "name"),
            include_archived=False,
            max_rows="1000",
            actor=LOCAL_ACTOR,
        )
        second = self.sources.define_odoo_capture_selection(
            self.workspace_state.workspace_id,
            dataset_name="odoo_contacts",
            model="res.partner",
            field_names=("name",),
            include_archived=True,
            max_rows=100,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(first.version, 1)
        self.assertEqual(first.field_names, ("active", "name"))
        self.assertEqual(second.selection_id, first.selection_id)
        self.assertEqual(second.version, 2)
        self.assertEqual(
            self.source_repository.get_current_odoo_capture_selection(
                self.workspace_state.workspace_id
            ),
            second,
        )
        self.assertEqual(
            self.source_repository.get_odoo_capture_selection_history(
                self.workspace_state.workspace_id
            ),
            (first, second),
        )
        self.assertTrue(
            self.workspace_state_repository.has_audit_event(
                self.workspace_state.workspace_id,
                "ODOO_CAPTURE_SELECTION_SAVED",
            )
        )
        self.assertEqual(first.connection_target_hash, schema.connection_target_hash)

        unchanged_snapshot = _metadata_snapshot()
        unchanged_snapshot = replace(
            unchanged_snapshot,
            fingerprint=replace(
                unchanged_snapshot.fingerprint,
                snapshot_timestamp="2026-08-24T12:00:00Z",
            ),
        )
        checked = self.schemas.check_refresh(
            self.workspace_state.workspace_id,
            unchanged_snapshot,
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(checked.content_hash, schema.content_hash)
        self.assertIsNone(checked.pending_refresh)
        self.assertEqual(
            self.source_repository.get_current_odoo_capture_selection(
                self.workspace_state.workspace_id
            ),
            second,
        )

        changed_snapshot = _metadata_snapshot()
        partner = changed_snapshot.models["res.partner"]
        changed_snapshot = replace(
            changed_snapshot,
            models={
                "res.partner": replace(
                    partner,
                    fields={
                        **partner.fields,
                        "name": replace(
                            partner.fields["name"],
                            required=False,
                        ),
                    },
                )
            },
        )
        review = self.schemas.check_refresh(
            self.workspace_state.workspace_id,
            changed_snapshot,
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            read_identity=_read_identity(("res.partner",)),
            actor=LOCAL_ACTOR,
        )

        self.assertIsNotNone(review.pending_refresh)
        pending = review.pending_refresh
        assert pending is not None
        self.assertEqual(pending.change_count, 1)
        self.assertEqual(pending.changes[0].kind, "FIELD_CHANGED")
        self.assertEqual(
            self.source_repository.get_current_odoo_capture_selection(
                self.workspace_state.workspace_id
            ),
            second,
        )

        confirmed = self.schemas.confirm_refresh(
            self.workspace_state.workspace_id,
            expected_current_content_hash=pending.expected_current_content_hash,
            expected_candidate_id=pending.candidate_id,
            expected_candidate_semantic_hash=pending.semantic_hash,
            actor=LOCAL_ACTOR,
        )

        self.assertIsNone(
            self.source_repository.get_current_odoo_capture_selection(
                self.workspace_state.workspace_id
            )
        )
        self.assertIsNone(confirmed.pending_refresh)
        self.assertNotEqual(confirmed.content_hash, schema.content_hash)
        self.assertEqual(
            len(
                self.source_repository.get_odoo_capture_selection_history(
                    self.workspace_state.workspace_id
                )
            ),
            2,
        )

    def test_confirm_freeze_capture_and_mapping_are_versioned_and_persisted(
        self,
    ) -> None:
        configuration = self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(configuration.catalog_hash, self.catalog.content_hash)

        selection = self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(selection.version, 1)
        self.assertEqual(selection.datasets[0].name, "customers")
        self.assertEqual(
            self.source_repository.get_source_selection(self.workspace_state.workspace_id),
            selection,
        )

        schema = self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(schema.models[0].name, "res.partner")
        self.assertEqual(
            {field.name: field.label for field in schema.models[0].fields}["name"],
            "Name",
        )

        dataset = selection.datasets[0]
        dataset_mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key=dataset.columns[0].stable_key,
                ),
            ),
        )
        draft = self.mappings.save_working_draft(
            self.workspace_state.workspace_id,
            datasets=(dataset_mapping,),
            expected_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(draft.version, 1)
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.workspace_state.workspace_id
            ),
            draft,
        )

        replacement = _catalog(
            self.source,
            datetime.now(timezone.utc),
            warning="Catalog regenerated",
        )
        self.source_repository.save_source_catalog(
            self.workspace_state.workspace_id,
            replacement,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            self.source_repository.get_source_configurations(self.workspace_state.workspace_id),
            (),
        )
        self.assertIsNone(
            self.source_repository.get_source_selection(self.workspace_state.workspace_id)
        )
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.workspace_state.workspace_id
            ),
            draft,
        )

    def test_formula_cells_block_source_confirmation_instead_of_warning(self) -> None:
        table = replace(
            self.catalog.tables[0],
            table_key="sheet:Sheet1",
            name="Sheet1",
            kind="WORKSHEET",
            formula_cell_count=4_003,
            first_formula_cell="M2",
            first_formula_column="Country Count",
        )
        catalog = replace(
            self.catalog,
            display_name="AX2012 - PLW - ClientsV4.xlsx",
            format="XLSX",
            tables=(table,),
        )
        self.source_repository.save_source_catalog(
            self.workspace_state.workspace_id,
            catalog,
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            'Excel formula found in "Country Count" at Sheet1!M2 in '
            "AX2012 - PLW - ClientsV4.xlsx",
        ):
            self.sources.confirm_source(
                self.workspace_state.workspace_id,
                self.source.file_id,
                selected_table_keys=("sheet:Sheet1",),
                warnings_acknowledged=True,
                actor=LOCAL_ACTOR,
            )

    def test_source_confirmation_rejects_a_worksheet_and_its_excel_table(self) -> None:
        worksheet = replace(
            self.catalog.tables[0],
            table_key="sheet:Sheet1",
            name="Sheet1",
            kind="WORKSHEET",
        )
        excel_table = replace(
            worksheet,
            table_key="table:Sheet1:Customers",
            name="Customers",
            kind="NAMED_TABLE",
        )
        catalog = replace(
            self.catalog,
            format="XLSX",
            tables=(worksheet, excel_table),
        )
        self.source_repository.save_source_catalog(
            self.workspace_state.workspace_id,
            catalog,
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "Choose either worksheet 'Sheet1' or its Excel tables, not both",
        ):
            self.sources.confirm_source(
                self.workspace_state.workspace_id,
                self.source.file_id,
                selected_table_keys=(
                    "sheet:Sheet1",
                    "table:Sheet1:Customers",
                ),
                warnings_acknowledged=False,
                actor=LOCAL_ACTOR,
            )

    def test_local_manual_schema_draft_needs_no_odoo_credential(self) -> None:
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )

        schema = self.schemas.capture_local_manual(
            self.workspace_state.workspace_id,
            (
                SchemaModel(
                    name="res.partner",
                    label="Contact",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                        ),
                    ),
                ),
            ),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(schema.origin, SchemaOrigin.LOCAL_MANUAL)
        self.assertEqual(
            schema.odoo_version,
            "unverified local draft (expected Odoo 19)",
        )
        self.assertEqual(
            self.schema_repository.get_odoo_schema_catalog(self.workspace_state.workspace_id),
            schema,
        )

        dataset = selection.datasets[0]
        dataset_mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key=dataset.columns[0].stable_key,
                ),
            ),
        )
        draft = self.mappings.save_working_draft(
            self.workspace_state.workspace_id,
            datasets=(dataset_mapping,),
            expected_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(draft.version, 1)
        checked, _validation = self.mappings.check_definition(
            self.workspace_state.workspace_id,
            datasets=(dataset_mapping,),
            expected_parent_version=None,
            expected_working_draft_version=1,
            actor=LOCAL_ACTOR,
        )
        with self.assertRaisesRegex(WorkspaceError, "live Odoo schema"):
            self.mappings.submit_current(
                self.workspace_state.workspace_id,
                datasets=(dataset_mapping,),
                expected_version=checked.version,
                expected_working_draft_version=2,
                actor=LOCAL_ACTOR,
            )

    def test_governed_mapping_revisions_and_submission_are_exact(self) -> None:
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        schema = self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        governance = self.schemas.govern(
            self.workspace_state.workspace_id,
            business_keys=(
                BusinessKeyDefinition(
                    key_id="partner-name",
                    model="res.partner",
                    key_fields=("name",),
                    description="Unique test contact name",
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            actor=LOCAL_ACTOR,
        )
        dataset = selection.datasets[0]
        name_column = next(
            item for item in dataset.columns if item.source_name == "name"
        )
        code_column = next(
            item for item in dataset.columns if item.source_name == "code"
        )
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            source_identity_column_keys=(code_column.stable_key,),
            target_identity=(
                IdentityComponentMapping(
                    source_column_keys=(name_column.stable_key,),
                    target_fields=("name",),
                ),
            ),
        )

        first, validation = self.mappings.check_definition(
            self.workspace_state.workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(first.version, 1)
        self.assertEqual(validation.status, MappingValidationStatus.VALID)

        second, repeated_validation = self.mappings.check_definition(
            self.workspace_state.workspace_id,
            datasets=(mapping,),
            expected_parent_version=1,
            expected_working_draft_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(second, first)
        self.assertEqual(repeated_validation, validation)
        submission = self.mappings.submit_current(
            self.workspace_state.workspace_id,
            datasets=(mapping,),
            expected_version=1,
            expected_working_draft_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            submission.mapping_content_hash,
            first.definition.content_hash,
        )
        self.assertEqual(submission.validation_hash, validation.validation_hash)
        self.assertEqual(
            self.workspace_state_repository.get(self.workspace_state.workspace_id).mapping_version,
            "1",
        )
        self.assertEqual(
            [item.version for item in self.mapping_repository.list_mapping_revisions(
                self.workspace_state.workspace_id
            )],
            [1],
        )

        with self.assertRaisesRegex(WorkspaceError, "modified"):
            self.mappings.check_definition(
                self.workspace_state.workspace_id,
                datasets=(mapping,),
                expected_parent_version=None,
                expected_working_draft_version=1,
                actor=LOCAL_ACTOR,
            )

        recaptured = self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        self.assertNotEqual(recaptured.captured_at, schema.captured_at)
        self.assertIsNone(
            self.mapping_repository.get_mapping_revision(self.workspace_state.workspace_id)
        )
        next_governance = self.schemas.govern(
            self.workspace_state.workspace_id,
            business_keys=governance.business_keys,
            actor=LOCAL_ACTOR,
        )
        third, _validation = self.mappings.check_definition(
            self.workspace_state.workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(next_governance.version, 1)
        self.assertEqual(third.version, 2)
        self.assertIsNone(third.parent_version)

        warning_mapping = replace(
            mapping,
            fields=(
                ScalarFieldMapping(
                    target_field="active",
                    source_column_key=code_column.stable_key,
                    value_type="boolean",
                ),
            ),
        )
        warning_revision, warning_validation = self.mappings.check_definition(
            self.workspace_state.workspace_id,
            datasets=(warning_mapping,),
            expected_parent_version=2,
            expected_working_draft_version=2,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(warning_revision.version, 3)
        with self.assertRaisesRegex(WorkspaceError, "Acknowledge"):
            self.mappings.submit_current(
                self.workspace_state.workspace_id,
                datasets=(warning_mapping,),
                expected_version=3,
                expected_working_draft_version=3,
                actor=LOCAL_ACTOR,
            )
        warning_revision = self.mapping_repository.get_mapping_revision(
            self.workspace_state.workspace_id
        )
        self.assertEqual(warning_revision.version, 3)
        self.assertEqual(
            warning_validation.status,
            MappingValidationStatus.VALID_WITH_WARNINGS,
        )
        warning_fingerprints = tuple(
            mapping_issue_fingerprint(item)
            for item in warning_validation.issues
            if item.severity == "warning"
        )
        warning_submission = self.mappings.submit_current(
            self.workspace_state.workspace_id,
            datasets=(warning_mapping,),
            expected_version=warning_revision.version,
            expected_working_draft_version=3,
            warning_acknowledgements=warning_fingerprints,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            warning_submission.warning_acknowledgements,
            warning_fingerprints,
        )
        repeated_submission = self.mappings.submit_current(
            self.workspace_state.workspace_id,
            datasets=(warning_mapping,),
            expected_version=warning_revision.version,
            expected_working_draft_version=3,
            warning_acknowledgements=warning_fingerprints,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(repeated_submission, warning_submission)

        invalid = replace(mapping, target_identity=())
        failed_revision, invalid_validation = self.mappings.check_definition(
            self.workspace_state.workspace_id,
            datasets=(invalid,),
            expected_parent_version=3,
            expected_working_draft_version=3,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(failed_revision.version, 4)
        self.assertEqual(
            invalid_validation.status,
            MappingValidationStatus.INVALID,
        )
        with self.assertRaisesRegex(WorkspaceError, "cannot be submitted"):
            self.mappings.submit_current(
                self.workspace_state.workspace_id,
                datasets=(invalid,),
                expected_version=4,
                expected_working_draft_version=4,
                actor=LOCAL_ACTOR,
            )
        self.assertEqual(
            self.mapping_repository.get_mapping_revision(
                self.workspace_state.workspace_id
            ),
            failed_revision,
        )
        self.assertIsNone(
            self.mapping_repository.get_mapping_submission(
                self.workspace_state.workspace_id,
                failed_revision.version,
            )
        )
        with self.assertRaisesRegex(WorkspaceError, "validation gate"):
            self.mapping_repository.save_mapping_submission(
                self.workspace_state.workspace_id,
                MappingSubmission(
                    submission_id=str(uuid4()),
                    mapping_id=failed_revision.mapping_id,
                    version=failed_revision.version,
                    mapping_content_hash=(
                        failed_revision.definition.content_hash
                    ),
                    validation_hash=invalid_validation.validation_hash,
                    warning_acknowledgements=(),
                    submitted_at=datetime.now(timezone.utc),
                    submitted_by=LOCAL_ACTOR.identity.display_name,
                ),
                actor=LOCAL_ACTOR,
            )

    def test_working_mapping_draft_is_recoverable_without_validation(
        self,
    ) -> None:
        self.sources.confirm_source(
            self.workspace_state.workspace_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.sources.freeze_selection(
            self.workspace_state.workspace_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        governance = self.schemas.govern(
            self.workspace_state.workspace_id,
            business_keys=(
                BusinessKeyDefinition(
                    key_id="partner-name",
                    model="res.partner",
                    key_fields=("name",),
                    description="Unique test contact name",
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            actor=LOCAL_ACTOR,
        )
        dataset = selection.datasets[0]
        incomplete = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="res.partner",
            fields=(
                ScalarFieldMapping(
                    target_field="active",
                    source_column_key="",
                    value_type="boolean",
                ),
            ),
        )

        first = self.mappings.save_working_draft(
            self.workspace_state.workspace_id,
            datasets=(incomplete,),
            expected_version=None,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(first.version, 1)
        self.assertEqual(first.definition.schema_hash, governance.content_hash)
        self.assertEqual(
            first.definition.datasets[0].fields[0].source_column_key,
            "",
        )
        self.assertIsNone(
            self.mapping_repository.get_mapping_revision(self.workspace_state.workspace_id)
        )
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.workspace_state.workspace_id
            ),
            first,
        )

        with self.assertRaisesRegex(WorkspaceError, "modified"):
            self.mappings.save_working_draft(
                self.workspace_state.workspace_id,
                datasets=(incomplete,),
                expected_version=None,
                actor=LOCAL_ACTOR,
            )

        second = self.mappings.save_working_draft(
            self.workspace_state.workspace_id,
            datasets=(incomplete,),
            expected_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(second.version, 2)
        self.schemas.capture(
            self.workspace_state.workspace_id,
            _metadata_snapshot(),
            read_credential_binding_hash=READ_CREDENTIAL_BINDING_HASH,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.workspace_state.workspace_id
            ),
            second,
        )


def _catalog(
    source: SourceFile,
    inspected_at: datetime,
    *,
    warning: str | None = None,
) -> SourceFileCatalog:
    columns = (
        SourceColumnProfile(
            ordinal=1,
            name="code",
            candidate_type="date",
            null_count=0,
            non_null_count=1,
            distinct_count=1,
            distinct_count_is_exact=True,
            duplicate_count=0,
            minimum="C001",
            maximum="C001",
            minimum_length=4,
            maximum_length=4,
        ),
        SourceColumnProfile(
            ordinal=2,
            name="name",
            candidate_type="string",
            null_count=0,
            non_null_count=1,
            distinct_count=1,
            distinct_count_is_exact=True,
            duplicate_count=0,
            minimum="Example",
            maximum="Example",
            minimum_length=7,
            maximum_length=7,
        ),
    )
    return SourceFileCatalog(
        contract_version=2,
        file_id=source.file_id,
        display_name=source.display_name,
        source_sha256=source.sha256,
        source_size_bytes=source.size_bytes,
        format="CSV",
        inspected_at=inspected_at,
        encoding="utf-8",
        delimiter=",",
        tables=(
            SourceTableCatalog(
                table_key="csv",
                name="customers",
                kind="CSV",
                hidden=False,
                header_row=1,
                row_count=1,
                column_count=2,
                columns=columns,
                preview_rows=(("C001", "Example"),),
            ),
        ),
        warnings=(warning,) if warning else (),
    )


def _metadata_snapshot(
    *,
    create_defaults=None,
) -> MetadataSnapshot:
    return MetadataSnapshot(
        fingerprint=TargetFingerprint(
            target_hash=target_identity_hash(
                connection_mode="LOCAL",
                base_url="http://127.0.0.1:8069",
                database="odoo19_local",
            ),
            connection_mode="LOCAL",
            database="odoo19_local",
            odoo_version="19.0",
            snapshot_timestamp="2026-07-29T12:00:00Z",
            module_versions={"base": "19.0.1.0"},
        ),
        models={
            "res.partner": ModelMetadata(
                model="res.partner",
                description="Contact",
                fields={
                    "name": FieldMetadata(
                        name="name",
                        type="char",
                        label="Name",
                        required=True,
                    ),
                    "display_name": FieldMetadata(
                        name="display_name",
                        type="char",
                        label="Display Name",
                        readonly=True,
                    ),
                    "active": FieldMetadata(
                        name="active",
                        type="boolean",
                        label="Active",
                    ),
                },
            )
        },
        create_defaults=create_defaults or {},
    )


def _read_identity(
    models: tuple[str, ...],
    *,
    principal_digit: str = "1",
) -> OdooReadIdentity:
    return OdooReadIdentity(
        target_hash=target_identity_hash(
            connection_mode="LOCAL",
            base_url="http://127.0.0.1:8069",
            database="odoo19_local",
        ),
        principal_hash="sha256:" + principal_digit * 64,
        permission_hash=(
            "sha256:" + ("2" if models == ("ir.model",) else "3") * 64
        ),
        context_hash="sha256:" + "4" * 64,
        readable_models=models,
        observed_at="2026-08-12T00:00:00Z",
    )


def _model_catalog_snapshot() -> RecordSnapshot:
    fingerprint = TargetFingerprint(
        target_hash=target_identity_hash(
            connection_mode="LOCAL",
            base_url="http://127.0.0.1:8069",
            database="odoo19_local",
        ),
        connection_mode="LOCAL",
        database="odoo19_local",
        odoo_version="19.0",
        snapshot_timestamp="2026-07-30T12:00:00Z",
        module_versions={"base": "19.0.1.0"},
    )
    return RecordSnapshot(
        fingerprint=fingerprint,
        records={
            "ir.model": (
                TargetRecord(
                    model="ir.model",
                    odoo_id=1,
                    values={
                        "name": "Contact",
                        "model": "res.partner",
                        "abstract": False,
                        "transient": False,
                        "modules": "base, contacts",
                        "state": "base",
                    },
                ),
                TargetRecord(
                    model="ir.model",
                    odoo_id=2,
                    values={
                        "name": "Temporary import",
                        "model": "x.import.wizard",
                        "abstract": False,
                        "transient": True,
                        "modules": "x_import",
                        "state": "base",
                    },
                ),
            )
        },
        requested_fields={
            "ir.model": (
                "name",
                "model",
                "abstract",
                "transient",
                "modules",
                "state",
            )
        },
        complete=True,
    )

