"""Canonical scalar evaluation shared by preview and staging."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Mapping

from ...value_rules import (
    ScalarRuleError,
    ScalarTransformPolicy,
    prepare_rule_text,
    round_decimal_value,
    validate_scalar_value,
)
from .contracts import ScalarFieldMapping, ScalarValueSource


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
    find_replace_observer: Callable[[bool, bool], None] | None = None,
    text_step_observer: Callable[[int, bool, bool], None] | None = None,
) -> str | int | Decimal | bool | date | datetime | None:
    """Evaluate one scalar through the shared preview/runtime boundary."""

    return canonicalize_scalar_value(
        mapping,
        raw_source_value,
        formula_context={
            "value": raw_source_value,
            **{
                f"column_{ordinal}": value
                for ordinal, value in sorted(
                    (source_values_by_ordinal or {}).items()
                )
            },
        },
        find_replace_observer=find_replace_observer,
        text_step_observer=text_step_observer,
    )


def canonicalize_scalar_value(
    mapping: ScalarFieldMapping,
    raw_source_value: Any,
    *,
    formula_context: Mapping[str, Any] | None = None,
    find_replace_observer: Callable[[bool, bool], None] | None = None,
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
                find_replace_observer=find_replace_observer,
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
