"""Build a frozen Odoo transfer review without contacting Odoo."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.domain.cutover.approvals import FrozenExportPlan
from impodo.domain.odoo_restore import (
    OdooRestorePlan,
    RestoreCoverageKind,
    restore_policy_binding,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.workspace.destination_matching import (
    DESTINATION_CREATE_ONLY_HANDLINGS,
    DESTINATION_NO_WRITE_HANDLINGS,
    DestinationMatchPlan,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_order import TransferOrderPlan
from impodo.domain.workspace.transfer_review import (
    TRANSFER_MODEL_POLICIES,
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
        model_policies: Mapping[str, str] | None = None,
        restore_plans: Mapping[str, OdooRestorePlan] | None = None,
    ) -> TransferReviewPackage:
        try:
            restore_hash = restore_policy_binding(
                restore_plans, workspace.saved_restore_generation_id,
                required_models=(item.model for item in match_plan.model_matches if item.destination_handling not in DESTINATION_NO_WRITE_HANDLINGS),
            )
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        if (
            workspace.destination_match_plan != match_plan
            or workspace.transfer_order_plan != order_plan
            or not workspace.transfer_order_ready(
                source_selection_hash=match_plan.source_selection_hash,
                source_schema_hash=match_plan.source_schema_hash,
            )
        ):
            raise WorkspaceError("Complete the current transfer order first")
        policies = dict(model_policies or {})
        model_names = {item.model for item in match_plan.model_matches}
        if restore_plans is not None and set(restore_plans) - model_names:
            raise WorkspaceError("Restore field plans contain unselected record types")
        if set(policies) - model_names or any(
            value not in TRANSFER_MODEL_POLICIES for value in policies.values()
        ):
            raise WorkspaceError("Choose a supported policy for every selected record type")
        for item in match_plan.model_matches:
            default_policy = (
                "reuse_only"
                if item.destination_handling in DESTINATION_NO_WRITE_HANDLINGS
                else "create_if_missing"
                if item.destination_handling in DESTINATION_CREATE_ONLY_HANDLINGS
                else "upsert"
            )
            model_policy = policies.get(item.model, default_policy)
            if (
                item.destination_handling in DESTINATION_NO_WRITE_HANDLINGS
                and model_policy != "reuse_only"
            ):
                raise WorkspaceError(
                    f"{item.model_label} was matched as reuse only; return to "
                    "destination matching before allowing transfers for this record type"
                )
            if (
                item.destination_handling in DESTINATION_CREATE_ONLY_HANDLINGS
                and model_policy != "create_if_missing"
            ):
                raise WorkspaceError(
                    f"{item.model_label} was selected to keep linked values; "
                    "existing records must stay unchanged and only missing records "
                    "may be created"
                )
            if model_policy == "reuse_only" and item.destination_create_key_count:
                raise WorkspaceError(
                    f"{item.model_label} has {item.destination_create_key_count} missing "
                    "destination record(s); choose create if missing or upsert"
                )
            will_write = (
                item.destination_create_key_count > 0
                or (model_policy == "upsert" and item.destination_existing_key_count > 0)
            )
            if will_write and item.requires_workflow_handler:
                raise WorkspaceError(
                    f"{item.model_label} requires a qualified business workflow "
                    "handler before creating or updating records"
                )
            if will_write and item.write_blocking_reasons:
                raise WorkspaceError(
                    f"{item.model_label} has missing or incompatible destination "
                    "write fields, or unresolved required create fields. Review "
                    "the destination matching issues before creating or updating."
                )

        relationships_by_owner: dict[str, list[str]] = {
            item.dataset_id: [] for item in match_plan.model_matches
        }
        required_relationships_by_owner: dict[str, list[str]] = {
            item.dataset_id: [] for item in match_plan.model_matches
        }
        for relation in match_plan.relationship_matches:
            relationships_by_owner[relation.dataset_id].append(relation.field_name)
            if relation.required:
                required_relationships_by_owner[relation.dataset_id].append(
                    relation.field_name
                )
        for item in match_plan.model_matches:
            if (
                item.destination_handling in DESTINATION_CREATE_ONLY_HANDLINGS
                and item.destination_create_key_count
            ):
                if not set(item.minimum_create_relationship_fields).issubset(
                    relationships_by_owner[item.dataset_id]
                ):
                    raise WorkspaceError(
                        f"{item.model_label} is missing relationship evidence needed "
                        "for minimum creation"
                    )
                if not set(
                    required_relationships_by_owner[item.dataset_id]
                ).issubset(item.minimum_create_relationship_fields):
                    raise WorkspaceError(
                        f"{item.model_label} has a required relationship outside "
                        "its reviewed minimum-create scope"
                    )
        scalar_write_fields_by_dataset: dict[str, tuple[str, ...]] = {}
        relationship_write_fields_by_dataset: dict[str, tuple[str, ...]] = {}
        for item in match_plan.model_matches:
            if item.destination_handling in DESTINATION_CREATE_ONLY_HANDLINGS:
                if not item.destination_create_key_count:
                    scalar_write_fields_by_dataset[item.dataset_id] = ()
                    relationship_write_fields_by_dataset[item.dataset_id] = ()
                    continue
                scalar_write_fields_by_dataset[item.dataset_id] = tuple(
                    sorted(
                        (
                            set(item.minimum_create_fields)
                            & set(item.compatible_fields)
                        )
                        - set(relationships_by_owner[item.dataset_id])
                    )
                )
                relationship_write_fields_by_dataset[item.dataset_id] = tuple(
                    sorted(
                        set(item.minimum_create_relationship_fields)
                        & set(item.compatible_fields)
                    )
                )
                continue
            scalar_write_fields_by_dataset[item.dataset_id] = tuple(
                sorted(
                    set(item.compatible_fields)
                    - set(relationships_by_owner[item.dataset_id])
                )
            )
            relationship_write_fields_by_dataset[item.dataset_id] = tuple(
                sorted(relationships_by_owner[item.dataset_id])
            )
        order_by_dataset = {item.dataset_id: item for item in order_plan.datasets}
        identity_aliases = {item.dataset_id: item.binding for item in match_plan.identity_alias_bindings}
        datasets = tuple(
            TransferReviewDataset(
                dataset_id=item.dataset_id,
                dataset_name=item.dataset_name,
                model=item.model,
                model_label=item.model_label,
                key_field=item.key_field,
                key_field_label=item.key_field_label,
                key_fields=item.key_fields,
                scope_fields=item.scope_fields,
                scope_dataset_ids=item.scope_dataset_ids,
                identity_alias_binding_hash=identity_aliases[item.dataset_id].content_hash if item.dataset_id in identity_aliases else "",
                identity_alias_count=identity_aliases[item.dataset_id].alias_count if item.dataset_id in identity_aliases else 0,
                source_row_count=item.source_row_count,
                destination_existing_record_count=(
                    item.destination_existing_key_count
                ),
                destination_create_record_count=item.destination_create_key_count,
                wave=_wave(order_by_dataset, item.dataset_id),
                scalar_write_fields=scalar_write_fields_by_dataset[item.dataset_id],
                relationship_write_fields=(
                    relationship_write_fields_by_dataset[item.dataset_id]
                ),
                model_policy=policies.get(
                    item.model,
                    (
                        "reuse_only"
                        if item.destination_handling
                        in DESTINATION_NO_WRITE_HANDLINGS
                        else "create_if_missing"
                        if item.destination_handling
                        in DESTINATION_CREATE_ONLY_HANDLINGS
                        else "upsert"
                    ),
                ),
                create_field_providers=(
                    tuple(
                        sorted(
                            (
                                (decision.field_name, decision.provider_kind)
                                for decision in match_plan.create_field_decisions
                                if decision.dataset_id == item.dataset_id
                                and decision.reviewed
                            ),
                            key=lambda choice: choice[0],
                        )
                    )
                    if item.destination_create_key_count
                    else ()
                ),
                excluded_source_fields=item.excluded_fields,
                excluded_source_record_count=len(
                    item.omitted_source_row_numbers
                ),
            )
            for item in match_plan.model_matches
        )
        if restore_plans is not None:
            mapped = []
            for item in datasets:
                plan = restore_plans.get(item.model)
                fields = {field.source.name: field.destination.name for field in plan.fields} if plan is not None else {}
                scalars = tuple(sorted(fields.get(name, name) for name in item.scalar_write_fields))
                relations = tuple(sorted(fields.get(name, name) for name in item.relationship_write_fields))
                excluded = set(item.excluded_source_fields)
                if plan is not None:
                    excluded.update(coverage.source_field for coverage in plan.coverage if coverage.kind is RestoreCoverageKind.EXCLUDED and coverage.source_field is not None)
                mapped.append(replace(
                    item, scalar_write_fields=scalars, relationship_write_fields=relations,
                    excluded_source_fields=tuple(sorted(excluded - set(scalars) - set(relations))),
                ))
            datasets = tuple(mapped)

        dependency_by_field = {
            (item.owner_dataset_id, item.field_name): item
            for item in order_plan.dependencies
        }
        authorized_relationships = {
            item.dataset_id: frozenset(item.relationship_write_fields)
            for item in datasets
        }
        relationships: list[TransferReviewRelationship] = []
        for item in match_plan.relationship_matches:
            plan = (restore_plans or {}).get(item.model)
            restored = next((field for field in plan.fields if field.source.name == item.field_name), None) if plan is not None else None
            target_field = restored.destination.name if restored is not None else item.field_name
            if target_field not in authorized_relationships[item.dataset_id]:
                continue
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
                    field_name=target_field,
                    field_label=restored.destination.label if restored is not None else item.field_label,
                    related_key_field=item.related_key_field,
                    related_key_fields=item.related_key_fields,
                    kind=item.kind,
                    operation=item.operation,
                    inverse_field=item.inverse_field,
                    required=restored.destination.required if restored is not None else item.required,
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
                **({"restore_policy": restore_hash} if restore_hash is not None else {}),
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
