"""Canonical source and target value parsing."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any

from .profile import FieldSpec, NormalizationSpec, ScalarType


TRUE_TOKENS = {"true", "1", "yes", "y"}
FALSE_TOKENS = {"false", "0", "no", "n"}


class ValueParseError(ValueError):
    pass


def normalize_string(value: str, policy: NormalizationSpec) -> str | None:
    normalized = value
    if policy.trim:
        normalized = normalized.strip()
    if policy.collapse_whitespace:
        normalized = re.sub(r"\s+", " ", normalized)
    if policy.casefold:
        normalized = normalized.casefold()
    if policy.empty_as_null and normalized == "":
        return None
    return normalized


def parse_value(
    raw: Any,
    value_type: ScalarType,
    normalization: NormalizationSpec,
    *,
    required: bool = False,
) -> str | int | Decimal | bool | date | datetime | None:
    if raw is None:
        value: Any = None
    elif isinstance(raw, str):
        value = normalize_string(raw, normalization)
    else:
        value = raw

    if value is None:
        if required:
            raise ValueParseError("required value is empty")
        return None

    try:
        if value_type == "string":
            return normalize_string(str(value), normalization)
        if value_type == "integer":
            if isinstance(value, bool):
                raise ValueError
            return int(str(value), 10)
        if value_type == "decimal":
            decimal_value = Decimal(str(value))
            if normalization.decimal_places is not None:
                quantum = Decimal(1).scaleb(-normalization.decimal_places)
                decimal_value = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
            return decimal_value
        if value_type == "boolean":
            if isinstance(value, bool):
                return value
            token = str(value).strip().casefold()
            if token in TRUE_TOKENS:
                return True
            if token in FALSE_TOKENS:
                return False
            raise ValueError
        if value_type == "date":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value))
        if value_type == "datetime":
            if isinstance(value, datetime):
                parsed = value
            else:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Version 2 supports UTC source timestamps. Other profile
                # timezones are validated but intentionally not guessed.
                if normalization.timezone != "UTC":
                    raise ValueParseError(
                        "naive datetime requires UTC in version 2"
                    )
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValueParseError(
            f"cannot parse {raw!r} as {value_type}"
        ) from exc
    raise ValueParseError(f"unsupported value type {value_type!r}")


def parse_field(raw: Any, spec: FieldSpec) -> Any:
    return parse_value(
        raw,
        spec.type,
        spec.normalize,
        required=spec.required,
    )


def values_equal(source: Any, target: Any, null_policy: str) -> bool:
    if null_policy == "ignore_source_null" and source is None:
        return True
    if null_policy == "equivalent":
        source = None if source == "" else source
        target = None if target == "" else target
    return source == target

