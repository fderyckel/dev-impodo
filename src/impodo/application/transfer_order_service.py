"""Build deterministic Stage 6 dataset ordering without contacting Odoo."""

from __future__ import annotations

from datetime import UTC, datetime

from impodo.domain.execution.dependency_scheduler import (
    DependencyEdge,
    DependencyNode,
    schedule_dependencies,
)
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    DestinationRelationshipMatch,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_order import (
    TransferOrderBlocker,
    TransferOrderDataset,
    TransferOrderDependency,
    TransferOrderPlan,
    TransferOrderWave,
)
from impodo.domain.workspace.workbench import WorkspaceState


class TransferOrderService:
    """Project ready relationship evidence through the canonical scheduler."""

    def build(
        self,
        workspace: WorkspaceState,
        match_plan: DestinationMatchPlan,
        *,
        recorded_by: str,
    ) -> TransferOrderPlan:
        if not workspace.destination_verified:
            raise WorkspaceError("Verify the destination Odoo connection first")
        if (
            workspace.destination_match_plan != match_plan
            or not workspace.destination_match_ready(
                source_selection_hash=match_plan.source_selection_hash,
                source_schema_hash=match_plan.source_schema_hash,
            )
        ):
            raise WorkspaceError("Complete current destination matching first")

        models = tuple(sorted(match_plan.model_matches, key=lambda item: item.model))
        nodes = tuple(
            DependencyNode(row_id=item.dataset_id, rank=index)
            for index, item in enumerate(models)
        )
        relation_by_edge: dict[DependencyEdge, DestinationRelationshipMatch] = {}
        edges: list[DependencyEdge] = []
        for relation in match_plan.relationship_matches:
            if relation.incoming_link_count <= 0:
                continue
            edge = DependencyEdge(
                dependency_row_id=relation.related_dataset_id,
                owner_row_id=relation.dataset_id,
                owner_field=relation.field_name,
                strength="hard" if relation.required else "deferrable",
            )
            edges.append(edge)
            relation_by_edge[edge] = relation

        schedule = schedule_dependencies(nodes, edges)
        deferred = set(schedule.deferred_edges)
        waves = tuple(
            TransferOrderWave(
                sequence=index,
                dataset_ids=tuple(sorted(dataset_ids)),
            )
            for index, dataset_ids in enumerate(schedule.components, start=1)
        )
        wave_by_dataset = {
            dataset_id: wave.sequence
            for wave in waves
            for dataset_id in wave.dataset_ids
        }
        datasets = tuple(
            TransferOrderDataset(
                dataset_id=item.dataset_id,
                dataset_name=item.dataset_name,
                model=item.model,
                model_label=item.model_label,
                source_row_count=item.source_row_count,
                destination_existing_key_count=item.destination_existing_key_count,
                destination_create_key_count=item.destination_create_key_count,
                wave=wave_by_dataset.get(item.dataset_id),
            )
            for item in models
        )
        dependencies = tuple(
            sorted(
                (
                    TransferOrderDependency(
                        owner_dataset_id=relation.dataset_id,
                        dependency_dataset_id=relation.related_dataset_id,
                        owner_model=relation.model,
                        dependency_model=relation.related_model,
                        field_name=relation.field_name,
                        field_label=relation.field_label,
                        kind=relation.kind,
                        strength=edge.strength,
                        incoming_link_count=relation.incoming_link_count,
                        deferred=edge in deferred,
                    )
                    for edge, relation in relation_by_edge.items()
                ),
                key=lambda item: (
                    item.owner_model,
                    item.field_name,
                    item.dependency_model,
                ),
            )
        )
        blockers = tuple(
            sorted(
                (
                    TransferOrderBlocker(
                        dataset_id=item.row_id,
                        code=item.code,
                        field_name=item.field,
                        dependency_dataset_id=item.dependency_row_id,
                    )
                    for item in schedule.blockers
                ),
                key=lambda item: (
                    item.dataset_id,
                    item.code,
                    item.field_name,
                    item.dependency_dataset_id,
                ),
            )
        )
        return TransferOrderPlan(
            workspace_id=workspace.workspace_id,
            destination_match_plan_hash=match_plan.content_hash,
            source_selection_hash=match_plan.source_selection_hash,
            source_schema_hash=match_plan.source_schema_hash,
            destination_target_hash=match_plan.destination_target_hash,
            datasets=datasets,
            dependencies=dependencies,
            waves=waves,
            blockers=blockers,
            recorded_at=datetime.now(UTC),
            recorded_by=recorded_by,
        )
