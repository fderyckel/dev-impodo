"""Source and target identity validation."""

from __future__ import annotations

from typing import Mapping

from impodo.domain.data_version.metadata import TYPE_COMPATIBILITY
from ..contracts import DatasetMapping, IdentityComponentMapping, ResolverOrigin
from .common import (
    _VALUE_TYPES,
    _check_column,
    _issue,
    _target_unknown,
)
from .evidence import MappingValidationIssue
from .context import SourceColumnView, ValidationContext
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
    dependencies: dict[str, set[str]],
    required_on_create_dependencies: dict[str, set[str]],
    issues: list[MappingValidationIssue],
) -> None:
    fields = context.fields_by_model[dataset.target_model]
    for column in component.source_column_keys:
        _check_column(dataset, column, path, columns, issues)
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
            issues.append(
                _issue(
                    "MAPPING_IDENTITY_ARITY_INVALID",
                    path,
                    "Scalar identity source and target arity differ.",
                    "Map one source column to each target component.",
                    dataset=dataset,
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
    _validate_resolver(
        context,
        dataset,
        component.resolver,
        path,
        component.source_column_keys,
        metadata.relation,
        metadata,
        dependencies,
        issues,
        require_governed_key=True,
    )
    if (
        component.resolver.origin is ResolverOrigin.DATASET
        and component.resolver.dataset_id
    ):
        required_on_create_dependencies.setdefault(
            dataset.dataset_id, set()
        ).add(component.resolver.dataset_id)
