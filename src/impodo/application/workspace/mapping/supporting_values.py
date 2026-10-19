"""Plan target-only lookup evidence required by one mapping definition.

The planner follows resolver paths from the user-selected target models.  It
does not add related models to the workspace's transfer scope; it only records
the bounded source keys and captured parent fields needed to prove existing
destination references during Match data.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    RelationshipValueSource,
    ResolverOrigin,
    RowInclusionPolicy,
    ValueMapping,
    relationship_target_fields,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.domain.workspace.reference_keys import (
    reference_policy_hash,
    standard_reference_key,
)
from impodo.domain.workspace.supporting_lookups import (
    SupportingLookupSnapshot,
    supporting_lookup_key,
)


class MappingSupportingLookupReader(Protocol):
    """Read exact current lookup pointers without one connection per key."""

    def list_current(
        self,
        workspace_id: str,
        lookup_keys: Collection[str],
    ) -> tuple[SupportingLookupSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class MappingSupportingRelationship:
    """Retain the captured parent field that reaches a supporting model."""

    parent_model: str
    relationship_field: str
    relationship_type: str


@dataclass(frozen=True, slots=True)
class MappingSupportingSource:
    """Describe one frozen source projection that supplies lookup keys."""

    dataset_id: str
    source_column_keys: tuple[str, ...]
    row_inclusion: RowInclusionPolicy
    value_mappings: tuple[ValueMapping, ...] = ()


@dataclass(frozen=True, slots=True)
class MappingSupportingRequirement:
    """Group every use of one exact supporting-model identity."""

    model_name: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    relationships: tuple[MappingSupportingRelationship, ...]
    sources: tuple[MappingSupportingSource, ...]
    constant_values: tuple[tuple[str, ...], ...] = ()


def plan_mapping_supporting_values(
    datasets: tuple[DatasetMapping, ...],
    schema: OdooSchemaCatalog,
) -> tuple[MappingSupportingRequirement, ...]:
    """Derive external resolver reads without widening the primary schema."""

    schema_models = {model.name: model for model in schema.models}
    primary_models = set(schema_models)
    paths: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]],
        set[MappingSupportingRelationship],
    ] = {}
    sources: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]],
        set[MappingSupportingSource],
    ] = {}
    constants: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]],
        set[tuple[str, ...]],
    ] = {}

    def add(
        dataset: DatasetMapping,
        *,
        parent_field: str,
        relationship_type: str,
        relation_model: str | None,
        resolver,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
        source_column_keys: tuple[str, ...],
        constant_values: tuple[str, ...] | None = None,
    ) -> None:
        if (
            resolver is None
            or resolver.origin
            not in {ResolverOrigin.TARGET_CATALOG, ResolverOrigin.TARGET_THEN_DATASET}
            or not resolver.model
            or resolver.model in primary_models
            or resolver.model != relation_model
            or not key_fields
            or (
                len(source_column_keys) != len((*key_fields, *scope_fields))
                and constant_values is None
            )
        ):
            return
        identity = (resolver.model, key_fields, scope_fields)
        paths.setdefault(identity, set()).add(
            MappingSupportingRelationship(
                parent_model=dataset.target_model,
                relationship_field=parent_field,
                relationship_type=relationship_type,
            )
        )
        if source_column_keys:
            sources.setdefault(identity, set()).add(
                MappingSupportingSource(
                    dataset_id=dataset.dataset_id,
                    source_column_keys=source_column_keys,
                    row_inclusion=dataset.row_inclusion,
                    value_mappings=resolver.value_mappings,
                )
            )
        if constant_values is not None:
            constants.setdefault(identity, set()).add(constant_values)

    for dataset in datasets:
        parent = schema_models.get(dataset.target_model)
        if parent is None:
            continue
        fields = {field.name: field for field in parent.fields}
        for component in (*dataset.target_identity, *dataset.target_scope):
            if component.resolver is None or len(component.target_fields) != 1:
                continue
            parent_field = component.target_fields[0]
            metadata = fields.get(parent_field)
            if metadata is None:
                continue
            resolver = component.resolver
            add(
                dataset,
                parent_field=parent_field,
                relationship_type=metadata.type,
                relation_model=metadata.relation,
                resolver=resolver,
                key_fields=tuple(
                    item.target_field for item in resolver.key_mappings
                ),
                scope_fields=tuple(
                    item.target_field for item in resolver.scope_mappings
                ),
                source_column_keys=tuple(
                    item.source_column_key
                    for item in (*resolver.key_mappings, *resolver.scope_mappings)
                ),
            )
        for relationship in dataset.relationships:
            metadata = fields.get(relationship.target_field)
            resolver = relationship.resolver
            if metadata is None or resolver is None:
                continue
            key_fields, scope_fields = relationship_target_fields(relationship)
            constant_values = None
            if (
                relationship.value_source
                is RelationshipValueSource.CONSTANT_EXISTING
                and relationship.constant_reference is not None
            ):
                constant_values = tuple(
                    item.value
                    for item in (
                        *relationship.constant_reference.key_values,
                        *relationship.constant_reference.scope_values,
                    )
                )
            add(
                dataset,
                parent_field=relationship.target_field,
                relationship_type=metadata.type,
                relation_model=metadata.relation,
                resolver=resolver,
                key_fields=key_fields,
                scope_fields=scope_fields,
                source_column_keys=tuple(
                    item.source_column_key
                    for item in (*resolver.key_mappings, *resolver.scope_mappings)
                ),
                constant_values=constant_values,
            )

    return tuple(
        MappingSupportingRequirement(
            model_name=model_name,
            key_fields=key_fields,
            scope_fields=scope_fields,
            relationships=tuple(
                sorted(
                    paths[identity],
                    key=lambda item: (
                        item.parent_model,
                        item.relationship_field,
                        item.relationship_type,
                    ),
                )
            ),
            sources=tuple(
                sorted(
                    sources.get(identity, ()),
                    key=lambda item: (item.dataset_id, item.source_column_keys),
                )
            ),
            constant_values=tuple(sorted(constants.get(identity, ()))),
        )
        for identity in sorted(paths)
        for model_name, key_fields, scope_fields in (identity,)
    )


def load_current_mapping_supporting_references(
    repository: MappingSupportingLookupReader | None,
    workspace_id: str,
    datasets: tuple[DatasetMapping, ...],
    schema: OdooSchemaCatalog,
) -> tuple[SupportingLookupSnapshot, ...]:
    """Load every current supporting contract in one bounded repository read."""

    if repository is None:
        return ()
    version_decision = assess_odoo_operation(
        schema.odoo_version,
        OdooOperation.COMPARE,
    )
    odoo_major_version = (
        version_decision.version.major if version_decision.allowed else -1
    )
    current_policy_hash = reference_policy_hash(odoo_major_version)
    if current_policy_hash is None:
        return ()
    lookup_keys = set()
    for requirement in plan_mapping_supporting_values(datasets, schema):
        standard = standard_reference_key(
            requirement.model_name,
            odoo_major_version=odoo_major_version,
        )
        display_field = (
            standard.display_field
            if standard is not None
            and standard.key_fields == requirement.key_fields
            and standard.scope_fields == requirement.scope_fields
            else requirement.key_fields[0]
        )
        lookup_keys.add(
            supporting_lookup_key(
                relation_model=requirement.model_name,
                key_fields=requirement.key_fields,
                scope_fields=requirement.scope_fields,
                display_field=display_field,
                reference_policy_hash=current_policy_hash,
            )
        )
    if not lookup_keys:
        return ()
    return tuple(
        snapshot
        for snapshot in repository.list_current(workspace_id, lookup_keys)
        if snapshot.workspace_id == workspace_id
        and snapshot.lookup_key in lookup_keys
        and snapshot.target_hash == schema.connection_target_hash
        and snapshot.read_credential_binding_hash
        == schema.read_credential_binding_hash
        and snapshot.read_principal_hash == schema.read_principal_hash
        and snapshot.read_context_hash == schema.read_context_hash
        and snapshot.reference_policy_hash == current_policy_hash
    )


__all__ = [
    "MappingSupportingLookupReader",
    "MappingSupportingRelationship",
    "MappingSupportingRequirement",
    "MappingSupportingSource",
    "load_current_mapping_supporting_references",
    "plan_mapping_supporting_values",
]
