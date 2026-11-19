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
from datetime import datetime

from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.preflight.reports import ReadinessReport

from .business_review import BusinessIntentReview
from .review_models import (
    MAX_VISIBLE_BLOCKER_GROUPS,
    MAX_VISIBLE_LOAD_GROUPS,
    ExecutionReviewBlockerGroup,
    ExecutionReviewBlockerSummary,
    ExecutionReviewDataset,
    ExecutionReviewDependencySummary,
    ExecutionReviewLoadGroup,
)
from .review_queries import (
    execution_blocker_summary,
    execution_dataset_previews,
    execution_dependency_summary,
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


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
    business_intent_state: str = ""
    business_intent_assessment_hash: str = ""

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
            or self.business_intent_state in {"MISSING", "STALE", "DEVIATION"}
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
    reconciliation_id: str = ""
    verified_at: datetime | None = None
    target_database: str = ""
    has_workbook_detail: bool = False


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
    business_intent: BusinessIntentReview | None = None

    def __post_init__(self) -> None:
        if (
            self.contract_version not in {1, 2}
            or self.snapshot.preflight_run_id != self.summary.preflight_run_id
            or self.snapshot.semantic_hash != self.summary.snapshot_hash
            or self.snapshot.root_hash != self.summary.snapshot_root_hash
            or self.snapshot.target_hash != self.summary.target_hash
            or self.deferred_create_count < 0
            or len(self.dependency_summary.groups) > MAX_VISIBLE_LOAD_GROUPS
            or len(self.blocker_summary.groups) > MAX_VISIBLE_BLOCKER_GROUPS
        ):
            raise ValueError("Execution review projection is invalid")
        if self.business_intent is not None and (
            self.business_intent.snapshot_hash != self.summary.snapshot_hash
            or self.business_intent.state != self.summary.business_intent_state
            or self.business_intent.assessment_hash != self.summary.business_intent_assessment_hash
        ):
            raise ValueError("Business review differs from the execution projection")

    def with_business_intent(self, review: BusinessIntentReview) -> ExecutionReviewProjection:
        """Attach committed expectations without changing snapshot identities."""
        return replace(self, business_intent=review, contract_version=2,
            summary=replace(self.summary, business_intent_state=review.state,
                business_intent_assessment_hash=review.assessment_hash))

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

    @property
    def can_complete_without_load(self) -> bool:
        """Offer explicit zero-write completion from bounded current evidence."""
        return bool(
            not self.snapshot.write_count
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
            business_intent=BusinessIntentReview.from_dict(payload["business_intent"]) if payload.get("business_intent") else None,
        )


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
    datasets = execution_dataset_previews(snapshot, dataset_labels=labels)
    dependency_summary = execution_dependency_summary(snapshot, dataset_labels=labels)
    blocker_summary = execution_blocker_summary(snapshot, dataset_labels=labels)
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
        blocker_summary=blocker_summary,
        deferred_create_count=dependency_summary.relationship_record_count,
    )
