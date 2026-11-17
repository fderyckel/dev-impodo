"""Validate explicit source-column exclusions against current target providers.

The same pure dependency projection serves authoring controls and semantic
validation. A row identifier or rows-to-use condition does not itself provide
an Odoo value, so those inputs can still be marked as deliberately unmapped.
"""

from __future__ import annotations

from collections.abc import Sequence

from impodo.domain.recipe.value_rules import FormulaValidationError, formula_input_names
from impodo.domain.source_binding import SourceOriginKind

from .contracts import DatasetMapping, ScalarFieldMapping
from .reference_preparation import preparation_source_keys
from .validation.context import SourceColumnView
from .validation.evidence import MappingValidationIssue


def _scalar_source_keys(
    field: ScalarFieldMapping,
    columns: Sequence[SourceColumnView],
) -> set[str]:
    keys = {field.source_column_key} if field.source_column_key else set()
    if field.concatenation is not None:
        keys.update(field.concatenation.source_column_keys)
    if field.selection_rules is not None:
        keys.update(
            condition.source_column_key
            for rule in field.selection_rules.rules
            for condition in rule.conditions
        )
    if field.reference_lookup is not None:
        keys.update(field.reference_lookup.key_source_column_keys)
    if field.transform.formula.strip():
        # Browser formulas already use the accepted column_N names. Use the
        # same ordinal fallback as semantic validation for structural fixtures.
        aliases = {
            f"column_{getattr(column, 'ordinal', index + 1)}": column.stable_key
            for index, column in enumerate(columns)
        }
        try:
            names = formula_input_names(
                field.transform.formula,
                allowed_names=set(aliases),
            )
        except FormulaValidationError:
            # The owning scalar validator reports an invalid formula. It
            # cannot authorize submission or an unproved source dependency.
            names = ()
        keys.update(aliases[name] for name in names if name in aliases)
    return keys


def target_source_column_keys(
    dataset: DatasetMapping,
    columns: Sequence[SourceColumnView],
) -> frozenset[str]:
    """Return source inputs that supply target values or matching components."""

    keys: set[str] = set()
    for component in (*dataset.target_identity, *dataset.target_scope):
        keys.update(component.source_column_keys)
        if component.resolver is not None:
            keys.update(
                key
                for item in (
                    *component.resolver.key_mappings,
                    *component.resolver.scope_mappings,
                )
                for key in preparation_source_keys(item)
            )
    for field in dataset.fields:
        keys.update(_scalar_source_keys(field, columns))
    for relationship in dataset.relationships:
        keys.update(relationship.source_column_keys)
        keys.update(
            key
            for item in (
                *relationship.resolver.key_mappings,
                *relationship.resolver.scope_mappings,
            )
            for key in preparation_source_keys(item)
        )
    if dataset.generated_ordering is not None:
        keys.update(dataset.generated_ordering.group_column_keys)
        keys.update(dataset.generated_ordering.sort_column_keys)
    if dataset.source_record_identity is not None:
        existing_key = dataset.source_record_identity.existing_external_id_column_key
        if existing_key is not None:
            keys.add(existing_key)
    return frozenset(keys)


def validate_source_column_exclusions(
    dataset: DatasetMapping,
    columns: Sequence[SourceColumnView],
    *,
    origin: SourceOriginKind,
    path: str,
) -> tuple[MappingValidationIssue, ...]:
    """Reject unknown or still-used exclusions without reading source values."""

    if not dataset.excluded_source_column_keys:
        return ()
    if origin is SourceOriginKind.ODOO:
        return (
            MappingValidationIssue(
                code="MAPPING_SOURCE_EXCLUSION_ODOO_UNSUPPORTED",
                severity="error",
                path=path,
                message="Source-column exclusions require a file source.",
                remediation="Clear these choices for protected Odoo records.",
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
            ),
        )
    available = {column.stable_key: column for column in columns}
    used = target_source_column_keys(dataset, columns)
    issues = []
    for key in dataset.excluded_source_column_keys:
        if key not in available:
            code = "MAPPING_SOURCE_EXCLUSION_UNKNOWN"
            message = "The excluded source column is not in the accepted dataset."
            remediation = "Choose a column from the current source data."
        elif key in used:
            code = "MAPPING_SOURCE_EXCLUSION_IN_USE"
            name = available[key].source_name or "The selected source column"
            message = f"{name} still supplies an Odoo field or matching rule."
            remediation = "Remove the field match or keep this column included."
        else:
            continue
        issues.append(
            MappingValidationIssue(
                code=code,
                severity="error",
                path=path,
                message=message,
                remediation=remediation,
                dataset_id=dataset.dataset_id,
                source_column_key=key,
                target_model=dataset.target_model,
            )
        )
    return tuple(issues)
