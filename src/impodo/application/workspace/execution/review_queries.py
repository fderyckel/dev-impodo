"""Build bounded execution review facts without owning write coordination.

Full load previews and persisted Check changes projections use the same
ordering, grouping, limits, and accepted guidance. A supplied label catalogue
is used only by the saved review; full command previews retain dataset keys.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from impodo.domain.execution_snapshot import ExecutionSnapshot

from .review_models import (
    MAX_VISIBLE_BLOCKER_DATASETS,
    MAX_VISIBLE_BLOCKER_GROUPS,
    MAX_VISIBLE_GROUP_DATASETS,
    MAX_VISIBLE_LOAD_GROUPS,
    ExecutionReviewBlockerGroup,
    ExecutionReviewBlockerSummary,
    ExecutionReviewDataset,
    ExecutionReviewDependencySummary,
    ExecutionReviewLoadGroup,
)


def execution_dataset_previews(
    snapshot: ExecutionSnapshot,
    *,
    dataset_labels: Mapping[str, str] | None = None,
) -> tuple[ExecutionReviewDataset, ...]:
    """Count planned outcomes once, retaining the frozen dataset order."""
    counts: dict[str, Counter[str]] = {}
    for row in snapshot.rows:
        counts.setdefault(row.dataset, Counter())[row.disposition] += 1
    return tuple(
        ExecutionReviewDataset(
            dataset=(
                item.dataset
                if dataset_labels is None
                else _dataset_label(item.dataset, dataset_labels)
            ),
            target_model=item.target_model,
            create_count=counts[item.dataset]["CREATE"],
            update_count=counts[item.dataset]["UPDATE"],
            unchanged_count=counts[item.dataset]["UNCHANGED"],
        )
        for item in snapshot.datasets
        if item.dataset in counts
    )


def _dataset_label(value: str, labels: Mapping[str, str] | None) -> str:
    fallback = value.replace("_", " ").strip().title() or "Prepared records"
    return labels.get(value, fallback) if labels is not None else fallback


def execution_dependency_summary(
    snapshot: ExecutionSnapshot,
    *,
    dataset_labels: Mapping[str, str] | None = None,
) -> ExecutionReviewDependencySummary:
    """Describe the first frozen load groups without copying the planner."""

    row_by_id = {row.row_id: row for row in snapshot.rows}
    dataset_rank = {item.dataset: item.sequence for item in snapshot.datasets}
    groups: list[ExecutionReviewLoadGroup] = []
    components = tuple(
        sorted(
            snapshot.relationship_plan.components,
            key=lambda item: item.sequence,
        )
    )
    for component in components[:MAX_VISIBLE_LOAD_GROUPS]:
        datasets = tuple(
            sorted(
                {
                    row_by_id[row_id].dataset
                    for row_id in component.row_ids
                    if row_id in row_by_id
                },
                key=lambda value: (dataset_rank.get(value, 10**9), value),
            )
        )
        visible_datasets = datasets[:MAX_VISIBLE_GROUP_DATASETS]
        groups.append(
            ExecutionReviewLoadGroup(
                number=component.sequence + 1,
                record_count=len(component.row_ids),
                dataset_labels=tuple(
                    _dataset_label(value, dataset_labels) for value in visible_datasets
                ),
                omitted_dataset_count=max(
                    0,
                    len(datasets) - len(visible_datasets),
                ),
            )
        )
    completion_rows = {item.row_id for item in snapshot.relationship_plan.completions}
    return ExecutionReviewDependencySummary(
        groups=tuple(groups),
        total_group_count=len(components),
        omitted_group_count=max(0, len(components) - len(groups)),
        relationship_record_count=len(completion_rows),
        relationship_field_count=snapshot.relationship_plan.completion_count,
        relationship_link_count=snapshot.relationship_plan.edge_count,
    )


_BLOCKER_GUIDANCE: dict[str, tuple[str, str]] = {
    "HARD_DEPENDENCY_CYCLE": (
        "Required relationships cannot be created in a safe order",
        (
            "Make one relationship optional, point it to an existing Odoo record, "
            "or correct the source relationships, then compare again."
        ),
    ),
    "MISSING_INCOMING_ROW": (
        "A related source record is missing",
        (
            "Add the supporting record or change the relationship to a valid "
            "reviewed record, then compare again."
        ),
    ),
    "DUPLICATE_INCOMING_ROW": (
        "A related source record is not unique",
        (
            "Remove the duplicate or choose a business key that identifies one "
            "record, then compare again."
        ),
    ),
    "INCOMPLETE_INCOMING_REFERENCE": (
        "A source relationship is incomplete",
        "Complete or remove the relationship in Match data, then compare again.",
    ),
    "INCOMING_MODEL_MISMATCH": (
        "A relationship points to the wrong Odoo record type",
        "Correct the relationship mapping and compare again.",
    ),
    "UNUSABLE_INCOMING_ROW": (
        "A supporting source record also needs attention",
        "Resolve the supporting record first, then compare again.",
    ),
    "MISSING_DEPENDENCY_STRENGTH": (
        "A relationship rule is incomplete",
        (
            "Review whether the relationship is required or optional, then compare "
            "again."
        ),
    ),
    "BLOCKED_DEPENDENCY": (
        "A record depends on another blocked source record",
        (
            "Resolve the earlier relationship warning; Impodo will then recalculate "
            "the load order."
        ),
    ),
    "AMBIGUOUS_ROWS": (
        "Some records match more than one Odoo record",
        "Choose a unique match in the final review, then compare again.",
    ),
    "BLOCKED_ROWS": (
        "Some records still need a valid mapping or value",
        (
            "Open the affected rows in Final review to see the field and reason. "
            "Correct the source, mapping, or Odoo record, then compare again."
        ),
    ),
}


def execution_blocker_summary(
    snapshot: ExecutionSnapshot,
    *,
    dataset_labels: Mapping[str, str] | None = None,
) -> ExecutionReviewBlockerSummary:
    """Group equivalent blocker evidence into bounded actionable messages."""

    row_by_id = {row.row_id: row for row in snapshot.rows}
    blockers_by_code: dict[str, set[str]] = {}
    for blocker in snapshot.relationship_plan.blockers:
        blockers_by_code.setdefault(blocker.code, set()).add(blocker.row_id)
    for disposition, code in (
        ("AMBIGUOUS", "AMBIGUOUS_ROWS"),
        ("BLOCKED", "BLOCKED_ROWS"),
    ):
        matching = {
            row.row_id for row in snapshot.rows if row.disposition == disposition
        }
        if matching:
            blockers_by_code[code] = matching

    groups: list[ExecutionReviewBlockerGroup] = []
    for code, row_ids in blockers_by_code.items():
        title, action = _BLOCKER_GUIDANCE.get(
            code,
            (
                "A reviewed relationship cannot be loaded safely",
                "Review the related source value and mapping, then compare again.",
            ),
        )
        datasets = tuple(
            sorted(
                {row_by_id[row_id].dataset for row_id in row_ids if row_id in row_by_id}
            )
        )
        visible_datasets = datasets[:MAX_VISIBLE_BLOCKER_DATASETS]
        groups.append(
            ExecutionReviewBlockerGroup(
                code=code,
                title=title,
                action=action,
                record_count=len(row_ids),
                dataset_labels=tuple(
                    _dataset_label(value, dataset_labels) for value in visible_datasets
                ),
                omitted_dataset_count=max(
                    0,
                    len(datasets) - len(visible_datasets),
                ),
            )
        )
    visible = tuple(groups[:MAX_VISIBLE_BLOCKER_GROUPS])
    return ExecutionReviewBlockerSummary(
        groups=visible,
        total_group_count=len(groups),
        omitted_group_count=max(0, len(groups) - len(visible)),
    )
