"""Extracted transformation impact domain behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import (
    Callable,
    Mapping,
)

from ...models import (
    canonical_json_bytes,
    portable_value,
)
from ...staging_contracts import BROWSER_EVALUATOR_VERSION
from ..contracts import (
    TRANSFORMATION_IMPACT_CONTRACT_VERSION,
    TRANSFORMATION_IMPACT_DETAIL_LIMIT,
)


@dataclass(frozen=True, slots=True)
class TransformationImpactRow:
    """One visible raw-to-proposed scalar value change."""

    dataset: str
    source_row: int
    source_column: str
    target_field: str
    raw_value: str
    proposed_value: str
    rules: str
    outcome: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class TransformationImpactReport:
    """Bounded browser projection with complete all-row outcome counts."""

    mapping_content_hash: str
    evaluated_count: int
    changed_count: int
    fallback_count: int
    null_count: int
    invalid_count: int
    provided_count: int
    unchanged_count: int
    rows: tuple[TransformationImpactRow, ...]
    detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT

    @property
    def impact_count(self) -> int:
        """Count every evaluated value whose outcome was not unchanged."""

        return (
            self.changed_count
            + self.fallback_count
            + self.null_count
            + self.invalid_count
            + self.provided_count
        )

    @property
    def truncated(self) -> bool:
        """Whether bounded display rows omit additional counted impacts."""

        return self.impact_count > len(self.rows)


@dataclass(frozen=True, slots=True)
class TransformationImpactCounts:
    """Complete outcome accounting for one bounded native result batch."""

    evaluated_count: int = 0
    changed_count: int = 0
    fallback_count: int = 0
    null_count: int = 0
    invalid_count: int = 0
    provided_count: int = 0
    unchanged_count: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.evaluated_count,
                self.changed_count,
                self.fallback_count,
                self.null_count,
                self.invalid_count,
                self.provided_count,
                self.unchanged_count,
            )
        ):
            raise ValueError("Transformation outcome counts cannot be negative")
        if self.evaluated_count != (
            self.changed_count
            + self.fallback_count
            + self.null_count
            + self.invalid_count
            + self.provided_count
            + self.unchanged_count
        ):
            raise ValueError("Transformation outcomes do not reconcile")

    @property
    def impact_count(self) -> int:
        return self.evaluated_count - self.unchanged_count


@dataclass(frozen=True, slots=True)
class TransformationImpactIdentity:
    """Hash-bound identity for one reusable transformation-impact snapshot."""

    physical_selection_hash: str
    source_selection_hash: str
    mapping_content_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    contract_version: int = TRANSFORMATION_IMPACT_CONTRACT_VERSION
    evaluator_version: int = BROWSER_EVALUATOR_VERSION

    @property
    def content_hash(self) -> str:
        """Hash every input/version that determines a reusable snapshot."""

        return "sha256:" + sha256(
            canonical_json_bytes(
                {
                    "physical_selection_hash": self.physical_selection_hash,
                    "source_selection_hash": self.source_selection_hash,
                    "mapping_content_hash": self.mapping_content_hash,
                    "schema_hash": self.schema_hash,
                    "derived_plan_hash": self.derived_plan_hash,
                    "contract_version": self.contract_version,
                    "evaluator_version": self.evaluator_version,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class TransformationImpactSnapshot:
    """Complete counts for one persisted, filterable impact projection."""

    identity: TransformationImpactIdentity
    created_at: datetime
    created_by: str
    affected_row_count: int
    report: TransformationImpactReport


@dataclass(frozen=True, slots=True)
class TransformationImpactFilter:
    """Server-side filters shared by the browser table and CSV export."""

    dataset: str = ""
    outcome: str = ""
    target_field: str = ""
    query: str = ""


@dataclass(frozen=True, slots=True)
class TransformationImpactPage:
    """One bounded, deterministically ordered impact-result page."""

    rows: tuple[TransformationImpactRow, ...]
    matching_count: int
    start_position: int
    end_position: int
    previous_before: int | None
    next_after: int | None


@dataclass(slots=True)
class _TransformationImpactCollector:
    mapping_content_hash: str
    detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT
    evaluated_count: int = 0
    changed_count: int = 0
    fallback_count: int = 0
    null_count: int = 0
    invalid_count: int = 0
    provided_count: int = 0
    unchanged_count: int = 0
    rows: list[TransformationImpactRow] | None = None
    sink: Callable[[TransformationImpactRow], None] | None = None

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = []

    def record(
        self,
        *,
        dataset: str,
        source_row: int,
        source_column: str,
        target_field: str,
        raw_value: object,
        proposed_value: object,
        rules: str,
        outcome: str,
        message: str = "",
    ) -> None:
        self.evaluated_count += 1
        attribute = f"{outcome}_count"
        setattr(self, attribute, getattr(self, attribute) + 1)
        if outcome == "unchanged":
            return
        impact = TransformationImpactRow(
            dataset=dataset,
            source_row=source_row,
            source_column=source_column,
            target_field=target_field,
            raw_value=_display_value(raw_value),
            proposed_value=_display_value(proposed_value),
            rules=rules,
            outcome=outcome,
            message=message,
        )
        if self.sink is not None:
            self.sink(impact)
        if len(self.rows or ()) >= self.detail_limit:
            return
        assert self.rows is not None
        self.rows.append(impact)

    def record_precomputed(
        self,
        counts: TransformationImpactCounts,
        impacts: tuple[TransformationImpactRow, ...],
    ) -> None:
        """Merge one native batch without replaying unchanged scalar cells."""

        if len(impacts) != counts.impact_count:
            raise ValueError("Transformation impact batch does not reconcile")
        actual = {
            outcome: sum(1 for row in impacts if row.outcome == outcome)
            for outcome in (
                "changed",
                "fallback",
                "null",
                "invalid",
                "provided",
            )
        }
        for outcome, expected in (
            ("changed", counts.changed_count),
            ("fallback", counts.fallback_count),
            ("null", counts.null_count),
            ("invalid", counts.invalid_count),
            ("provided", counts.provided_count),
        ):
            if actual[outcome] != expected:
                raise ValueError("Transformation impact outcomes are incomplete")
        self.evaluated_count += counts.evaluated_count
        self.changed_count += counts.changed_count
        self.fallback_count += counts.fallback_count
        self.null_count += counts.null_count
        self.invalid_count += counts.invalid_count
        self.provided_count += counts.provided_count
        self.unchanged_count += counts.unchanged_count
        assert self.rows is not None
        for impact in impacts:
            if self.sink is not None:
                self.sink(impact)
            if len(self.rows) < self.detail_limit:
                self.rows.append(impact)

    def report(self) -> TransformationImpactReport:
        return TransformationImpactReport(
            mapping_content_hash=self.mapping_content_hash,
            evaluated_count=self.evaluated_count,
            changed_count=self.changed_count,
            fallback_count=self.fallback_count,
            null_count=self.null_count,
            invalid_count=self.invalid_count,
            provided_count=self.provided_count,
            unchanged_count=self.unchanged_count,
            rows=tuple(self.rows or ()),
            detail_limit=self.detail_limit,
        )


def _display_value(value: object) -> str:
    # Scalar mappings overwhelmingly compare primitive source and prepared
    # values.  Keep their established display semantics without first building
    # recursive portable dictionaries for every unchanged field.
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return " / ".join(_display_value(item) for item in value)
    portable = portable_value(value)
    if isinstance(portable, Mapping) and "value" in portable:
        return str(portable["value"])
    if isinstance(portable, list):
        return " / ".join(_display_value(item) for item in portable)
    if isinstance(portable, Mapping):
        return json.dumps(portable, ensure_ascii=False, separators=(",", ":"))
    return str(portable) if portable is not None else "—"


def _display_values_equal(left: object, right: object) -> bool:
    """Compare values using the existing normalization-review semantics."""

    if left is right:
        return True
    return _display_value(left) == _display_value(right)
