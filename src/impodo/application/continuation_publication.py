"""Seal a verified continuation as the immutable origin of the next cycle.

The next origin keeps the complete protected record association from its
parent, including records unchanged by this cycle. Its new mapping and native
prepared evidence belong to the completed successor. Publication never searches
Odoo or infers a target from business values.
"""

from dataclasses import asdict, replace
from uuid import UUID, uuid5

from impodo.domain.correction import CorrectionPlan
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
    CorrectionPreparedArtifact,
    CorrectionTargetIndex,
    ProtectedCorrectionArtifactReference,
)
from impodo.domain.execution.models import ExecutionRun, ExecutionRunStatus
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.reconciliation import ReconciliationRun, ReconciliationRunStatus
from impodo.domain.run.models import MigrationRun
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.models import MigrationWorkspace

from .correction_orchestration import CorrectionBinding, CorrectionOriginPublisher


def publish_continued_load(
    publisher: CorrectionOriginPublisher,
    parent: CorrectionBinding,
    parent_origin: CorrectionOriginManifest,
    parent_index: CorrectionTargetIndex,
    *,
    run: MigrationRun,
    workspace: MigrationWorkspace,
    mapping: MappingRevision,
    prepared: tuple[PreparedSnapshot, ...],
    execution: ExecutionRun,
    reconciliation: ReconciliationRun,
    plan: CorrectionPlan,
    actor: Actor,
) -> CorrectionBinding:
    """Bind retained native intent to an exact verified continuation receipt."""

    ordered = tuple(sorted(prepared, key=lambda item: item.dataset_id))
    if (
        execution.status is not ExecutionRunStatus.COMPLETED
        or reconciliation.status is not ReconciliationRunStatus.VERIFIED
        or execution.workspace_id != workspace.workspace_id
        or reconciliation.workspace_id != workspace.workspace_id
        or reconciliation.execution_run_id != execution.run_id
        or reconciliation.snapshot_hash != execution.snapshot_hash
        or parent.successor_workspace_id != workspace.workspace_id
        or parent.successor_migration_run_id != run.migration_run_id
        or workspace.migration_run_id != run.migration_run_id
        or run.project_id != parent.project_id
        or run.data_version_id != parent.data_version_id
        or plan.workspace_id != workspace.workspace_id
        or plan.origin_evidence_hash != parent_origin.manifest_hash
        or plan.target_hash != parent_origin.target_hash
        or execution.target_hash != plan.target_hash
        or reconciliation.target_hash != plan.target_hash
        or parent.current_plan is None
        or parent.current_plan.logical_hash != plan.plan_hash
        or mapping.definition.content_hash != parent.current_mapping_hash
        or content_hash([item.to_portable_dict() for item in ordered])
        != plan.corrected_prepared_hash
        or parent_index.index_hash != parent_origin.target_index.logical_hash
        or not ordered
        or {item.dataset_id for item in ordered}
        != {item.dataset_id for item in parent_origin.prepared_artifacts}
        or any(
            item.workspace_id != workspace.workspace_id
            or item.mapping_hash != mapping.definition.content_hash
            for item in ordered
        )
    ):
        raise CorrectionOriginError(
            "The verified continuation and its saved rules no longer match."
        )
    created_at = execution.completed_at
    if created_at is None:
        raise CorrectionOriginError("The verified continuation has no completion time.")

    def identifier(kind: str) -> str:
        return str(uuid5(UUID(run.migration_run_id), f"continuation:{kind}"))

    index = CorrectionTargetIndex.create(
        index_id=identifier("targets"),
        project_id=parent.project_id,
        completed_migration_run_id=run.migration_run_id,
        completed_workspace_id=workspace.workspace_id,
        entries=parent_index.entries,
        created_at=created_at,
    )
    stored_index = publisher.protected.put_target_index(index)
    index_reference = ProtectedCorrectionArtifactReference(
        stored_index.index_id,
        stored_index.index_hash,
        stored_index.storage_key,
        stored_index.artifact_hash,
    )
    manifest = CorrectionOriginManifest.create(
        manifest_id=identifier("origin"),
        project_id=parent.project_id,
        data_version_id=parent.data_version_id,
        completed_migration_run_id=run.migration_run_id,
        completed_workspace_id=workspace.workspace_id,
        mapping_id=mapping.mapping_id,
        mapping_version=mapping.version,
        mapping_content_hash=mapping.definition.content_hash,
        prepared_artifacts=tuple(
            CorrectionPreparedArtifact(
                dataset_id=item.dataset_id,
                dataset_name=item.dataset_name,
                source_snapshot_hash=item.source_snapshot_hash,
                logical_hash=item.logical_hash,
                content_hash=item.content_hash,
                parquet_storage_key=item.parquet_storage_key,
                parquet_sha256=item.parquet_sha256,
                row_count=item.row_count,
            )
            for item in ordered
        ),
        execution_snapshot_hash=execution.snapshot_hash,
        execution_snapshot_root_hash=execution.snapshot_root_hash,
        preflight_run_id=execution.preflight_run_id,
        execution_run_id=execution.run_id,
        execution_evidence_hash=content_hash(asdict(execution)),
        reconciliation_id=reconciliation.reconciliation_id,
        reconciliation_hash=reconciliation.semantic_hash,
        target_hash=plan.target_hash,
        schema_hash=ordered[0].schema_hash,
        read_context_hash=plan.read_context_hash,
        target_observed_at=plan.read_observed_at,
        target_index=index_reference,
        created_by=actor.identity,
        created_at=created_at,
    )
    stored_origin = publisher.protected.put_origin(manifest)
    binding = replace(
        parent,
        correction_binding_id=identifier("binding"),
        completed_migration_run_id=run.migration_run_id,
        completed_workspace_id=workspace.workspace_id,
        origin=ProtectedCorrectionArtifactReference(
            stored_origin.manifest_id,
            stored_origin.manifest_hash,
            stored_origin.storage_key,
            stored_origin.artifact_hash,
        ),
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
    return publisher.bindings.seal_completed_origin(
        binding,
        expected_run_revision=run.optimistic_revision,
        expected_workspace_revision=workspace.optimistic_revision,
        actor=actor,
    )
