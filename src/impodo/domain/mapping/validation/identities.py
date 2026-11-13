"""Source and target identity validation."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.domain.data_version.metadata import TYPE_COMPATIBILITY
from impodo.domain.source_binding import DerivedSourceBinding

from ..contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    ResolverOrigin,
)
from .common import (
    _VALUE_TYPES,
    _check_column,
    _issue,
    _target_unknown,
)
from .context import SourceColumnView, ValidationContext
from .evidence import MappingValidationIssue
from .relationships import _validate_resolver


def _validate_source_identity(
    dataset: DatasetMapping,
    base: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    if not dataset.source_identity_column_keys:
        issues.append(
            _issue(
                "MAPPING_SOURCE_IDENTITY_MISSING",
                f"{base}/source_identity_column_keys",
                "A source trace identity is required.",
                "Choose one or more stable source columns.",
                dataset=dataset,
            )
        )
    if len(set(dataset.source_identity_column_keys)) != len(
        dataset.source_identity_column_keys
    ):
        issues.append(
            _issue(
                "MAPPING_SOURCE_IDENTITY_DUPLICATE",
                f"{base}/source_identity_column_keys",
                "A source identity column is repeated.",
                "Keep each source identity component once.",
                dataset=dataset,
            )
        )
    for column in dataset.source_identity_column_keys:
        _check_column(dataset, column, base, columns, issues)


def _validate_identity_component(
    context: ValidationContext,
    dataset: DatasetMapping,
    component: IdentityComponentMapping,
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
    *,
    is_scope: bool,
) -> None:
    fields = context.fields_by_model[dataset.target_model]
    for column in component.source_column_keys:
        _check_column(dataset, column, path, columns, issues)
    explicit_scope_null = (
        component.null_policy is not IdentityNullPolicy.REJECT
    )
    if explicit_scope_null and (not is_scope or component.resolver is None):
        issues.append(
            _issue(
                "MAPPING_IDENTITY_NULL_POLICY_INVALID",
                f"{path}/null_policy",
                "A blank identity value cannot use hierarchy-root semantics.",
                "Use explicit null only for an optional self-parent scope.",
                dataset=dataset,
            )
        )
    if not component.target_fields:
        issues.append(
            _issue(
                "MAPPING_IDENTITY_ARITY_INVALID",
                f"{path}/target_fields",
                "An identity component requires a target field.",
                "Choose the corresponding target business-key field.",
                dataset=dataset,
            )
        )
        return
    if component.resolver is None:
        if len(component.source_column_keys) != len(
            component.target_fields
        ):
            target_field = (
                component.target_fields[0]
                if len(component.target_fields) == 1
                else None
            )
            metadata = fields.get(target_field) if target_field else None
            label = metadata.label if metadata else target_field
            missing_source = not component.source_column_keys and label is not None
            issues.append(
                _issue(
                    "MAPPING_IDENTITY_ARITY_INVALID",
                    path,
                    f"{label} has no source column selected."
                    if missing_source
                    else "The number of selected source columns does not match the Odoo matching fields.",
                    f"Choose a source column for {label} in the Odoo matching rule."
                    if missing_source
                    else "Select one source column for each Odoo matching field.",
                    dataset=dataset,
                    target_field=target_field,
                )
            )
        for target_field in component.target_fields:
            metadata = fields.get(target_field)
            if metadata is None:
                issues.append(
                    _target_unknown(dataset, path, target_field)
                )
            elif component.value_type not in _VALUE_TYPES or (
                metadata.type
                not in TYPE_COMPATIBILITY.get(
                    component.value_type, frozenset()
                )
            ):
                issues.append(
                    _issue(
                        "MAPPING_TYPE_INCOMPATIBLE",
                        path,
                        f"Identity field {target_field} is type-incompatible.",
                        "Choose a compatible canonical type.",
                        dataset=dataset,
                        target_field=target_field,
                    )
                )
        return

    if len(component.target_fields) != 1:
        issues.append(
            _issue(
                "MAPPING_IDENTITY_ARITY_INVALID",
                path,
                "A relational identity component targets one many2one field.",
                "Choose exactly one relational target field.",
                dataset=dataset,
            )
        )
        return
    target_field = component.target_fields[0]
    metadata = fields.get(target_field)
    if metadata is None:
        issues.append(_target_unknown(dataset, path, target_field))
        return
    if metadata.type != "many2one":
        issues.append(
            _issue(
                "MAPPING_RELATION_KIND_INCORRECT",
                path,
                f"Relational identity field {target_field} is not many2one.",
                "Choose a many2one identity/scope field.",
                dataset=dataset,
                target_field=target_field,
            )
        )
    source = context.source_datasets[dataset.dataset_id].source
    hierarchy_company = (
        isinstance(source, DerivedSourceBinding) and source.hierarchy_parent is not None
        and source.hierarchy_parent.company_field == target_field
        and metadata.type == "many2one" and metadata.relation == "res.company" and not metadata.required
        and component.null_policy is IdentityNullPolicy.EXPLICIT_SCOPE_NULL
        and component.resolver.origin is ResolverOrigin.TARGET_CATALOG
    )
    if explicit_scope_null and not hierarchy_company and (
        metadata.type != "many2one"
        or metadata.required
        or metadata.relation != dataset.target_model
        or component.resolver.origin is not ResolverOrigin.DATASET
        or component.resolver.dataset_id != dataset.dataset_id
        or (
            component.null_policy is IdentityNullPolicy.ZERO_AS_SCOPE_NULL
            and len(component.source_column_keys) != 1
        )
    ):
        issues.append(
            _issue(
                "MAPPING_IDENTITY_NULL_POLICY_INVALID",
                f"{path}/null_policy",
                "Blank scope is not a valid root for this relationship.",
                "Use explicit null only for an optional self-parent scope.",
                dataset=dataset,
                target_field=target_field,
            )
        )
    _validate_resolver(
        context,
        dataset,
        component.resolver,
        path,
        component.source_column_keys,
        metadata.relation,
        metadata,
        issues,
        require_governed_key=True,
    )
