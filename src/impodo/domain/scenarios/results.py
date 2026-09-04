"""Define compact, non-secret results for governed scenario runs.

Migration stages: cross-cutting source through reconciliation. Layer: domain.

The result deliberately contains only counts, hashes, timings, and controlled
status codes. Source values, target values, connection URLs, credentials, and
protected Odoo identifiers are never part of this portable contract.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/domain/scenarios/test_results.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping
from uuid import UUID

from impodo.domain.shared.models import canonical_json_bytes


_CLASSIFICATIONS = ("CREATE", "UPDATE", "UNCHANGED", "BLOCKED", "AMBIGUOUS")


class ScenarioRunStatus(StrEnum):
    """Classify one completed attempt without hiding unsafe outcomes."""

    PASSED = "PASSED"
    EXPECTED_BLOCK_PASSED = "EXPECTED_BLOCK_PASSED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    UNSAFE_TO_CONTINUE = "UNSAFE_TO_CONTINUE"


class ScenarioFailureStage(StrEnum):
    """Name the workflow owner of a failed scenario assertion."""

    TARGET_POLICY = "TARGET_POLICY"
    PREPARATION = "PREPARATION"
    FIRST_COMPARISON = "FIRST_COMPARISON"
    EXECUTION = "EXECUTION"
    RECONCILIATION = "RECONCILIATION"
    REPEAT_COMPARISON = "REPEAT_COMPARISON"
    INFRASTRUCTURE = "INFRASTRUCTURE"


class ScenarioReasonCode(StrEnum):
    """Provide actionable, non-free-form diagnostic categories."""

    NONE = "NONE"
    UNSUPPORTED_SCENARIO_STAGE = "UNSUPPORTED_SCENARIO_STAGE"
    SOURCE_INPUT_INVALID = "SOURCE_INPUT_INVALID"
    PREPARATION_EXPECTATION_MISMATCH = "PREPARATION_EXPECTATION_MISMATCH"
    COMPARISON_EXPECTATION_MISMATCH = "COMPARISON_EXPECTATION_MISMATCH"
    COMPARISON_FAILED = "COMPARISON_FAILED"
    EXPECTED_BLOCK_NOT_OBSERVED = "EXPECTED_BLOCK_NOT_OBSERVED"
    UNEXPECTED_BLOCKER = "UNEXPECTED_BLOCKER"
    TARGET_POLICY_MISMATCH = "TARGET_POLICY_MISMATCH"
    CREDENTIAL_FAILURE = "CREDENTIAL_FAILURE"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    EXECUTION_EXPECTATION_MISMATCH = "EXECUTION_EXPECTATION_MISMATCH"
    RECONCILIATION_EXPECTATION_MISMATCH = "RECONCILIATION_EXPECTATION_MISMATCH"
    TARGET_PROJECTION_MISMATCH = "TARGET_PROJECTION_MISMATCH"
    REPEAT_COMPARISON_EXPECTATION_MISMATCH = (
        "REPEAT_COMPARISON_EXPECTATION_MISMATCH"
    )
    UNSAFE_WRITE_OUTCOME = "UNSAFE_WRITE_OUTCOME"


def normalized_comparison_counts(values: Mapping[str, int] | None) -> dict[str, int]:
    """Return every classification key and reject invalid counts."""

    if values is None:
        return {}
    unknown = set(values) - set(_CLASSIFICATIONS)
    if unknown:
        raise ValueError("scenario comparison contains unknown classifications")
    normalized = {name: int(values.get(name, 0)) for name in _CLASSIFICATIONS}
    if any(value < 0 for value in normalized.values()):
        raise ValueError("scenario comparison counts cannot be negative")
    return normalized


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    """Retain the compact evidence needed to qualify one scenario attempt."""

    contract_version: int
    run_id: str
    scenario_id: str
    scenario_hash: str
    fixture_hash: str
    expectation_hash: str
    started_at: datetime
    completed_at: datetime
    status: ScenarioRunStatus
    reason_code: ScenarioReasonCode = ScenarioReasonCode.NONE
    failure_stage: ScenarioFailureStage | None = None
    prepared_rows: int = 0
    source_issues: int = 0
    expected_first_comparison: Mapping[str, int] = field(default_factory=dict)
    actual_first_comparison: Mapping[str, int] = field(default_factory=dict)
    expected_execution: Mapping[str, int] = field(default_factory=dict)
    actual_execution: Mapping[str, int] = field(default_factory=dict)
    expected_reconciliation: Mapping[str, int] = field(default_factory=dict)
    actual_reconciliation: Mapping[str, int] = field(default_factory=dict)
    expected_repeat_comparison: Mapping[str, int] = field(default_factory=dict)
    actual_repeat_comparison: Mapping[str, int] = field(default_factory=dict)
    expected_projection_records: int = 0
    verified_projection_records: int = 0
    projection_difference_count: int = 0
    target_hash: str = ""
    preflight_hash: str = ""
    execution_snapshot_hash: str = ""
    reconciliation_hash: str = ""
    odoo_version: str = ""
    module_versions_hash: str = ""
    phase_seconds: Mapping[str, float] = field(default_factory=dict)
    write_attempt_count: int = 0

    def __post_init__(self) -> None:
        UUID(self.run_id)
        for name, value in (
            ("scenario_hash", self.scenario_hash),
            ("fixture_hash", self.fixture_hash),
            ("expectation_hash", self.expectation_hash),
        ):
            if not value.startswith("sha256:") or len(value) != 71:
                raise ValueError(f"{name} must be a SHA-256 content hash")
        for value in (
            self.target_hash,
            self.preflight_hash,
            self.execution_snapshot_hash,
            self.reconciliation_hash,
            self.module_versions_hash,
        ):
            if value and (not value.startswith("sha256:") or len(value) != 71):
                raise ValueError("scenario evidence hashes must use SHA-256")
        if self.completed_at < self.started_at:
            raise ValueError("scenario completion cannot precede its start")
        if self.prepared_rows < 0 or self.source_issues < 0:
            raise ValueError("scenario preparation counts cannot be negative")
        if self.write_attempt_count < 0:
            raise ValueError("scenario write-attempt count cannot be negative")
        if any(
            value < 0
            for value in (
                self.expected_projection_records,
                self.verified_projection_records,
                self.projection_difference_count,
            )
        ):
            raise ValueError("scenario projection counts cannot be negative")
        if any(float(value) < 0 for value in self.phase_seconds.values()):
            raise ValueError("scenario phase durations cannot be negative")
        expected = normalized_comparison_counts(self.expected_first_comparison)
        actual = normalized_comparison_counts(self.actual_first_comparison)
        object.__setattr__(self, "expected_first_comparison", expected)
        object.__setattr__(self, "actual_first_comparison", actual)
        object.__setattr__(
            self,
            "expected_repeat_comparison",
            normalized_comparison_counts(self.expected_repeat_comparison),
        )
        object.__setattr__(
            self,
            "actual_repeat_comparison",
            normalized_comparison_counts(self.actual_repeat_comparison),
        )
        for field_name in (
            "expected_execution",
            "actual_execution",
            "expected_reconciliation",
            "actual_reconciliation",
        ):
            counts = {str(name): int(value) for name, value in getattr(self, field_name).items()}
            if any(value < 0 for value in counts.values()):
                raise ValueError("scenario outcome counts cannot be negative")
            object.__setattr__(self, field_name, counts)
        if self.status in {
            ScenarioRunStatus.PASSED,
            ScenarioRunStatus.EXPECTED_BLOCK_PASSED,
        }:
            if self.failure_stage is not None or self.reason_code is not ScenarioReasonCode.NONE:
                raise ValueError("a passing scenario cannot contain a failure")
        elif self.failure_stage is None or self.reason_code is ScenarioReasonCode.NONE:
            raise ValueError("a failed scenario requires a stage and reason code")

    def to_portable_dict(self) -> dict[str, object]:
        """Serialize only the allowlisted, non-secret result surface."""

        return {
            "contract_version": self.contract_version,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "fixture_hash": self.fixture_hash,
            "expectation_hash": self.expectation_hash,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "status": self.status.value,
            "failure": (
                None
                if self.failure_stage is None
                else {
                    "stage": self.failure_stage.value,
                    "reason_code": self.reason_code.value,
                }
            ),
            "preparation": {
                "prepared_rows": self.prepared_rows,
                "source_issues": self.source_issues,
            },
            "first_comparison": {
                "expected": dict(self.expected_first_comparison),
                "actual": dict(self.actual_first_comparison),
                "preflight_hash": self.preflight_hash or None,
            },
            "execution": {
                "expected": dict(self.expected_execution),
                "actual": dict(self.actual_execution),
                "snapshot_hash": self.execution_snapshot_hash or None,
            },
            "reconciliation": {
                "expected": dict(self.expected_reconciliation),
                "actual": dict(self.actual_reconciliation),
                "result_hash": self.reconciliation_hash or None,
            },
            "target_projection": {
                "expected_records": self.expected_projection_records,
                "verified_records": self.verified_projection_records,
                "difference_count": self.projection_difference_count,
            },
            "repeat_comparison": {
                "expected": dict(self.expected_repeat_comparison),
                "actual": dict(self.actual_repeat_comparison),
            },
            "target": {
                "target_hash": self.target_hash or None,
                "odoo_version": self.odoo_version or None,
                "module_versions_hash": self.module_versions_hash or None,
            },
            "phase_seconds": {
                name: round(float(value), 6)
                for name, value in sorted(self.phase_seconds.items())
            },
            "write_attempt_count": self.write_attempt_count,
        }

    def to_json_bytes(self) -> bytes:
        """Return deterministic newline-free JSON bytes."""

        return canonical_json_bytes(self.to_portable_dict())
