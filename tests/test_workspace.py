from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.connectors import MetadataSnapshot, RecordSnapshot
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
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
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.mapping_repository import MappingRepository
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.schema_repository import SchemaRepository
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectStatus,
    SourceMode,
    SourceFile,
)
from impodo.application.mapping_workspace_service import MappingWorkspaceService
from impodo.application.schema_workspace_service import SchemaWorkspaceService
from impodo.application.source_workspace_service import SourceWorkspaceService
from impodo.workspace_contracts import (
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.workspace_errors import WorkspaceError


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbDatabase(self.temporary.name)
        self.project_repository = ProjectRepository(database)
        derived_entity_repository = DerivedEntityRepository(database)
        self.source_repository = SourceRepository(
            database,
            derived_entity_repository,
        )
        self.schema_repository = SchemaRepository(database)
        self.mapping_repository = MappingRepository(database)
        now = datetime.now(timezone.utc)
        self.source = SourceFile(
            file_id=str(uuid4()),
            display_name="customers.csv",
            stored_name="source.csv",
            size_bytes=42,
            sha256="a" * 64,
            received_at=now,
        )
        self.project = MigrationProject(
            project_id=str(uuid4()),
            name="Customer migration",
            source_system="CSV",
            data_manager="Data Manager",
            functional_owner="Functional Owner",
            business_unit="Example Business Unit",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_applications=("Contacts",),
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        self.project_repository.create(self.project, actor=LOCAL_ACTOR)
        self.project = replace(
            self.project,
            source_files=(self.source,),
            revision=2,
            updated_at=now,
        )
        self.project_repository.add_source_file(
            self.project,
            self.source,
            expected_revision=1,
            actor=LOCAL_ACTOR,
        )
        self.authorization = CapabilityAuthorizationPolicy()
        self.sources = SourceWorkspaceService(
            self.project_repository,
            self.source_repository,
            self.authorization,
        )
        self.schemas = SchemaWorkspaceService(
            self.project_repository,
            self.source_repository,
            self.schema_repository,
            self.authorization,
        )
        self.mappings = MappingWorkspaceService(
            self.source_repository,
            self.schema_repository,
            self.mapping_repository,
            self.authorization,
        )
        self.catalog = _catalog(self.source, now)
        self.source_repository.save_source_catalogs(
            self.project.project_id,
            (self.catalog,),
            actor=LOCAL_ACTOR,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_model_discovery_filters_nonpersistent_models_and_is_persisted(
        self,
    ) -> None:
        catalog = self.schemas.discover_models(
            self.project.project_id,
            _model_catalog_snapshot(),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(
            tuple(model.name for model in catalog.models),
            ("res.partner",),
        )
        self.assertEqual(catalog.models[0].label, "Contact")
        self.assertEqual(catalog.models[0].modules, ("base", "contacts"))
        self.assertEqual(
            self.schema_repository.get_odoo_model_catalog(self.project.project_id),
            catalog,
        )

    def test_odoo_source_captures_eligibility_schema_before_source_freeze(
        self,
    ) -> None:
        with self.assertRaisesRegex(WorkspaceError, "Freeze source datasets"):
            self.schemas.capture(
                self.project.project_id,
                _metadata_snapshot(),
                actor=LOCAL_ACTOR,
            )

        odoo_project = replace(
            self.project,
            source_mode=SourceMode.ODOO,
            revision=self.project.revision + 1,
        )
        self.project_repository.save(
            odoo_project,
            expected_revision=self.project.revision,
            event_type="TEST_ODOO_SOURCE_MODE",
            event_detail="",
            actor=LOCAL_ACTOR,
        )
        self.project = odoo_project

        schema = self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(schema.models[0].name, "res.partner")
        with self.assertRaisesRegex(
            WorkspaceError,
            "Freeze the selected Odoo source records",
        ):
            self.schemas.govern(
                self.project.project_id,
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

    def test_confirm_freeze_capture_and_mapping_are_versioned_and_persisted(
        self,
    ) -> None:
        configuration = self.sources.confirm_source(
            self.project.project_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(configuration.catalog_hash, self.catalog.content_hash)

        selection = self.sources.freeze_selection(
            self.project.project_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(selection.version, 1)
        self.assertEqual(selection.datasets[0].name, "customers")
        self.assertEqual(
            self.source_repository.get_source_selection(self.project.project_id),
            selection,
        )

        schema = self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
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
            self.project.project_id,
            datasets=(dataset_mapping,),
            expected_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(draft.version, 1)
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.project.project_id
            ),
            draft,
        )

        replacement = _catalog(
            self.source,
            datetime.now(timezone.utc),
            warning="Catalog regenerated",
        )
        self.source_repository.save_source_catalog(
            self.project.project_id,
            replacement,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            self.source_repository.get_source_configurations(self.project.project_id),
            (),
        )
        self.assertIsNone(
            self.source_repository.get_source_selection(self.project.project_id)
        )
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.project.project_id
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
            self.project.project_id,
            catalog,
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            'Excel formula found in "Country Count" at Sheet1!M2 in '
            "AX2012 - PLW - ClientsV4.xlsx",
        ):
            self.sources.confirm_source(
                self.project.project_id,
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
            self.project.project_id,
            catalog,
            actor=LOCAL_ACTOR,
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "Choose either worksheet 'Sheet1' or its Excel tables, not both",
        ):
            self.sources.confirm_source(
                self.project.project_id,
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
            self.project.project_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.sources.freeze_selection(
            self.project.project_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )

        schema = self.schemas.capture_local_manual(
            self.project.project_id,
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
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(schema.origin, SchemaOrigin.LOCAL_MANUAL)
        self.assertEqual(
            schema.odoo_version,
            "unverified local draft (expected Odoo 19)",
        )
        self.assertEqual(
            self.schema_repository.get_odoo_schema_catalog(self.project.project_id),
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
            self.project.project_id,
            datasets=(dataset_mapping,),
            expected_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(draft.version, 1)
        checked, _validation = self.mappings.check_definition(
            self.project.project_id,
            datasets=(dataset_mapping,),
            expected_parent_version=None,
            expected_working_draft_version=1,
            actor=LOCAL_ACTOR,
        )
        with self.assertRaisesRegex(WorkspaceError, "live Odoo schema"):
            self.mappings.submit_current(
                self.project.project_id,
                datasets=(dataset_mapping,),
                expected_version=checked.version,
                expected_working_draft_version=2,
                actor=LOCAL_ACTOR,
            )

    def test_governed_mapping_revisions_and_submission_are_exact(self) -> None:
        self.sources.confirm_source(
            self.project.project_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.sources.freeze_selection(
            self.project.project_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        schema = self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
            actor=LOCAL_ACTOR,
        )
        governance = self.schemas.govern(
            self.project.project_id,
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
            self.project.project_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(first.version, 1)
        self.assertEqual(validation.status, MappingValidationStatus.VALID)

        second, repeated_validation = self.mappings.check_definition(
            self.project.project_id,
            datasets=(mapping,),
            expected_parent_version=1,
            expected_working_draft_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(second, first)
        self.assertEqual(repeated_validation, validation)
        submission = self.mappings.submit_current(
            self.project.project_id,
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
            self.project_repository.get(self.project.project_id).mapping_version,
            "1",
        )
        self.assertEqual(
            [item.version for item in self.mapping_repository.list_mapping_revisions(
                self.project.project_id
            )],
            [1],
        )

        with self.assertRaisesRegex(WorkspaceError, "modified"):
            self.mappings.check_definition(
                self.project.project_id,
                datasets=(mapping,),
                expected_parent_version=None,
                expected_working_draft_version=1,
                actor=LOCAL_ACTOR,
            )

        recaptured = self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
            actor=LOCAL_ACTOR,
        )
        self.assertNotEqual(recaptured.captured_at, schema.captured_at)
        self.assertIsNone(
            self.mapping_repository.get_mapping_revision(self.project.project_id)
        )
        next_governance = self.schemas.govern(
            self.project.project_id,
            business_keys=governance.business_keys,
            actor=LOCAL_ACTOR,
        )
        third, _validation = self.mappings.check_definition(
            self.project.project_id,
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
            self.project.project_id,
            datasets=(warning_mapping,),
            expected_parent_version=2,
            expected_working_draft_version=2,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(warning_revision.version, 3)
        with self.assertRaisesRegex(WorkspaceError, "Acknowledge"):
            self.mappings.submit_current(
                self.project.project_id,
                datasets=(warning_mapping,),
                expected_version=3,
                expected_working_draft_version=3,
                actor=LOCAL_ACTOR,
            )
        warning_revision = self.mapping_repository.get_mapping_revision(
            self.project.project_id
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
            self.project.project_id,
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
            self.project.project_id,
            datasets=(warning_mapping,),
            expected_version=warning_revision.version,
            expected_working_draft_version=3,
            warning_acknowledgements=warning_fingerprints,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(repeated_submission, warning_submission)

        invalid = replace(mapping, target_identity=())
        failed_revision, invalid_validation = self.mappings.check_definition(
            self.project.project_id,
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
                self.project.project_id,
                datasets=(invalid,),
                expected_version=4,
                expected_working_draft_version=4,
                actor=LOCAL_ACTOR,
            )
        self.assertEqual(
            self.mapping_repository.get_mapping_revision(
                self.project.project_id
            ),
            failed_revision,
        )
        self.assertIsNone(
            self.mapping_repository.get_mapping_submission(
                self.project.project_id,
                failed_revision.version,
            )
        )
        with self.assertRaisesRegex(WorkspaceError, "validation gate"):
            self.mapping_repository.save_mapping_submission(
                self.project.project_id,
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
            self.project.project_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        selection = self.sources.freeze_selection(
            self.project.project_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
            actor=LOCAL_ACTOR,
        )
        governance = self.schemas.govern(
            self.project.project_id,
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
            self.project.project_id,
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
            self.mapping_repository.get_mapping_revision(self.project.project_id)
        )
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.project.project_id
            ),
            first,
        )

        with self.assertRaisesRegex(WorkspaceError, "modified"):
            self.mappings.save_working_draft(
                self.project.project_id,
                datasets=(incomplete,),
                expected_version=None,
                actor=LOCAL_ACTOR,
            )

        second = self.mappings.save_working_draft(
            self.project.project_id,
            datasets=(incomplete,),
            expected_version=1,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(second.version, 2)
        self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            self.mapping_repository.get_mapping_working_draft(
                self.project.project_id
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
        contract_version=1,
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


def _metadata_snapshot() -> MetadataSnapshot:
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
