"""Define output-based correction meaning independently of mapping controls.

The correction workflow compares canonical target-field intent.  It does not
care whether that intent came from a source field, Selection rule, constant,
fallback, casing transformation, formula, or resolved relationship.  Target
I/O, persistence, encryption, and Polars execution remain outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable


class CorrectionValueKind(StrEnum):
    """Name the canonical comparison family for one target-field value."""

    SCALAR = "SCALAR"
    MANY2ONE = "MANY2ONE"


class CorrectionFieldOutcome(StrEnum):
    """Classify one previous/current/corrected target-field comparison."""

    UNCHANGED_INTENT = "UNCHANGED_INTENT"
    READY = "READY"
    ALREADY_CORRECTED = "ALREADY_CORRECTED"
    CONFLICT = "CONFLICT"


CanonicalEquality = Callable[[Any, Any], bool]


@dataclass(frozen=True, slots=True)
class CorrectionCandidate:
    """One field whose corrected canonical intent differs from prior intent."""

    dataset: str
    source_row: int
    target_model: str
    target_field: str
    value_kind: CorrectionValueKind
    previous: Any
    corrected: Any

    def __post_init__(self) -> None:
        for value, label in (
            (self.dataset, "dataset"),
            (self.target_model, "target model"),
            (self.target_field, "target field"),
        ):
            if not value or len(value) > 200:
                raise ValueError(f"Correction {label} is invalid")
        if self.source_row < 1:
            raise ValueError("Correction source row is invalid")
        object.__setattr__(self, "value_kind", CorrectionValueKind(self.value_kind))


@dataclass(frozen=True, slots=True)
class CorrectionFieldDecision:
    """Bind a changed-intent candidate to one freshly read current value."""

    candidate: CorrectionCandidate
    current: Any
    outcome: CorrectionFieldOutcome

    @property
    def writable(self) -> bool:
        """Return whether the first correction delivery may write this field."""

        return self.outcome is CorrectionFieldOutcome.READY


def classify_correction_field(
    candidate: CorrectionCandidate,
    current: Any,
    *,
    equal: CanonicalEquality | None = None,
) -> CorrectionFieldDecision:
    """Apply the fail-closed three-way rule to canonical field values.

    Candidate generation normally removes unchanged intent with native Polars
    expressions.  The first branch remains part of the domain truth table so a
    caller cannot turn an accidentally supplied unchanged candidate into a
    write.
    """

    same = equal or _equal
    previous = candidate.previous
    corrected = candidate.corrected
    if same(previous, corrected):
        outcome = CorrectionFieldOutcome.UNCHANGED_INTENT
    elif same(current, corrected):
        outcome = CorrectionFieldOutcome.ALREADY_CORRECTED
    elif same(current, previous):
        outcome = CorrectionFieldOutcome.READY
    else:
        outcome = CorrectionFieldOutcome.CONFLICT
    return CorrectionFieldDecision(
        candidate=candidate,
        current=current,
        outcome=outcome,
    )


def _equal(left: Any, right: Any) -> bool:
    """Compare already canonical values without lossy string coercion."""

    return left == right


__all__ = [
    "CanonicalEquality",
    "CorrectionCandidate",
    "CorrectionFieldDecision",
    "CorrectionFieldOutcome",
    "CorrectionValueKind",
    "classify_correction_field",
]
