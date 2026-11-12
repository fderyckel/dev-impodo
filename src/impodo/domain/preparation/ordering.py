"""Compute the bounded Python ordering oracle without changing source records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import cmp_to_key
from typing import Any

from impodo.domain.mapping.generated_ordering import GeneratedOrdering
from impodo.domain.preparation.canonical import ValueParseError, parse_value
from impodo.domain.recipe.profile import NormalizationSpec
from impodo.domain.shared.models import InvalidPreparedValue


@dataclass(frozen=True, slots=True)
class GeneratedOrderValues:
    """Keep only a generated integer or a row-local correction for each row."""

    values: Mapping[int, int | InvalidPreparedValue]


def generate_ordering(policy: GeneratedOrdering, rows: Iterable[tuple[int, Mapping[str, Any]]]) -> GeneratedOrderValues:
    """Rank rows within reviewed groups using typed keys and physical-row ties."""

    groups: dict[tuple[str, ...], list[tuple[int, tuple[Any, ...]]]] = defaultdict(list)
    errors: dict[int, InvalidPreparedValue] = {}
    normalization = NormalizationSpec(trim=True, empty_as_null=True)
    for number, values in rows:
        group = tuple("" if values.get(key) is None else str(values[key]).strip() for key in policy.group_column_keys)
        sort_values = []
        for key, value_type in zip(policy.sort_column_keys, policy.sort_value_types, strict=True):
            try:
                if value_type in {"integer", "decimal"} and any(ord(character) > 127 for character in str(values.get(key, ""))):
                    raise ValueParseError("Numeric ordering needs invariant digits")
                value = parse_value(values.get(key), value_type, normalization, required=False)
                if isinstance(value, Decimal):
                    exponent = value.as_tuple().exponent
                    if not value.is_finite() or not isinstance(exponent, int) or abs(exponent) > 2_147_483_647:
                        raise ValueParseError("Numeric ordering needs a finite supported exponent")
            except ValueParseError:
                value = None
                errors[number] = InvalidPreparedValue("SOURCE_ORDERING_VALUE_INVALID", f"The ordering value in {key} cannot be read as {value_type}. Review that column or its ordering type.")
            sort_values.append(value)
        groups[group].append((number, tuple(sort_values)))

    def compare(left, right):
        for first, second in zip(left[1], right[1], strict=True):
            if first is None or second is None:
                result = 0 if first is second else (-1 if first is None else 1) * (1 if policy.blanks == "first" else -1)
            else:
                result = (first > second) - (first < second)
                if policy.descending:
                    result = -result
            if result:
                return result
        return (left[0] > right[0]) - (left[0] < right[0])

    output: dict[int, int | InvalidPreparedValue] = {}
    for entries in groups.values():
        for ordinal, (number, _) in enumerate(sorted(entries, key=cmp_to_key(compare))):
            value = policy.start + ordinal * policy.step
            output[number] = errors.get(number) or (
                value if value <= 2_147_483_647 else InvalidPreparedValue("SOURCE_ORDERING_VALUE_INVALID", "The generated ordering exceeds Odoo's whole-number range. Choose a smaller start or step.")
            )
    return GeneratedOrderValues(output)
