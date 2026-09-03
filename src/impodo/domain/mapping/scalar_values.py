"""Canonical scalar evaluation shared by preview and staging."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping

from impodo.domain.recipe.value_rules import (
    MAX_RULE_OUTPUT_LENGTH,
    ScalarRuleError,
    ScalarTransformPolicy,
    prepare_rule_text,
    round_decimal_value,
    validate_scalar_value,
)
from .contracts import (
    ConcatenationBlankHandling,
    ScalarFieldMapping,
    ScalarValueSource,
)
from .contracts import (
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRuleJoin,
)


_DECIMAL_LOCALES = frozenset({"invariant", "en_US", "de_DE", "fr_FR"})
_DATE_FORMATS = {
    "iso": "%Y-%m-%d",
    "dmy_slash": "%d/%m/%Y",
    "mdy_slash": "%m/%d/%Y",
    "dmy_dot": "%d.%m.%Y",
}
_DATETIME_FORMATS = {
    "iso": None,
    "dmy_slash": "%d/%m/%Y %H:%M:%S",
    "mdy_slash": "%m/%d/%Y %H:%M:%S",
    "dmy_dot": "%d.%m.%Y %H:%M:%S",
}


class ScalarValueError(ValueError):
    """Raised when a governed scalar value cannot be canonicalized."""


class ScalarValueRuleError(ScalarValueError):
    """Raised when a row fails one configured transformation or check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evaluate_scalar_mapping_value(
    mapping: ScalarFieldMapping,
    raw_source_value: Any,
    *,
    source_values_by_ordinal: Mapping[int, Any] | None = None,
    source_values_by_key: Mapping[str, Any] | None = None,
    text_step_observer: Callable[[int, bool, bool], None] | None = None,
    selection_rule_observer: Callable[[int, bool, bool, bool], None]
    | None = None,
) -> str | int | Decimal | bool | date | datetime | None:
    """Evaluate one scalar through the shared preview/runtime boundary."""

    selected_value = raw_source_value
    if mapping.value_source is ScalarValueSource.CONDITIONAL_RULES:
        selected_value = _evaluate_selection_rules(
            mapping,
            source_values_by_key or {},
            observer=selection_rule_observer,
        )
    elif mapping.value_source is ScalarValueSource.CONCATENATE:
        selected_value = _concatenate_source_values(
            mapping,
            source_values_by_key or {},
        )
    return canonicalize_scalar_value(
        mapping,
        selected_value,
        formula_context={
            "value": selected_value,
            **{
                f"column_{ordinal}": value
                for ordinal, value in sorted(
                    (source_values_by_ordinal or {}).items()
                )
            },
        },
        text_step_observer=text_step_observer,
    )


def _concatenate_source_values(
    mapping: ScalarFieldMapping,
    source_values_by_key: Mapping[str, Any],
) -> str | None:
    """Evaluate one guided ordered concatenation before later text rules."""

    rule = mapping.concatenation
    if rule is None:
        raise ScalarValueRuleError(
            "SOURCE_CONCATENATION_INVALID",
            "The source-column combination is incomplete",
        )
    parts: list[str] = []
    for source_column_key in rule.source_column_keys:
        raw = source_values_by_key.get(source_column_key)
        rendered = "" if raw is None else str(raw)
        if rule.trim_parts:
            rendered = rendered.strip()
        if not rendered.strip():
            if rule.blank_handling is ConcatenationBlankHandling.BLOCK_ROW:
                raise ScalarValueRuleError(
                    "SOURCE_CONCATENATION_PART_BLANK",
                    "A required part of the combined value is blank",
                )
            continue
        parts.append(rendered)
    if not parts:
        return None
    combined = rule.separator.join(parts)
    if len(combined) > MAX_RULE_OUTPUT_LENGTH:
        raise ScalarValueRuleError(
            "SOURCE_RULE_OUTPUT_TOO_LONG",
            (
                "A value rule produced more than "
                f"{MAX_RULE_OUTPUT_LENGTH} characters"
            ),
        )
    return combined


def canonicalize_scalar_value(
    mapping: ScalarFieldMapping,
    raw_source_value: Any,
    *,
    formula_context: Mapping[str, Any] | None = None,
    text_step_observer: Callable[[int, bool, bool], None] | None = None,
) -> str | int | Decimal | bool | date | datetime | None:
    """Apply one browser-authored value provider and transformation policy."""

    if mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
        raise ScalarValueError("Odoo-default fields have no local proposed value")
    source_choice = (
        str(raw_source_value).strip()
        if raw_source_value is not None
        else None
    )
    matched_value = next(
        (
            item.target_value
            for item in mapping.value_mappings
            if item.source_value == source_choice
        ),
        None,
    )
    if matched_value is not None:
        prepared = matched_value
    else:
        if mapping.value_source is ScalarValueSource.CONSTANT:
            raw_value = mapping.literal_value
        elif mapping.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK:
            prepared_source = _transform_scalar_text(
                raw_source_value,
                mapping.transform,
            )
            raw_value = (
                mapping.literal_value
                if prepared_source is None
                else prepared_source
            )
        else:
            raw_value = raw_source_value

        try:
            rule_context = dict(formula_context or {})
            rule_context["value"] = raw_value
            prepared = prepare_rule_text(
                raw_value,
                mapping.transform,
                formula_context=rule_context,
                text_step_observer=text_step_observer,
            )
        except ScalarRuleError as error:
            raise ScalarValueRuleError(error.code, str(error)) from error
        if prepared is None:
            if mapping.required:
                raise ScalarValueError(
                    "Required value is empty after transformation"
                )
            return None

    try:
        if mapping.value_type == "string":
            result: Any = prepared
        elif mapping.value_type == "integer":
            if not re.fullmatch(r"[+-]?\d+", prepared):
                raise ValueError
            result = int(prepared, 10)
        elif mapping.value_type == "decimal":
            result = _parse_decimal(prepared, mapping.transform.decimal_locale)
        elif mapping.value_type == "boolean":
            token = prepared.casefold()
            if token in {"true", "1", "yes", "y"}:
                result = True
            elif token in {"false", "0", "no", "n"}:
                result = False
            else:
                raise ValueError
        elif mapping.value_type == "date":
            result = datetime.strptime(
                prepared,
                _DATE_FORMATS[mapping.transform.date_format],
            ).date()
        elif mapping.value_type == "datetime":
            date_format = _DATETIME_FORMATS[mapping.transform.date_format]
            parsed = (
                datetime.fromisoformat(prepared.replace("Z", "+00:00"))
                if date_format is None
                else datetime.strptime(prepared, date_format)
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.astimezone(timezone.utc)
        else:
            raise ScalarValueError(
                f"Unsupported canonical value type {mapping.value_type!r}."
            )
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ScalarValueError(
            f"Cannot parse {prepared!r} as {mapping.value_type}."
        ) from error
    try:
        if isinstance(result, Decimal):
            result = round_decimal_value(result, mapping.transform)
        validate_scalar_value(result, mapping.validation)
    except ScalarRuleError as error:
        raise ScalarValueRuleError(error.code, str(error)) from error
    return result


def _evaluate_selection_rules(
    mapping: ScalarFieldMapping,
    source_values_by_key: Mapping[str, Any],
    *,
    observer: Callable[[int, bool, bool, bool], None] | None = None,
) -> str:
    """Return the first matching portable Odoo key or block the row."""

    rule_set = mapping.selection_rules
    if rule_set is None:
        raise ScalarValueRuleError(
            "SOURCE_SELECTION_RULE_INVALID",
            "Conditional Selection rules are missing.",
        )
    matches_by_rule: list[bool] = []
    for rule in rule_set.rules:
        matches = tuple(
            _selection_condition_matches(
                condition,
                source_values_by_key.get(condition.source_column_key),
            )
            for condition in rule.conditions
        )
        rule_matches = (
            all(matches)
            if rule.join is SelectionRuleJoin.ALL
            else any(matches)
        )
        matches_by_rule.append(rule_matches)
    selected_index = next(
        (index for index, matched in enumerate(matches_by_rule) if matched),
        None,
    )
    overlapping = sum(matches_by_rule) > 1
    if observer is not None:
        for index, matched in enumerate(matches_by_rule):
            observer(
                index,
                matched,
                index == selected_index,
                matched and overlapping,
            )
    if selected_index is not None:
        return rule_set.rules[selected_index].target_value
    if rule_set.otherwise_value is not None:
        return rule_set.otherwise_value
    raise ScalarValueRuleError(
        "SOURCE_SELECTION_RULE_UNRESOLVED",
        "No Selection rule matched this row and no otherwise choice was set.",
    )


def _selection_condition_matches(
    condition: SelectionCondition,
    raw_value: Any,
) -> bool:
    operator = condition.operator
    blank = raw_value is None or str(raw_value).strip() == ""
    if operator is SelectionConditionOperator.IS_BLANK:
        return blank
    if operator is SelectionConditionOperator.IS_NOT_BLANK:
        return not blank
    if operator is SelectionConditionOperator.IS_TRUE:
        parsed_boolean = _selection_boolean(raw_value)
        if not blank and parsed_boolean is None:
            raise ScalarValueRuleError(
                "SOURCE_SELECTION_RULE_SOURCE_INVALID",
                "A source value could not be read as yes or no.",
            )
        return parsed_boolean is True
    if operator is SelectionConditionOperator.IS_FALSE:
        parsed_boolean = _selection_boolean(raw_value)
        if not blank and parsed_boolean is None:
            raise ScalarValueRuleError(
                "SOURCE_SELECTION_RULE_SOURCE_INVALID",
                "A source value could not be read as yes or no.",
            )
        return parsed_boolean is False
    if blank:
        return False

    comparison = condition.comparison_value
    if comparison is None:
        return False
    if condition.value_type == "string":
        left = str(raw_value)
        right = comparison
    else:
        try:
            left = _selection_typed_value(raw_value, condition.value_type)
            right = _selection_typed_value(comparison, condition.value_type)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ScalarValueRuleError(
                "SOURCE_SELECTION_RULE_SOURCE_INVALID",
                "A source value does not match the rule's comparison type.",
            ) from error

    if operator is SelectionConditionOperator.EQUALS:
        return left == right
    if operator is SelectionConditionOperator.NOT_EQUALS:
        return left != right
    if operator is SelectionConditionOperator.EQUALS_IGNORE_CASE:
        return str(left).lower() == str(right).lower()
    if operator is SelectionConditionOperator.CONTAINS:
        return str(right) in str(left)
    if operator is SelectionConditionOperator.STARTS_WITH:
        return str(left).startswith(str(right))
    if operator is SelectionConditionOperator.ENDS_WITH:
        return str(left).endswith(str(right))
    if operator is SelectionConditionOperator.LESS_THAN:
        return left < right
    if operator is SelectionConditionOperator.LESS_THAN_OR_EQUAL:
        return left <= right
    if operator is SelectionConditionOperator.GREATER_THAN:
        return left > right
    if operator is SelectionConditionOperator.GREATER_THAN_OR_EQUAL:
        return left >= right
    return False


def _selection_typed_value(value: Any, value_type: str) -> Any:
    text = str(value).strip()
    if value_type == "integer":
        return int(text, 10)
    if value_type == "decimal":
        parsed = Decimal(text)
        if (
            not parsed.is_finite()
            or len(parsed.as_tuple().digits) > 38
            or max(-parsed.as_tuple().exponent, 0) > 12
        ):
            raise ValueError("Decimal comparison exceeds 38 digits or 12 places")
        return parsed
    if value_type == "boolean":
        parsed = _selection_boolean(value)
        if parsed is None:
            raise ValueError("Not a boolean")
        return parsed
    if value_type == "date":
        return date.fromisoformat(text)
    if value_type == "datetime":
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return text


def _selection_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = str(value).strip().casefold() if value is not None else ""
    if token in {"true", "1", "yes", "y"}:
        return True
    if token in {"false", "0", "no", "n"}:
        return False
    return None


def _transform_scalar_text(
    raw_value: Any,
    policy: ScalarTransformPolicy,
) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value)
    if policy.trim:
        value = value.strip()
    if policy.collapse_whitespace:
        value = re.sub(r"\s+", " ", value)
    if policy.case_mode == "uppercase":
        value = value.upper()
    elif policy.case_mode == "lowercase":
        value = value.lower()
    elif policy.case_mode == "sentence":
        value = next(
            (
                f"{value[:index]}{character.upper()}{value[index + 1:]}"
                for index, character in enumerate(value)
                if character.isalpha()
            ),
            value,
        )
    elif policy.case_mode == "title":
        value = value.title()
    if policy.empty_as_null and value == "":
        return None
    return value


def _parse_decimal(value: str, locale: str) -> Decimal:
    patterns = {
        "invariant": (r"[+-]?\d+(?:\.\d+)?", "", "."),
        "en_US": (
            r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?",
            ",",
            ".",
        ),
        "de_DE": (
            r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?",
            ".",
            ",",
        ),
        "fr_FR": (
            r"[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:,\d+)?",
            " ",
            ",",
        ),
    }
    pattern, grouping, decimal_separator = patterns[locale]
    if re.fullmatch(pattern, value) is None:
        raise ValueError
    normalized = value
    if locale == "fr_FR":
        normalized = re.sub(r"[ \u00a0\u202f]", "", normalized)
    elif grouping:
        normalized = normalized.replace(grouping, "")
    if decimal_separator != ".":
        normalized = normalized.replace(decimal_separator, ".")
    return Decimal(normalized)
