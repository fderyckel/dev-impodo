"""Prepare exact related-record references without changing source evidence.

Each output belongs to one related matching component. Formula names retain
their stable source bindings when a Recipe is applied to another delivery.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from impodo.domain.recipe.value_rules import (
    FormulaValidationError,
    evaluate_reference_formula,
    formula_input_names,
)


@dataclass(frozen=True, slots=True)
class ReferencePart:
    """One ordered source value or fixed text in a combined reference."""

    source_column_key: str | None = None
    literal: str | None = None

    def __post_init__(self) -> None:
        if (self.source_column_key is None) == (self.literal is None):
            raise ValueError(
                "Choose a source column or fixed text for each reference part"
            )
        if self.source_column_key is not None and (
            not self.source_column_key or len(self.source_column_key) > 500
        ):
            raise ValueError("Reference source column is invalid")
        if self.literal is not None and len(self.literal) > 1_000:
            raise ValueError("Reference fixed text is too long")


@dataclass(frozen=True, slots=True)
class ReferencePreparation:
    """One safe formula or ordered combination for a related matching value."""

    formula: str = ""
    bindings: tuple[tuple[str, str], ...] = ()
    parts: tuple[ReferencePart, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "bindings", tuple(tuple(item) for item in self.bindings)
        )
        object.__setattr__(self, "parts", tuple(self.parts))
        if self.formula.strip() and self.parts:
            raise ValueError("Choose a formula or combined parts for this reference")
        if (
            len(self.formula) > 1_000
            or len(self.parts) > 20
            or len(self.bindings) > 200
        ):
            raise ValueError("Reference preparation exceeds its supported size")
        names = tuple(name for name, _key in self.bindings)
        if len(set(names)) != len(names) or any(
            not name.startswith("column_")
            or not name[7:].isdigit()
            or int(name[7:]) < 1
            or not key
            or len(key) > 500
            for name, key in self.bindings
        ):
            raise ValueError("Reference formula source bindings are invalid")
        if self.parts and self.bindings:
            raise ValueError("Combined reference parts cannot carry formula bindings")


class PreparedReferenceMapping(Protocol):
    """Matching input shape shared without importing the containing contracts."""

    @property
    def source_column_key(self) -> str: ...

    @property
    def preparation(self) -> ReferencePreparation | None: ...


class ReferencePreparationError(ValueError):
    """A row cannot supply a complete, valid related matching value."""


def validate_reference_preparation(mapping: PreparedReferenceMapping) -> None:
    """Validate an authored reference without losing a recoverable draft."""

    preparation = mapping.preparation
    if preparation is None or preparation.parts:
        return
    names = {name for name, _key in preparation.bindings}
    used = formula_input_names(preparation.formula, allowed_names={"value", *names})
    if names != set(used) - {"value"}:
        raise ValueError("Bind every referenced source column once")


def preparation_source_keys(mapping: PreparedReferenceMapping) -> tuple[str, ...]:
    """Return original source dependencies in deterministic first-use order."""

    preparation = mapping.preparation
    if preparation is None:
        return (mapping.source_column_key,)
    if preparation.parts:
        keys = tuple(
            part.source_column_key
            for part in preparation.parts
            if part.source_column_key is not None
        )
    else:
        try:
            used = formula_input_names(
                preparation.formula,
                allowed_names={"value", *(name for name, _ in preparation.bindings)},
            )
        except FormulaValidationError:
            used = ("value",)
        keys = (
            *((mapping.source_column_key,) if "value" in used else ()),
            *(key for _name, key in preparation.bindings),
        )
    return tuple(dict.fromkeys(keys))


def prepare_reference_value(
    mapping: PreparedReferenceMapping,
    source_values: Mapping[str, object],
) -> object:
    """Evaluate one reference, preserving identifiers and rejecting partial paths.

    All-empty inputs remain empty for the owning relationship's blank policy.
    Mixed populated and blank inputs fail rather than matching a partial key.
    """

    preparation = mapping.preparation
    if preparation is None:
        return source_values.get(mapping.source_column_key)
    try:
        validate_reference_preparation(mapping)
    except (ValueError, TypeError) as error:
        raise ReferencePreparationError(str(error)) from error
    keys = preparation_source_keys(mapping)
    blanks = tuple(
        source_values.get(key) is None or str(source_values.get(key)).strip() == ""
        for key in keys
    )
    if blanks and all(blanks):
        return None
    if any(blanks):
        raise ReferencePreparationError(
            "A source value needed for this reference is blank"
        )
    try:
        if preparation.parts:
            result = "".join(
                str(source_values[part.source_column_key])
                if part.source_column_key is not None
                else part.literal or ""
                for part in preparation.parts
            )
        else:
            result = evaluate_reference_formula(
                preparation.formula,
                {
                    "value": source_values.get(mapping.source_column_key),
                    **{
                        name: source_values.get(key)
                        for name, key in preparation.bindings
                    },
                },
            )
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        raise ReferencePreparationError(str(error)) from error
    if not result.strip():
        raise ReferencePreparationError("The prepared reference is blank")
    if len(result) > 10_000:
        raise ReferencePreparationError("The prepared reference is too long")
    return result
