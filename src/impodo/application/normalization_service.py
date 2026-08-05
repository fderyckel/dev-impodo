"""Application service for prepared-value review and approval."""

from __future__ import annotations

from typing import Iterable

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.staging.evaluator import StagedBrowserMapping
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..governance import DryRun
from ..domain.mapping.artifacts import MappingRevision
from ..normalization import (
    NormalizationCandidate,
    NormalizationError,
    NormalizationEvaluation,
    NormalizationReviewGroup,
    NormalizationRunSummary,
    evaluate_normalization,
)
from ..projects import MigrationProject
from ..quality import QualityRun, QualityRunSummary
from ..staging import StagingRunSummary
from ..workspace_contracts import SourceSelection
from ..domain.errors import ReadinessError
from .readiness_ports import NormalizationRepository


class NormalizationService:
    """Evaluate, review, decide, and freeze target-independent changes."""

    def __init__(
        self,
        repository: NormalizationRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def current_summary(self, project_id: str) -> NormalizationRunSummary | None:
        return self.repository.get_current_normalization_summary(project_id)

    def current_review(
        self,
        project_id: str,
    ) -> tuple[NormalizationRunSummary, NormalizationEvaluation, DryRun] | None:
        summary = self.current_summary(project_id)
        if summary is None:
            return None
        evaluation = self.repository.get_normalization_evaluation(
            project_id, summary.run_id
        )
        dry_run = self.repository.get_normalization_dry_run(
            project_id, summary.run_id
        )
        if evaluation is None or dry_run is None:
            raise ReadinessError("Prepared review evidence is incomplete")
        return summary, evaluation, dry_run

    def current_group_review(
        self,
        project_id: str,
    ) -> tuple[
        NormalizationRunSummary,
        tuple[NormalizationReviewGroup, ...],
        DryRun,
        int,
    ] | None:
        summary = self.current_summary(project_id)
        if summary is None:
            return None
        groups, automatic_record_count = self.repository.get_normalization_review_groups(
            project_id, summary.run_id
        )
        dry_run = self.repository.get_normalization_dry_run(
            project_id, summary.run_id
        )
        if dry_run is None:
            raise ReadinessError("Prepared review evidence is incomplete")
        return summary, groups, dry_run, automatic_record_count

    def decide_group(
        self,
        project_id: str,
        run_id: str,
        group_id: str,
        *,
        approve: bool,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            project_id=project_id,
        )
        return self.repository.decide_normalization_group(
            project_id,
            run_id,
            group_id,
            approve=approve,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
        )

    def approve(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        self.authorization.require(
            actor,
            Capability.NORMALIZATION_APPROVE,
            project_id=project_id,
        )
        return self.repository.approve_and_freeze_normalization(
            project_id,
            run_id,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
        )

    def evaluate_and_publish(
        self,
        project: MigrationProject,
        revision: MappingRevision,
        selection: SourceSelection,
        staged: StagedBrowserMapping,
        staging: StagingRunSummary,
        quality_run: QualityRun,
        quality: QualityRunSummary,
        impact_rows: Iterable[TransformationImpactRow],
        source_hashes: dict[str, str],
        *,
        actor: Actor,
    ) -> NormalizationRunSummary:
        effective_by_id = {item.dataset_id: item for item in selection.datasets}
        mappings = {
            effective_by_id[item.dataset_id].name: item
            for item in revision.definition.datasets
        }
        candidates = (
            NormalizationCandidate(
                dataset=item.dataset,
                source_row=item.source_row,
                source_label=item.source_column,
                target_field=item.target_field,
                raw_display=item.raw_value,
                proposed_display=item.proposed_value,
                rules=item.rules,
                outcome=item.outcome,
                message=item.message,
            )
            for item in impact_rows
        )
        try:
            evaluation = evaluate_normalization(
                project=project,
                staging=staged.canonical_run,
                quality=quality_run,
                mappings=mappings,
                candidates=candidates,
            )
        except NormalizationError as error:
            raise ReadinessError(str(error)) from error
        return self.repository.publish_normalization_run(
            project.project_id,
            evaluation,
            staging_run_id=staging.run_id,
            quality_run_id=quality.run_id,
            source_hashes=source_hashes,
            actor=actor,
        )
