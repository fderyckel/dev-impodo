"""Narrow persistence ports used by preparation and preflight services."""

from __future__ import annotations

from typing import Protocol

from ..access import Actor
from ..derived_entities import DerivedEntityPlan
from ..domain.mapping.artifacts import MappingRevision, MappingSubmission
from ..connectors import MetadataSnapshot, RecordSnapshot
from ..domain.preflight.reports import ReadinessReport, ReadinessRowPage
from ..governance import DryRun
from ..inspection import SourceFileCatalog
from ..normalization import (
    NormalizationEvaluation,
    NormalizationReviewGroup,
    NormalizationRunSummary,
)
from ..projects import MigrationProject
from ..quality import QualityRuleSet, QualityRun, QualityRunSummary
from ..staging import StagingRunSummary
from ..staging_contracts import CanonicalStagingRun
from ..workspace_contracts import MappingWorkingDraft, SourceSelection


class PreparationProjectRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...


class PreparationSourceRepository(Protocol):
    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...
    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...


class PreparationDerivedRepository(Protocol):
    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None: ...


class PreparationMappingRepository(Protocol):
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None: ...


class PreparationStagingRepository(Protocol):
    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary: ...


class QualityMappingRepository(Protocol):
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None: ...


class QualitySourceRepository(Protocol):
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...


class QualityRepository(Protocol):
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


class PreflightStagingRepository(Protocol):
    def get_current_staging_summary(
        self, project_id: str
    ) -> StagingRunSummary | None: ...
    def get_canonical_staging_run(
        self, project_id: str, run_id: str
    ) -> CanonicalStagingRun | None: ...


class PreflightQualityRepository(Protocol):
    def get_current_quality_summary(
        self, project_id: str
    ) -> QualityRunSummary | None: ...
    def get_quality_run(self, project_id: str, run_id: str) -> QualityRun | None: ...


class PreflightNormalizationRepository(Protocol):
    def get_current_normalization_summary(
        self, project_id: str
    ) -> NormalizationRunSummary | None: ...
    def get_normalization_dry_run(
        self, project_id: str, run_id: str
    ) -> DryRun | None: ...


class PreflightProjectRepository(Protocol):
    def get(self, project_id: str) -> MigrationProject: ...


class PreflightSourceRepository(Protocol):
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...


class PreflightMappingRepository(Protocol):
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None: ...


class PreflightRepository(Protocol):
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
        normalization_run_id: str,
        normalization_content_hash: str,
        normalization_lifecycle_version: int,
        eligible_dataset_hash: str,
    ) -> ReadinessReport | None: ...
    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        metadata_snapshot: MetadataSnapshot,
        record_snapshot: RecordSnapshot,
        actor: Actor,
    ) -> None: ...
    def get_readiness_rows(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str = "",
        dataset: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> ReadinessRowPage: ...
