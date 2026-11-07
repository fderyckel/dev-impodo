"""Describe exact saved field choices for Excel without evaluating source data.

These export-only descriptions come from the mapping and source selection
already verified for the comparison. They do not enter any evidence hash.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ..mapping.contracts import (
    MappingDefinition,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    relationship_input_columns,
)
from ..mapping.row_inclusion_review import describe_row_inclusion_policy
from ..workspace.contracts import SourceSelection


@dataclass(frozen=True, slots=True)
class ReviewWorkbookFieldRule:
    """Source labels and identifiers plus an ordered, explicit field recipe."""

    target_model: str
    source_keys: tuple[str, ...]
    source_labels: tuple[str, ...]
    steps: tuple[str, ...]


def workbook_mapping_rules(
    definition: MappingDefinition,
    selection: SourceSelection,
    *,
    restricted: bool = False,
) -> dict[tuple[str, str], ReviewWorkbookFieldRule]:
    """Project the verified mapping without reading rows or running formulas."""

    datasets = {item.dataset_id: item for item in selection.datasets}
    result: dict[tuple[str, str], ReviewWorkbookFieldRule] = {}
    for mapping in definition.datasets:
        dataset = datasets[mapping.dataset_id]
        labels = {column.stable_key: column.source_name for column in dataset.columns}
        scope = (
            "Rows to use conditions withheld for restricted data."
            if restricted
            else describe_row_inclusion_policy(
                mapping.row_inclusion,
                {key: f"{label} [{key}]" for key, label in labels.items()},
            )
        )

        def add(
            target: str,
            keys: tuple[str, ...],
            steps: tuple[str, ...],
            *,
            name: str = dataset.name,
            model: str = mapping.target_model,
            column_labels: Mapping[str, str] = labels,
            row_scope: str = scope,
        ) -> None:
            result[(name, target)] = ReviewWorkbookFieldRule(
                model,
                keys,
                tuple(column_labels.get(key, key) for key in keys),
                (f"Rows to use: {row_scope}", *steps),
            )

        for component in (*mapping.target_identity, *mapping.target_scope):
            for target in component.target_fields:
                add(
                    target,
                    component.source_column_keys,
                    (
                        (
                            f"Identify records using this source key as {component.value_type}; "
                            f"missing-key policy: {component.null_policy.value}."
                        ),
                    ),
                )
        for field in mapping.fields:
            keys = list(
                field.concatenation.source_column_keys if field.concatenation else ()
            )
            if field.source_column_key and (
                field.value_source
                in {ScalarValueSource.SOURCE, ScalarValueSource.SOURCE_WITH_FALLBACK}
                or field.value_mappings
            ):
                keys.append(field.source_column_key)
            if field.selection_rules:
                keys.extend(
                    condition.source_column_key
                    for rule in field.selection_rules.rules
                    for condition in rule.conditions
                )
            if field.reference_lookup:
                keys.extend(field.reference_lookup.key_source_column_keys)
            ordinals = _formula_source_ordinals(field.transform.formula)
            keys.extend(
                column.stable_key
                for column in dataset.columns
                if column.ordinal in ordinals
            )
            add(
                field.target_field,
                tuple(dict.fromkeys(keys)),
                _scalar_steps(field, labels, restricted),
            )
        for relation in mapping.relationships:
            steps = [
                f"Resolve linked records from {relation.resolver.origin.value} using "
                + ", ".join(
                    item.target_field for item in relation.resolver.key_mappings
                )
                + "."
            ]
            for key in (
                *relation.resolver.key_mappings,
                *relation.resolver.scope_mappings,
            ):
                steps.append(
                    f"Match {labels.get(key.source_column_key, key.source_column_key)} "
                    f"[{key.source_column_key}] to {relation.resolver.model or 'incoming record'}.{key.target_field}."
                )
            if relation.constant_reference:
                steps = [
                    "Use the same existing linked record for every included row: "
                    + _literal(
                        {
                            item.target_field: item.value
                            for item in (
                                *relation.constant_reference.key_values,
                                *relation.constant_reference.scope_values,
                            )
                        },
                        restricted,
                    )
                    + "."
                ]
            if relation.resolver.value_mappings:
                steps.append(
                    "Match linked source choices: "
                    + "; ".join(
                        f"{_literal(item.source_value, restricted)} → {_literal(item.target_value, restricted)}"
                        for item in relation.resolver.value_mappings
                    )
                    + "."
                )
            if relation.blank_source_values:
                steps.append(
                    "Leave the link blank for source choices "
                    + _literal(relation.blank_source_values, restricted)
                    + "."
                )
            steps.append(
                f"Missing match: {relation.on_missing}; multiple matches: {relation.on_ambiguous}; "
                f"linked-value operation: {relation.operation}."
            )
            add(
                relation.target_field,
                relationship_input_columns(relation),
                tuple(steps),
            )
        for disposition in mapping.target_field_dispositions:
            add(
                disposition.target_field,
                (),
                (
                    (
                        "Send no value; let Odoo supply its verified default."
                        if disposition.handling.value == "odoo_default"
                        else "Send no value; Odoo creates or manages this field."
                    ),
                ),
            )
    return result


def _formula_source_ordinals(expression: str) -> set[int]:
    if not expression.strip():
        return set()
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return set()
    return {
        int(node.id[7:])
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id.startswith("column_")
        and node.id[7:].isdigit()
    }


def _literal(value: object, restricted: bool) -> str:
    return (
        "Hidden for restricted data"
        if restricted
        else json.dumps(value, ensure_ascii=False)
    )


def _condition(
    condition: SelectionCondition, labels: Mapping[str, str], restricted: bool
) -> str:
    key = condition.source_column_key
    column = f"{labels.get(key, key)} [{key}]"
    operator = condition.operator.value.replace("_", " ")
    value = (
        ""
        if condition.comparison_value is None
        else " " + _literal(condition.comparison_value, restricted)
    )
    return f"{column} ({condition.value_type}) {operator}{value}"


def _scalar_steps(
    field: ScalarFieldMapping, labels: Mapping[str, str], restricted: bool
) -> tuple[str, ...]:
    def literal(value: object) -> str:
        return _literal(value, restricted)

    steps: list[str] = []
    if (
        field.value_source is ScalarValueSource.CONDITIONAL_RULES
        and field.selection_rules
    ):
        steps.append(
            "Choose the first matching condition below; later matches do not override it."
        )
        for index, rule in enumerate(field.selection_rules.rules, 1):
            conditions = (" AND " if rule.join.value == "all" else " OR ").join(
                _condition(item, labels, restricted) for item in rule.conditions
            )
            steps.append(
                f"Choice {index}: if {conditions}, use {literal(rule.target_value)}."
            )
        otherwise = field.selection_rules.otherwise_value
        steps.append(
            "If no condition matches, "
            + (
                "use " + literal(otherwise) + "."
                if otherwise is not None
                else "block this row."
            )
        )
    elif field.value_source is ScalarValueSource.CONCATENATE and field.concatenation:
        rule = field.concatenation
        columns = ", ".join(
            f"{labels.get(key, key)} [{key}]" for key in rule.source_column_keys
        )
        steps.append(
            f"Combine {columns}, in this order, separated by {literal(rule.separator)}; "
            f"trim each part: {rule.trim_parts}; blank parts: {rule.blank_handling.value}."
        )
    if field.value_mappings:
        steps.append(
            "Match source choices after trimming surrounding spaces: "
            + "; ".join(
                f"{literal(item.source_value)} → {literal(item.target_value)}"
                for item in field.value_mappings
            )
            + ". A match skips the provider and text preparation below; type conversion still applies."
        )
    if field.value_source is ScalarValueSource.CONSTANT:
        steps.append(
            f"For every included row, set the initial value to {literal(field.literal_value)}."
        )
    elif field.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK:
        transform = field.transform
        steps.append(
            "Check the source after "
            + (
                f"trim={transform.trim}, collapse repeated spaces={transform.collapse_whitespace}, "
                f"letter case={transform.case_mode}, empty-as-blank={transform.empty_as_null}"
            )
            + f". If the source is missing or this preparation produces null, use {literal(field.literal_value)}; otherwise use that prepared source value."
        )
    elif field.value_source is ScalarValueSource.SOURCE:
        steps.append("Use the source column value.")
    elif field.value_source is ScalarValueSource.ODOO_DEFAULT:
        return ("Send no value; let Odoo supply its verified default.",)
    if field.reference_lookup:
        lookup = field.reference_lookup
        steps.append(
            f"Look up reference {lookup.reference_id}, version {lookup.reference_content_hash}, "
            f"return field {lookup.value_field}; blank key: {lookup.on_blank}; unknown key: {lookup.on_unknown}."
        )
    transform = field.transform
    if transform.formula:
        steps.append(
            "Calculate formula: "
            + ("Hidden for restricted data" if restricted else transform.formula)
            + ". value is the current input; column_N is source column ordinal N."
        )
    if transform.trim:
        steps.append("Remove leading and trailing whitespace.")
    if transform.collapse_whitespace:
        steps.append("Replace each run of whitespace with one space.")
    for index, step in enumerate(transform.configured_text_steps, 1):
        if step.kind == "remove_separators_between_digits":
            action = (
                f"Remove characters {literal(step.characters)} only between digits."
            )
        elif step.search_mode in {"starts_with", "ends_with"}:
            boundary = "prefix" if step.search_mode == "starts_with" else "suffix"
            action = (
                f"If the text has {boundary} {literal(step.search_value)}, "
                f"replace that {boundary} with {literal(step.replacement_value)}."
            )
        else:
            occurrence = "every match" if step.replace_all else "the first match"
            mode = (
                "regular expression"
                if step.search_mode == "pattern"
                else "literal text"
            )
            action = (
                f"Replace {occurrence} of {mode} {literal(step.search_value)} "
                f"with {literal(step.replacement_value)}."
            )
        steps.append(f"Text change {index}: {action}")
    if transform.case_mode != "preserve":
        steps.append(
            {
                "uppercase": "Convert all letters to uppercase.",
                "lowercase": "Convert all letters to lowercase.",
                "title": "Capitalize each word using title case.",
                "sentence": "Capitalize the first alphabetic character; preserve the remaining letter case.",
            }[transform.case_mode]
        )
    if transform.empty_as_null:
        steps.append("Convert an empty result to blank (null).")
    if field.value_type != "string":
        details = ""
        if field.value_type == "decimal":
            details = f"; number format: {transform.decimal_locale}"
        elif field.value_type in {"date", "datetime"}:
            details = f"; input date format: {transform.date_format}"
        elif field.value_type == "boolean":
            details = "; true/1/yes/y → true; false/0/no/n → false (ignoring case)"
        steps.append(f"Convert to {field.value_type}{details}.")
    if transform.decimal_places is not None and field.value_type == "decimal":
        steps.append(
            f"Round to {transform.decimal_places} decimal places using {transform.rounding_mode}."
        )
    validation = field.validation
    checks = {
        key: value
        for key, value in (
            ("exact length", validation.exact_length),
            ("segment location", validation.segment_location),
            ("segment length", validation.segment_length),
            ("character class", validation.character_class),
            ("pattern", validation.pattern),
        )
        if value not in (None, "", "none")
    }
    if checks:
        steps.append("Validate the result: " + literal(checks) + ".")
    if field.validate_only:
        steps.append("Validate this field only; do not send its value to Odoo.")
    return tuple(steps)
