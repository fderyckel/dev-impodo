"""Bounded execution facts used by shared workflow navigation.

The browser sidebar does not need the immutable row schedule.  This projection
is published from that schedule during preflight and contains only the scalar
facts needed to classify the load stage.  Execution continues to validate the
complete snapshot at the confirm and write boundaries.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace

from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.preflight.reports import ReadinessReport

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
MAX_VISIBLE_LOAD_GROUPS = 5
MAX_VISIBLE_GROUP_DATASETS = 3
MAX_VISIBLE_BLOCKER_GROUPS = 5
MAX_VISIBLE_BLOCKER_DATASETS = 3


@dataclass(frozen=True, slots=True)
class ExecutionPreviewSummary:
    """Small, immutable navigation projection of one execution snapshot."""

    preflight_run_id: str
    snapshot_hash: str
    snapshot_root_hash: str
    comparison_status: str
    create_count: int
    update_count: int
    unchanged_count: int
    blocked_count: int
    ambiguous_count: int
    relationship_blocker_count: int
    target_hash: str
    target_odoo_version: str
    read_credential_binding_hash: str
    read_principal_hash: str
    read_permission_hash: str
    read_context_hash: str
    execution_shape_ready: bool
    contract_version: int = 1

    def __post_init__(self) -> None:
        counts = (
            self.create_count,
            self.update_count,
            self.unchanged_count,
            self.blocked_count,
            self.ambiguous_count,
            self.relationship_blocker_count,
        )
        if (
            self.contract_version != 1
            or not self.preflight_run_id
            or _SHA256.fullmatch(self.snapshot_hash) is None
            or _SHA256.fullmatch(self.snapshot_root_hash) is None
            or _SHA256.fullmatch(self.target_hash) is None
            or self.comparison_status not in {"READY", "NEEDS_REVIEW", "BLOCKED"}
            or any(value < 0 for value in counts)
        ):
            raise ValueError("Execution preview summary is invalid")

    @property
    def write_count(self) -> int:
        """Return planned creates plus updates for navigation presentation."""
        return self.create_count + self.update_count

    @property
    def has_attention(self) -> bool:
        """Return whether navigation must send the operator back to review.

        Used by ``ExecutionNavigationPreview.can_load``. It is deliberately
        conservative and does not replace full snapshot validation at load.
        """
        return bool(
            self.comparison_status != "READY"
            or self.blocked_count
            or self.ambiguous_count
            or self.relationship_blocker_count
            or not self.execution_shape_ready
        )


def build_execution_preview_summary(
    report: ReadinessReport,
    snapshot: ExecutionSnapshot,
    *,
    execution_shape_ready: bool,
) -> ExecutionPreviewSummary:
    """Project validated preflight and snapshot evidence into scalar facts."""

    if (
        snapshot.preflight_run_id != report.run_id
        or snapshot.target_hash != report.target_hash
        or snapshot.preflight_result_hash != report.result_hash
    ):
        raise ValueError("Execution preview summary evidence does not agree")
    counts = snapshot.counts
    return ExecutionPreviewSummary(
        preflight_run_id=report.run_id,
        snapshot_hash=snapshot.semantic_hash,
        snapshot_root_hash=snapshot.root_hash,
        comparison_status=report.status,
        create_count=int(counts.get("CREATE", 0)),
        update_count=int(counts.get("UPDATE", 0)),
        unchanged_count=int(counts.get("UNCHANGED", 0)),
        blocked_count=int(counts.get("BLOCKED", 0)),
        ambiguous_count=int(counts.get("AMBIGUOUS", 0)),
        relationship_blocker_count=snapshot.relationship_plan.blocker_count,
        target_hash=snapshot.target_hash,
        target_odoo_version=snapshot.target_odoo_version,
        read_credential_binding_hash=snapshot.read_credential_binding_hash,
        read_principal_hash=snapshot.read_principal_hash,
        read_permission_hash=snapshot.read_permission_hash,
        read_context_hash=snapshot.read_context_hash,
        execution_shape_ready=execution_shape_ready,
    )


@dataclass(frozen=True, slots=True)
class ExecutionNavigationState:
    """Summary plus bounded current execution and reconciliation status."""

    summary: ExecutionPreviewSummary
    execution_run_id: str = ""
    execution_status: str = ""
    reconciliation_status: str = ""


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
class ExecutionReviewSnapshot:
    """Small snapshot identity used by the read-only review template."""

    preflight_run_id: str
    semantic_hash: str
    root_hash: str
    target_hash: str
    target_database: str
    target_odoo_version: str
    counts: dict[str, int]

    @property
    def write_count(self) -> int:
        return int(self.counts.get("CREATE", 0)) + int(
            self.counts.get("UPDATE", 0)
        )

    def __post_init__(self) -> None:
        if (
            not self.preflight_run_id
            or _SHA256.fullmatch(self.semantic_hash) is None
            or _SHA256.fullmatch(self.root_hash) is None
            or _SHA256.fullmatch(self.target_hash) is None
            or not self.target_database
            or any(value < 0 for value in self.counts.values())
        ):
            raise ValueError("Execution review snapshot is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionReviewLoadGroup:
    number: int
    record_count: int
    dataset_labels: tuple[str, ...]
    omitted_dataset_count: int


@dataclass(frozen=True, slots=True)
class ExecutionReviewDependencySummary:
    groups: tuple[ExecutionReviewLoadGroup, ...] = ()
    total_group_count: int = 0
    omitted_group_count: int = 0
    relationship_record_count: int = 0
    relationship_field_count: int = 0
    relationship_link_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionReviewBlockerGroup:
    code: str
    title: str
    action: str
    record_count: int
    dataset_labels: tuple[str, ...] = ()
    omitted_dataset_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionReviewBlockerSummary:
    groups: tuple[ExecutionReviewBlockerGroup, ...] = ()
    total_group_count: int = 0
    omitted_group_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionReviewProjection:
    """Hash-bound bounded projection consumed only by Check changes."""

    summary: ExecutionPreviewSummary
    snapshot: ExecutionReviewSnapshot
    datasets: tuple[ExecutionReviewDataset, ...]
    dependency_summary: ExecutionReviewDependencySummary
    blocker_summary: ExecutionReviewBlockerSummary
    deferred_create_count: int
    current_run_id: str = ""
    current_execution_status: str = ""
    reconciliation_status: str = ""
    scope_error: str = ""
    scope_error_code: str = ""
    credential_refresh_required: bool = False
    contract_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.contract_version != 1
            or self.snapshot.preflight_run_id != self.summary.preflight_run_id
            or self.snapshot.semantic_hash != self.summary.snapshot_hash
            or self.snapshot.root_hash != self.summary.snapshot_root_hash
            or self.snapshot.target_hash != self.summary.target_hash
            or self.deferred_create_count < 0
            or len(self.dependency_summary.groups) > MAX_VISIBLE_LOAD_GROUPS
            or len(self.blocker_summary.groups) > MAX_VISIBLE_BLOCKER_GROUPS
        ):
            raise ValueError("Execution review projection is invalid")

    @property
    def current_run(self) -> str | None:
        return self.current_run_id or None

    @property
    def can_load(self) -> bool:
        return bool(
            self.snapshot.write_count
            and not self.current_run_id
            and not self.scope_error
            and not self.summary.has_attention
        )

    def with_runtime(
        self,
        *,
        current_run_id: str,
        current_execution_status: str,
        reconciliation_status: str,
    ) -> ExecutionReviewProjection:
        return replace(
            self,
            current_run_id=current_run_id,
            current_execution_status=current_execution_status,
            reconciliation_status=reconciliation_status,
        )

    def with_scope_error(
        self,
        message: str,
        *,
        code: str = "",
        credential_refresh_required: bool = False,
    ) -> ExecutionReviewProjection:
        """Return one request-local validation view of persisted facts."""

        return replace(
            self,
            scope_error=message,
            scope_error_code=code,
            credential_refresh_required=credential_refresh_required,
        )

    def to_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> ExecutionReviewProjection:
        payload = json.loads(value)
        return cls(
            summary=ExecutionPreviewSummary(**payload["summary"]),
            snapshot=ExecutionReviewSnapshot(**payload["snapshot"]),
            datasets=tuple(
                ExecutionReviewDataset(**item) for item in payload["datasets"]
            ),
            dependency_summary=ExecutionReviewDependencySummary(
                groups=tuple(
                    ExecutionReviewLoadGroup(
                        **{
                            **item,
                            "dataset_labels": tuple(item["dataset_labels"]),
                        }
                    )
                    for item in payload["dependency_summary"]["groups"]
                ),
                **{
                    key: item
                    for key, item in payload["dependency_summary"].items()
                    if key != "groups"
                },
            ),
            blocker_summary=ExecutionReviewBlockerSummary(
                groups=tuple(
                    ExecutionReviewBlockerGroup(
                        **{
                            **item,
                            "dataset_labels": tuple(item["dataset_labels"]),
                        }
                    )
                    for item in payload["blocker_summary"]["groups"]
                ),
                **{
                    key: item
                    for key, item in payload["blocker_summary"].items()
                    if key != "groups"
                },
            ),
            deferred_create_count=int(payload["deferred_create_count"]),
            current_run_id=str(payload.get("current_run_id", "")),
            current_execution_status=str(
                payload.get("current_execution_status", "")
            ),
            reconciliation_status=str(payload.get("reconciliation_status", "")),
            scope_error=str(payload.get("scope_error", "")),
            scope_error_code=str(payload.get("scope_error_code", "")),
            credential_refresh_required=bool(
                payload.get("credential_refresh_required", False)
            ),
            contract_version=int(payload["contract_version"]),
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


def build_execution_review_projection(
    report: ReadinessReport,
    snapshot: ExecutionSnapshot,
    *,
    execution_shape_ready: bool,
) -> ExecutionReviewProjection:
    """Build the complete bounded Check changes projection in one pass."""

    summary = build_execution_preview_summary(
        report,
        snapshot,
        execution_shape_ready=execution_shape_ready,
    )
    labels = {item.dataset: item.label for item in report.datasets}
    dataset_counts: dict[str, dict[str, int]] = {}
    for row in snapshot.rows:
        counts = dataset_counts.setdefault(row.dataset, {})
        counts[row.disposition] = counts.get(row.disposition, 0) + 1
    datasets = tuple(
        ExecutionReviewDataset(
            dataset=labels.get(item.dataset, _dataset_label(item.dataset)),
            target_model=item.target_model,
            create_count=dataset_counts.get(item.dataset, {}).get("CREATE", 0),
            update_count=dataset_counts.get(item.dataset, {}).get("UPDATE", 0),
            unchanged_count=dataset_counts.get(item.dataset, {}).get(
                "UNCHANGED", 0
            ),
        )
        for item in snapshot.datasets
        if item.dataset in dataset_counts
    )
    row_by_id = {row.row_id: row for row in snapshot.rows}
    dataset_rank = {item.dataset: item.sequence for item in snapshot.datasets}
    components = tuple(
        sorted(
            snapshot.relationship_plan.components,
            key=lambda item: item.sequence,
        )
    )
    groups = []
    for component in components[:MAX_VISIBLE_LOAD_GROUPS]:
        component_datasets = tuple(
            sorted(
                {
                    row_by_id[row_id].dataset
                    for row_id in component.row_ids
                    if row_id in row_by_id
                },
                key=lambda value: (dataset_rank.get(value, 10**9), value),
            )
        )
        visible = component_datasets[:MAX_VISIBLE_GROUP_DATASETS]
        groups.append(
            ExecutionReviewLoadGroup(
                number=component.sequence + 1,
                record_count=len(component.row_ids),
                dataset_labels=tuple(
                    labels.get(value, _dataset_label(value)) for value in visible
                ),
                omitted_dataset_count=max(0, len(component_datasets) - len(visible)),
            )
        )
    completion_rows = {
        item.row_id for item in snapshot.relationship_plan.completions
    }
    dependency_summary = ExecutionReviewDependencySummary(
        groups=tuple(groups),
        total_group_count=len(components),
        omitted_group_count=max(0, len(components) - len(groups)),
        relationship_record_count=len(completion_rows),
        relationship_field_count=snapshot.relationship_plan.completion_count,
        relationship_link_count=snapshot.relationship_plan.edge_count,
    )
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
    blocker_groups = []
    for code, row_ids in blockers_by_code.items():
        title, action = _BLOCKER_GUIDANCE.get(
            code,
            (
                "A reviewed relationship cannot be loaded safely",
                "Review the related source value and mapping, then compare again.",
            ),
        )
        blocker_datasets = tuple(
            sorted(
                {
                    row_by_id[row_id].dataset
                    for row_id in row_ids
                    if row_id in row_by_id
                }
            )
        )
        visible = blocker_datasets[:MAX_VISIBLE_BLOCKER_DATASETS]
        blocker_groups.append(
            ExecutionReviewBlockerGroup(
                code=code,
                title=title,
                action=action,
                record_count=len(row_ids),
                dataset_labels=tuple(
                    labels.get(value, _dataset_label(value)) for value in visible
                ),
                omitted_dataset_count=max(0, len(blocker_datasets) - len(visible)),
            )
        )
    visible_blockers = tuple(blocker_groups[:MAX_VISIBLE_BLOCKER_GROUPS])
    return ExecutionReviewProjection(
        summary=summary,
        snapshot=ExecutionReviewSnapshot(
            preflight_run_id=report.run_id,
            semantic_hash=snapshot.semantic_hash,
            root_hash=snapshot.root_hash,
            target_hash=snapshot.target_hash,
            target_database=report.target_database,
            target_odoo_version=snapshot.target_odoo_version,
            counts={
                key: int(snapshot.counts.get(key, 0))
                for key in (
                    "CREATE",
                    "UPDATE",
                    "UNCHANGED",
                    "BLOCKED",
                    "AMBIGUOUS",
                )
            },
        ),
        datasets=datasets,
        dependency_summary=dependency_summary,
        blocker_summary=ExecutionReviewBlockerSummary(
            groups=visible_blockers,
            total_group_count=len(blocker_groups),
            omitted_group_count=max(0, len(blocker_groups) - len(visible_blockers)),
        ),
        deferred_create_count=len(completion_rows),
    )


def _dataset_label(value: str) -> str:
    return value.replace("_", " ").strip().title() or "Prepared records"
