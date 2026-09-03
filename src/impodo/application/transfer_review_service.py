"""Build a frozen Stage 7 Odoo transfer review without contacting Odoo."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from impodo.domain.cutover.approvals import FrozenExportPlan
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_order import TransferOrderPlan
from impodo.domain.workspace.transfer_review import (
    TRANSFER_REVIEW_POLICY_VERSION,
    TransferReviewDataset,
    TransferReviewPackage,
    TransferReviewRelationship,
    transfer_review_actions_hash,
    transfer_review_totals,
)
from impodo.domain.workspace.workbench import WorkspaceState


class TransferReviewService:
    """Freeze exact generic write scope and reconciled control totals."""

    def build(
        self,
        workspace: WorkspaceState,
        match_plan: DestinationMatchPlan,
        order_plan: TransferOrderPlan,
        *,
        run_id: str,
        data_version_id: str,
        built_by: ActorIdentity,
    ) -> TransferReviewPackage:
        if (
            workspace.destination_match_plan != match_plan
            or workspace.transfer_order_plan != order_plan
            or not workspace.transfer_order_ready(
                source_selection_hash=match_plan.source_selection_hash,
                source_schema_hash=match_plan.source_schema_hash,
            )
        ):
            raise WorkspaceError("Complete the current transfer order first")

        relationships_by_owner: dict[str, list[str]] = {
            item.dataset_id: [] for item in match_plan.model_matches
        }
        for relation in match_plan.relationship_matches:
            relationships_by_owner[relation.dataset_id].append(relation.field_name)
        order_by_dataset = {item.dataset_id: item for item in order_plan.datasets}
        datasets = tuple(
            TransferReviewDataset(
                dataset_id=item.dataset_id,
                dataset_name=item.dataset_name,
                model=item.model,
                model_label=item.model_label,
                key_field=item.key_field,
                key_field_label=item.key_field_label,
                source_row_count=item.source_row_count,
                destination_existing_record_count=(
                    item.destination_existing_key_count
                ),
                destination_create_record_count=item.destination_create_key_count,
                wave=_wave(order_by_dataset, item.dataset_id),
                scalar_write_fields=tuple(
                    sorted(
                        set(item.compatible_fields)
                        - set(relationships_by_owner[item.dataset_id])
                    )
                ),
                relationship_write_fields=tuple(
                    sorted(relationships_by_owner[item.dataset_id])
                ),
            )
            for item in match_plan.model_matches
        )

        dependency_by_field = {
            (item.owner_dataset_id, item.field_name): item
            for item in order_plan.dependencies
        }
        relationships: list[TransferReviewRelationship] = []
        for item in match_plan.relationship_matches:
            dependency = dependency_by_field.get((item.dataset_id, item.field_name))
            if item.incoming_link_count > 0 and (
                dependency is None
                or dependency.dependency_dataset_id != item.related_dataset_id
                or dependency.incoming_link_count != item.incoming_link_count
                or dependency.kind != item.kind
            ):
                raise WorkspaceError(
                    "The transfer order does not cover current relationship evidence"
                )
            if item.incoming_link_count == 0 and dependency is not None:
                raise WorkspaceError(
                    "The transfer order contains an unnecessary relationship edge"
                )
            relationships.append(
                TransferReviewRelationship(
                    owner_dataset_id=item.dataset_id,
                    related_dataset_id=item.related_dataset_id,
                    owner_model=item.model,
                    owner_model_label=item.model_label,
                    related_model=item.related_model,
                    related_model_label=item.related_model_label,
                    field_name=item.field_name,
                    field_label=item.field_label,
                    related_key_field=item.related_key_field,
                    kind=item.kind,
                    operation=item.operation,
                    inverse_field=item.inverse_field,
                    required=item.required,
                    source_link_count=item.source_link_count,
                    destination_reused_link_count=(
                        item.destination_reused_link_count
                    ),
                    incoming_link_count=item.incoming_link_count,
                    phase=(
                        "post_create"
                        if dependency is not None and dependency.deferred
                        else "create_or_update"
                    ),
                )
            )
        relationship_tuple = tuple(
            sorted(relationships, key=lambda item: (item.owner_model, item.field_name))
        )
        totals = transfer_review_totals(datasets, relationship_tuple)
        frozen_at = datetime.now(UTC)
        actions_hash = transfer_review_actions_hash(
            datasets,
            relationship_tuple,
            totals,
        )
        export_plan = FrozenExportPlan(
            plan_id=str(uuid4()),
            workspace_id=workspace.workspace_id,
            run_id=run_id,
            source_hashes={
                "source_selection": match_plan.source_selection_hash,
                "source_schema": match_plan.source_schema_hash,
                "destination_matching": match_plan.content_hash,
                "transfer_order": order_plan.content_hash,
                "destination_target": match_plan.destination_target_hash,
                "destination_permission": (
                    match_plan.destination_read_permission_hash
                ),
                "destination_context": match_plan.destination_read_context_hash,
                "data_version": content_hash(
                    {"data_version_id": data_version_id}
                ),
            },
            mapping_hash=match_plan.content_hash,
            ruleset_hash=content_hash(
                {"policy_version": TRANSFER_REVIEW_POLICY_VERSION}
            ),
            canonical_dataset_hash=match_plan.source_selection_hash,
            target_snapshot_hash=match_plan.destination_record_snapshot_hash,
            actions_hash=actions_hash,
            frozen_at=frozen_at,
        )
        return TransferReviewPackage(
            export_plan=export_plan,
            datasets=datasets,
            relationships=relationship_tuple,
            totals=totals,
            built_by=built_by,
        )


def _wave(order_by_dataset, dataset_id: str) -> int:
    item = order_by_dataset.get(dataset_id)
    if item is None or item.wave is None:
        raise WorkspaceError("The transfer order does not cover every selected dataset")
    return item.wave
