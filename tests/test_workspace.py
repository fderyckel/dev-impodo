from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from uc_migration_profiler.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from uc_migration_profiler.connectors import MetadataSnapshot
from uc_migration_profiler.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from uc_migration_profiler.models import (
    EnvironmentFingerprint,
    FieldMetadata,
    ModelMetadata,
)
from uc_migration_profiler.project_store import DuckDbProjectRepository
from uc_migration_profiler.projects import (
    MigrationProject,
    OdooConnectionMode,
    ProjectStatus,
    SourceFile,
    TargetEnvironment,
)
from uc_migration_profiler.workspace import (
    FieldMapping,
    MappingStatus,
    MappingWorkspaceService,
    SchemaWorkspaceService,
    SourceWorkspaceService,
    WorkspaceError,
)


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.repository = DuckDbProjectRepository(self.temporary.name)
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
            business_unit="UC",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            target_environment=TargetEnvironment.DEV,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_dev",
            intended_applications=("Contacts",),
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        self.repository.create(self.project, actor=LOCAL_ACTOR)
        self.project = replace(
            self.project,
            source_files=(self.source,),
            revision=2,
            updated_at=now,
        )
        self.repository.add_source_file(
            self.project,
            self.source,
            expected_revision=1,
            actor=LOCAL_ACTOR,
        )
        self.authorization = CapabilityAuthorizationPolicy()
        self.sources = SourceWorkspaceService(
            self.repository,
            self.authorization,
        )
        self.schemas = SchemaWorkspaceService(
            self.repository,
            self.authorization,
        )
        self.mappings = MappingWorkspaceService(
            self.repository,
            self.authorization,
        )
        self.catalog = _catalog(self.source, now)
        self.repository.save_source_catalogs(
            self.project.project_id,
            (self.catalog,),
            actor=LOCAL_ACTOR,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
            self.repository.get_source_selection(self.project.project_id),
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

        draft = self.mappings.save(
            self.project.project_id,
            proposals=(
                FieldMapping(
                    dataset_name="customers",
                    source_column="name",
                    target_model="res.partner",
                    target_field="name",
                ),
            ),
            submit=True,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(draft.status, MappingStatus.SUBMITTED)
        self.assertEqual(draft.version, 1)
        self.assertEqual(
            self.repository.get_mapping_draft(self.project.project_id),
            draft,
        )

        replacement = _catalog(
            self.source,
            datetime.now(timezone.utc),
            warning="Catalog regenerated",
        )
        self.repository.save_source_catalog(
            self.project.project_id,
            replacement,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            self.repository.get_source_configurations(self.project.project_id),
            (),
        )
        self.assertIsNone(
            self.repository.get_source_selection(self.project.project_id)
        )
        self.assertIsNone(
            self.repository.get_mapping_draft(self.project.project_id)
        )

    def test_readonly_target_field_is_rejected(self) -> None:
        self.sources.confirm_source(
            self.project.project_id,
            self.source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=LOCAL_ACTOR,
        )
        self.sources.freeze_selection(
            self.project.project_id,
            dataset_names={(self.source.file_id, "csv"): "customers"},
            actor=LOCAL_ACTOR,
        )
        self.schemas.capture(
            self.project.project_id,
            _metadata_snapshot(),
            actor=LOCAL_ACTOR,
        )
        with self.assertRaisesRegex(WorkspaceError, "readonly"):
            self.mappings.save(
                self.project.project_id,
                proposals=(
                    FieldMapping(
                        dataset_name="customers",
                        source_column="code",
                        target_model="res.partner",
                        target_field="display_name",
                    ),
                ),
                submit=False,
                actor=LOCAL_ACTOR,
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
            candidate_type="string",
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
        fingerprint=EnvironmentFingerprint(
            environment="DEV",
            database="odoo19_dev",
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
                },
            )
        },
    )
