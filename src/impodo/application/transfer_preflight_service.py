"""Compare a fresh destination read with an exact approved Stage 7 package."""

from __future__ import annotations

from datetime import UTC, datetime

from impodo.domain.shared.access import ActorIdentity
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_preflight import (
    TransferPreflightDataset,
    TransferPreflightRelationship,
    TransferPreflightReport,
)
from impodo.domain.workspace.transfer_review import (
    TransferReviewApproval,
    TransferReviewPackage,
)
from impodo.domain.workspace.workbench import WorkspaceState


class TransferPreflightService:
    """Build aggregate 8A evidence without exposing or changing Odoo data."""

    def build(
        self,
        workspace: WorkspaceState,
        package: TransferReviewPackage,
        approval: TransferReviewApproval,
        approved_match: DestinationMatchPlan,
        fresh_match: DestinationMatchPlan,
        *,
        recorded_by: ActorIdentity,
    ) -> TransferPreflightReport:
        if (
            workspace.transfer_review_package != package
            or workspace.transfer_review_approval != approval
            or workspace.destination_match_plan != approved_match
            or not workspace.transfer_review_approved(
                source_selection_hash=package.source_selection_hash,
                source_schema_hash=package.source_schema_hash,
            )
        ):
            raise WorkspaceError("Approve the current transfer package first")
        if fresh_match.workspace_id != workspace.workspace_id:
            raise WorkspaceError("The destination preflight changed workspace identity")
        if (
            fresh_match.source_selection_hash != package.source_selection_hash
            or fresh_match.source_schema_hash != package.source_schema_hash
            or fresh_match.destination_target_hash != package.destination_target_hash
            or fresh_match.destination_credential_binding_hash
            != approved_match.destination_credential_binding_hash
            or fresh_match.destination_read_principal_hash
            != approved_match.destination_read_principal_hash
        ):
            raise WorkspaceError(
                "The destination access changed; verify it and rebuild the transfer plan"
            )

        global_blockers: set[str] = set()
        if (
            fresh_match.destination_read_permission_hash
            != approved_match.destination_read_permission_hash
        ):
            global_blockers.add("DESTINATION_PERMISSION_DRIFT")
        if (
            fresh_match.destination_read_context_hash
            != approved_match.destination_read_context_hash
        ):
            global_blockers.add("DESTINATION_CONTEXT_DRIFT")

        approved_models = {item.dataset_id: item for item in package.datasets}
        prior_matches = {item.dataset_id: item for item in approved_match.model_matches}
        fresh_matches = {item.dataset_id: item for item in fresh_match.model_matches}
        if set(approved_models) != set(fresh_matches):
            global_blockers.add("DESTINATION_MODEL_SCOPE_DRIFT")

        datasets: list[TransferPreflightDataset] = []
        for item in package.datasets:
            prior = prior_matches.get(item.dataset_id)
            fresh = fresh_matches.get(item.dataset_id)
            blockers: set[str] = set()
            approved_fields = tuple(
                sorted(item.scalar_write_fields + item.relationship_write_fields)
            )
            if prior is None or fresh is None:
                blockers.add("DESTINATION_MODEL_SCOPE_DRIFT")
            else:
                blockers.update(fresh.blocking_reasons)
                if (
                    fresh.model != item.model
                    or fresh.key_field != item.key_field
                    or fresh.source_column_key != prior.source_column_key
                    or fresh.source_row_count != item.source_row_count
                    or fresh.source_distinct_key_count != prior.source_distinct_key_count
                ):
                    blockers.add("DESTINATION_MATCH_KEY_DRIFT")
                if (
                    fresh.destination_existing_key_count
                    != item.destination_existing_record_count
                    or fresh.destination_create_key_count
                    != item.destination_create_record_count
                ):
                    blockers.add("DESTINATION_RECORD_CLASSIFICATION_DRIFT")
                if (
                    fresh.destination_key_binding_hash
                    != prior.destination_key_binding_hash
                ):
                    blockers.add("DESTINATION_RECORD_IDENTITY_DRIFT")
                if fresh.compatible_fields != approved_fields:
                    blockers.add("DESTINATION_FIELD_SCOPE_DRIFT")
            datasets.append(
                TransferPreflightDataset(
                    dataset_id=item.dataset_id,
                    dataset_name=item.dataset_name,
                    model=item.model,
                    model_label=item.model_label,
                    key_field=item.key_field,
                    source_row_count=item.source_row_count,
                    approved_existing_record_count=(
                        item.destination_existing_record_count
                    ),
                    approved_create_record_count=item.destination_create_record_count,
                    observed_existing_record_count=(
                        fresh.destination_existing_key_count if fresh else 0
                    ),
                    observed_create_record_count=(
                        fresh.destination_create_key_count if fresh else 0
                    ),
                    approved_key_binding_hash=(
                        prior.destination_key_binding_hash
                        if prior is not None
                        else approved_match.destination_record_snapshot_hash
                    ),
                    observed_key_binding_hash=(
                        fresh.destination_key_binding_hash
                        if fresh is not None
                        else fresh_match.destination_record_snapshot_hash
                    ),
                    approved_write_fields=approved_fields,
                    observed_compatible_fields=(
                        fresh.compatible_fields if fresh else ()
                    ),
                    blocker_codes=tuple(sorted(blockers)),
                )
            )

        fresh_relations = {
            (item.dataset_id, item.field_name): item
            for item in fresh_match.relationship_matches
        }
        approved_relation_keys = {
            (item.owner_dataset_id, item.field_name) for item in package.relationships
        }
        if set(fresh_relations) != approved_relation_keys:
            global_blockers.add("DESTINATION_RELATIONSHIP_SCOPE_DRIFT")
        relationships: list[TransferPreflightRelationship] = []
        for item in package.relationships:
            fresh = fresh_relations.get((item.owner_dataset_id, item.field_name))
            blockers: set[str] = set()
            if fresh is None:
                blockers.add("DESTINATION_RELATIONSHIP_SCOPE_DRIFT")
            else:
                blockers.update(fresh.blocking_reasons)
                if (
                    fresh.model != item.owner_model
                    or fresh.related_dataset_id != item.related_dataset_id
                    or fresh.related_model != item.related_model
                    or fresh.kind != item.kind
                    or fresh.operation != item.operation
                    or fresh.inverse_field != item.inverse_field
                    or fresh.required != item.required
                ):
                    blockers.add("DESTINATION_RELATIONSHIP_SCOPE_DRIFT")
                if (
                    fresh.source_link_count != item.source_link_count
                    or fresh.destination_reused_link_count
                    != item.destination_reused_link_count
                    or fresh.incoming_link_count != item.incoming_link_count
                ):
                    blockers.add("DESTINATION_RELATIONSHIP_RESOLUTION_DRIFT")
            relationships.append(
                TransferPreflightRelationship(
                    owner_dataset_id=item.owner_dataset_id,
                    owner_model=item.owner_model,
                    field_name=item.field_name,
                    related_dataset_id=item.related_dataset_id,
                    related_model=item.related_model,
                    kind=item.kind,
                    operation=item.operation,
                    phase=item.phase,
                    approved_link_count=item.source_link_count,
                    approved_reused_link_count=(
                        item.destination_reused_link_count
                    ),
                    approved_incoming_link_count=item.incoming_link_count,
                    observed_link_count=fresh.source_link_count if fresh else 0,
                    observed_reused_link_count=(
                        fresh.destination_reused_link_count if fresh else 0
                    ),
                    observed_incoming_link_count=(
                        fresh.incoming_link_count if fresh else 0
                    ),
                    blocker_codes=tuple(sorted(blockers)),
                )
            )

        return TransferPreflightReport(
            workspace_id=workspace.workspace_id,
            review_package_hash=package.content_hash,
            review_approval_hash=approval.content_hash,
            approved_match_plan_hash=approved_match.content_hash,
            fresh_match_plan_hash=fresh_match.content_hash,
            source_selection_hash=package.source_selection_hash,
            source_schema_hash=package.source_schema_hash,
            destination_target_hash=fresh_match.destination_target_hash,
            destination_credential_binding_hash=(
                fresh_match.destination_credential_binding_hash
            ),
            destination_read_principal_hash=(
                fresh_match.destination_read_principal_hash
            ),
            approved_permission_hash=approved_match.destination_read_permission_hash,
            observed_permission_hash=fresh_match.destination_read_permission_hash,
            approved_context_hash=approved_match.destination_read_context_hash,
            observed_context_hash=fresh_match.destination_read_context_hash,
            destination_schema_snapshot_hash=(
                fresh_match.destination_schema_snapshot_hash
            ),
            destination_record_snapshot_hash=(
                fresh_match.destination_record_snapshot_hash
            ),
            datasets=tuple(sorted(datasets, key=lambda current: current.model)),
            relationships=tuple(
                sorted(
                    relationships,
                    key=lambda current: (current.owner_model, current.field_name),
                )
            ),
            recorded_at=datetime.now(UTC),
            recorded_by=recorded_by,
            blocker_codes=tuple(sorted(global_blockers)),
        )
