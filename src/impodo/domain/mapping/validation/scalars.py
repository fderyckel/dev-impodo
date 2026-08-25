"""Scalar field mapping validation."""

from __future__ import annotations

import re
from typing import Mapping

from ....metadata import TYPE_COMPATIBILITY
from ....value_rules import (
    CASE_MODES,
    CHARACTER_CLASSES,
    MAX_RULE_SIZE,
    ROUNDING_MODES,
    SEARCH_MODES,
    SEGMENT_LOCATIONS,
    ScalarTransformPolicy,
    validate_formula,
    validate_pattern,
)
from ..contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionConditionOperator,
    TargetFieldHandling,
)
from ..create_field_policy import (
    CreateFieldCoverage,
    evaluate_create_field,
)
from ..scalar_values import (
    ScalarValueError,
    _DATE_FORMATS,
    _DECIMAL_LOCALES,
    canonicalize_scalar_value,
    _selection_typed_value,
)
from .common import (
    _NULL_POLICIES,
    _RELATION_TYPES,
    _VALUE_TYPES,
    _check_column,
    _issue,
    _target_unknown,
)
from .context import SourceColumnView, ValidationContext
from .evidence import MappingValidationIssue


def _validate_scalar(
    context: ValidationContext,
    dataset: DatasetMapping,
    field_mapping: ScalarFieldMapping,
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    fields = context.fields_by_model[dataset.target_model]
    source_required = field_mapping.value_source in {
        ScalarValueSource.SOURCE,
        ScalarValueSource.SOURCE_WITH_FALLBACK,
    }
    literal_required = field_mapping.value_source in {
        ScalarValueSource.CONSTANT,
        ScalarValueSource.SOURCE_WITH_FALLBACK,
    }
    if source_required:
        if field_mapping.source_column_key:
            _check_column(
                dataset,
                field_mapping.source_column_key,
                path,
                columns,
                issues,
            )
        else:
            issues.append(
                _issue(
                    "MAPPING_VALUE_PROVIDER_INVALID",
                    path,
                    "The selected value provider requires a source column.",
                    "Choose a frozen source column.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if field_mapping.reference_lookup is not None:
            for key in field_mapping.reference_lookup.key_source_column_keys[1:]:
                _check_column(dataset, key, path, columns, issues)
    elif field_mapping.source_column_key is not None:
        issues.append(
            _issue(
                "MAPPING_VALUE_PROVIDER_INVALID",
                path,
                "This value provider must not reference a source column.",
                "Clear the source column or choose a source-based provider.",
                dataset=dataset,
                source_column=field_mapping.source_column_key,
                target_field=field_mapping.target_field,
            )
        )
    if literal_required and field_mapping.literal_value is None:
        issues.append(
            _issue(
                "MAPPING_VALUE_PROVIDER_INVALID",
                path,
                "The selected value provider requires a literal value.",
                "Enter a constant or fallback value.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    elif (
        not literal_required
        and field_mapping.literal_value is not None
    ):
        issues.append(
            _issue(
                "MAPPING_VALUE_PROVIDER_INVALID",
                path,
                "This value provider must not contain a literal value.",
                "Clear the literal or choose constant/fallback.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    metadata = fields.get(field_mapping.target_field)
    if metadata is None:
        issues.append(
            _target_unknown(dataset, path, field_mapping.target_field)
        )
        return
    if metadata.type in _RELATION_TYPES:
        issues.append(
            _issue(
                "MAPPING_RELATION_KIND_INCORRECT",
                path,
                f"{field_mapping.target_field} is relational, not scalar.",
                "Configure it in the relationship builder.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    _validate_categorical_policy(
        context,
        dataset,
        field_mapping,
        metadata.type,
        path,
        issues,
    )
    _validate_selection_rules(
        dataset,
        field_mapping,
        metadata.type,
        metadata.selection,
        path,
        columns,
        issues,
    )
    if field_mapping.value_mappings:
        if field_mapping.value_source not in {
            ScalarValueSource.SOURCE,
            ScalarValueSource.SOURCE_WITH_FALLBACK,
        }:
            issues.append(
                _issue(
                    "MAPPING_VALUE_MATCH_INVALID",
                    path,
                    "Value matching requires a source column.",
                    "Choose Source column or Source + fallback.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        selection_keys = {str(item[0]) for item in metadata.selection}
        if not selection_keys:
            issues.append(
                _issue(
                    "MAPPING_VALUE_MATCH_INVALID",
                    path,
                    "This Odoo field does not provide a list of choices.",
                    "Use value matching only for captured Odoo choices.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        invalid_target = next(
            (
                item.target_value
                for item in field_mapping.value_mappings
                if item.target_value not in selection_keys
            ),
            None,
        )
        if invalid_target is not None:
            issues.append(
                _issue(
                    "MAPPING_SELECTION_VALUE_INVALID",
                    path,
                    (
                        f"{invalid_target!r} is not an available Odoo "
                        f"choice for {field_mapping.target_field}."
                    ),
                    "Choose one of the captured Odoo choices.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
    if field_mapping.value_type not in _VALUE_TYPES or (
        metadata.type
        not in TYPE_COMPATIBILITY.get(
            field_mapping.value_type, frozenset()
        )
    ):
        issues.append(
            _issue(
                "MAPPING_TYPE_INCOMPATIBLE",
                path,
                (
                    f"{field_mapping.target_field} is {metadata.type}, "
                    f"not compatible with {field_mapping.value_type}."
                ),
                "Choose a compatible canonical value type.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    _validate_transform_policy(
        dataset,
        field_mapping,
        path,
        columns,
        issues,
    )
    if metadata.readonly and not field_mapping.validate_only:
        issues.append(
            _issue(
                "MAPPING_TARGET_FIELD_READONLY",
                path,
                f"{field_mapping.target_field} is readonly.",
                "Remove it or mark it validate-only.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if field_mapping.validate_only and field_mapping.compare:
        issues.append(
            _issue(
                "MAPPING_FIELD_POLICY_INVALID",
                path,
                "A validate-only scalar cannot also be compared.",
                "Disable comparison or validate-only.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if (
        field_mapping.value_source is ScalarValueSource.ODOO_DEFAULT
        and (
            field_mapping.compare
            or field_mapping.validate_only
            or field_mapping.required
            or field_mapping.required_on_create
            or field_mapping.transform.configured_text_steps
            or field_mapping.transform.formula
            or field_mapping.transform.case_mode != "preserve"
            or field_mapping.transform.decimal_places is not None
            or field_mapping.validation.configured
        )
    ):
        issues.append(
            _issue(
                "MAPPING_FIELD_POLICY_INVALID",
                path,
                "An Odoo-default field has no local value to compare or validate.",
                "Disable compare, validate-only, and required value checks.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if field_mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
        default_assessment = evaluate_create_field(
            metadata,
            provided=False,
            handling=TargetFieldHandling.ODOO_DEFAULT,
        )
        if default_assessment.coverage is CreateFieldCoverage.DEFAULT_UNVERIFIED:
            issues.append(
                _issue(
                    "MAPPING_ODOO_DEFAULT_UNVERIFIED",
                    path,
                    (
                        f"{field_mapping.target_field} has no verified Odoo "
                        "create default for this target context."
                    ),
                    (
                        "Provide a value or refresh Odoo details and review "
                        "an available default."
                    ),
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
    if field_mapping.null_policy not in _NULL_POLICIES:
        issues.append(
            _issue(
                "MAPPING_FIELD_POLICY_INVALID",
                path,
                "The scalar null policy is unsupported.",
                "Choose distinct, equivalent, or ignore_source_null.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if literal_required and field_mapping.literal_value is not None:
        try:
            proposed = canonicalize_scalar_value(
                field_mapping,
                None,
            )
        except ScalarValueError as error:
            issues.append(
                _issue(
                    "MAPPING_LITERAL_INVALID",
                    path,
                    str(error),
                    "Correct the literal or its parsing policy.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        else:
            selection_keys = {
                str(item[0]) for item in metadata.selection
            }
            if metadata.required and proposed in {None, ""}:
                issues.append(
                    _issue(
                        "MAPPING_LITERAL_INVALID",
                        path,
                        (
                            f"{field_mapping.target_field} is required but "
                            "the governed literal resolves to empty."
                        ),
                        "Enter a non-empty constant or fallback.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            if (
                metadata.type == "selection"
                and proposed is not None
                and str(proposed) not in selection_keys
            ):
                issues.append(
                    _issue(
                        "MAPPING_SELECTION_VALUE_INVALID",
                        path,
                        (
                            f"{proposed!r} is not an allowed selection "
                            f"value for {field_mapping.target_field}."
                        ),
                        "Choose one of the captured Odoo selection keys.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
    column = columns.get(field_mapping.source_column_key)
    expected_candidate = {
        "boolean": "boolean",
        "integer": "integer",
        "decimal": "decimal",
        "date": "date",
        "datetime": "datetime",
    }.get(field_mapping.value_type)
    if (
        column is not None
        and expected_candidate is not None
        and column.candidate_type
        not in {expected_candidate, "string", "mixed", "empty"}
    ):
        issues.append(
            _issue(
                "MAPPING_SOURCE_TYPE_ADVISORY_MISMATCH",
                path,
                (
                    f"Source candidate type {column.candidate_type} differs "
                    f"from {field_mapping.value_type}."
                ),
                "Review samples; candidate types are advisory.",
                severity="warning",
                dataset=dataset,
                source_column=field_mapping.source_column_key,
                target_field=field_mapping.target_field,
            )
        )


def _validate_selection_rules(
    dataset: DatasetMapping,
    field_mapping: ScalarFieldMapping,
    target_type: str,
    selection: tuple[tuple[str, str], ...],
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    rule_set = field_mapping.selection_rules
    if field_mapping.value_source is not ScalarValueSource.CONDITIONAL_RULES:
        return
    if target_type != "selection":
        issues.append(
            _issue(
                "MAPPING_SELECTION_RULE_TARGET_INVALID",
                path,
                "Conditional choice rules can only provide an Odoo Selection field.",
                "Choose an Odoo choice field or use another value provider.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
        return
    if rule_set is None:
        return
    if field_mapping.transform != ScalarTransformPolicy():
        issues.append(
            _issue(
                "MAPPING_SELECTION_RULE_TRANSFORM_INVALID",
                f"{path}/transform",
                "Conditional choice rules already return an exact Odoo value.",
                "Remove the additional field transformation.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    selection_keys = {str(item[0]) for item in selection}
    target_values = [rule.target_value for rule in rule_set.rules]
    if rule_set.otherwise_value is not None:
        target_values.append(rule_set.otherwise_value)
    for target_value in target_values:
        if target_value not in selection_keys:
            issues.append(
                _issue(
                    "MAPPING_SELECTION_VALUE_INVALID",
                    f"{path}/selection_rules",
                    f"{target_value!r} is not an available Odoo choice.",
                    "Choose one of the captured Odoo choices.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
    text_only = {
        SelectionConditionOperator.EQUALS_IGNORE_CASE,
        SelectionConditionOperator.CONTAINS,
        SelectionConditionOperator.STARTS_WITH,
        SelectionConditionOperator.ENDS_WITH,
    }
    ordered = {
        SelectionConditionOperator.LESS_THAN,
        SelectionConditionOperator.LESS_THAN_OR_EQUAL,
        SelectionConditionOperator.GREATER_THAN,
        SelectionConditionOperator.GREATER_THAN_OR_EQUAL,
    }
    boolean_only = {
        SelectionConditionOperator.IS_TRUE,
        SelectionConditionOperator.IS_FALSE,
    }
    for rule_index, rule in enumerate(rule_set.rules):
        for condition_index, condition in enumerate(rule.conditions):
            condition_path = (
                f"{path}/selection_rules/rules/{rule_index}/conditions/"
                f"{condition_index}"
            )
            _check_column(
                dataset,
                condition.source_column_key,
                condition_path,
                columns,
                issues,
            )
            if condition.operator in text_only and condition.value_type != "string":
                issues.append(
                    _issue(
                        "MAPPING_SELECTION_RULE_OPERATOR_INVALID",
                        condition_path,
                        "This comparison is only available for text values.",
                        "Choose a text comparison or change the comparison type.",
                        dataset=dataset,
                        source_column=condition.source_column_key,
                        target_field=field_mapping.target_field,
                    )
                )
            if condition.operator in ordered and condition.value_type not in {
                "integer",
                "decimal",
                "date",
                "datetime",
            }:
                issues.append(
                    _issue(
                        "MAPPING_SELECTION_RULE_OPERATOR_INVALID",
                        condition_path,
                        "This ordered comparison requires a number or date.",
                        "Choose the matching comparison type.",
                        dataset=dataset,
                        source_column=condition.source_column_key,
                        target_field=field_mapping.target_field,
                    )
                )
            if condition.operator in boolean_only and condition.value_type != "boolean":
                issues.append(
                    _issue(
                        "MAPPING_SELECTION_RULE_OPERATOR_INVALID",
                        condition_path,
                        "True and false comparisons require a yes/no source value.",
                        "Choose the yes/no comparison type.",
                        dataset=dataset,
                        source_column=condition.source_column_key,
                        target_field=field_mapping.target_field,
                    )
                )
            if condition.value_type == "boolean" and condition.operator not in {
                SelectionConditionOperator.IS_BLANK,
                SelectionConditionOperator.IS_NOT_BLANK,
                *boolean_only,
            }:
                issues.append(
                    _issue(
                        "MAPPING_SELECTION_RULE_OPERATOR_INVALID",
                        condition_path,
                        "A yes/no source value requires a yes, no, or blank comparison.",
                        "Choose a yes/no comparison.",
                        dataset=dataset,
                        source_column=condition.source_column_key,
                        target_field=field_mapping.target_field,
                    )
                )
            if condition.comparison_value is not None:
                try:
                    _selection_typed_value(
                        condition.comparison_value,
                        condition.value_type,
                    )
                except (InvalidOperation, TypeError, ValueError):
                    issues.append(
                        _issue(
                            "MAPPING_SELECTION_RULE_VALUE_INVALID",
                            condition_path,
                            "The comparison value does not match its selected type.",
                            "Correct the value or choose another comparison type.",
                            dataset=dataset,
                            source_column=condition.source_column_key,
                            target_field=field_mapping.target_field,
                        )
                    )


def _validate_categorical_policy(
    context: ValidationContext,
    dataset: DatasetMapping,
    field_mapping: ScalarFieldMapping,
    target_type: str,
    path: str,
    issues: list[MappingValidationIssue],
) -> None:
    """Require explicit closed-domain meaning for scalar selections."""
    policy = field_mapping.categorical_policy
    is_selection = target_type == "selection"
    provides_value = field_mapping.value_source is not ScalarValueSource.ODOO_DEFAULT
    if is_selection and provides_value and policy is None:
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_REQUIRED",
                f"{path}/categorical_policy",
                "This Odoo choice field has no categorical coverage policy.",
                "Confirm exact Odoo values or require an explicit match for every source choice.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
        return
    if not is_selection and policy is not None:
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_INVALID",
                f"{path}/categorical_policy",
                "Categorical target-value policy is only valid for an Odoo choice field.",
                "Remove the categorical policy from this scalar field.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
        return
    if policy not in {
        None,
        CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
        CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
    }:
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_INVALID",
                f"{path}/categorical_policy",
                "A business-key policy cannot govern an Odoo scalar choice.",
                "Choose exact target value or explicit value match.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    elif (
        policy is CategoricalCoveragePolicy.EXACT_TARGET_VALUE
        and field_mapping.value_mappings
    ):
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_CONFLICT",
                f"{path}/categorical_policy",
                "Exact target-value coverage cannot also translate source choices.",
                "Remove the value matches or choose explicit value match.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    elif (
        policy is CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH
        and field_mapping.value_source
        not in {ScalarValueSource.SOURCE, ScalarValueSource.SOURCE_WITH_FALLBACK}
    ):
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_CONFLICT",
                f"{path}/categorical_policy",
                "Explicit value matching requires a source-based provider.",
                "Choose a source column or exact target-value coverage.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )


def _validate_transform_policy(
    dataset: DatasetMapping,
    field_mapping: ScalarFieldMapping,
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    policy = field_mapping.transform
    if policy.case_mode not in CASE_MODES:
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "The case transformation is unsupported.",
                (
                    "Choose keep as-is, uppercase, lowercase, sentence "
                    "case, or title case."
                ),
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    elif (
        policy.case_mode != "preserve"
        and field_mapping.value_type != "string"
    ):
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "Case transformations apply only to string values.",
                "Preserve case or choose the string canonical type.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    for step_index, step in enumerate(policy.text_steps):
        step_path = f"{path}/transform/text_steps/{step_index}"
        if step.kind == "remove_separators_between_digits":
            if not step.characters:
                issues.append(
                    _issue(
                        "MAPPING_TRANSFORM_INVALID",
                        step_path,
                        "Choose at least one separator to remove.",
                        "Select spaces, dots, hyphens, or another separator.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            elif any(
                character.isalnum() or character == "+"
                for character in step.characters
            ):
                issues.append(
                    _issue(
                        "MAPPING_TRANSFORM_INVALID",
                        step_path,
                        "Separator cleanup cannot remove letters, numbers, or plus signs.",
                        "Choose punctuation or spaces only.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            if field_mapping.value_type != "string":
                issues.append(
                    _issue(
                        "MAPPING_TRANSFORM_INVALID",
                        step_path,
                        "Separator cleanup applies only to text values.",
                        "Choose the Text value type or remove this cleanup step.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            continue
        if step.search_mode not in SEARCH_MODES:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    step_path,
                    "The find-and-replace mode is unsupported.",
                    "Choose anywhere, beginning, end, or advanced pattern.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        elif step.search_mode == "pattern" and step.search_value:
            try:
                matcher = validate_pattern(step.search_value)
                matcher.sub(step.replacement_value, "", count=1)
            except (re.error, ValueError) as error:
                issues.append(
                    _issue(
                        "MAPPING_TRANSFORM_INVALID",
                        step_path,
                        str(error),
                        "Correct the advanced find pattern or use plain text.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
        if not step.search_value:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    step_path,
                    "This cleanup step has no text to find.",
                    "Enter the text to find or remove this cleanup step.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if step.search_value and field_mapping.value_type != "string":
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    step_path,
                    "Find and replace applies only to text values.",
                    "Choose the string value type or clear find and replace.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
    if policy.formula:
        aliases = {
            f"column_{getattr(column, 'ordinal', index + 1)}"
            for index, column in enumerate(columns.values())
        }
        try:
            validate_formula(policy.formula, allowed_names=aliases)
        except ValueError as error:
            issues.append(
                _issue(
                    "MAPPING_FORMULA_INVALID",
                    path,
                    str(error),
                    "Correct the formula using the available column names.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
    if policy.rounding_mode not in ROUNDING_MODES:
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "The rounding method is unsupported.",
                "Choose one of the listed rounding methods.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.decimal_places is not None and (
        policy.decimal_places < 0
        or policy.decimal_places > 18
        or field_mapping.value_type != "decimal"
    ):
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "Decimal rounding needs a decimal value and 0 to 18 places.",
                "Choose decimal as the value type and a valid number of places.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.decimal_locale not in _DECIMAL_LOCALES:
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "The decimal locale is unsupported.",
                "Choose invariant, en_US, de_DE, or fr_FR.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.date_format not in _DATE_FORMATS:
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "The date format is unsupported.",
                "Choose one of the explicit date formats.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.timezone != "UTC":
        issues.append(
            _issue(
                "MAPPING_TRANSFORM_INVALID",
                path,
                "The current mapping rules support the explicit UTC timezone only.",
                "Choose UTC; broader IANA timezone support is deferred.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    _validate_scalar_rules(
        dataset,
        field_mapping,
        path,
        issues,
    )

def _validate_scalar_rules(
    dataset: DatasetMapping,
    field_mapping: ScalarFieldMapping,
    path: str,
    issues: list[MappingValidationIssue],
) -> None:
    policy = field_mapping.validation
    if policy.configured and field_mapping.value_type != "string":
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "Length and character checks apply only to text values.",
                "Choose the string value type or clear the text checks.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.exact_length is not None and not (
        1 <= policy.exact_length <= MAX_RULE_SIZE
    ):
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                f"Exact length must be between 1 and {MAX_RULE_SIZE}.",
                "Enter a valid exact length.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.segment_location not in SEGMENT_LOCATIONS:
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "The part of the value to check is unsupported.",
                "Choose whole value, first characters, or last characters.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.character_class not in CHARACTER_CLASSES:
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "The required character type is unsupported.",
                "Choose digits, capital letters, or lowercase letters.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.character_class != "none" and policy.segment_location == "none":
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "Choose which part of the value should be checked.",
                "Choose whole value, first characters, or last characters.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.segment_location != "none" and policy.character_class == "none":
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "Choose what kind of characters are allowed.",
                "Choose digits, capital letters, or lowercase letters.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.segment_location in {"first", "last"} and (
        policy.segment_length is None
        or not 1 <= policy.segment_length <= MAX_RULE_SIZE
    ):
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                f"First/last character count must be between 1 and {MAX_RULE_SIZE}.",
                "Enter how many characters should be checked.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.segment_location in {"none", "entire"} and (
        policy.segment_length is not None
    ):
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "A character count is used only for first or last characters.",
                "Clear the count or choose first/last characters.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if (
        policy.exact_length is not None
        and policy.segment_length is not None
        and policy.segment_length > policy.exact_length
    ):
        issues.append(
            _issue(
                "MAPPING_VALUE_RULE_INVALID",
                path,
                "The character check is longer than the exact field length.",
                "Reduce the checked character count or increase exact length.",
                dataset=dataset,
                target_field=field_mapping.target_field,
            )
        )
    if policy.pattern:
        try:
            validate_pattern(policy.pattern)
        except ValueError as error:
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    str(error),
                    "Correct the advanced custom pattern.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
