"""Validate fresh control totals against the exact reusable Recipe rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from impodo.domain.recipe_applications import RecipeApplicationError


def normalize_control_total(value: object, *, label: str) -> str:
    """Keep finite decimal totals exact and bound their stored representation."""

    raw = str(value).strip()
    if not raw:
        raise RecipeApplicationError(f"Enter {label}")
    if len(raw) > 1000:
        raise RecipeApplicationError(f"{label} is too large")
    try:
        number = Decimal(raw)
    except InvalidOperation as error:
        raise RecipeApplicationError(f"{label} must be a number") from error
    if not number.is_finite():
        raise RecipeApplicationError(f"{label} must be a finite number")
    if abs(number.adjusted()) > 1000 or abs(number.as_tuple().exponent) > 1000:
        raise RecipeApplicationError(f"{label} is too large or too precise")
    return format(number, "f")


def normalize_recipe_control_values(
    definitions: Sequence[Mapping[str, object]],
    supplied: Mapping[str, object],
    *,
    require_all: bool = False,
) -> dict[str, str]:
    """Supply invariant totals from the Recipe and validate delivery totals."""

    expected = {str(item["logical_control_id"]): item for item in definitions}
    unknown = sorted(set(supplied) - set(expected))
    if unknown:
        raise RecipeApplicationError(f"Control {unknown[0]} is not declared by this Recipe")
    result = {}
    for logical_id, definition in expected.items():
        label = str(definition.get("name", logical_id))
        if definition.get("invariant_expectation"):
            total = normalize_control_total(definition["invariant_expected_total"], label=label)
            if logical_id in supplied and Decimal(
                normalize_control_total(supplied[logical_id], label=label)
            ) != Decimal(total):
                raise RecipeApplicationError(f"{label} is fixed by the selected Recipe")
        else:
            raw = supplied.get(logical_id, "")
            if not require_all and not str(raw).strip():
                continue
            total = normalize_control_total(raw, label=label)
        result[logical_id] = total
    return result
