"""Pure typed comparisons shared by source-driven mapping decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import SelectionConditionOperator


class SourceConditionValueError(ValueError):
    """Raised when a source or comparison value cannot be read safely."""

    def __init__(
        self,
        message: str,
        *,
        source_column_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.source_column_key = source_column_key


@dataclass(frozen=True, slots=True)
class SourceConditionConfigurationProblem:
    """One operator or literal problem suitable for a mapping issue."""

    kind: str
    message: str
    remediation: str


def source_condition_configuration_problems(
    *,
    operator: SelectionConditionOperator,
    comparison_value: str | None,
    value_type: str,
) -> tuple[SourceConditionConfigurationProblem, ...]:
    """Return every semantic problem in one portable source condition."""

    operator = SelectionConditionOperator(operator)
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
    problems: list[SourceConditionConfigurationProblem] = []
    if operator in text_only and value_type != "string":
        problems.append(
            SourceConditionConfigurationProblem(
                kind="operator",
                message="This comparison is only available for text values.",
                remediation=(
                    "Choose a text comparison or change the comparison type."
                ),
            )
        )
    if operator in ordered and value_type not in {
        "integer",
        "decimal",
        "date",
        "datetime",
    }:
        problems.append(
            SourceConditionConfigurationProblem(
                kind="operator",
                message="This ordered comparison requires a number or date.",
                remediation="Choose the matching comparison type.",
            )
        )
    if operator in boolean_only and value_type != "boolean":
        problems.append(
            SourceConditionConfigurationProblem(
                kind="operator",
                message=(
                    "True and false comparisons require a yes/no source value."
                ),
                remediation="Choose the yes/no comparison type.",
            )
        )
    if value_type == "boolean" and operator not in {
        SelectionConditionOperator.IS_BLANK,
        SelectionConditionOperator.IS_NOT_BLANK,
        *boolean_only,
    }:
        problems.append(
            SourceConditionConfigurationProblem(
                kind="operator",
                message=(
                    "A yes/no source value requires a yes, no, or blank comparison."
                ),
                remediation="Choose a yes/no comparison.",
            )
        )
    if comparison_value is not None:
        try:
            parse_source_condition_value(comparison_value, value_type)
        except (InvalidOperation, TypeError, ValueError):
            problems.append(
                SourceConditionConfigurationProblem(
                    kind="value",
                    message=(
                        "The comparison value does not match its selected type."
                    ),
                    remediation=(
                        "Correct the value or choose another comparison type."
                    ),
                )
            )
    return tuple(problems)


def source_condition_matches(
    *,
    raw_value: Any,
    operator: SelectionConditionOperator,
    comparison_value: str | None,
    value_type: str,
) -> bool:
    """Evaluate one bounded condition without persistence or external access."""

    operator = SelectionConditionOperator(operator)
    blank = raw_value is None or str(raw_value).strip() == ""
    if operator is SelectionConditionOperator.IS_BLANK:
        return blank
    if operator is SelectionConditionOperator.IS_NOT_BLANK:
        return not blank
    if operator is SelectionConditionOperator.IS_TRUE:
        parsed_boolean = parse_source_boolean(raw_value)
        if not blank and parsed_boolean is None:
            raise SourceConditionValueError(
                "A source value could not be read as yes or no."
            )
        return parsed_boolean is True
    if operator is SelectionConditionOperator.IS_FALSE:
        parsed_boolean = parse_source_boolean(raw_value)
        if not blank and parsed_boolean is None:
            raise SourceConditionValueError(
                "A source value could not be read as yes or no."
            )
        return parsed_boolean is False
    if blank:
        return False
    if comparison_value is None:
        return False

    if value_type == "string":
        left = str(raw_value)
        right = comparison_value
    else:
        try:
            left = parse_source_condition_value(raw_value, value_type)
            right = parse_source_condition_value(comparison_value, value_type)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise SourceConditionValueError(
                "A source value does not match the rule's comparison type."
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


def parse_source_condition_value(value: Any, value_type: str) -> Any:
    """Parse one condition operand using deterministic portable types."""

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
        parsed = parse_source_boolean(value)
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
    if value_type == "string":
        return str(value)
    raise ValueError("Unsupported source comparison type")


def parse_source_boolean(value: Any) -> bool | None:
    """Return one bounded Boolean interpretation or ``None`` when unknown."""

    if isinstance(value, bool):
        return value
    token = str(value).strip().casefold() if value is not None else ""
    if token in {"true", "1", "yes", "y"}:
        return True
    if token in {"false", "0", "no", "n"}:
        return False
    return None
