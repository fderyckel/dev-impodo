"""Protected Stage-3 evidence for checked source-row inclusion decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json
from typing import Any, Mapping

from impodo.domain.mapping.contracts import (
    RowInclusionJoin,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.domain.serialization import canonical_json, content_hash


ROW_INCLUSION_REVIEW_CONTRACT_VERSION = 1
ROW_INCLUSION_REVIEW_EVALUATOR_VERSION = 1
MAX_ROW_INCLUSION_REVIEW_PAGE_SIZE = 100


class RowInclusionReviewOutcome(StrEnum):
    """Closed browser vocabulary for one checked source-row decision."""

    INCLUDED = "included"
    EXCLUDED = "excluded"
    CANNOT_EVALUATE = "cannot_evaluate"


@dataclass(frozen=True, slots=True)
class RowInclusionReviewIdentity:
    """Bind a row check to every input that can change its meaning."""

    physical_selection_hash: str
    source_selection_hash: str
    mapping_content_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    evaluator_version: int = ROW_INCLUSION_REVIEW_EVALUATOR_VERSION

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))


@dataclass(frozen=True, slots=True)
class RowInclusionSourceValue:
    """One relevant source value shown without target-oriented meaning."""

    source_column_key: str
    source_column_label: str
    value: str


@dataclass(frozen=True, slots=True)
class RowInclusionReviewRow:
    """One bounded, reviewable source-row admission decision."""

    dataset_id: str
    dataset_name: str
    source_row: int
    values: tuple[RowInclusionSourceValue, ...]
    outcome: RowInclusionReviewOutcome
    rule_sentence: str
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", RowInclusionReviewOutcome(self.outcome))
        if self.source_row < 1:
            raise ValueError("Row-inclusion source row must be positive")
        keys = tuple(item.source_column_key for item in self.values)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("Row-inclusion review values are invalid")


@dataclass(frozen=True, slots=True)
class RowInclusionDatasetReview:
    """Account for every checked row governed by one dataset rule."""

    dataset_id: str
    dataset_name: str
    source_row_count: int
    included_count: int
    excluded_count: int
    cannot_evaluate_count: int
    rule_sentence: str

    def __post_init__(self) -> None:
        counts = (
            self.source_row_count,
            self.included_count,
            self.excluded_count,
            self.cannot_evaluate_count,
        )
        if any(item < 0 for item in counts):
            raise ValueError("Row-inclusion counts cannot be negative")
        if self.source_row_count != sum(counts[1:]):
            raise ValueError("Row-inclusion counts do not reconcile")

    @property
    def confirmable(self) -> bool:
        return self.included_count > 0 and self.cannot_evaluate_count == 0


@dataclass(frozen=True, slots=True)
class RowInclusionReviewReport:
    """Complete decision rows and reconciled dataset counts for one check."""

    identity: RowInclusionReviewIdentity
    datasets: tuple[RowInclusionDatasetReview, ...]
    rows: tuple[RowInclusionReviewRow, ...]

    def __post_init__(self) -> None:
        dataset_ids = tuple(item.dataset_id for item in self.datasets)
        if len(set(dataset_ids)) != len(dataset_ids):
            raise ValueError("Row-inclusion dataset reviews are duplicated")
        expected = sum(item.source_row_count for item in self.datasets)
        if len(self.rows) != expected:
            raise ValueError("Row-inclusion decision rows are incomplete")
        if any(row.dataset_id not in dataset_ids for row in self.rows):
            raise ValueError("Row-inclusion decision has no dataset review")
        reviews = {item.dataset_id: item for item in self.datasets}
        for dataset_id, review in reviews.items():
            rows = tuple(row for row in self.rows if row.dataset_id == dataset_id)
            if len({row.source_row for row in rows}) != len(rows):
                raise ValueError("Row-inclusion source rows are duplicated")
            if any(
                row.dataset_name != review.dataset_name
                or row.rule_sentence != review.rule_sentence
                for row in rows
            ):
                raise ValueError("Row-inclusion row descriptions do not reconcile")
            actual_counts = {
                outcome: sum(row.outcome is outcome for row in rows)
                for outcome in RowInclusionReviewOutcome
            }
            if (
                actual_counts[RowInclusionReviewOutcome.INCLUDED]
                != review.included_count
                or actual_counts[RowInclusionReviewOutcome.EXCLUDED]
                != review.excluded_count
                or actual_counts[RowInclusionReviewOutcome.CANNOT_EVALUATE]
                != review.cannot_evaluate_count
            ):
                raise ValueError("Row-inclusion decision counts do not reconcile")

    @property
    def content_hash(self) -> str:
        return content_hash(
            {
                "identity_hash": self.identity.content_hash,
                "datasets": [asdict(item) for item in self.datasets],
                "rows": [
                    {
                        **asdict(row),
                        "outcome": row.outcome.value,
                    }
                    for row in self.rows
                ],
            }
        )

    @property
    def confirmable(self) -> bool:
        return bool(self.datasets) and all(item.confirmable for item in self.datasets)

    @property
    def source_row_count(self) -> int:
        return sum(item.source_row_count for item in self.datasets)

    @property
    def included_count(self) -> int:
        return sum(item.included_count for item in self.datasets)

    @property
    def excluded_count(self) -> int:
        return sum(item.excluded_count for item in self.datasets)

    @property
    def cannot_evaluate_count(self) -> int:
        return sum(item.cannot_evaluate_count for item in self.datasets)


@dataclass(frozen=True, slots=True)
class RowInclusionReviewSnapshot:
    """Durable checked counts for one exact mapping and Data version."""

    identity: RowInclusionReviewIdentity
    datasets: tuple[RowInclusionDatasetReview, ...]
    snapshot_hash: str
    checked_at: datetime
    checked_by: str

    @property
    def content_hash(self) -> str:
        return self.snapshot_hash

    @property
    def confirmable(self) -> bool:
        return bool(self.datasets) and all(item.confirmable for item in self.datasets)

    @property
    def source_row_count(self) -> int:
        return sum(item.source_row_count for item in self.datasets)

    @property
    def included_count(self) -> int:
        return sum(item.included_count for item in self.datasets)

    @property
    def excluded_count(self) -> int:
        return sum(item.excluded_count for item in self.datasets)

    @property
    def cannot_evaluate_count(self) -> int:
        return sum(item.cannot_evaluate_count for item in self.datasets)

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "contract_version": ROW_INCLUSION_REVIEW_CONTRACT_VERSION,
            "identity": asdict(self.identity),
            "datasets": [asdict(item) for item in self.datasets],
            "checked_at": self.checked_at.isoformat(),
            "checked_by": self.checked_by,
            "content_hash": self.snapshot_hash,
        }
        return canonical_json(payload)

    @classmethod
    def from_json(cls, value: str) -> "RowInclusionReviewSnapshot":
        payload = json.loads(value)
        expected = {
            "contract_version",
            "identity",
            "datasets",
            "checked_at",
            "checked_by",
            "content_hash",
        }
        if set(payload) != expected:
            raise ValueError("Row-inclusion review fields are invalid")
        if int(payload["contract_version"]) != ROW_INCLUSION_REVIEW_CONTRACT_VERSION:
            raise ValueError("Row-inclusion review contract is unsupported")
        identity = RowInclusionReviewIdentity(**payload["identity"])
        datasets = tuple(
            RowInclusionDatasetReview(**item) for item in payload["datasets"]
        )
        snapshot = cls(
            identity=identity,
            datasets=datasets,
            snapshot_hash=str(payload["content_hash"]),
            checked_at=datetime.fromisoformat(str(payload["checked_at"])),
            checked_by=str(payload["checked_by"]),
        )
        if not snapshot.snapshot_hash.startswith("sha256:"):
            raise ValueError("Row-inclusion review hash is invalid")
        return snapshot


@dataclass(frozen=True, slots=True)
class RowInclusionReviewConfirmation:
    """Record explicit approval of exact checked counts and rule meaning."""

    snapshot_hash: str
    mapping_content_hash: str
    source_selection_hash: str
    confirmed_at: datetime
    confirmed_by: str


@dataclass(frozen=True, slots=True)
class RowInclusionReviewFilter:
    """Bounded server-side filters for the checked-row review."""

    dataset_id: str = ""
    outcome: str = ""
    query: str = ""

    def __post_init__(self) -> None:
        if self.outcome:
            RowInclusionReviewOutcome(self.outcome)
        if len(self.dataset_id) > 200 or len(self.query) > 128:
            raise ValueError("Row-inclusion review filter is too long")


@dataclass(frozen=True, slots=True)
class RowInclusionReviewPage:
    """One server-filtered page of protected row decisions."""

    rows: tuple[RowInclusionReviewRow, ...]
    matching_count: int
    start_position: int
    end_position: int
    previous_before: int | None
    next_after: int | None


def describe_row_inclusion_policy(
    policy: RowInclusionPolicy,
    source_labels: Mapping[str, str],
) -> str:
    """Return the guided rule as one readable positive sentence."""

    condition_sentences = tuple(
        _condition_sentence(
            source_labels.get(condition.source_column_key, condition.source_column_key),
            condition.operator,
            condition.comparison_value,
        )
        for condition in policy.conditions
    )
    if not condition_sentences:
        return "Use every row."
    joiner = " and " if policy.join is RowInclusionJoin.ALL else " or "
    return f"Include a row when {joiner.join(condition_sentences)}."


def _condition_sentence(
    label: str,
    operator: SelectionConditionOperator,
    comparison_value: str | None,
) -> str:
    phrases = {
        SelectionConditionOperator.EQUALS: "is exactly",
        SelectionConditionOperator.NOT_EQUALS: "is not",
        SelectionConditionOperator.EQUALS_IGNORE_CASE: "is exactly, ignoring case",
        SelectionConditionOperator.CONTAINS: "contains",
        SelectionConditionOperator.STARTS_WITH: "starts with",
        SelectionConditionOperator.ENDS_WITH: "ends with",
        SelectionConditionOperator.IS_BLANK: "is blank",
        SelectionConditionOperator.IS_NOT_BLANK: "is not blank",
        SelectionConditionOperator.IS_TRUE: "is yes",
        SelectionConditionOperator.IS_FALSE: "is no",
        SelectionConditionOperator.LESS_THAN: "is less than",
        SelectionConditionOperator.LESS_THAN_OR_EQUAL: "is at most",
        SelectionConditionOperator.GREATER_THAN: "is greater than",
        SelectionConditionOperator.GREATER_THAN_OR_EQUAL: "is at least",
    }
    phrase = phrases[SelectionConditionOperator(operator)]
    return f"{label} {phrase}" + (
        f" {comparison_value}" if comparison_value is not None else ""
    )
