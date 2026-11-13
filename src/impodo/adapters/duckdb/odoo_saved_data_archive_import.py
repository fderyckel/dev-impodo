"""Admit verified archives under new DataVersion and protected-key ownership."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    SourcePackageOrigin,
    SourcePackageState,
)
from impodo.application.odoo_provenance_service import OdooCaptureProvenanceCandidate
from impodo.application.odoo_saved_data_service import SavedOdooDataService
from impodo.application.odoo_saved_data_workspaces import OdooSavedDataWorkspaceService
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.domain.odoo_capture import OdooCaptureSelection
from impodo.domain.odoo_provenance import OdooCaptureManifest
from impodo.domain.project.foundation import (
    MigrationNotFoundError,
    MigrationOperationState,
)
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import (
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
    SourceDataset,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateNotFoundError,
    WorkspaceStatus,
)

from .odoo_provenance_repository import OdooProvenanceRepository
from .odoo_saved_data_archive_payload import OdooArchivePayload
from .run_aware_schema_repository import RunAwareSchemaRepository
from .source_repository import SourceRepository


class OdooSavedDataArchiveImporter:
    """Resume one package admission without borrowing its source installation."""

    def __init__(
        self,
        *,
        saved_data: SavedOdooDataService,
        workspace_factory: OdooSavedDataWorkspaceService,
        sources: SourceRepository,
        schemas: RunAwareSchemaRepository,
        provenance: OdooProvenanceRepository,
        artifacts: DataVersionSourceArtifactStore,
    ) -> None:
        self.saved_data = saved_data
        self.factory = workspace_factory
        self.sources = sources
        self.schemas = schemas
        self.provenance = provenance
        self.artifacts = artifacts

    def admit(
        self,
        project_id: str,
        payload: OdooArchivePayload,
        *,
        package_hash: str,
        actor: Actor,
    ):
        """Create new ownership only after complete transport validation."""
        operation_id = str(
            uuid5(UUID(project_id), f"saved-odoo-archive:{package_hash}")
        )
        saved_id = self._child(operation_id, "definition")
        try:
            existing = self.saved_data.get(project_id, saved_id, actor=actor)
        except MigrationNotFoundError:
            existing = None
        if existing is not None and existing.latest_version is not None:
            return existing
        state, data = self.saved_data.local_operation(
            lambda: self._provision(project_id, payload, operation_id, actor)
        )
        current = self.sources.get_source_selection(state.workspace_id)
        if current is None:
            _schema, captures = self.saved_data.local_operation(
                lambda: self._seed(
                    state, data.data_version_id, payload, operation_id, actor
                )
            )
            old_by_model = {item.selection.model: item for item in payload.datasets}
            datasets = tuple(
                SourceDataset(
                    dataset_id=item.dataset_id,
                    name=item.dataset_name,
                    source=item.source_binding,
                    row_count=old_by_model[item.model].snapshot.row_count,
                    columns=next(
                        dataset.columns
                        for dataset in payload.source_selection.datasets
                        if dataset.dataset_id
                        == old_by_model[item.model].selection.dataset_id
                    ),
                )
                for item in captures
            )
            selection = SourceSelection(
                selection_id=self._child(operation_id, "source-selection"),
                version=1,
                data_version_id=data.data_version_id,
                created_at=payload.source_selection.created_at,
                created_by=payload.source_selection.created_by,
                datasets=datasets,
                content_hash=content_hash(
                    {
                        "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
                        "data_version_id": data.data_version_id,
                        "version": 1,
                        "datasets": [item.to_dict() for item in datasets],
                    }
                ),
            )
            snapshots = tuple(
                SourceSnapshot.create(
                    data_version_id=data.data_version_id,
                    dataset_id=capture.dataset_id,
                    dataset_name=capture.dataset_name,
                    source=capture.source_binding,
                    physical_selection_hash=selection.content_hash,
                    schema=old_by_model[capture.model].snapshot.schema,
                    row_count=old_by_model[capture.model].snapshot.row_count,
                    data_logical_hash=old_by_model[
                        capture.model
                    ].snapshot.data_logical_hash,
                    parquet_sha256=old_by_model[capture.model].snapshot.parquet_sha256,
                    created_at=old_by_model[capture.model].snapshot.created_at,
                )
                for capture in captures
            )
            self.artifacts.ensure_source_snapshot_capacity(
                data.data_version_id,
                required_bytes=sum(
                    item.manifest.data_size_bytes for item in payload.datasets
                ),
            )
            for capture, snapshot in zip(captures, snapshots, strict=True):
                with self.artifacts.prepare_source_snapshot(
                    data.data_version_id
                ) as temporary:
                    candidate = temporary / "import.parquet"
                    shutil.copyfile(old_by_model[capture.model].path, candidate)
                    self.artifacts.publish_source_snapshot(
                        data.data_version_id,
                        candidate,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )
            protected = self.saved_data.local_operation(
                lambda: self._origins(
                    state.workspace_id, captures, snapshots, payload, actor
                )
            )
            self.saved_data.local_operation(
                lambda: self.provenance.publish_complete_captures(
                    state.workspace_id,
                    tuple((item.manifest, item.encrypted_bytes) for item in protected),
                    selection,
                    snapshots,
                    actor=actor,
                )
            )
            manifests = tuple(item.manifest for item in protected)
        else:
            selection = current
            snapshots = self.sources.get_current_source_snapshots(state.workspace_id)
            manifests = self.provenance.get_currents(state.workspace_id)
            if {item.model: item.data_sha256 for item in manifests} != {
                item.selection.model: item.snapshot.parquet_sha256
                for item in payload.datasets
            }:
                raise WorkspaceError("The resumed archive source evidence changed")
        self.saved_data.local_operation(
            lambda: self.saved_data.projections.accept_odoo_capture(
                state.workspace_id, selection, snapshots, manifests, actor=actor
            )
        )
        return self.saved_data.local_operation(
            lambda: self.saved_data.retain_imported_version(
                state.workspace_id,
                actor=actor,
                label=payload.label,
                all_fields=payload.all_fields,
                saved_data_id=saved_id,
                operation_id=self._child(operation_id, "retain"),
                package_hash=package_hash,
                expected_source_selection_hash=selection.content_hash,
                expected_manifest_hashes=tuple(
                    sorted((item.model, item.content_hash) for item in manifests)
                ),
            )
        )

    def _provision(self, project_id, payload, operation_id, actor):
        project = self.factory.projects.get(project_id, actor=actor)
        data_op = self._child(operation_id, "data-version")
        data_repo = self.factory.data_versions.repository
        intent = self._intent(data_op)
        if intent is None:
            data = self.factory.data_versions.create(
                project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                purpose=DataVersionPurpose.AUTHORING,
                label="Imported saved Odoo data",
                operation_id=data_op,
            )
        elif intent.state is MigrationOperationState.COMMITTED:
            data = data_repo.get_data_version(intent.owner_id)
        else:
            data = data_repo.resume_data_version_creation(data_op, actor=actor)
        packages = self.factory.source_restarts.source_packages
        if packages.repository.get_source_package(data.data_version_id) is None:
            packages.replace_draft(
                DataVersionSourcePackage(
                    data_version_id=data.data_version_id,
                    project_id=project_id,
                    revision=1,
                    origin=SourcePackageOrigin.ODOO,
                    state=SourcePackageState.DRAFT,
                    files=(),
                    catalogs=(),
                    configurations=(),
                    datasets=(),
                    updated_at=datetime.now(UTC),
                ),
                actor=actor,
                expected_package_revision=None,
            )
        run_op = self._child(operation_id, "run")
        intent = self._intent(run_op)
        if intent is None:
            project = self.factory.projects.get(project_id, actor=actor)
            run = self.factory.runs.create(
                project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data.data_version_id,
                purpose=MigrationRunPurpose.AUTHORING,
                label="Import saved Odoo archive",
                operation_id=run_op,
            )
        elif intent.state is MigrationOperationState.COMMITTED:
            run = self.factory.runs.repository.get_migration_run(intent.owner_id)
        else:
            run = self.factory.runs.repository.resume_migration_run_creation(
                run_op, actor=actor
            )
        workspace_op = self._child(operation_id, "workspace")
        intent = self._intent(workspace_op)
        if intent is None:
            project = self.factory.projects.get(project_id, actor=actor)
            workspace = self.factory.workspaces.create(
                project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data.data_version_id,
                migration_run_id=run.migration_run_id,
                display_name="Imported saved Odoo data",
                operation_id=workspace_op,
            )
        elif intent.state is MigrationOperationState.COMMITTED:
            workspace = self.factory.workspaces.repository.get_migration_workspace(
                intent.owner_id
            )
        else:
            workspace = (
                self.factory.workspaces.repository.resume_migration_workspace_creation(
                    workspace_op, actor=actor
                )
            )
        try:
            state = self.factory.workspace_states.repository.get(workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            state = self.factory.workspace_states.provision_migration_workspace(
                workspace.workspace_id,
                actor=actor,
                name=workspace.display_name,
                source_system=payload.source_system,
                source_mode=SourceMode.ODOO,
                data_classification=project.data_classification,
                retention_days=project.retention_days,
            )
        if state.status is WorkspaceStatus.DRAFT:
            state = self.factory.workspace_states.update_target(
                state.workspace_id,
                actor=actor,
                expected_revision=state.revision,
                odoo_connection_mode=payload.source_mode.value,
                odoo_base_url=payload.source_url,
                odoo_database=payload.source_database,
                intended_applications=(),
                intended_models=tuple(
                    item.selection.model for item in payload.datasets
                ),
            )
            state = self.factory.workspace_states.register(
                state.workspace_id, actor=actor, expected_revision=state.revision
            )
        if state.odoo_company != payload.company:
            self.saved_data.source_companies.set_source_company(
                state.workspace_id,
                payload.company,
                actor=actor,
                expected_revision=state.revision,
            )
            state = self.factory.workspace_states.repository.get(state.workspace_id)
        return state, data

    def _seed(self, state, data_version_id, payload, operation_id, actor):
        schema = replace(payload.schema, workspace_id=state.workspace_id)
        current_schema = self.schemas.get_odoo_schema_catalog(state.workspace_id)
        if current_schema is None:
            scope = payload.scope
            self.factory.workspace_states.update_schema_scope(
                state.workspace_id,
                actor=actor,
                expected_revision=state.revision,
                permitted_models=tuple(item.name for item in schema.models),
                relationship_decisions=scope.decisions if scope else (),
                relationship_root_models=scope.root_models
                if scope
                else tuple(item.name for item in schema.models),
                relationship_explicit_actions=scope.explicit_actions_by_model
                if scope
                else (),
            )
            self.schemas.save_odoo_schema_catalog(
                state.workspace_id, schema, actor=actor
            )
        elif current_schema != schema:
            raise WorkspaceError("The imported Odoo schema changed during admission")
        existing = {
            item.model: item
            for item in self.sources.get_current_odoo_capture_selections(
                state.workspace_id
            )
        }
        captures = []
        for dataset in payload.datasets:
            old = dataset.selection
            selection_id = self._child(operation_id, f"capture:{old.model}")
            protected_hash = None
            if dataset.filters:
                store = self.saved_data.sources.capture_filters
                if store is None:
                    raise WorkspaceError(
                        "Protected source filters are unavailable for this import"
                    )
                protected_hash = store.put(
                    self.factory.workspaces.get(
                        state.workspace_id, actor=actor
                    ).project_id,
                    selection_id=selection_id,
                    version=1,
                    data_version_id=data_version_id,
                    clauses=dataset.filters,
                )
            capture = OdooCaptureSelection.create(
                selection_id=selection_id,
                version=1,
                data_version_id=data_version_id,
                dataset_name=old.dataset_name,
                model=old.model,
                field_names=old.field_names,
                filter_clauses=(),
                protected_filter_artifact_hash=protected_hash,
                capture_role=old.capture_role,
                filter_policy=old.filter_policy,
                max_rows=old.max_rows,
                page_size=old.page_size,
                policy_hash=old.policy_hash,
                connection_target_hash=old.connection_target_hash,
                schema_scope_hash=old.schema_scope_hash,
                read_principal_hash=old.read_principal_hash,
                read_permission_hash=old.read_permission_hash,
                context_hash=old.context_hash,
                created_at=old.created_at,
                created_by=old.created_by,
            )
            if old.model not in existing:
                self.sources.save_odoo_capture_selection(
                    state.workspace_id, capture, actor=actor
                )
            elif existing[old.model] != capture:
                raise WorkspaceError("The imported capture configuration changed")
            captures.append(capture)
        return schema, tuple(captures)

    def _origins(self, workspace_id, captures, snapshots, payload, actor):
        old_by_model = {item.selection.model: item for item in payload.datasets}
        protected = []
        retention = datetime.now(UTC) + timedelta(
            days=self.factory.workspace_states.repository.get(
                workspace_id
            ).retention_days
        )
        for capture, snapshot in zip(captures, snapshots, strict=True):
            old = old_by_model[capture.model]
            candidate = self.saved_data.provenance.prepare_capture_origins(
                workspace_id,
                actor=actor,
                selection=capture,
                header=old.header,
                batches=old.origins,
                row_count=snapshot.row_count,
                data_logical_hash=snapshot.data_logical_hash,
                data_sha256=snapshot.parquet_sha256,
                data_storage_key=snapshot.parquet_storage_key,
                data_size_bytes=old.manifest.data_size_bytes,
                capture_started_at=old.manifest.capture_started_at,
                capture_finished_at=old.manifest.capture_finished_at,
            )
            manifest = candidate.manifest
            retained = OdooCaptureManifest.create(
                manifest_id=manifest.manifest_id,
                selection=capture,
                dataset_id=capture.dataset_id,
                column_stable_keys=capture.column_stable_keys,
                row_count=manifest.row_count,
                data_logical_hash=manifest.data_logical_hash,
                data_sha256=manifest.data_sha256,
                data_storage_key=manifest.data_storage_key,
                data_size_bytes=manifest.data_size_bytes,
                provenance_logical_hash=manifest.provenance_logical_hash,
                provenance_sha256=manifest.provenance_sha256,
                provenance_storage_key=manifest.provenance_storage_key,
                provenance_size_bytes=manifest.provenance_size_bytes,
                capture_started_at=manifest.capture_started_at,
                capture_finished_at=manifest.capture_finished_at,
                retention_until=retention,
                created_by=manifest.created_by,
            )
            protected.append(
                OdooCaptureProvenanceCandidate(retained, candidate.encrypted_bytes)
            )
        return tuple(protected)

    def _intent(self, operation_id):
        try:
            return self.factory.workspaces.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return None

    @staticmethod
    def _child(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), f"saved-odoo-archive:{name}"))
