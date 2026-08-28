"""Compile browser mapping definitions into shared runtime semantics."""

from __future__ import annotations
from typing import Collection, Mapping

from ..mapping.contracts import (
    MappingDefinition,
    RelationshipResolver,
    ResolverOrigin,
    ScalarValueSource,
)
from impodo.domain.recipe.profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    NormalizationSpec,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.workspace.contracts import SourceSelection
from ..errors import ReadinessError
from ..staging.fields import synthetic_field, synthetic_relationship_field
from .contracts import CompiledMigrationPlan

def compile_browser_mapping(
    definition: MappingDefinition,
    selection: SourceSelection,
    *,
    derived_plan_hash: str | None = None,
    required_relationship_fields: Mapping[str, Collection[str]] | None = None,
) -> CompiledMigrationPlan:
    """Compile browser authoring semantics into the shared runtime contract.

    Captured Odoo-required relationship fields are normalized into the same
    ``required_on_create`` meaning used by authored profiles.
    """

    datasets = {item.dataset_id: item for item in selection.datasets}
    mappings = {item.dataset_id: item for item in definition.datasets}
    captured_required = required_relationship_fields or {}

    def resolver(value: RelationshipResolver) -> ResolveSpec:
        if value.origin is ResolverOrigin.DATASET:
            target_mapping = mappings.get(str(value.dataset_id))
            target_dataset = datasets.get(str(value.dataset_id))
            if target_mapping is None or target_dataset is None:
                raise ReadinessError("A mapped relationship dataset is missing")
            return ResolveSpec(
                dataset=target_dataset.name,
                target_source_fields=target_mapping.source_identity_column_keys,
            )
        if value.origin is ResolverOrigin.TARGET_THEN_DATASET:
            target_mapping = mappings.get(str(value.dataset_id))
            target_dataset = datasets.get(str(value.dataset_id))
            if target_mapping is None or target_dataset is None:
                raise ReadinessError("A mapped relationship dataset is missing")
            return ResolveSpec(
                dataset=target_dataset.name,
                target_source_fields=target_mapping.source_identity_column_keys,
                target_model=value.model,
                target_fields=tuple(
                    item.target_field for item in value.key_mappings
                ),
                target_scope_fields=tuple(
                    item.target_field for item in value.scope_mappings
                ),
                target_value_mappings=(
                    tuple(
                        (item.source_value, item.target_value)
                        for item in value.value_mappings
                    )
                    or None
                ),
            )
        return ResolveSpec(
            target_model=value.model,
            target_fields=tuple(item.target_field for item in value.key_mappings),
            target_scope_fields=tuple(
                item.target_field for item in value.scope_mappings
            ),
        )

    profile_datasets: list[DatasetSpec] = []
    for mapping in definition.datasets:
        source_dataset = datasets[mapping.dataset_id]
        scalar_fields = {}
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            scalar_fields[field.target_field] = FieldSpec(
                source=synthetic_field(index),
                type=field.value_type,
                required=field.required,
                required_on_create=field.required_on_create,
                compare=field.compare,
                validate_only=field.validate_only,
                normalize=NormalizationSpec(empty_as_null=True),
                null_policy=field.null_policy,
            )
        relations = {
            item.target_field: RelationSpec(
                kind=item.kind,
                source_fields=tuple(
                    synthetic_relationship_field(index, source_index)
                    for source_index, _source_column_key in enumerate(
                        item.source_column_keys
                    )
                ),
                resolve=resolver(item.resolver),
                compare=item.compare,
                validate_only=item.validate_only,
                required=item.required,
                required_on_create=(
                    item.required_on_create
                    or item.target_field
                    in captured_required.get(mapping.target_model, ())
                ),
                on_missing=item.on_missing,
                on_ambiguous=item.on_ambiguous,
                operation=item.operation,
                separator=item.separator,
                null_policy=item.null_policy,
            )
            for index, item in enumerate(mapping.relationships)
        }
        identity_normalization = NormalizationSpec(
            trim=True,
            collapse_whitespace=True,
            empty_as_null=True,
        )
        profile_datasets.append(
            DatasetSpec(
                name=source_dataset.name,
                source=SourceSpec(file=f"{source_dataset.name}.csv"),
                target=TargetSpec(
                    model=mapping.target_model,
                    mode=mapping.mode.value,
                    on_existing=mapping.on_existing,
                ),
                source_identity=SourceIdentitySpec(
                    fields=mapping.source_identity_column_keys
                ),
                target_identity=TargetIdentitySpec(
                    components=tuple(
                        IdentityComponent(
                            source_fields=item.source_column_keys,
                            target_fields=item.target_fields,
                            type=item.value_type,
                            normalize=identity_normalization,
                            resolve=(
                                resolver(item.resolver)
                                if item.resolver is not None
                                else None
                            ),
                        )
                        for item in mapping.target_identity
                    ),
                    scope=tuple(
                        IdentityComponent(
                            source_fields=item.source_column_keys,
                            target_fields=item.target_fields,
                            type=item.value_type,
                            normalize=identity_normalization,
                            resolve=(
                                resolver(item.resolver)
                                if item.resolver is not None
                                else None
                            ),
                        )
                        for item in mapping.target_scope
                    ),
                ),
                fields=scalar_fields,
                relations=relations,
            )
        )
    token = definition.content_hash.removeprefix("sha256:")[:24]
    return CompiledMigrationPlan(
        plan_id=f"browser_{token}",
        origin="browser_mapping",
        origin_hash=definition.content_hash,
        source_selection_hash=selection.content_hash,
        schema_hash=definition.schema_hash,
        derived_plan_hash=derived_plan_hash,
        datasets=tuple(profile_datasets),
    )


def browser_mapping_labels(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Compile display labels without loading or evaluating source rows."""

    datasets = {item.dataset_id: item for item in selection.datasets}
    dataset_labels = {
        item.name: item.name.replace("_", " ").title()
        for item in selection.datasets
    }
    field_labels: dict[tuple[str, str], str] = {}
    for mapping in definition.datasets:
        dataset = datasets.get(mapping.dataset_id)
        if dataset is None:
            raise ReadinessError("A submitted mapping dataset is missing")
        names = {column.stable_key: column.source_name for column in dataset.columns}
        for column in dataset.columns:
            field_labels[(dataset.name, column.stable_key)] = column.source_name
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            rule_source_key = next(
                (
                    condition.source_column_key
                    for rule in (
                        field.selection_rules.rules
                        if field.selection_rules is not None
                        else ()
                    )
                    for condition in rule.conditions
                ),
                "",
            )
            field_labels[(dataset.name, synthetic_field(index))] = (
                names.get(field.source_column_key or rule_source_key)
                or field.target_field
            )
        for relationship_index, relationship in enumerate(mapping.relationships):
            for source_index, source_column_key in enumerate(
                relationship.source_column_keys
            ):
                field_labels[
                    (
                        dataset.name,
                        synthetic_relationship_field(
                            relationship_index,
                            source_index,
                        ),
                    )
                ] = names.get(source_column_key) or relationship.target_field
    return dataset_labels, field_labels
