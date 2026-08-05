"""Extracted browser mapping compiler domain behavior."""

from __future__ import annotations


from ...mapping_semantics import (
    MappingDefinition,
    RelationshipResolver,
    ResolverOrigin,
    ScalarValueSource,
)
from ...profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    NormalizationSpec,
    ProfileDocument,
    ProfileIdentity,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from ...workspace_contracts import SourceSelection
from ..errors import ReadinessError
from ..staging.fields import synthetic_field




def _compile_profile(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> ProfileDocument:
    datasets = {item.dataset_id: item for item in selection.datasets}
    mappings = {item.dataset_id: item for item in definition.datasets}

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
                source_fields=item.source_column_keys,
                resolve=resolver(item.resolver),
                compare=item.compare,
                validate_only=item.validate_only,
                required=item.required,
                required_on_create=item.required_on_create,
                on_missing=item.on_missing,
                on_ambiguous=item.on_ambiguous,
                operation=item.operation,
                separator=item.separator,
                null_policy=item.null_policy,
            )
            for item in mapping.relationships
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
    return ProfileDocument(
        profile=ProfileIdentity(
            id=f"browser_{token}",
            description="Compiled from a submitted Impodo browser mapping",
        ),
        datasets=tuple(profile_datasets),
    )


compile_browser_mapping = _compile_profile
