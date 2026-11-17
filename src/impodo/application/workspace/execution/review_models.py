"""Immutable bounded facts shared by full execution and saved review queries."""

from __future__ import annotations

from dataclasses import dataclass

MAX_VISIBLE_LOAD_GROUPS = 5
MAX_VISIBLE_GROUP_DATASETS = 3
MAX_VISIBLE_BLOCKER_GROUPS = 5
MAX_VISIBLE_BLOCKER_DATASETS = 3


@dataclass(frozen=True, slots=True)
class ExecutionReviewDataset:
    """Value-free per-dataset totals displayed by Check changes."""

    dataset: str
    target_model: str
    create_count: int
    update_count: int
    unchanged_count: int

    def __post_init__(self) -> None:
        if (
            not self.dataset
            or not self.target_model
            or min(
                self.create_count,
                self.update_count,
                self.unchanged_count,
            )
            < 0
        ):
            raise ValueError("Execution review dataset is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionReviewLoadGroup:
    """One bounded layer from the frozen relationship schedule."""

    number: int
    record_count: int
    dataset_labels: tuple[str, ...]
    omitted_dataset_count: int


@dataclass(frozen=True, slots=True)
class ExecutionReviewDependencySummary:
    """Bounded group and relationship totals shared by review queries."""

    groups: tuple[ExecutionReviewLoadGroup, ...] = ()
    total_group_count: int = 0
    omitted_group_count: int = 0
    relationship_record_count: int = 0
    relationship_field_count: int = 0
    relationship_link_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionReviewBlockerGroup:
    """One accepted explanation for equivalent blocked rows."""

    code: str
    title: str
    action: str
    record_count: int
    dataset_labels: tuple[str, ...] = ()
    omitted_dataset_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionReviewBlockerSummary:
    """Bounded blocker categories without retaining source values or row IDs."""

    groups: tuple[ExecutionReviewBlockerGroup, ...] = ()
    total_group_count: int = 0
    omitted_group_count: int = 0
