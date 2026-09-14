"""Bounded execution facts used by shared workflow navigation.

The browser sidebar does not need the immutable row schedule.  This projection
is published from that schedule during preflight and contains only the scalar
facts needed to classify the load stage.  Execution continues to validate the
complete snapshot at the confirm and write boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.preflight.reports import ReadinessReport


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
        return self.create_count + self.update_count

    @property
    def has_attention(self) -> bool:
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
