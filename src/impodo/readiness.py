"""Compatibility surface for browser preparation and read-only preflight."""

from __future__ import annotations

from typing import Protocol

from .access import Actor, AuthorizationPolicy
from .application.errors import ReadinessError
from .application.normalization_service import NormalizationService
from .application.preflight_service import (
    MANIFEST_NAME,
    PreflightService,
    ReadinessReader,
)
from .application.preparation_service import (
    BROWSER_EVALUATION_ROW_LIMIT,
    BrowserEvaluationScale,
    PreparationService,
    PreparedReadinessContext,
    browser_evaluation_scale,
    canonical_source_hashes,
    require_supported_browser_scale,
    stage_browser_mapping,
)
from .application.quality_service import QualityService
from .application.readiness_ports import (
    NormalizationRepository,
    PreflightRepository,
    PreparationRepository,
    QualityRepository,
)
from .artifacts import ArtifactStore
from .domain.preflight.reports import (
    ReadinessDataset,
    ReadinessReport,
    ReadinessRow,
)
from .domain.contracts import (
    READINESS_CONTRACT_VERSION,
    TRANSFORMATION_IMPACT_CONTRACT_VERSION,
    TRANSFORMATION_IMPACT_DETAIL_LIMIT,
)
from .domain.staging.evaluator import (
    StagedBrowserMapping,
    evaluate_browser_mapping,
)
from .domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    TransformationImpactPage,
    TransformationImpactReport,
    TransformationImpactRow,
    TransformationImpactSnapshot,
)
from .governance import DryRun
from .normalization import (
    NormalizationEvaluation,
    NormalizationReviewGroup,
    NormalizationRunSummary,
)
from .quality import QualityRun, QualityRunSummary
from .staging import StagingRunSummary


class ReadinessRepository(
    PreparationRepository,
    QualityRepository,
    NormalizationRepository,
    PreflightRepository,
    Protocol,
):
    """Compatibility aggregate; new services depend on the narrow ports above."""


class BrowserReadinessService:
    """Compatibility facade over preparation, quality, normalization and preflight."""

    def __init__(
        self,
        repository: ReadinessRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.authorization = authorization
        self.quality = QualityService(repository)
        self.normalization = NormalizationService(repository, authorization)
        self.preparation = PreparationService(
            repository,
            artifacts,
            authorization,
            self.quality,
            self.normalization,
        )
        self.preflight = PreflightService(repository, artifacts, authorization)
        self.engine = self.preflight.engine

    def current_report(self, project_id: str) -> ReadinessReport | None:
        return self.preflight.current_report(project_id)

    def current_staging(self, project_id: str) -> StagingRunSummary | None:
        return self.preflight.current_staging(project_id)

    def current_quality_summary(
        self, project_id: str
    ) -> QualityRunSummary | None:
        return self.quality.current_summary(project_id)

    def current_quality(self, project_id: str) -> QualityRun | None:
        return self.quality.current_run(project_id)

    def current_normalization_summary(
        self, project_id: str
    ) -> NormalizationRunSummary | None:
        return self.normalization.current_summary(project_id)

    def current_normalization_review(
        self,
        project_id: str,
    ) -> tuple[NormalizationRunSummary, NormalizationEvaluation, DryRun] | None:
        return self.normalization.current_review(project_id)

    def current_normalization_group_review(
        self,
        project_id: str,
    ) -> tuple[
        NormalizationRunSummary,
        tuple[NormalizationReviewGroup, ...],
        DryRun,
        int,
    ] | None:
        return self.normalization.current_group_review(project_id)

    def decide_normalization_group(
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
        return self.normalization.decide_group(
            project_id,
            run_id,
            group_id,
            approve=approve,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
        )

    def approve_normalization(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        return self.normalization.approve(
            project_id,
            run_id,
            expected_version=expected_version,
            actor=actor,
            reason=reason,
        )

    def prepare(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> NormalizationRunSummary:
        return self.preparation.prepare(project_id, actor=actor)

    def run(
        self,
        project_id: str,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        prepared = self.preparation.prepare_context(project_id, actor=actor)
        return self.preflight.compare(prepared, reader=reader, actor=actor)


_canonical_source_hashes = canonical_source_hashes


__all__ = [
    "BROWSER_EVALUATION_ROW_LIMIT",
    "BrowserEvaluationScale",
    "BrowserReadinessService",
    "MANIFEST_NAME",
    "PreparedReadinessContext",
    "READINESS_CONTRACT_VERSION",
    "ReadinessDataset",
    "ReadinessError",
    "ReadinessReport",
    "ReadinessRepository",
    "ReadinessRow",
    "StagedBrowserMapping",
    "TRANSFORMATION_IMPACT_CONTRACT_VERSION",
    "TRANSFORMATION_IMPACT_DETAIL_LIMIT",
    "TransformationImpactFilter",
    "TransformationImpactIdentity",
    "TransformationImpactPage",
    "TransformationImpactReport",
    "TransformationImpactRow",
    "TransformationImpactSnapshot",
    "_canonical_source_hashes",
    "browser_evaluation_scale",
    "evaluate_browser_mapping",
    "require_supported_browser_scale",
    "stage_browser_mapping",
]
