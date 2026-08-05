"""Narrow persistence ports used by preparation and preflight services."""

from __future__ import annotations

from typing import Protocol

from ..access import Actor
from ..derived_entities import DerivedEntityPlan
from ..domain.preflight.reports import ReadinessReport
from ..inspection import SourceFileCatalog
from ..mapping_semantics import MappingRevision, MappingSubmission
from ..normalization import (
    NormalizationEvaluation,
    NormalizationReviewGroup,
    NormalizationRunSummary,
)
from ..governance import DryRun
from ..projects import MigrationProject
from ..quality import QualityRuleSet, QualityRun, QualityRunSummary
from ..staging import StagingRunSummary
from ..staging_contracts import CanonicalStagingRun
from ..workspace import MappingWorkingDraft, SourceSelection


class PreparationRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...
    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...
    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...
    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None: ...
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None: ...
    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary: ...


class QualityRepository(Protocol):
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...
    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None: ...
    def get_current_quality_ruleset(
        self, project_id: str
    ) -> QualityRuleSet | None: ...
    def publish_quality_ruleset(
        self,
        project_id: str,
        ruleset: QualityRuleSet,
        *,
        actor: Actor,
    ) -> QualityRuleSet: ...
    def publish_quality_run(
        self,
        project_id: str,
        run: QualityRun,
        *,
        staging_run_id: str,
        actor: Actor,
    ) -> QualityRunSummary: ...
    def get_current_quality_summary(
        self, project_id: str
    ) -> QualityRunSummary | None: ...
    def get_quality_run(self, project_id: str, run_id: str) -> QualityRun | None: ...


class NormalizationRepository(Protocol):
    def publish_normalization_run(
        self,
        project_id: str,
        evaluation: NormalizationEvaluation,
        *,
        staging_run_id: str,
        quality_run_id: str,
        source_hashes: dict[str, str],
        actor: Actor,
    ) -> NormalizationRunSummary: ...
    def get_current_normalization_summary(
        self, project_id: str
    ) -> NormalizationRunSummary | None: ...
    def get_normalization_evaluation(
        self, project_id: str, run_id: str
    ) -> NormalizationEvaluation | None: ...
    def get_normalization_dry_run(
        self, project_id: str, run_id: str
    ) -> DryRun | None: ...
    def get_normalization_review_groups(
        self, project_id: str, run_id: str
    ) -> tuple[tuple[NormalizationReviewGroup, ...], int]: ...
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
    ) -> NormalizationRunSummary: ...
    def approve_and_freeze_normalization(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary: ...


class PreflightRepository(Protocol):
    def get_current_staging_summary(
        self, project_id: str
    ) -> StagingRunSummary | None: ...
    def get_current_quality_summary(
        self, project_id: str
    ) -> QualityRunSummary | None: ...
    def get_current_normalization_summary(
        self, project_id: str
    ) -> NormalizationRunSummary | None: ...
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None: ...
    def get_readiness_report(
        self,
        project_id: str,
        mapping_id: str,
        mapping_version: int,
        mapping_content_hash: str,
        staging_run_id: str,
        staging_content_hash: str,
        quality_run_id: str,
        quality_content_hash: str,
    ) -> ReadinessReport | None: ...
    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        actor: Actor,
    ) -> None: ...
