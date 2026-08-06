"""Persistence ports for the target-independent preparation pipeline.

Stages E-G depend on these protocols instead of DuckDB directly: preparation
publishes canonical staging, quality overlays eligibility and quarantine, and
normalization records review decisions before freezing the eligible dataset.
The preflight-only ports at the end consume those frozen artifacts in Stage H.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Protocol, Sequence

from ..access import Actor
from ..derived_entities import DerivedEntityPlan
from ..domain.mapping.artifacts import MappingRevision, MappingSubmission
from ..connectors import MetadataSnapshot, RecordSnapshot
from ..domain.preflight.reports import ReadinessReport, ReadinessRow, ReadinessRowPage
from ..domain.resolution import EffectiveDataset
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
from ..staging_contracts import (
    CanonicalControlTotal,
    CanonicalStagingRun,
    StagingDatasetRole,
)
from ..workspace_contracts import MappingWorkingDraft, SourceSelection
from ..domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    PreparationSessionSummary,
    PreparedSessionRow,
    StoredCanonicalStagingRun,
)
from ..domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
)
from ..models import Issue


class PreparationProjectRepository(Protocol):
    """Load the project policy and ownership context used during preparation."""

    def get(self, project_id: str) -> MigrationProject:
        """Return the migration project identified by ``project_id``."""
        ...


class PreparationSourceRepository(Protocol):
    """Read physical/effective frozen sources and their inspected catalogs."""

    def get_source_selection(self, project_id: str) -> SourceSelection | None:
        """Return the frozen physical source selection."""
        ...

    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None:
        """Return the effective selection seen by mapping and staging."""
        ...

    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]:
        """Return inspected catalogs used to materialize the selected rows."""
        ...


class PreparationDerivedRepository(Protocol):
    """Load optional virtual datasets inserted between source and mapping."""

    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None:
        """Return the current derived-entity plan, if the project has one."""
        ...


class PreparationMappingRepository(Protocol):
    """Read the validated mapping revision and its immutable submission."""

    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None:
        """Return a published mapping revision, defaulting to the current one."""
        ...

    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None:
        """Return the submitted evidence paired with a mapping revision."""
        ...


class PreparationStagingRepository(Protocol):
    """Publish and reload immutable canonical staging runs for Stage E."""

    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun | StoredCanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary:
        """Persist ``run`` and return its durable lifecycle summary."""
        ...

    def get_canonical_staging_run(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_content_hash: str | None = None,
    ) -> CanonicalStagingRun | None:
        """Reload a run, optionally rejecting content-hash drift."""
        ...


class PreparationSessionRepository(Protocol):
    """Store unpublished bounded batches until canonical publication succeeds."""

    def begin_session(
        self,
        project_id: str,
        bindings: PreparationSessionBindings,
    ) -> PreparationSessionSummary: ...

    def append_provisional_rows(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[PreparedSessionRow],
    ) -> None: ...

    def append_impacts(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[TransformationImpactRow],
    ) -> None: ...

    def append_session_batch(
        self,
        project_id: str,
        session_id: str,
        rows: Sequence[PreparedSessionRow | CanonicalPreparedSessionRow],
        impacts: Sequence[TransformationImpactRow],
    ) -> None: ...

    def finalize_session(
        self,
        project_id: str,
        session_id: str,
        *,
        modes: Mapping[str, str],
        field_sources: Mapping[str, Mapping[str, tuple[str, ...]]],
        dataset_evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
        run_issues: Sequence[Issue],
        control_totals: Sequence[CanonicalControlTotal],
        impact_report: TransformationImpactReport,
    ) -> StoredCanonicalStagingRun: ...

    def physical_rows(
        self,
        project_id: str,
        session_id: str,
    ) -> dict[str, tuple[int, ...]]: ...

    def iter_impacts(
        self,
        project_id: str,
        session_id: str,
    ) -> Iterable[TransformationImpactRow]: ...

    def mark_published(self, project_id: str, session_id: str) -> None: ...

    def fail_session(
        self,
        project_id: str,
        session_id: str,
        failure_code: str,
    ) -> None: ...

class QualityMappingRepository(Protocol):
    """Supply the published and editable mapping state used by Stage F."""

    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None:
        """Return the published mapping to which checks must be bound."""
        ...

    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None:
        """Return the draft used to detect unsaved semantic changes."""
        ...


class QualitySourceRepository(Protocol):
    """Supply the effective datasets against which quality rules are scoped."""

    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None:
        """Return effective datasets/names used to scope generated rules."""
        ...


class QualityRepository(Protocol):
    """Persist versioned rulesets and their hash-bound quality evaluations."""

    def get_current_quality_ruleset(
        self, project_id: str
    ) -> QualityRuleSet | None:
        """Return the ruleset selected by the current pointer."""
        ...

    def publish_quality_ruleset(
        self,
        project_id: str,
        ruleset: QualityRuleSet,
        *,
        actor: Actor,
    ) -> QualityRuleSet:
        """Publish a new ruleset version and retire dependent quality output."""
        ...

    def publish_quality_run(
        self,
        project_id: str,
        run: QualityRun,
        *,
        staging_run_id: str,
        effective_dataset_run_id: str | None = None,
        actor: Actor,
    ) -> QualityRunSummary:
        """Atomically persist a full overlay and advance the current pointer."""
        ...

    def get_current_quality_summary(
        self, project_id: str
    ) -> QualityRunSummary | None:
        """Return the current quality lifecycle/count projection."""
        ...

    def get_quality_run(self, project_id: str, run_id: str) -> QualityRun | None:
        """Reload the full immutable overlay referenced by ``run_id``."""
        ...


class NormalizationRepository(Protocol):
    """Own Stage G evaluation, group decisions, approval, and dataset freeze.

    Implementations must use lifecycle versions for optimistic concurrency so
    two reviewers cannot silently overwrite one another's decisions.
    """
    def publish_normalization_run(
        self,
        project_id: str,
        evaluation: NormalizationEvaluation,
        *,
        staging_run_id: str,
        quality_run_id: str,
        source_hashes: dict[str, str],
        actor: Actor,
    ) -> NormalizationRunSummary:
        """Create review evidence bound to staging, quality, and source hashes."""
        ...
    def get_current_normalization_summary(
        self, project_id: str
    ) -> NormalizationRunSummary | None:
        """Return the current review/freeze lifecycle summary."""
        ...

    def get_normalization_evaluation(
        self, project_id: str, run_id: str
    ) -> NormalizationEvaluation | None:
        """Reload immutable effects and groups for a review run."""
        ...

    def get_normalization_dry_run(
        self, project_id: str, run_id: str
    ) -> DryRun | None:
        """Reload the mutable, versioned decision state for a review run."""
        ...

    def get_normalization_review_groups(
        self, project_id: str, run_id: str
    ) -> tuple[tuple[NormalizationReviewGroup, ...], int]:
        """Return review groups plus the automatic affected-record count."""
        ...
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
        """Record one group decision and return the updated lifecycle summary."""
        ...
    def approve_and_freeze_normalization(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        """Approve all resolved groups and freeze the eligible dataset hash."""
        ...


class PreflightStagingRepository(Protocol):
    """Load the current Stage-E summary and its immutable canonical rows."""

    def get_current_staging_summary(
        self, project_id: str
    ) -> StagingRunSummary | None:
        """Return the staging run selected by the current pointer."""
        ...

    def get_canonical_staging_run(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_content_hash: str | None = None,
    ) -> CanonicalStagingRun | None:
        """Reload full canonical evidence, optionally requiring its hash."""
        ...


class PreflightQualityRepository(Protocol):
    """Load the current Stage-F eligibility summary and full overlay."""

    def get_current_quality_summary(
        self, project_id: str
    ) -> QualityRunSummary | None:
        """Return the quality run selected by the current pointer."""
        ...

    def get_quality_run(self, project_id: str, run_id: str) -> QualityRun | None:
        """Reload all row dispositions, accounting, issues, and quarantine."""
        ...


class PreflightEffectiveRepository(Protocol):
    """Load the frozen post-resolution rows selected for quality."""

    def get_current_effective_dataset(
        self,
        project_id: str,
    ) -> EffectiveDataset | None:
        """Return current effective rows, if advanced resolution is active."""
        ...

class PreflightNormalizationRepository(Protocol):
    """Load Stage-G freeze identity and its versioned approval evidence."""

    def get_current_normalization_summary(
        self, project_id: str
    ) -> NormalizationRunSummary | None:
        """Return the current review run and eligible-dataset hash."""
        ...

    def get_normalization_dry_run(
        self, project_id: str, run_id: str
    ) -> DryRun | None:
        """Return the approval state used to prove the run is frozen."""
        ...


class PreflightProjectRepository(Protocol):
    """Load target identity and project policy for comparison publication."""

    def get(self, project_id: str) -> MigrationProject:
        """Return the migration project identified by ``project_id``."""
        ...


class PreflightSourceRepository(Protocol):
    """Load the effective frozen selection used by the submitted mapping."""

    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None:
        """Return stable dataset/column identities used to compile Stage H."""
        ...


class PreflightMappingRepository(Protocol):
    """Load the current mapping revision and proof it was submitted."""

    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None:
        """Return the immutable mapping revision to compile for comparison."""
        ...

    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None:
        """Return exact submission evidence for the requested revision."""
        ...


class PreflightRepository(Protocol):
    """Publish and query portable readiness evidence plus protected snapshots.

    The report lookup takes every upstream identity explicitly so stale runs
    cannot be returned through a loose project-level current pointer.
    """

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
    ) -> ReadinessReport | None:
        """Return the current report only when every supplied binding matches."""
        ...
    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        decision_rows: Iterable[ReadinessRow],
        decision_count: int,
        metadata_snapshot: MetadataSnapshot,
        record_snapshot: RecordSnapshot,
        actor: Actor,
    ) -> None:
        """Atomically store header, streamed rows, snapshots, pointer, and audit."""
        ...
    def get_readiness_rows(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str = "",
        dataset: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> ReadinessRowPage:
        """Return a validated, filtered page of portable decision rows."""
        ...
