"""Repair unambiguous links between user-selected incoming datasets.

The data manager chooses transfer roots.  When one mapped field points to a
model supplied by exactly one other selected dataset, Impodo may derive that
dependency from the referenced dataset's declared source identity.  This is a
mapping repair only; it never widens the selected Odoo model or write scope.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.recipe.source_binding import normalize_recipe_source_name
from impodo.domain.schema.governance import (
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.reference_keys import (
    GovernedReferenceRequest,
    ReferenceReadPurpose,
    authorize_governed_reference,
    captured_reference_field_contracts,
)


@dataclass(frozen=True, slots=True)
class IncomingDependencyRepair:
    """Describe one relationship redirected to an incoming transfer root."""

    dataset_id: str
    target_field: str
    referenced_dataset_id: str
    source_column_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncomingDependencyRepairResult:
    """Return repaired mappings plus an inspectable decision record."""

    datasets: tuple[DatasetMapping, ...]
    repairs: tuple[IncomingDependencyRepair, ...]


def repair_unambiguous_incoming_dependencies(
    datasets: tuple[DatasetMapping, ...],
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    governance: SchemaGovernance | None,
) -> IncomingDependencyRepairResult:
    """Use a unique selected dataset when an Odoo resolver is ungoverned.

    A repair is deliberately narrow: one writable many-to-one, one other
    selected dataset for the related model, no explicit value translations,
    and a complete one-to-one column-name match to that dataset's declared
    source identity. A valid governed Odoo resolver always wins and is left
    untouched.
    """

    source_by_id = {item.dataset_id: item for item in selection.datasets}
    schema_by_model = {item.name: item for item in schema.models}
    mapped_by_model: dict[str, list[DatasetMapping]] = {}
    for dataset in datasets:
        mapped_by_model.setdefault(dataset.target_model, []).append(dataset)
    governed_signatures = frozenset(
        (item.model, item.key_fields, item.scope_fields)
        for item in (governance.business_keys if governance is not None else ())
        if item.status is BusinessKeyStatus.CONFIRMED
    )
    version_decision = assess_odoo_operation(
        getattr(schema, "odoo_version", ""),
        OdooOperation.COMPARE,
    )
    assessed_version = version_decision.version
    odoo_major_version = (
        assessed_version.major
        if version_decision.allowed
        and assessed_version is not None
        and assessed_version.major is not None
        else -1
    )

    repaired_datasets: list[DatasetMapping] = []
    repairs: list[IncomingDependencyRepair] = []
    for dataset in datasets:
        model = schema_by_model.get(dataset.target_model)
        fields = {item.name: item for item in model.fields} if model is not None else {}
        relationships = []
        for relationship in dataset.relationships:
            resolver = relationship.resolver
            metadata = fields.get(relationship.target_field)
            related_model_name = (
                metadata.relation
                if metadata is not None and metadata.relation is not None
                else ""
            )
            candidates = tuple(
                item
                for item in mapped_by_model.get(
                    related_model_name,
                    (),
                )
                if item.dataset_id != dataset.dataset_id
            )
            if (
                resolver.origin is not ResolverOrigin.TARGET_CATALOG
                or relationship.value_source is not RelationshipValueSource.SOURCE
                or relationship.kind != "many2one"
                or metadata is None
                or metadata.type != "many2one"
                or metadata.readonly
                or relationship.validate_only
                or metadata.relation != resolver.model
                or resolver.dataset_id is not None
                or resolver.dataset_projection_field is not None
                or resolver.value_mappings
                or relationship.categorical_policy
                is not CategoricalCoveragePolicy.EXACT_BUSINESS_KEY
                or _target_resolver_is_governed(
                    dataset=dataset,
                    metadata=metadata,
                    resolver=resolver,
                    schema_by_model=schema_by_model,
                    governed_signatures=governed_signatures,
                    odoo_major_version=odoo_major_version,
                )
                or len(candidates) != 1
            ):
                relationships.append(relationship)
                continue
            referenced = candidates[0]
            owner_source = source_by_id.get(dataset.dataset_id)
            referenced_source = source_by_id.get(referenced.dataset_id)
            if (
                owner_source is None
                or referenced_source is None
                or owner_source.origin is SourceOriginKind.ODOO
                or referenced_source.origin is SourceOriginKind.ODOO
            ):
                relationships.append(relationship)
                continue
            inferred_sources = match_incoming_identity_columns(
                owner=owner_source,
                referenced=referenced_source,
                referenced_identity=referenced.source_identity_column_keys,
            )
            if inferred_sources is None:
                relationships.append(relationship)
                continue
            repaired = replace(
                relationship,
                source_column_keys=inferred_sources,
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.DATASET,
                    dataset_id=referenced.dataset_id,
                ),
            )
            relationships.append(repaired)
            repairs.append(
                IncomingDependencyRepair(
                    dataset_id=dataset.dataset_id,
                    target_field=relationship.target_field,
                    referenced_dataset_id=referenced.dataset_id,
                    source_column_keys=inferred_sources,
                )
            )
        repaired_datasets.append(replace(dataset, relationships=tuple(relationships)))
    return IncomingDependencyRepairResult(
        datasets=tuple(repaired_datasets),
        repairs=tuple(repairs),
    )


def _target_resolver_is_governed(
    *,
    dataset: DatasetMapping,
    metadata: SchemaField,
    resolver: RelationshipResolver,
    schema_by_model,
    governed_signatures: frozenset[tuple[str, tuple[str, ...], tuple[str, ...]]],
    odoo_major_version: int,
) -> bool:
    """Use the same reviewed-reference decision as semantic validation."""

    related_model = schema_by_model.get(resolver.model or "")
    if related_model is None:
        return False
    key_fields = tuple(item.target_field for item in resolver.key_mappings)
    scope_fields = tuple(item.target_field for item in resolver.scope_mappings)
    signature = (resolver.model or "", key_fields, scope_fields)
    if signature in governed_signatures or odoo_major_version < 0:
        return True
    decision = authorize_governed_reference(
        GovernedReferenceRequest(
            parent_model=dataset.target_model,
            relationship_field=metadata.name,
            relationship_type=metadata.type,
            relationship_model=metadata.relation,
            related_model=resolver.model or "",
            key_fields=key_fields,
            scope_fields=scope_fields,
            requested_fields=(*key_fields, *scope_fields),
            purpose=ReferenceReadPurpose.MATCH_VALIDATION,
            odoo_major_version=odoo_major_version,
            governed_key=False,
        ),
        captured_fields=captured_reference_field_contracts(related_model.fields),
    )
    return decision.accepted


def match_incoming_identity_columns(
    *,
    owner: SourceDataset | None,
    referenced: SourceDataset | None,
    referenced_identity: tuple[str, ...],
    owner_column_keys: tuple[str, ...] | None = None,
) -> tuple[str, ...] | None:
    """Match harmless spacing differences only when every name is unique."""

    if owner is None or referenced is None or not referenced_identity:
        return None
    referenced_by_key = {item.stable_key: item for item in referenced.columns}
    referenced_tokens = tuple(
        _column_name_token(referenced_by_key[key].source_name)
        for key in referenced_identity
        if key in referenced_by_key
    )
    if len(referenced_tokens) != len(referenced_identity) or len(
        set(referenced_tokens)
    ) != len(referenced_tokens):
        return None
    owner_by_token: dict[str, list[SourceDatasetColumn]] = {}
    for column in owner.columns:
        if owner_column_keys is not None and column.stable_key not in owner_column_keys:
            continue
        owner_by_token.setdefault(
            _column_name_token(column.source_name),
            [],
        ).append(column)
    if owner_column_keys is not None and sum(
        len(items) for items in owner_by_token.values()
    ) != len(owner_column_keys):
        return None
    if any(len(owner_by_token.get(token, ())) != 1 for token in referenced_tokens):
        return None
    matched = tuple(owner_by_token[token][0] for token in referenced_tokens)
    referenced_columns = tuple(referenced_by_key[key] for key in referenced_identity)
    if any(
        owner_column.candidate_type.casefold()
        != referenced_column.candidate_type.casefold()
        for owner_column, referenced_column in zip(
            matched,
            referenced_columns,
            strict=True,
        )
    ):
        return None
    return tuple(column.stable_key for column in matched)


def _column_name_token(value: str) -> str:
    """Treat spaces and punctuation as harmless after Unicode normalization."""

    return normalize_recipe_source_name(value).replace(" ", "")


__all__ = [
    "IncomingDependencyRepair",
    "IncomingDependencyRepairResult",
    "match_incoming_identity_columns",
    "repair_unambiguous_incoming_dependencies",
]
