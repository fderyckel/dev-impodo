"""Verify Phase M2 DataVersion ownership and workspace source projections."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import duckdb

from impodo.access import (
    Actor,
    ActorIdentity,
    AuthorizationError,
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.artifacts import StoredArtifact
from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.schema.data_version_store import (
    DATA_VERSION_STORE_GENERATION,
)
from impodo.adapters.duckdb.schema.migration_registry import (
    MIGRATION_REGISTRY_GENERATION,
)
from impodo.adapters.duckdb.schema.migration_workspace_store import (
    MIGRATION_WORKSPACE_GENERATION,
)
from impodo.application.workspace_source_projection import (
    WorkspaceMappingSourceProjection,
)
from impodo.data_version_sources import (
    DataVersionSourcePackage,
    DataVersionSourcePackageService,
    DataVersionSourceIntakeService,
    SourcePackageCatalog,
    SourcePackageConfiguration,
    SourcePackageDataset,
    SourcePackageFile,
    SourcePackageOrigin,
    SourcePackageState,
    WorkspaceSourceProjectionService,
    source_column_contract_hash,
)
from impodo.data_versions import DataVersionService, DataVersionState
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import FileSourceBinding, OdooSourceBinding
from impodo.migration_foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationOperationState,
    MigrationStorageCompatibilityError,
    utc_now,
)
from impodo.migration_projects import MigrationProjectService
from impodo.migration_runs import MigrationRunService
from impodo.migration_workspaces import MigrationWorkspaceService
from impodo.workspace_contracts import SourceDatasetColumn


ROOT = Path(__file__).resolve().parents[1]


class SimulatedCrash(RuntimeError):
    pass


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}

    def store_source(
        self,
        project_id,
        *,
        artifact_id,
        suffix,
        stream,
        maximum_bytes,
        chunk_bytes,
        validator,
    ):
        del chunk_bytes, validator
        payload = stream.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise AssertionError("Test upload exceeded its declared bound")
        storage_key = f"inbox/{artifact_id}{suffix}"
        self.items[(project_id, storage_key)] = payload
        return StoredArtifact(
            storage_key=storage_key,
            size_bytes=len(payload),
            sha256=f"sha256:{sha256(payload).hexdigest()}",
        )

    def delete_source(self, project_id, storage_key) -> None:
        del self.items[(project_id, storage_key)]


def _crash_at(expected_stage: str):
    def crash(stage: str) -> None:
        if stage == expected_stage:
            raise SimulatedCrash(stage)

    return crash


class MigrationProjectPhaseM2SourcePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = MigrationFoundationDatabase(self.temporary.name)
        self.repository = MigrationFoundationRepository(self.database)
        authorization = CapabilityAuthorizationPolicy()
        self.projects = MigrationProjectService(self.repository, authorization)
        self.data_versions = DataVersionService(self.repository, authorization)
        self.runs = MigrationRunService(self.repository, authorization)
        self.workspaces = MigrationWorkspaceService(
            self.repository,
            authorization,
        )
        self.sources = DataVersionSourcePackageService(
            self.repository,
            authorization,
        )
        self.projections = WorkspaceSourceProjectionService(
            self.repository,
            authorization,
        )
        self.project = self.projects.create(
            actor=LOCAL_ACTOR,
            display_name="Legacy ERP rollout",
            migration_purpose="Move governed master data to Odoo 19",
            source_system_identity="Fictional Legacy ERP",
        )
        self.data_version = self.data_versions.create(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            expected_project_revision=self.project.optimistic_revision,
            purpose="AUTHORING",
            label="Representative source package",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _package(self, *, revision: int = 1) -> DataVersionSourcePackage:
        received_at = utc_now()
        file_id = str(uuid4())
        file_hash = content_hash("fictional-source-bytes")
        catalog_payload = {
            "format": "CSV",
            "tables": ["Customers", "Products"],
        }
        catalog = SourcePackageCatalog(
            file_id=file_id,
            source_sha256=file_hash,
            payload=catalog_payload,
        )
        columns = (
            SourceDatasetColumn(
                ordinal=1,
                source_name="Legacy ID",
                stable_key="column:legacy_id",
                candidate_type="INTEGER",
            ),
            SourceDatasetColumn(
                ordinal=2,
                source_name="Name",
                stable_key="column:name",
                candidate_type="STRING",
            ),
        )
        return DataVersionSourcePackage(
            data_version_id=self.data_version.data_version_id,
            project_id=self.project.project_id,
            revision=revision,
            origin=SourcePackageOrigin.FILE,
            state=SourcePackageState.DRAFT,
            files=(
                SourcePackageFile(
                    file_id=file_id,
                    display_name="legacy-export.csv",
                    storage_key=f"source/{file_hash}/legacy-export.csv",
                    size_bytes=2_048,
                    sha256=file_hash,
                    received_at=received_at,
                ),
            ),
            catalogs=(catalog,),
            configurations=(
                SourcePackageConfiguration(
                    file_id=file_id,
                    catalog_hash=catalog.content_hash,
                    payload={
                        "encoding": "utf-8",
                        "selected_tables": ["Customers", "Products"],
                    },
                ),
            ),
            datasets=(
                SourcePackageDataset(
                    dataset_id="customers",
                    display_name="Customers",
                    source_file_ids=(file_id,),
                    source=FileSourceBinding(
                        file_id=file_id,
                        table_key="Customers",
                        source_sha256=file_hash,
                        catalog_hash=catalog.content_hash,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=150,
                    columns=columns,
                    schema_hash=source_column_contract_hash(columns),
                    snapshot_hash=content_hash("customer-snapshot"),
                    snapshot_storage_key="snapshots/customers.parquet",
                    manifest={"logical_name": "customers"},
                ),
                SourcePackageDataset(
                    dataset_id="products",
                    display_name="Products",
                    source_file_ids=(file_id,),
                    source=FileSourceBinding(
                        file_id=file_id,
                        table_key="Products",
                        source_sha256=file_hash,
                        catalog_hash=catalog.content_hash,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=75,
                    columns=columns,
                    schema_hash=source_column_contract_hash(columns),
                    snapshot_hash=content_hash("product-snapshot"),
                    snapshot_storage_key="snapshots/products.parquet",
                    manifest={"logical_name": "products"},
                ),
            ),
            updated_at=received_at,
        )

    def _freeze(self, *, fault=None, operation_id: str | None = None):
        if self.sources.repository.get_source_package(
            self.data_version.data_version_id
        ) is None:
            self.sources.replace_draft(
                self._package(),
                actor=LOCAL_ACTOR,
                expected_package_revision=None,
            )
        return self.sources.freeze(
            self.data_version.data_version_id,
            actor=LOCAL_ACTOR,
            expected_data_version_revision=self.data_version.optimistic_revision,
            expected_package_revision=1,
            operation_id=operation_id or str(uuid4()),
            fault=fault,
        )

    def _odoo_package(self) -> DataVersionSourcePackage:
        now = utc_now()
        evidence_hash = content_hash("odoo-source-evidence")
        columns = (
            SourceDatasetColumn(
                ordinal=1,
                source_name="Name",
                stable_key="column:name",
                candidate_type="STRING",
            ),
        )
        return DataVersionSourcePackage(
            data_version_id=self.data_version.data_version_id,
            project_id=self.project.project_id,
            revision=1,
            origin=SourcePackageOrigin.ODOO,
            state=SourcePackageState.DRAFT,
            files=(),
            catalogs=(),
            configurations=(),
            datasets=(
                SourcePackageDataset(
                    dataset_id="partners",
                    display_name="Contacts",
                    source_file_ids=(),
                    source=OdooSourceBinding(
                        capture_selection_hash=evidence_hash,
                        model="res.partner",
                        policy_hash=content_hash("source-policy"),
                        connection_target_hash=content_hash("source-target"),
                        schema_scope_hash=content_hash("source-schema"),
                        read_principal_hash=content_hash("source-principal"),
                        read_permission_hash=content_hash("source-permission"),
                        context_hash=content_hash("source-context"),
                    ),
                    row_count=20,
                    columns=columns,
                    schema_hash=source_column_contract_hash(columns),
                    snapshot_hash=content_hash("partner-snapshot"),
                    snapshot_storage_key="snapshots/partners.parquet",
                    manifest={"model": "res.partner"},
                ),
            ),
            updated_at=now,
        )

    def _run_and_workspaces(self):
        project = self.projects.get(self.project.project_id, actor=LOCAL_ACTOR)
        run = self.runs.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_project_revision=project.optimistic_revision,
            data_version_id=self.data_version.data_version_id,
            purpose="AUTHORING",
            label="Authoring run",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        customer = self.workspaces.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_project_revision=project.optimistic_revision,
            data_version_id=self.data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Customer workspace",
        )
        project = self.projects.get(project.project_id, actor=LOCAL_ACTOR)
        product = self.workspaces.create(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_project_revision=project.optimistic_revision,
            data_version_id=self.data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Product workspace",
        )
        return customer, product

    def test_exact_m2_generations_store_source_only_in_data_version(self) -> None:
        candidate = self._package()
        reordered = replace(candidate, datasets=tuple(reversed(candidate.datasets)))
        self.assertEqual(reordered.content_hash, candidate.content_hash)
        self.assertEqual(
            tuple(item.dataset_id for item in reordered.datasets),
            ("customers", "products"),
        )
        package = self.sources.replace_draft(
            reordered,
            actor=LOCAL_ACTOR,
            expected_package_revision=None,
        )
        self.assertEqual(package.revision, 1)
        data_path = self.database.data_version_store_path(
            self.project.project_id,
            self.data_version.data_version_id,
        )
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT generation FROM schema_version"
                ).fetchone(),
                (MIGRATION_REGISTRY_GENERATION,),
            )
            self.assertIn("expected_revision", {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('project_operation_intent')"
                ).fetchall()
            })
        with self.database.connect(data_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT generation FROM schema_version"
                ).fetchone(),
                (DATA_VERSION_STORE_GENERATION,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM source_package_file"
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM source_package_dataset"
                ).fetchone(),
                (2,),
            )

    def test_file_intake_builds_one_draft_before_acceptance(self) -> None:
        artifacts = MemoryArtifactStore()
        intake = DataVersionSourceIntakeService(
            self.repository,
            CapabilityAuthorizationPolicy(),
            artifacts,
        )
        draft = intake.accept(
            self.data_version.data_version_id,
            actor=LOCAL_ACTOR,
            expected_package_revision=None,
            display_name="customers.csv",
            stream=BytesIO(b"legacy_id,name\n1,Ada\n"),
        )
        self.assertEqual(draft.revision, 1)
        self.assertEqual(draft.catalogs, ())
        with self.assertRaises(MigrationFoundationError):
            self.sources.freeze(
                self.data_version.data_version_id,
                actor=LOCAL_ACTOR,
                expected_data_version_revision=(
                    self.data_version.optimistic_revision
                ),
                expected_package_revision=draft.revision,
                operation_id=str(uuid4()),
            )
        source_file = draft.files[0]
        catalog = SourcePackageCatalog(
            file_id=source_file.file_id,
            source_sha256=source_file.sha256,
            payload={"format": "CSV", "tables": ["Customers"]},
        )
        draft = self.sources.record_catalog(
            draft.data_version_id,
            catalog,
            actor=LOCAL_ACTOR,
            expected_package_revision=draft.revision,
        )
        draft = self.sources.confirm_configuration(
            draft.data_version_id,
            SourcePackageConfiguration(
                file_id=source_file.file_id,
                catalog_hash=catalog.content_hash,
                payload={"encoding": "utf-8", "selected_tables": ["Customers"]},
            ),
            actor=LOCAL_ACTOR,
            expected_package_revision=draft.revision,
        )
        columns = (
            SourceDatasetColumn(
                ordinal=1,
                source_name="Legacy ID",
                stable_key="column:legacy_id",
                candidate_type="INTEGER",
            ),
        )
        draft = self.sources.replace_datasets(
            draft.data_version_id,
            (
                SourcePackageDataset(
                    dataset_id="customers",
                    display_name="Customers",
                    source_file_ids=(source_file.file_id,),
                    source=FileSourceBinding(
                        file_id=source_file.file_id,
                        table_key="Customers",
                        source_sha256=source_file.sha256,
                        catalog_hash=catalog.content_hash,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=1,
                    columns=columns,
                    schema_hash=source_column_contract_hash(columns),
                    snapshot_hash=content_hash("customer-snapshot"),
                    snapshot_storage_key="snapshots/customers.parquet",
                    manifest={"logical_name": "customers"},
                ),
            ),
            actor=LOCAL_ACTOR,
            expected_package_revision=draft.revision,
        )
        frozen = self.sources.freeze(
            draft.data_version_id,
            actor=LOCAL_ACTOR,
            expected_data_version_revision=self.data_version.optimistic_revision,
            expected_package_revision=draft.revision,
            operation_id=str(uuid4()),
        )
        self.assertEqual(frozen.state, SourcePackageState.FROZEN)
        self.assertIn(
            (draft.data_version_id, source_file.storage_key),
            artifacts.items,
        )

    def test_two_workspaces_reference_different_datasets_without_copying(self) -> None:
        frozen = self._freeze()
        customer, product = self._run_and_workspaces()
        customer_projection = self.projections.materialize(
            customer.workspace_id,
            actor=LOCAL_ACTOR,
            dataset_ids=("customers",),
            expected_workspace_revision=customer.optimistic_revision,
            operation_id=str(uuid4()),
        )
        product_projection = self.projections.materialize(
            product.workspace_id,
            actor=LOCAL_ACTOR,
            dataset_ids=("products",),
            expected_workspace_revision=product.optimistic_revision,
            operation_id=str(uuid4()),
        )
        self.assertEqual(customer_projection.package_hash, frozen.content_hash)
        self.assertEqual(product_projection.package_hash, frozen.content_hash)
        self.assertEqual(
            tuple(item.dataset_id for item in customer_projection.datasets),
            ("customers",),
        )
        self.assertEqual(
            tuple(item.dataset_id for item in product_projection.datasets),
            ("products",),
        )
        mapping_sources = WorkspaceMappingSourceProjection(self.repository)
        customer_selection = mapping_sources.get_mapping_source_selection(
            customer.workspace_id
        )
        product_selection = mapping_sources.get_mapping_source_selection(
            product.workspace_id
        )
        assert customer_selection is not None
        assert product_selection is not None
        self.assertEqual(
            tuple(item.dataset_id for item in customer_selection.datasets),
            ("customers",),
        )
        self.assertEqual(
            tuple(item.dataset_id for item in product_selection.datasets),
            ("products",),
        )
        self.assertNotEqual(
            customer_selection.content_hash,
            product_selection.content_hash,
        )
        for workspace in (customer, product):
            path = self.database.workspace_store_path(
                workspace.project_id,
                workspace.workspace_id,
            )
            with self.database.connect(path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT generation FROM schema_version"
                    ).fetchone(),
                    (MIGRATION_WORKSPACE_GENERATION,),
                )
                tables = {
                    str(row[0])
                    for row in connection.execute("SHOW TABLES").fetchall()
                }
                self.assertNotIn("source_package_file", tables)
                self.assertNotIn("source_package_catalog", tables)
                self.assertNotIn("source_snapshot_manifest", tables)
        data_path = self.database.data_version_store_path(
            self.project.project_id,
            self.data_version.data_version_id,
        )
        self.assertEqual(
            list(Path(self.temporary.name).rglob("data-version.duckdb")),
            [data_path],
        )

    def test_freeze_is_immutable_and_updates_data_version_identity(self) -> None:
        frozen = self._freeze()
        self.assertEqual(frozen.state, SourcePackageState.FROZEN)
        data_version = self.data_versions.get(
            self.data_version.data_version_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(data_version.state, DataVersionState.FROZEN)
        self.assertEqual(data_version.source_package_hash, frozen.content_hash)
        with self.assertRaises(MigrationConflictError):
            self.repository.replace_draft_source_package(
                replace(
                    frozen,
                    revision=frozen.revision + 1,
                    state=SourcePackageState.DRAFT,
                    frozen_at=None,
                ),
                expected_package_revision=frozen.revision,
                actor=LOCAL_ACTOR,
            )

    def test_odoo_package_uses_the_same_data_version_boundary(self) -> None:
        saved = self.sources.replace_draft(
            self._odoo_package(),
            actor=LOCAL_ACTOR,
            expected_package_revision=None,
        )
        self.assertEqual(saved.origin, SourcePackageOrigin.ODOO)
        self.assertEqual(saved.files, ())
        frozen = self.sources.freeze(
            self.data_version.data_version_id,
            actor=LOCAL_ACTOR,
            expected_data_version_revision=self.data_version.optimistic_revision,
            expected_package_revision=saved.revision,
            operation_id=str(uuid4()),
        )
        self.assertEqual(frozen.state, SourcePackageState.FROZEN)
        self.assertEqual(
            tuple(item.dataset_id for item in frozen.datasets),
            ("partners",),
        )

    def test_projection_rejects_unknown_dataset_without_mutation(self) -> None:
        self._freeze()
        workspace, _ = self._run_and_workspaces()
        with self.assertRaises(MigrationConflictError):
            self.projections.materialize(
                workspace.workspace_id,
                actor=LOCAL_ACTOR,
                dataset_ids=("unknown",),
                expected_workspace_revision=workspace.optimistic_revision,
                operation_id=str(uuid4()),
            )
        self.assertIsNone(
            self.repository.get_workspace_source_projection(
                workspace.workspace_id
            )
        )

    def test_authorization_rejects_package_and_projection_mutation(self) -> None:
        denied = Actor(
            identity=ActorIdentity(
                issuer="impodo.test",
                subject_id="denied",
                display_name="Denied actor",
            ),
            capabilities=frozenset(),
        )
        with self.assertRaises(AuthorizationError):
            self.sources.replace_draft(
                self._package(),
                actor=denied,
                expected_package_revision=None,
            )
        artifacts = MemoryArtifactStore()
        intake = DataVersionSourceIntakeService(
            self.repository,
            CapabilityAuthorizationPolicy(),
            artifacts,
        )
        with self.assertRaises(AuthorizationError):
            intake.accept(
                self.data_version.data_version_id,
                actor=denied,
                expected_package_revision=None,
                display_name="customers.csv",
                stream=BytesIO(b"id,name\n1,Ada\n"),
            )
        self.assertEqual(artifacts.items, {})
        self._freeze()
        workspace, _ = self._run_and_workspaces()
        with self.assertRaises(AuthorizationError):
            self.projections.materialize(
                workspace.workspace_id,
                actor=denied,
                dataset_ids=("customers",),
                expected_workspace_revision=workspace.optimistic_revision,
                operation_id=str(uuid4()),
            )

    def test_package_and_projection_use_optimistic_revisions(self) -> None:
        package = self.sources.replace_draft(
            self._package(),
            actor=LOCAL_ACTOR,
            expected_package_revision=None,
        )
        with self.assertRaises(MigrationConflictError):
            self.sources.replace_draft(
                replace(package, revision=2, updated_at=utc_now()),
                actor=LOCAL_ACTOR,
                expected_package_revision=None,
            )
        self._freeze()
        workspace, _ = self._run_and_workspaces()
        projection = self.projections.materialize(
            workspace.workspace_id,
            actor=LOCAL_ACTOR,
            dataset_ids=("customers",),
            expected_workspace_revision=workspace.optimistic_revision,
            operation_id=str(uuid4()),
        )
        self.assertEqual(projection.workspace_id, workspace.workspace_id)
        with self.assertRaises(MigrationConflictError):
            self.projections.materialize(
                workspace.workspace_id,
                actor=LOCAL_ACTOR,
                dataset_ids=("products",),
                expected_workspace_revision=workspace.optimistic_revision,
                operation_id=str(uuid4()),
            )

    def test_freeze_and_projection_recover_after_cross_store_faults(self) -> None:
        self.sources.replace_draft(
            self._package(),
            actor=LOCAL_ACTOR,
            expected_package_revision=None,
        )
        freeze_operation = str(uuid4())
        with self.assertRaises(SimulatedCrash):
            self._freeze(
                operation_id=freeze_operation,
                fault=_crash_at("STORE_CREATED"),
            )
        self.assertEqual(
            self.repository.get_operation_intent(freeze_operation).state,
            MigrationOperationState.PENDING,
        )
        frozen = self._freeze(operation_id=freeze_operation)
        self.assertEqual(frozen.state, SourcePackageState.FROZEN)

        workspace, _ = self._run_and_workspaces()
        projection_operation = str(uuid4())
        with self.assertRaises(SimulatedCrash):
            self.projections.materialize(
                workspace.workspace_id,
                actor=LOCAL_ACTOR,
                dataset_ids=("customers",),
                expected_workspace_revision=workspace.optimistic_revision,
                operation_id=projection_operation,
                fault=_crash_at("STORE_CREATED"),
            )
        projection = self.projections.materialize(
            workspace.workspace_id,
            actor=LOCAL_ACTOR,
            dataset_ids=("customers",),
            expected_workspace_revision=workspace.optimistic_revision,
            operation_id=projection_operation,
        )
        self.assertEqual(
            tuple(item.dataset_id for item in projection.datasets),
            ("customers",),
        )
        path = self.database.workspace_store_path(
            workspace.project_id,
            workspace.workspace_id,
        )
        with self.database.connect(path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM workspace_source_projection"
                ).fetchone(),
                (1,),
            )

    def test_m1_storage_is_rejected_instead_of_upgraded(self) -> None:
        old_root = Path(self.temporary.name) / "old-m1"
        old_root.mkdir()
        registry = old_root / "registry.duckdb"
        with duckdb.connect(str(registry)) as connection:
            connection.execute(
                "CREATE TABLE schema_version "
                "(singleton_id INTEGER, generation VARCHAR, version INTEGER)"
            )
            connection.execute(
                "INSERT INTO schema_version VALUES "
                "(1, 'impodo-migration-registry-2026-08-m1', 1)"
            )
        with self.assertRaises(MigrationStorageCompatibilityError):
            MigrationFoundationDatabase(old_root)
        with duckdb.connect(str(registry), read_only=True) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT generation FROM schema_version"
                ).fetchone(),
                ("impodo-migration-registry-2026-08-m1",),
            )


if __name__ == "__main__":
    unittest.main()
