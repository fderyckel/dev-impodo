"""Bounded, target-specific checks for numeric values prepared for Odoo."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def unrepresentable_decimal(
    value: object,
    digits: tuple[int, int],
) -> Decimal | None:
    """Return a value if the captured Odoo precision would alter or overflow it."""

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite():
        return number
    precision, scale = digits
    if required_decimal_places(number) > scale:
        return number
    integral_digits = max(0, number.adjusted() + 1) if number else 0
    if integral_digits > precision - scale:
        return number
    return None


def required_decimal_places(value: Decimal) -> int:
    """Count significant fractional places after removing trailing zeros."""

    if not value.is_finite():
        return 0
    parts = value.as_tuple()
    if not any(parts.digits):
        return 0
    assert isinstance(parts.exponent, int)
    trailing_zeros = 0
    for digit in reversed(parts.digits):
        if digit != 0:
            break
        trailing_zeros += 1
    return max(0, -parts.exponent - trailing_zeros)


@dataclass(slots=True)
class _PrecisionLoss:
    count: int = 0
    zeroed_positive_count: int = 0
    required_places: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TargetNumericPrecisionSummary:
    """Expose aggregate precision evidence without retaining every value."""

    model: str
    field_name: str
    digits: tuple[int, int]
    count: int
    zeroed_positive_count: int
    required_places: int
    examples: tuple[str, ...]

    def message(self) -> str:
        """Render the authoritative pre-load precision explanation."""

        guidance = (
            f" These values need at least {self.required_places} decimal places."
            if self.required_places > self.digits[1]
            else " These values exceed the target numeric precision."
        )
        zero_warning = (
            f" Rounding to {self.digits[1]} places would turn "
            f"{self.zeroed_positive_count:,} positive value(s) into zero."
            if self.zeroed_positive_count
            else ""
        )
        return (
            f"{self.model}.{self.field_name}: {self.count:,} prepared value(s) "
            f"cannot be represented at Odoo precision "
            f"({self.digits[0]}, {self.digits[1]}) without changing them."
            f"{guidance}{zero_warning} Change the Odoo precision or approve an "
            "explicit rounding or conversion rule, then compare again. "
            f"Examples: {', '.join(self.examples)}. "
            "Support code: TARGET_NUMERIC_PRECISION_LOSS."
        )


class TargetNumericPrecisionLosses:
    """Keep compact evidence for every affected model and field."""

    def __init__(self) -> None:
        self._groups: dict[tuple[str, str, tuple[int, int]], _PrecisionLoss] = {}

    def add(
        self,
        model: str,
        field_name: str,
        digits: tuple[int, int],
        value: object,
    ) -> None:
        number = unrepresentable_decimal(value, digits)
        if number is None:
            return
        group = self._groups.setdefault((model, field_name, digits), _PrecisionLoss())
        group.count += 1
        if len(group.examples) < 3:
            group.examples.append(str(number))
        if number.is_finite():
            group.required_places = max(group.required_places, required_decimal_places(number))
            if number > 0 and number < Decimal(5).scaleb(-digits[1] - 1):
                group.zeroed_positive_count += 1

    def first_message(self) -> str | None:
        """Explain the largest affected field without retaining every value."""

        summary = self.first_summary()
        return summary.message() if summary is not None else None

    def first_summary(self) -> TargetNumericPrecisionSummary | None:
        """Return the largest deterministic aggregate for early-stage warnings."""

        if not self._groups:
            return None
        (model, field_name, digits), group = min(self._groups.items(), key=lambda item: (-item[1].count, item[0]))
        return TargetNumericPrecisionSummary(
            model=model,
            field_name=field_name,
            digits=digits,
            count=group.count,
            zeroed_positive_count=group.zeroed_positive_count,
            required_places=group.required_places,
            examples=tuple(group.examples),
        )
