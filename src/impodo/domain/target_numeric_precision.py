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

        if not self._groups:
            return None
        (model, field_name, digits), group = sorted(
            self._groups.items(), key=lambda item: (-item[1].count, item[0])
        )[0]
        guidance = (
            f" These values need at least {group.required_places} decimal places."
            if group.required_places > digits[1]
            else " These values exceed the target numeric precision."
        )
        zero_warning = (
            f" Rounding to {digits[1]} places would turn "
            f"{group.zeroed_positive_count:,} positive value(s) into zero."
            if group.zeroed_positive_count
            else ""
        )
        return (
            f"{model}.{field_name}: {group.count:,} prepared value(s) cannot be "
            f"represented at Odoo precision ({digits[0]}, {digits[1]}) without "
            f"changing them.{guidance}{zero_warning} Change the Odoo precision "
            "or approve an explicit rounding or conversion rule, then compare again. "
            f"Examples: {', '.join(group.examples)}. "
            "Support code: TARGET_NUMERIC_PRECISION_LOSS."
        )
