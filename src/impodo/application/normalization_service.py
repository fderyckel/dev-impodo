"""Application service for prepared-value review and approval."""

from __future__ import annotations

from typing import Iterable

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..domain.resolution import EffectiveDataset
from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..governance import DryRun
from ..domain.mapping.artifacts import MappingRevision
from ..normalization import (
    NormalizationCandidate,
    NormalizationError,
    NormalizationEvaluation,
    NormalizationPolicyError,
    NormalizationReviewGroup,
    NormalizationRunSummary,
    StoredNormalizationEvaluation,
    evaluate_normalization,
)
from ..projects import WorkspaceState
from ..quality import QualityRun, QualityRunSummary, StoredQualityRun
from ..staging import StagingRunSummary
from ..staging_contracts import CanonicalStagingRun
from ..workspace_contracts import SourceSelection
from ..domain.errors import NormalizationReviewPolicyError, ReadinessError
from .readiness_ports import NormalizationRepository
from .bounded_normalization import (
    BoundedNormalizationUnsupported,
    build_bounded_normalization_evaluation,
)


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
        """Return the lifecycle/count projection for the current Stage-G run."""

        return self.repository.get_current_normalization_summary(project_id)

    def current_review(
        self,
        project_id: str,
    ) -> tuple[NormalizationRunSummary, NormalizationEvaluation, DryRun] | None:
        """Load the current summary, immutable evaluation, and decision state."""

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
        """Load paginable groups with their shared governance decision state."""

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
        """Authorize and record one approve/reject group decision."""

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
        """Accept pending groups, approve, and freeze the eligible dataset."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            project_id=project_id,
        )
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

    def reopen_review(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        """Reopen the current review after an accidental send-back decision."""

        self.authorization.require(
            actor,
            Capability.NORMALIZATION_DECIDE,
            project_id=project_id,
        )
        return self.repository.reopen_normalization_review(
            project_id,
            run_id,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
        )

    def evaluate_and_publish(
        self,
        project: WorkspaceState,
        revision: MappingRevision,
        selection: SourceSelection,
        canonical_run: CanonicalStagingRun | StoredCanonicalStagingRun,
        staging: StagingRunSummary,
        quality_run: QualityRun | StoredQualityRun,
        quality: QualityRunSummary,
        impact_rows: Iterable[TransformationImpactRow],
        source_hashes: dict[str, str],
        effective: EffectiveDataset | None = None,
        *,
        actor: Actor,
        allow_materialized_fallback: bool = True,
    ) -> NormalizationRunSummary:
        """Convert impact rows into Stage-G evidence and publish a review run.

        The service joins effective dataset names to mapping definitions,
        adapts streamed transformation impacts into candidates, and binds the
        evaluation to the already published staging and quality hashes.
        """

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
            if (
                isinstance(canonical_run, StoredCanonicalStagingRun)
                and isinstance(quality_run, StoredQualityRun)
            ):
                try:
                    evaluation: (
                        NormalizationEvaluation
                        | StoredNormalizationEvaluation
                    ) = build_bounded_normalization_evaluation(
                        project=project,
                        staging=canonical_run,
                        quality=quality_run,
                        mappings=mappings,
                        impact_rows=impact_rows,
                        staging_content_hash=staging.content_hash,
                        quality_content_hash=quality.content_hash,
                        effective=effective,
                    )
                except BoundedNormalizationUnsupported as error:
                    if not allow_materialized_fallback:
                        raise ReadinessError(
                            "The review-evidence route could not stay bounded "
                            "for this project. Whole-run fallback is disabled "
                            "above the materialized safety limit; no fallback "
                            "was run."
                        ) from error
                    evaluation = evaluate_normalization(
                        project=project,
                        staging=canonical_run,
                        quality=quality_run,
                        mappings=mappings,
                        candidates=candidates,
                        published_staging_content_hash=staging.content_hash,
                        published_quality_content_hash=quality.content_hash,
                        effective=effective,
                    )
            else:
                evaluation = evaluate_normalization(
                    project=project,
                    staging=canonical_run,
                    quality=quality_run,
                    mappings=mappings,
                    candidates=candidates,
                    published_staging_content_hash=staging.content_hash,
                    published_quality_content_hash=quality.content_hash,
                    effective=effective,
                )
        except NormalizationPolicyError as error:
            raise NormalizationReviewPolicyError(str(error)) from error
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
