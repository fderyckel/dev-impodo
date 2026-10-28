"""Bounded, target-specific checks for numeric values prepared for Odoo."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def _validate_digits(digits: tuple[int, int]) -> None:
    precision, scale = digits
    if (
        type(precision) is not int
        or type(scale) is not int
        or precision < 1
        or scale < 0
        or scale > precision
    ):
        raise ValueError("Odoo numeric precision is invalid")


def unrepresentable_decimal(
    value: object,
    digits: tuple[int, int],
) -> Decimal | None:
    """Return a value if the captured Odoo precision would alter or overflow it."""

    _validate_digits(digits)
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
    if not isinstance(parts.exponent, int):
        raise TypeError("A finite Decimal must have an integer exponent")
    trailing_zeros = 0
    for digit in reversed(parts.digits):
        if digit != 0:
            break
        trailing_zeros += 1
    return max(0, -parts.exponent - trailing_zeros)


@dataclass(frozen=True, slots=True)
class PreparedNumericField:
    """One mapped prepared field with captured Odoo numeric capacity."""

    dataset: str
    target_model: str
    field_name: str
    digits: tuple[int, int]

    def __post_init__(self) -> None:
        if not self.dataset or not self.target_model or not self.field_name:
            raise ValueError("Prepared numeric fields require table and target names")
        _validate_digits(self.digits)


@dataclass(frozen=True, slots=True)
class PreparedNumericPrecisionFinding:
    """Value-free evidence that one exact prepared number exceeds capacity."""

    row_id: str
    dataset: str
    source_row: int
    target_model: str
    field_name: str
    digits: tuple[int, int]
    required_decimal_places: int
    integral_overflow: bool
    zeroed_positive: bool

    def __post_init__(self) -> None:
        if (
            not self.row_id
            or not self.dataset
            or not self.target_model
            or not self.field_name
            or self.source_row < 1
            or self.required_decimal_places < 0
        ):
            raise ValueError("Prepared numeric precision evidence is invalid")
        _validate_digits(self.digits)


def prepared_numeric_precision_finding(
    *,
    row_id: str,
    dataset: str,
    source_row: int,
    target_model: str,
    field_name: str,
    digits: tuple[int, int],
    value: object,
) -> PreparedNumericPrecisionFinding | None:
    """Classify one exact prepared number without retaining its value."""

    number = unrepresentable_decimal(value, digits)
    if number is None:
        return None
    required_places = required_decimal_places(number)
    integral_digits = (
        max(0, number.adjusted() + 1)
        if number.is_finite() and number
        else 0
    )
    return PreparedNumericPrecisionFinding(
        row_id=row_id,
        dataset=dataset,
        source_row=source_row,
        target_model=target_model,
        field_name=field_name,
        digits=digits,
        required_decimal_places=required_places,
        integral_overflow=(
            not number.is_finite()
            or integral_digits > digits[0] - digits[1]
        ),
        zeroed_positive=(
            number.is_finite()
            and number > 0
            and number < Decimal(5).scaleb(-digits[1] - 1)
        ),
    )


def prepared_numeric_precision_message(
    finding: PreparedNumericPrecisionFinding,
) -> str:
    """Explain captured capacity without persisting or displaying the value."""

    precision, scale = finding.digits
    if finding.required_decimal_places > scale:
        detail = (
            f" It needs at least {finding.required_decimal_places} decimal places."
        )
    else:
        detail = " It exceeds the available total-digit capacity."
    zero_warning = (
        " Applying this precision could turn the positive value into zero."
        if finding.zeroed_positive
        else ""
    )
    return (
        f"The exact prepared value cannot fit Odoo precision "
        f"({precision}, {scale}) without changing.{detail}{zero_warning} "
        "Change the explicit field rule or the captured Odoo precision, then "
        "prepare the complete data again."
    )


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
