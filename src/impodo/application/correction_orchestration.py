"""Coordinate completed-load origin publication and focused correction review.

The coordinator composes existing run, workspace, mapping, preparation,
columnar, target-read, and protected-evidence owners.  It persists only one
run-owned binding/current pointer and never introduces a correction lifecycle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol
from uuid import UUID, uuid5

from impodo.application.correction_service import (
    CorrectionPlanService,
    CorrectionReview,
    CorrectionReviewService,
    build_completed_load_target_index,
)
from impodo.domain.correction import (
    CorrectionCandidate,
    CorrectionConfirmation,
    CorrectionPlan,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarTransformationProgram,
)
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
    CorrectionPreparedArtifact,
    CorrectionTargetIndex,
    ProtectedCorrectionArtifactReference,
)
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.project.foundation import (
    FaultInjector,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
)
from impodo.domain.reconciliation import ReconciliationRun
from impodo.domain.run.models import MigrationRun, MigrationRunPurpose
from impodo.domain.run.setup import MigrationRunTargetSetupService
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
)


@dataclass(frozen=True, slots=True)
class CorrectionBinding:
    """Registry projection; current pointers are evidence, not lifecycle state."""

    correction_binding_id: str
    project_id: str
    data_version_id: str
    completed_migration_run_id: str
    completed_workspace_id: str
    origin: ProtectedCorrectionArtifactReference
    target_index: ProtectedCorrectionArtifactReference
    successor_migration_run_id: str | None
    successor_workspace_id: str | None
    current_mapping_hash: str | None
    current_prepared_hash: str | None
    current_plan: ProtectedCorrectionArtifactReference | None
    current_confirmation: ProtectedCorrectionArtifactReference | None
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.correction_binding_id, "correction_binding_id"),
            (self.project_id, "project_id"),
            (self.data_version_id, "data_version_id"),
            (self.completed_migration_run_id, "completed_migration_run_id"),
            (self.completed_workspace_id, "completed_workspace_id"),
        ):
            require_uuid(value, name)
        if (self.successor_migration_run_id is None) != (
            self.successor_workspace_id is None
        ):
            raise CorrectionOriginError(
                "Correction successor run and workspace must be bound together"
            )
        if self.successor_migration_run_id is not None:
            require_uuid(
                self.successor_migration_run_id,
                "successor_migration_run_id",
            )
            require_uuid(self.successor_workspace_id, "successor_workspace_id")
        for value, name in (
            (self.current_mapping_hash, "current_mapping_hash"),
            (self.current_prepared_hash, "current_prepared_hash"),
        ):
            if value is not None:
                require_hash(value, name)
        if self.current_plan is not None and (
            self.successor_workspace_id is None
            or self.current_mapping_hash is None
            or self.current_prepared_hash is None
        ):
            raise CorrectionOriginError(
                "A current correction plan requires current successor evidence"
            )
        if self.current_confirmation is not None and self.current_plan is None:
            raise CorrectionOriginError(
                "A correction confirmation requires a current plan"
            )
        require_revision(self.optimistic_revision, "correction_binding_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


class CorrectionBindingRepository(Protocol):
    """Consumer-owned atomic registry commands for the correction binding."""

    def get_for_completed_workspace(
        self,
        completed_workspace_id: str,
    ) -> CorrectionBinding | None: ...

    def get_for_successor_workspace(
        self,
        successor_workspace_id: str,
    ) -> CorrectionBinding | None: ...

    def list_for_project(self, project_id: str) -> tuple[CorrectionBinding, ...]: ...

    def seal_completed_origin(
        self,
        binding: CorrectionBinding,
        *,
        expected_run_revision: int,
        expected_workspace_revision: int,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> CorrectionBinding: ...

    def attach_successor(
        self,
        completed_workspace_id: str,
        *,
        successor_migration_run_id: str,
        successor_workspace_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding: ...

    def publish_plan(
        self,
        completed_workspace_id: str,
        *,
        successor_workspace_id: str,
        mapping_hash: str,
        prepared_hash: str,
        plan: ProtectedCorrectionArtifactReference,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding: ...

    def invalidate_plan(
        self,
        completed_workspace_id: str,
        *,
        current_mapping_hash: str | None,
        current_prepared_hash: str | None,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding: ...

    def publish_confirmation(
        self,
        completed_workspace_id: str,
        *,
        successor_workspace_id: str,
        plan_id: str,
        plan_hash: str,
        confirmation: ProtectedCorrectionArtifactReference,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding: ...

    def complete_verified_successor(
        self,
        completed_workspace_id: str,
        *,
        successor_migration_run_id: str,
        successor_workspace_id: str,
        execution_run_id: str,
        reconciliation_id: str,
        reconciliation_hash: str,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding: ...


class StoredTargetIndex(Protocol):
    project_id: str
    index_id: str
    index_hash: str
    storage_key: str
    artifact_hash: str


class StoredOrigin(Protocol):
    project_id: str
    manifest_id: str
    manifest_hash: str
    storage_key: str
    artifact_hash: str


class StoredPlan(Protocol):
    project_id: str
    plan_id: str
    plan_hash: str
    storage_key: str
    artifact_hash: str


class StoredConfirmation(Protocol):
    project_id: str
    confirmation_id: str
    confirmation_hash: str
    storage_key: str
    artifact_hash: str


class CorrectionProtectedStore(Protocol):
    def put_target_index(self, index: CorrectionTargetIndex) -> StoredTargetIndex: ...

    def put_origin(self, manifest: CorrectionOriginManifest) -> StoredOrigin: ...

    def put_plan(self, plan: CorrectionPlan) -> StoredPlan: ...

    def put_confirmation(
        self,
        plan: CorrectionPlan,
        confirmation: CorrectionConfirmation,
    ) -> StoredConfirmation: ...

    def read_target_index(
        self,
        reference: StoredTargetIndex,
    ) -> CorrectionTargetIndex: ...

    def read_origin(
        self,
        reference: StoredOrigin,
    ) -> CorrectionOriginManifest: ...

    def read_plan(self, reference: StoredPlan) -> CorrectionPlan: ...

    def read_confirmation(
        self,
        reference: StoredConfirmation,
    ) -> CorrectionConfirmation: ...


@dataclass(frozen=True, slots=True)
class CorrectionOriginRequest:
    """Existing evidence required to seal one completed Authoring load."""

    completed_run: MigrationRun
    completed_workspace: MigrationWorkspace
    mapping: MappingRevision
    prepared_snapshots: tuple[PreparedSnapshot, ...]
    execution_snapshot: ExecutionSnapshot
    execution: ExecutionRun
    reconciliation: ReconciliationRun
    target_records: RecordSnapshot


@dataclass(frozen=True, slots=True)
class CorrectionOriginPublication:
    binding: CorrectionBinding
    manifest: CorrectionOriginManifest
    target_index: CorrectionTargetIndex


class CorrectionOriginPublisher:
    """Publish protected origin evidence, then atomically seal its owners."""

    def __init__(
        self,
        bindings: CorrectionBindingRepository,
        protected: CorrectionProtectedStore,
    ) -> None:
        self.bindings = bindings
        self.protected = protected

    def publish(
        self,
        request: CorrectionOriginRequest,
        *,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> CorrectionOriginPublication:
        run = request.completed_run
        workspace = request.completed_workspace
        snapshot = request.execution_snapshot
        owner_checks = (
            (
                run.purpose is MigrationRunPurpose.AUTHORING,
                "CORRECTION_ORIGIN_RUN_PURPOSE_INVALID",
            ),
            (
                run.project_id == workspace.project_id,
                "CORRECTION_ORIGIN_PROJECT_MISMATCH",
            ),
            (
                run.data_version_id == workspace.data_version_id,
                "CORRECTION_ORIGIN_DATA_VERSION_MISMATCH",
            ),
            (
                run.migration_run_id == workspace.migration_run_id,
                "CORRECTION_ORIGIN_RUN_MISMATCH",
            ),
            (
                snapshot.workspace_id == workspace.workspace_id,
                "CORRECTION_ORIGIN_WORKSPACE_MISMATCH",
            ),
            (
                request.mapping.mapping_id == snapshot.mapping_id,
                "CORRECTION_ORIGIN_MAPPING_ID_MISMATCH",
            ),
            (
                request.mapping.version == snapshot.mapping_version,
                "CORRECTION_ORIGIN_MAPPING_VERSION_MISMATCH",
            ),
            (
                request.mapping.definition.content_hash
                == snapshot.mapping_content_hash,
                "CORRECTION_ORIGIN_MAPPING_HASH_MISMATCH",
            ),
        )
        for eligible, failure_code in owner_checks:
            if not eligible:
                raise CorrectionOriginError(
                    "Completed load owners do not form one correction origin",
                    failure_code=failure_code,
                )
        prepared = tuple(
            sorted(request.prepared_snapshots, key=lambda item: item.dataset_id)
        )
        expected_datasets = {item.dataset for item in snapshot.datasets}
        prepared_checks = (
            (bool(prepared), "CORRECTION_ORIGIN_PREPARED_MISSING"),
            (
                {item.dataset_name for item in prepared} == expected_datasets,
                "CORRECTION_ORIGIN_DATASET_SET_MISMATCH",
            ),
            (
                all(item.workspace_id == workspace.workspace_id for item in prepared),
                "CORRECTION_ORIGIN_PREPARED_WORKSPACE_MISMATCH",
            ),
            (
                all(
                    item.mapping_hash == snapshot.mapping_content_hash
                    for item in prepared
                ),
                "CORRECTION_ORIGIN_PREPARED_MAPPING_HASH_MISMATCH",
            ),
            (
                len({item.dataset_id for item in prepared}) == len(prepared),
                "CORRECTION_ORIGIN_DUPLICATE_DATASET",
            ),
            (
                len({item.schema_hash for item in prepared}) == 1,
                "CORRECTION_ORIGIN_SCHEMA_HASH_MISMATCH",
            ),
        )
        for eligible, failure_code in prepared_checks:
            if not eligible:
                raise CorrectionOriginError(
                    "Completed prepared evidence is not eligible for correction",
                    failure_code=failure_code,
                )
        entries = build_completed_load_target_index(
            snapshot,
            request.execution,
            request.reconciliation,
            request.target_records,
        )
        completed_at = request.execution.completed_at
        if completed_at is None:
            raise CorrectionOriginError("Completed execution has no completion time")
        created_at = completed_at.astimezone(timezone.utc)
        index = CorrectionTargetIndex.create(
            index_id=_child_id(run.migration_run_id, "correction-target-index"),
            project_id=run.project_id,
            completed_migration_run_id=run.migration_run_id,
            completed_workspace_id=workspace.workspace_id,
            entries=entries,
            created_at=created_at,
        )
        stored_index = self.protected.put_target_index(index)
        index_reference = ProtectedCorrectionArtifactReference(
            artifact_id=stored_index.index_id,
            logical_hash=stored_index.index_hash,
            storage_key=stored_index.storage_key,
            artifact_hash=stored_index.artifact_hash,
        )
        manifest = CorrectionOriginManifest.create(
            manifest_id=_child_id(run.migration_run_id, "correction-origin"),
            project_id=run.project_id,
            data_version_id=run.data_version_id,
            completed_migration_run_id=run.migration_run_id,
            completed_workspace_id=workspace.workspace_id,
            mapping_id=snapshot.mapping_id,
            mapping_version=snapshot.mapping_version,
            mapping_content_hash=snapshot.mapping_content_hash,
            prepared_artifacts=tuple(_prepared(item) for item in prepared),
            execution_snapshot_hash=snapshot.semantic_hash,
            execution_snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            execution_run_id=request.execution.run_id,
            execution_evidence_hash=_execution_evidence_hash(request.execution),
            reconciliation_id=request.reconciliation.reconciliation_id,
            reconciliation_hash=request.reconciliation.semantic_hash,
            target_hash=snapshot.target_hash,
            schema_hash=prepared[0].schema_hash,
            read_context_hash=snapshot.read_context_hash,
            target_observed_at=snapshot.target_snapshot_at,
            target_index=index_reference,
            created_by=actor.identity,
            created_at=created_at,
        )
        stored_origin = self.protected.put_origin(manifest)
        origin_reference = ProtectedCorrectionArtifactReference(
            artifact_id=stored_origin.manifest_id,
            logical_hash=stored_origin.manifest_hash,
            storage_key=stored_origin.storage_key,
            artifact_hash=stored_origin.artifact_hash,
        )
        candidate = CorrectionBinding(
            correction_binding_id=_child_id(run.migration_run_id, "correction-binding"),
            project_id=run.project_id,
            data_version_id=run.data_version_id,
            completed_migration_run_id=run.migration_run_id,
            completed_workspace_id=workspace.workspace_id,
            origin=origin_reference,
            target_index=index_reference,
            successor_migration_run_id=None,
            successor_workspace_id=None,
            current_mapping_hash=None,
            current_prepared_hash=None,
            current_plan=None,
            current_confirmation=None,
            optimistic_revision=1,
            created_at=created_at,
            updated_at=created_at,
        )
        binding = self.bindings.seal_completed_origin(
            candidate,
            expected_run_revision=run.optimistic_revision,
            expected_workspace_revision=workspace.optimistic_revision,
            actor=actor,
            fault=fault,
        )
        return CorrectionOriginPublication(binding, manifest, index)


@dataclass(frozen=True, slots=True)
class CorrectionSuccessor:
    binding: CorrectionBinding
    run: MigrationRun
    workspace: MigrationWorkspace


class CorrectionMappingSeeder(Protocol):
    """Seed prior business rules through existing workspace evidence owners."""

    def seed(
        self,
        completed_workspace_id: str,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> None: ...


class CorrectionMappingSeedService:
    """Seed prior rules through the existing schema and mapping services."""

    def __init__(self, *, schemas, mappings) -> None:
        self.schemas = schemas
        self.mappings = mappings

    def seed(
        self,
        completed_workspace_id: str,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> None:
        previous = self.mappings.mappings.get_mapping_revision(
            completed_workspace_id
        )
        if previous is None:
            raise CorrectionOriginError("Completed mapping revision is missing")
        existing = self.mappings.mappings.get_mapping_working_draft(
            successor_workspace_id
        )
        if existing is not None:
            if existing.definition.datasets != previous.definition.datasets:
                raise CorrectionOriginError(
                    "Correction workspace already has another mapping seed"
                )
            return
        self.schemas.seed_historical_correction(
            completed_workspace_id,
            successor_workspace_id,
            actor=actor,
        )
        seeded = self.mappings.save_working_draft(
            successor_workspace_id,
            datasets=previous.definition.datasets,
            expected_version=None,
            actor=actor,
        )
        if seeded.definition.datasets != previous.definition.datasets:
            raise CorrectionOriginError("Correction mapping seed changed prior rules")


@dataclass(frozen=True, slots=True)
class CorrectionDatasetReviewInput:
    """One previous/corrected native prepared dataset comparison."""

    previous_path: Path
    previous_snapshot: PreparedSnapshot
    previous_program: ColumnarTransformationProgram
    corrected_path: Path
    corrected_snapshot: PreparedSnapshot
    corrected_program: ColumnarTransformationProgram


@dataclass(frozen=True, slots=True)
class CorrectionTargetReviewEvidence:
    """Fresh read-only target capability resolved after local quality gates."""

    reader: OdooReadbackReader
    reader_scope_hash: str
    read_credential_binding_hash: str
    read_identity: OdooReadIdentity
    reviewed_at: datetime


@dataclass(frozen=True, slots=True)
class CorrectionAuthoringReviewResult:
    """Outputs of the ordered existing-owner review stages."""

    mapping: MappingRevision
    datasets: tuple[CorrectionDatasetReviewInput, ...]
    reader: OdooReadbackReader
    reader_scope_hash: str
    read_credential_binding_hash: str
    read_identity: OdooReadIdentity
    reviewed_at: datetime


class CorrectionMappingReviewStage(Protocol):
    def validate_and_submit(
        self,
        manifest: CorrectionOriginManifest,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> MappingRevision: ...


class CorrectionPreparationReviewStage(Protocol):
    def prepare_native(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[CorrectionDatasetReviewInput, ...]: ...


class CorrectionQualityReviewStage(Protocol):
    def require_current_quality(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        datasets: tuple[CorrectionDatasetReviewInput, ...],
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> None: ...


class CorrectionTargetReviewStage(Protocol):
    def refresh_read_capability(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        datasets: tuple[CorrectionDatasetReviewInput, ...],
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionTargetReviewEvidence: ...


class CorrectionNoChangedIntent(CorrectionOriginError):
    """Signal that submitted rules still match the completed-load origin."""

    def __init__(self, mapping_hash: str) -> None:
        self.mapping_hash = require_hash(mapping_hash, "mapping_hash")
        super().__init__("No correction rule changes were found")


class CorrectionAuthoringStageCoordinator:
    """Run existing mapping, preparation, quality, then target-read owners."""

    def __init__(
        self,
        *,
        mapping: CorrectionMappingReviewStage,
        preparation: CorrectionPreparationReviewStage,
        quality: CorrectionQualityReviewStage,
        target: CorrectionTargetReviewStage,
    ) -> None:
        self.mapping = mapping
        self.preparation = preparation
        self.quality = quality
        self.target = target

    def prepare_review(
        self,
        manifest: CorrectionOriginManifest,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionAuthoringReviewResult:
        mapping = self.mapping.validate_and_submit(
            manifest,
            successor_workspace_id,
            actor=actor,
        )
        if mapping.definition.content_hash == manifest.mapping_content_hash:
            # Exit before Parquet materialization, quality work, or Odoo reads.
            raise CorrectionNoChangedIntent(mapping.definition.content_hash)
        datasets = self.preparation.prepare_native(
            manifest,
            mapping,
            successor_workspace_id,
            actor=actor,
        )
        self.quality.require_current_quality(
            manifest,
            mapping,
            datasets,
            successor_workspace_id,
            actor=actor,
        )
        target = self.target.refresh_read_capability(
            manifest,
            mapping,
            datasets,
            successor_workspace_id,
            actor=actor,
        )
        return CorrectionAuthoringReviewResult(
            mapping=mapping,
            datasets=datasets,
            reader=target.reader,
            reader_scope_hash=target.reader_scope_hash,
            read_credential_binding_hash=target.read_credential_binding_hash,
            read_identity=target.read_identity,
            reviewed_at=target.reviewed_at,
        )


class CorrectionSuccessorService:
    """Create one restart-safe Authoring successor over the same DataVersion."""

    def __init__(
        self,
        *,
        bindings: CorrectionBindingRepository,
        runs,
        workspaces,
        workspace_states: WorkspaceStateService,
        source_projections,
        target_setups: MigrationRunTargetSetupService,
        mapping_seeder: CorrectionMappingSeeder,
    ) -> None:
        self.bindings = bindings
        self.runs = runs
        self.workspaces = workspaces
        self.workspace_states = workspace_states
        self.source_projections = source_projections
        self.target_setups = target_setups
        self.mapping_seeder = mapping_seeder

    def start(
        self,
        completed_workspace_id: str,
        *,
        actor: Actor,
        request_id: str,
    ) -> CorrectionSuccessor:
        require_uuid(request_id, "request_id")
        binding = self.bindings.get_for_completed_workspace(
            require_uuid(completed_workspace_id, "completed_workspace_id")
        )
        if binding is None:
            raise CorrectionOriginError(
                "Completed load has no published correction origin"
            )
        if binding.successor_migration_run_id is not None:
            return CorrectionSuccessor(
                binding=binding,
                run=self.runs.get(binding.successor_migration_run_id, actor=actor),
                workspace=self.workspaces.get(
                    binding.successor_workspace_id,
                    actor=actor,
                ),
            )
        project = self.runs.repository.get_project(binding.project_id)
        run = self.runs.create(
            binding.project_id,
            actor=actor,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=binding.data_version_id,
            purpose=MigrationRunPurpose.AUTHORING,
            label="Correction of completed Odoo load",
            operation_id=_child_id(
                request_id,
                f"correction-successor-run:{binding.correction_binding_id}",
            ),
        )
        project = self.runs.repository.get_project(binding.project_id)
        workspace = self.workspaces.create(
            binding.project_id,
            actor=actor,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=binding.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Correct completed Odoo load",
            operation_id=_child_id(
                request_id,
                f"correction-successor-workspace:{binding.correction_binding_id}",
            ),
        )
        project_root = self.runs.repository.get_project(binding.project_id)
        package = self.runs.repository.get_source_package(binding.data_version_id)
        if package is None:
            raise CorrectionOriginError("Correction DataVersion package is missing")
        try:
            self.workspace_states.repository.get(workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            self.workspace_states.provision_migration_workspace(
                workspace.workspace_id,
                actor=actor,
                name=workspace.display_name,
                source_system=project_root.source_system_identity,
                source_mode=SourceMode(package.origin.value),
                data_classification=project_root.data_classification.value,
                retention_days=project_root.retention_days,
            )
        prior_target = self.target_setups.get(
            binding.completed_migration_run_id,
            actor=actor,
        )
        if prior_target is None:
            raise CorrectionOriginError("Completed load target setup is missing")
        try:
            prior_workspace_state = self.workspace_states.repository.get(
                binding.completed_workspace_id
            )
            successor_state = self.workspace_states.repository.get(
                workspace.workspace_id
            )
        except WorkspaceStateNotFoundError:
            # Narrow application fakes may model only canonical registry roots.
            # The concrete composition always persists both workbench states.
            pass
        else:
            target_mode = getattr(
                prior_target.connection_mode,
                "value",
                prior_target.connection_mode,
            )
            if successor_state.odoo_connection_mode is None:
                successor_state = self.workspace_states.update_target(
                    workspace.workspace_id,
                    actor=actor,
                    expected_revision=successor_state.revision,
                    odoo_connection_mode=target_mode,
                    odoo_base_url=prior_target.base_url,
                    odoo_database=prior_target.database,
                    intended_applications=prior_target.intended_applications,
                    intended_models=prior_workspace_state.intended_models,
                )
            elif (
                getattr(
                    successor_state.odoo_connection_mode,
                    "value",
                    successor_state.odoo_connection_mode,
                )
                != target_mode
                or successor_state.odoo_base_url != prior_target.base_url
                or successor_state.odoo_database != prior_target.database
                or successor_state.intended_applications
                != prior_target.intended_applications
                or successor_state.intended_models
                != prior_workspace_state.intended_models
            ):
                raise CorrectionOriginError(
                    "Correction workspace target no longer matches the completed load"
                )
        workspace = self.workspaces.get(workspace.workspace_id, actor=actor)
        self.source_projections.materialize(
            workspace.workspace_id,
            actor=actor,
            dataset_ids=tuple(item.dataset_id for item in package.datasets),
            expected_workspace_revision=workspace.optimistic_revision,
            operation_id=_child_id(
                request_id,
                f"correction-source-projection:{binding.correction_binding_id}",
            ),
        )
        self.target_setups.replace(
            run.migration_run_id,
            actor=actor,
            expected_revision=None,
            connection_mode=prior_target.connection_mode,
            base_url=prior_target.base_url,
            database=prior_target.database,
            intended_applications=prior_target.intended_applications,
        )
        workspace = self.workspaces.get(workspace.workspace_id, actor=actor)
        if workspace.setup_state.value == "DRAFT":
            workspace = self.workspaces.complete_setup(
                workspace.workspace_id,
                actor=actor,
                expected_revision=workspace.optimistic_revision,
            )
        self.mapping_seeder.seed(
            binding.completed_workspace_id,
            workspace.workspace_id,
            actor=actor,
        )
        current = self.bindings.get_for_completed_workspace(
            binding.completed_workspace_id
        )
        if current is None:
            raise CorrectionOriginError("Correction binding disappeared")
        attached = self.bindings.attach_successor(
            binding.completed_workspace_id,
            successor_migration_run_id=run.migration_run_id,
            successor_workspace_id=workspace.workspace_id,
            expected_revision=current.optimistic_revision,
            actor=actor,
        )
        return CorrectionSuccessor(attached, run, workspace)


@dataclass(frozen=True, slots=True)
class CorrectionReviewEvidence:
    """Current outputs produced by existing validation/preparation owners."""

    mapping: MappingRevision
    previous_prepared_hash: str
    corrected_prepared_hash: str
    candidate_batches: Iterable[tuple[CorrectionCandidate, ...]]
    reader: OdooReadbackReader
    reader_scope_hash: str
    read_credential_binding_hash: str
    read_identity: OdooReadIdentity
    reviewed_at: datetime


class CorrectionReviewPipeline(Protocol):
    """Run existing validation, native preparation, quality, and A/C stages."""

    def run(
        self,
        manifest: CorrectionOriginManifest,
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionReviewEvidence: ...


class CorrectionReviewOrchestrator:
    """Resume existing stages and publish one deterministic current plan."""

    def __init__(
        self,
        *,
        bindings: CorrectionBindingRepository,
        protected: CorrectionProtectedStore,
        pipeline: CorrectionReviewPipeline,
        review_service: CorrectionReviewService | None = None,
        plan_service: CorrectionPlanService | None = None,
    ) -> None:
        self.bindings = bindings
        self.protected = protected
        self.pipeline = pipeline
        self.review_service = review_service or CorrectionReviewService()
        self.plan_service = plan_service or CorrectionPlanService()

    def review(
        self,
        manifest: CorrectionOriginManifest,
        target_index: CorrectionTargetIndex,
        *,
        actor: Actor,
        review_request_id: str,
    ) -> tuple[CorrectionReview, CorrectionPlan | None, CorrectionBinding]:
        require_uuid(review_request_id, "review_request_id")
        binding = self.bindings.get_for_completed_workspace(
            manifest.completed_workspace_id
        )
        if (
            binding is None
            or binding.origin.logical_hash != manifest.manifest_hash
            or binding.target_index.logical_hash != target_index.index_hash
            or binding.successor_workspace_id is None
            or binding.successor_migration_run_id is None
        ):
            raise CorrectionOriginError("Correction review origin is not current")
        try:
            evidence = self.pipeline.run(
                manifest,
                binding.successor_workspace_id,
                actor=actor,
            )
        except CorrectionNoChangedIntent as unchanged_intent:
            current = self.bindings.get_for_completed_workspace(
                manifest.completed_workspace_id
            )
            if current is None:
                raise CorrectionOriginError("Correction binding disappeared")
            unchanged = self.bindings.invalidate_plan(
                manifest.completed_workspace_id,
                current_mapping_hash=unchanged_intent.mapping_hash,
                current_prepared_hash=None,
                expected_revision=current.optimistic_revision,
                actor=actor,
            )
            return (
                CorrectionReview(
                    target_hash=manifest.target_hash,
                    fields=(),
                    blockers=(),
                ),
                None,
                unchanged,
            )
        if evidence.mapping.definition.content_hash == manifest.mapping_content_hash:
            # Defensive fallback for pipeline implementations that do not use the
            # current stage coordinator.
            current = self.bindings.get_for_completed_workspace(
                manifest.completed_workspace_id
            )
            if current is None:
                raise CorrectionOriginError("Correction binding disappeared")
            unchanged = self.bindings.invalidate_plan(
                manifest.completed_workspace_id,
                current_mapping_hash=evidence.mapping.definition.content_hash,
                current_prepared_hash=evidence.corrected_prepared_hash,
                expected_revision=current.optimistic_revision,
                actor=actor,
            )
            return (
                CorrectionReview(
                    target_hash=manifest.target_hash,
                    fields=(),
                    blockers=(),
                ),
                None,
                unchanged,
            )
        review = self.review_service.review(
            evidence.candidate_batches,
            target_index.entries,
            reader=evidence.reader,
            expected_target_hash=manifest.target_hash,
            expected_reader_scope_hash=evidence.reader_scope_hash,
        )
        if review.blockers or not review.ready_fields:
            current = self.bindings.get_for_completed_workspace(
                manifest.completed_workspace_id
            )
            if current is None:
                raise CorrectionOriginError("Correction binding disappeared")
            invalidated = self.bindings.invalidate_plan(
                manifest.completed_workspace_id,
                current_mapping_hash=evidence.mapping.definition.content_hash,
                current_prepared_hash=evidence.corrected_prepared_hash,
                expected_revision=current.optimistic_revision,
                actor=actor,
            )
            return review, None, invalidated
        plan = self.plan_service.create_plan(
            review,
            plan_id=_child_id(
                review_request_id,
                f"correction-plan:{binding.correction_binding_id}",
            ),
            project_id=manifest.project_id,
            completed_migration_run_id=manifest.completed_migration_run_id,
            successor_migration_run_id=binding.successor_migration_run_id,
            workspace_id=binding.successor_workspace_id,
            origin_evidence_hash=manifest.manifest_hash,
            previous_prepared_hash=evidence.previous_prepared_hash,
            corrected_prepared_hash=evidence.corrected_prepared_hash,
            read_credential_binding_hash=evidence.read_credential_binding_hash,
            read_identity=evidence.read_identity,
            created_by=actor.identity,
            created_at=evidence.reviewed_at,
        )
        stored = self.protected.put_plan(plan)
        reference = ProtectedCorrectionArtifactReference(
            artifact_id=stored.plan_id,
            logical_hash=stored.plan_hash,
            storage_key=stored.storage_key,
            artifact_hash=stored.artifact_hash,
        )
        current = self.bindings.get_for_completed_workspace(
            manifest.completed_workspace_id
        )
        if current is None:
            raise CorrectionOriginError("Correction binding disappeared")
        published = self.bindings.publish_plan(
            manifest.completed_workspace_id,
            successor_workspace_id=binding.successor_workspace_id,
            mapping_hash=evidence.mapping.definition.content_hash,
            prepared_hash=evidence.corrected_prepared_hash,
            plan=reference,
            expected_revision=current.optimistic_revision,
            actor=actor,
        )
        return review, plan, published


def _prepared(snapshot: PreparedSnapshot) -> CorrectionPreparedArtifact:
    return CorrectionPreparedArtifact(
        dataset_id=snapshot.dataset_id,
        dataset_name=snapshot.dataset_name,
        source_snapshot_hash=snapshot.source_snapshot_hash,
        logical_hash=snapshot.logical_hash,
        content_hash=snapshot.content_hash,
        parquet_storage_key=snapshot.parquet_storage_key,
        parquet_sha256=snapshot.parquet_sha256,
        row_count=snapshot.row_count,
    )


def _execution_evidence_hash(execution: ExecutionRun) -> str:
    """Hash the journal once without adding hashes to individual attempts."""

    return content_hash(
        {
            "batch_rows": execution.batch_rows,
            "completed_at": (
                execution.completed_at.isoformat()
                if execution.completed_at is not None
                else None
            ),
            "preflight_run_id": execution.preflight_run_id,
            "rows": [
                {
                    **asdict(row),
                    "status": row.status.value,
                }
                for row in execution.rows
            ],
            "run_id": execution.run_id,
            "snapshot_hash": execution.snapshot_hash,
            "snapshot_root_hash": execution.snapshot_root_hash,
            "started_at": execution.started_at.isoformat(),
            "status": execution.status.value,
            "target_hash": execution.target_hash,
            "workspace_id": execution.workspace_id,
            "write_context_hash": execution.write_context_hash,
            "write_credential_binding_hash": execution.write_credential_binding_hash,
            "write_permission_hash": execution.write_permission_hash,
            "write_principal_hash": execution.write_principal_hash,
        }
    )


def _child_id(parent_id: str, name: str) -> str:
    return str(uuid5(UUID(require_uuid(parent_id, "parent_id")), name))


__all__ = [
    "CorrectionBinding",
    "CorrectionBindingRepository",
    "CorrectionAuthoringReviewResult",
    "CorrectionAuthoringStageCoordinator",
    "CorrectionDatasetReviewInput",
    "CorrectionOriginPublication",
    "CorrectionOriginPublisher",
    "CorrectionOriginRequest",
    "CorrectionMappingSeedService",
    "CorrectionNoChangedIntent",
    "CorrectionReviewEvidence",
    "CorrectionReviewOrchestrator",
    "CorrectionReviewPipeline",
    "CorrectionSuccessor",
    "CorrectionSuccessorService",
    "CorrectionTargetReviewEvidence",
]
