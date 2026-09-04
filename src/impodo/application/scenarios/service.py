"""Orchestrate and assert governed scenario checkpoints.

Migration stages: cross-cutting source through reconciliation. Layer:
application.

The first executable slice owns preparation and first-comparison assertions.
It is intentionally read-only. Write-capable definitions produce retained
``UNSAFE_TO_CONTINUE`` results until the normal journalled execution and
reconciliation path is connected in the next slice.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/application/scenarios/test_service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from impodo.domain.scenarios import (
    ScenarioDefinition,
    ScenarioExpectedOutcome,
    ScenarioFailureStage,
    ScenarioReasonCode,
    ScenarioRunResult,
    ScenarioRunStatus,
    ScenarioStopAfter,
    ScenarioWritePolicy,
    normalized_comparison_counts,
)
from impodo.domain.serialization import content_hash


@dataclass(frozen=True, slots=True)
class ScenarioPreparationEvidence:
    """Carry safe totals plus runtime-owned prepared state to comparison."""

    prepared_rows: int
    source_issues: int
    state: object = field(repr=False)

    def __post_init__(self) -> None:
        if self.prepared_rows < 0 or self.source_issues < 0:
            raise ValueError("scenario preparation counts cannot be negative")


@dataclass(frozen=True, slots=True)
class ScenarioComparisonEvidence:
    """Carry compact evidence from the production comparison engine."""

    counts: Mapping[str, int]
    target_hash: str
    preflight_hash: str
    odoo_version: str
    module_versions_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", normalized_comparison_counts(self.counts))


@dataclass(frozen=True, slots=True)
class ScenarioExecutionEvidence:
    """Summarize one normal journalled execution run."""

    committed: int
    failed: int
    partially_applied: int
    outcome_unknown: int
    snapshot_hash: str
    state: object = field(repr=False)

    @property
    def total(self) -> int:
        return self.committed + self.failed + self.partially_applied + self.outcome_unknown


@dataclass(frozen=True, slots=True)
class ScenarioReconciliationEvidence:
    """Summarize the independent production read-back result."""

    verified: int
    fallout: int
    outcome_unknown: int
    result_hash: str
    state: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class ScenarioProjectionEvidence:
    """Summarize comparison with the independent target oracle."""

    expected_records: int
    verified_records: int
    difference_count: int


class ScenarioReadOnlyWorkflow(Protocol):
    """Port implemented by the profile and future Recipe scenario adapters."""

    def prepare(self, definition: ScenarioDefinition) -> ScenarioPreparationEvidence:
        """Run normal source preparation without contacting Odoo."""

    def compare(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
    ) -> ScenarioComparisonEvidence:
        """Run the existing bounded, read-only destination comparison."""


class ScenarioWriteWorkflow(ScenarioReadOnlyWorkflow, Protocol):
    """Port for the existing writer, read-back, and repeat comparison."""

    def execute(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
        comparison: ScenarioComparisonEvidence,
    ) -> ScenarioExecutionEvidence:
        """Execute only through the normal journal-before-transport service."""

    def reconcile(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
        execution: ScenarioExecutionEvidence,
    ) -> ScenarioReconciliationEvidence:
        """Read back through the normal independent reconciliation service."""

    def verify_projection(
        self,
        definition: ScenarioDefinition,
        preparation: ScenarioPreparationEvidence,
        reconciliation: ScenarioReconciliationEvidence,
    ) -> ScenarioProjectionEvidence:
        """Compare actual target values with the reviewed independent oracle."""


class ScenarioWorkflowFailure(RuntimeError):
    """Report one controlled stage failure without attaching sensitive values."""

    def __init__(
        self,
        stage: ScenarioFailureStage,
        reason_code: ScenarioReasonCode,
    ) -> None:
        super().__init__(f"scenario failed at {stage.value}: {reason_code.value}")
        self.stage = stage
        self.reason_code = reason_code


class ScenarioRunner:
    """Evaluate a scenario independently from the implementation's output."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
        run_id_factory: Callable[[], object] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._timer = timer or perf_counter
        self._run_id_factory = run_id_factory or uuid4

    def run_read_only(
        self,
        definition: ScenarioDefinition,
        *,
        fixture_hash: str,
        workflow: ScenarioReadOnlyWorkflow,
    ) -> ScenarioRunResult:
        """Run preparation and optional comparison with zero write authority."""

        run_id = str(self._run_id_factory())
        started_at = self._now()
        expectation_hash = content_hash(
            definition.expectations.model_dump(mode="json")
        )
        phase_seconds: dict[str, float] = {}

        def result(
            *,
            status: ScenarioRunStatus,
            reason_code: ScenarioReasonCode = ScenarioReasonCode.NONE,
            failure_stage: ScenarioFailureStage | None = None,
            preparation: ScenarioPreparationEvidence | None = None,
            comparison: ScenarioComparisonEvidence | None = None,
        ) -> ScenarioRunResult:
            expected = (
                definition.expectations.first_comparison.as_classification_counts()
                if definition.expectations.first_comparison is not None
                else {}
            )
            return ScenarioRunResult(
                contract_version=definition.contract_version,
                run_id=run_id,
                scenario_id=definition.scenario_id,
                scenario_hash=definition.semantic_hash,
                fixture_hash=fixture_hash,
                expectation_hash=expectation_hash,
                started_at=started_at,
                completed_at=self._now(),
                status=status,
                reason_code=reason_code,
                failure_stage=failure_stage,
                prepared_rows=preparation.prepared_rows if preparation else 0,
                source_issues=preparation.source_issues if preparation else 0,
                expected_first_comparison=expected,
                actual_first_comparison=comparison.counts if comparison else {},
                target_hash=comparison.target_hash if comparison else "",
                preflight_hash=comparison.preflight_hash if comparison else "",
                odoo_version=comparison.odoo_version if comparison else "",
                module_versions_hash=(
                    comparison.module_versions_hash if comparison else ""
                ),
                phase_seconds=phase_seconds,
                write_attempt_count=0,
            )

        if definition.execution.write_policy is not ScenarioWritePolicy.READ_ONLY:
            return result(
                status=ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                reason_code=ScenarioReasonCode.UNSUPPORTED_SCENARIO_STAGE,
                failure_stage=ScenarioFailureStage.EXECUTION,
            )

        preparation_started = self._timer()
        try:
            preparation = workflow.prepare(definition)
        except ScenarioWorkflowFailure as exc:
            phase_seconds["preparation"] = self._timer() - preparation_started
            return result(
                status=ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                reason_code=exc.reason_code,
                failure_stage=exc.stage,
            )
        phase_seconds["preparation"] = self._timer() - preparation_started
        expected_preparation = (
            definition.expectations.prepared_rows,
            definition.expectations.source_issues,
        )
        actual_preparation = (preparation.prepared_rows, preparation.source_issues)
        if actual_preparation != expected_preparation:
            return result(
                status=ScenarioRunStatus.NEEDS_ATTENTION,
                reason_code=ScenarioReasonCode.PREPARATION_EXPECTATION_MISMATCH,
                failure_stage=ScenarioFailureStage.PREPARATION,
                preparation=preparation,
            )

        if definition.execution.stop_after is ScenarioStopAfter.PREPARATION:
            return result(status=ScenarioRunStatus.PASSED, preparation=preparation)

        comparison_started = self._timer()
        try:
            comparison = workflow.compare(definition, preparation)
        except ScenarioWorkflowFailure as exc:
            phase_seconds["first_comparison"] = self._timer() - comparison_started
            return result(
                status=ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                reason_code=exc.reason_code,
                failure_stage=exc.stage,
                preparation=preparation,
            )
        phase_seconds["first_comparison"] = self._timer() - comparison_started
        expected_counts = (
            definition.expectations.first_comparison.as_classification_counts()
            if definition.expectations.first_comparison is not None
            else {}
        )
        if comparison.counts != expected_counts:
            return result(
                status=ScenarioRunStatus.NEEDS_ATTENTION,
                reason_code=ScenarioReasonCode.COMPARISON_EXPECTATION_MISMATCH,
                failure_stage=ScenarioFailureStage.FIRST_COMPARISON,
                preparation=preparation,
                comparison=comparison,
            )

        blocked = comparison.counts.get("BLOCKED", 0) + comparison.counts.get(
            "AMBIGUOUS", 0
        )
        if definition.expectations.expected_outcome is ScenarioExpectedOutcome.EXPECTED_BLOCK:
            if blocked == 0:
                return result(
                    status=ScenarioRunStatus.NEEDS_ATTENTION,
                    reason_code=ScenarioReasonCode.EXPECTED_BLOCK_NOT_OBSERVED,
                    failure_stage=ScenarioFailureStage.FIRST_COMPARISON,
                    preparation=preparation,
                    comparison=comparison,
                )
            return result(
                status=ScenarioRunStatus.EXPECTED_BLOCK_PASSED,
                preparation=preparation,
                comparison=comparison,
            )
        if blocked:
            return result(
                status=ScenarioRunStatus.NEEDS_ATTENTION,
                reason_code=ScenarioReasonCode.UNEXPECTED_BLOCKER,
                failure_stage=ScenarioFailureStage.FIRST_COMPARISON,
                preparation=preparation,
                comparison=comparison,
            )
        return result(
            status=ScenarioRunStatus.PASSED,
            preparation=preparation,
            comparison=comparison,
        )

    def run_write(
        self,
        definition: ScenarioDefinition,
        *,
        fixture_hash: str,
        workflow: ScenarioWriteWorkflow,
    ) -> ScenarioRunResult:
        """Run a complete disposable write, reconciliation, and repeat proof."""

        run_id = str(self._run_id_factory())
        started_at = self._now()
        expectation_hash = content_hash(
            definition.expectations.model_dump(mode="json")
        )
        phase_seconds: dict[str, float] = {}
        preparation: ScenarioPreparationEvidence | None = None
        first: ScenarioComparisonEvidence | None = None
        execution: ScenarioExecutionEvidence | None = None
        reconciliation: ScenarioReconciliationEvidence | None = None
        projection: ScenarioProjectionEvidence | None = None
        repeated: ScenarioComparisonEvidence | None = None

        expected_first = (
            definition.expectations.first_comparison.as_classification_counts()
            if definition.expectations.first_comparison is not None
            else {}
        )
        expected_reconciliation = (
            {
                "verified": definition.expectations.reconciliation.verified,
                "fallout": definition.expectations.reconciliation.fallout,
                "outcome_unknown": (
                    definition.expectations.reconciliation.outcome_unknown
                ),
            }
            if definition.expectations.reconciliation is not None
            else {}
        )
        expected_repeat = (
            definition.expectations.repeat_comparison.as_classification_counts()
            if definition.expectations.repeat_comparison is not None
            else {}
        )
        expected_execution = {
            "committed": expected_first.get("CREATE", 0)
            + expected_first.get("UPDATE", 0),
            "failed": 0,
            "partially_applied": 0,
            "outcome_unknown": 0,
        }

        def finish(
            status: ScenarioRunStatus,
            *,
            stage: ScenarioFailureStage | None = None,
            reason: ScenarioReasonCode = ScenarioReasonCode.NONE,
        ) -> ScenarioRunResult:
            actual_execution = (
                {
                    "committed": execution.committed,
                    "failed": execution.failed,
                    "partially_applied": execution.partially_applied,
                    "outcome_unknown": execution.outcome_unknown,
                }
                if execution is not None
                else {}
            )
            actual_reconciliation = (
                {
                    "verified": reconciliation.verified,
                    "fallout": reconciliation.fallout,
                    "outcome_unknown": reconciliation.outcome_unknown,
                }
                if reconciliation is not None
                else {}
            )
            return ScenarioRunResult(
                contract_version=definition.contract_version,
                run_id=run_id,
                scenario_id=definition.scenario_id,
                scenario_hash=definition.semantic_hash,
                fixture_hash=fixture_hash,
                expectation_hash=expectation_hash,
                started_at=started_at,
                completed_at=self._now(),
                status=status,
                reason_code=reason,
                failure_stage=stage,
                prepared_rows=preparation.prepared_rows if preparation else 0,
                source_issues=preparation.source_issues if preparation else 0,
                expected_first_comparison=expected_first,
                actual_first_comparison=first.counts if first else {},
                expected_execution=expected_execution,
                actual_execution=actual_execution,
                expected_reconciliation=expected_reconciliation,
                actual_reconciliation=actual_reconciliation,
                expected_repeat_comparison=expected_repeat,
                actual_repeat_comparison=repeated.counts if repeated else {},
                expected_projection_records=(
                    projection.expected_records if projection else 0
                ),
                verified_projection_records=(
                    projection.verified_records if projection else 0
                ),
                projection_difference_count=(
                    projection.difference_count if projection else 0
                ),
                target_hash=first.target_hash if first else "",
                preflight_hash=first.preflight_hash if first else "",
                execution_snapshot_hash=(
                    execution.snapshot_hash if execution else ""
                ),
                reconciliation_hash=(
                    reconciliation.result_hash if reconciliation else ""
                ),
                odoo_version=first.odoo_version if first else "",
                module_versions_hash=first.module_versions_hash if first else "",
                phase_seconds=phase_seconds,
                write_attempt_count=execution.total if execution else 0,
            )

        if (
            definition.execution.write_policy
            is not ScenarioWritePolicy.DISPOSABLE_SCENARIO_ONLY
            or definition.execution.stop_after
            not in {
                ScenarioStopAfter.RECONCILIATION,
                ScenarioStopAfter.REPEAT_COMPARISON,
            }
        ):
            return finish(
                ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                stage=ScenarioFailureStage.EXECUTION,
                reason=ScenarioReasonCode.UNSUPPORTED_SCENARIO_STAGE,
            )

        try:
            phase_started = self._timer()
            preparation = workflow.prepare(definition)
            phase_seconds["preparation"] = self._timer() - phase_started
            if (
                preparation.prepared_rows,
                preparation.source_issues,
            ) != (
                definition.expectations.prepared_rows,
                definition.expectations.source_issues,
            ):
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.PREPARATION,
                    reason=ScenarioReasonCode.PREPARATION_EXPECTATION_MISMATCH,
                )

            phase_started = self._timer()
            first = workflow.compare(definition, preparation)
            phase_seconds["first_comparison"] = self._timer() - phase_started
            if first.counts != expected_first:
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.FIRST_COMPARISON,
                    reason=ScenarioReasonCode.COMPARISON_EXPECTATION_MISMATCH,
                )
            if first.counts.get("BLOCKED", 0) or first.counts.get("AMBIGUOUS", 0):
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.FIRST_COMPARISON,
                    reason=ScenarioReasonCode.UNEXPECTED_BLOCKER,
                )

            phase_started = self._timer()
            execution = workflow.execute(definition, preparation, first)
            phase_seconds["execution"] = self._timer() - phase_started
            if execution.partially_applied or execution.outcome_unknown:
                return finish(
                    ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                    stage=ScenarioFailureStage.EXECUTION,
                    reason=ScenarioReasonCode.UNSAFE_WRITE_OUTCOME,
                )
            actual_execution = {
                "committed": execution.committed,
                "failed": execution.failed,
                "partially_applied": execution.partially_applied,
                "outcome_unknown": execution.outcome_unknown,
            }
            if actual_execution != expected_execution:
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.EXECUTION,
                    reason=ScenarioReasonCode.EXECUTION_EXPECTATION_MISMATCH,
                )

            phase_started = self._timer()
            reconciliation = workflow.reconcile(
                definition,
                preparation,
                execution,
            )
            phase_seconds["reconciliation"] = self._timer() - phase_started
            actual_reconciliation = {
                "verified": reconciliation.verified,
                "fallout": reconciliation.fallout,
                "outcome_unknown": reconciliation.outcome_unknown,
            }
            if reconciliation.outcome_unknown:
                return finish(
                    ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                    stage=ScenarioFailureStage.RECONCILIATION,
                    reason=ScenarioReasonCode.UNSAFE_WRITE_OUTCOME,
                )
            if actual_reconciliation != expected_reconciliation:
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.RECONCILIATION,
                    reason=ScenarioReasonCode.RECONCILIATION_EXPECTATION_MISMATCH,
                )

            phase_started = self._timer()
            projection = workflow.verify_projection(
                definition,
                preparation,
                reconciliation,
            )
            phase_seconds["target_projection"] = self._timer() - phase_started
            if (
                projection.difference_count
                or projection.verified_records != projection.expected_records
            ):
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.RECONCILIATION,
                    reason=ScenarioReasonCode.TARGET_PROJECTION_MISMATCH,
                )

            if definition.execution.stop_after is ScenarioStopAfter.RECONCILIATION:
                return finish(ScenarioRunStatus.PASSED)

            phase_started = self._timer()
            repeated = workflow.compare(definition, preparation)
            phase_seconds["repeat_comparison"] = self._timer() - phase_started
            if repeated.counts != expected_repeat:
                return finish(
                    ScenarioRunStatus.NEEDS_ATTENTION,
                    stage=ScenarioFailureStage.REPEAT_COMPARISON,
                    reason=(
                        ScenarioReasonCode.REPEAT_COMPARISON_EXPECTATION_MISMATCH
                    ),
                )
            return finish(ScenarioRunStatus.PASSED)
        except ScenarioWorkflowFailure as exc:
            return finish(
                ScenarioRunStatus.UNSAFE_TO_CONTINUE,
                stage=exc.stage,
                reason=exc.reason_code,
            )
