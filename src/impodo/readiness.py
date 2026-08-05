"""Browser mapping staging and row-level read-only readiness checks.

This module joins the submitted browser mapping to the existing preflight
engine. Source artifacts remain immutable, parent/child preparation is
repeated over every row, and Odoo requirements are planned in batches rather
than requested inside a source-row loop.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
import unicodedata
from uuid import UUID, uuid4

from .access import Actor, AuthorizationPolicy, Capability
from .artifacts import ArtifactStore
from .connectors import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from .derived_entities import (
    DerivedDatasetLink,
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
    _display_path,
    _normalized_path,
    derived_dataset_links,
)
from .engine import PreflightEngine
from .inspection import SourceFileCatalog
from .mapping_semantics import (
    DatasetMapping,
    MappingDefinition,
    MappingRevision,
    MappingSubmission,
    RelationshipResolver,
    ResolverOrigin,
    ScalarValueError,
    ScalarValueRuleError,
    ScalarValueSource,
    evaluate_scalar_mapping_value,
)
from .models import (
    Classification,
    Decision,
    InvalidPreparedValue,
    Issue,
    PreflightResult,
    Severity,
    canonical_json_bytes,
    portable_value,
    target_identity_hash,
)
from .governance import DryRun
from .normalization import (
    NormalizationCandidate,
    NormalizationEvaluation,
    NormalizationError,
    NormalizationReviewGroup,
    NormalizationRunSummary,
    evaluate_normalization,
)
from .planner import plan_metadata_requests, plan_record_requests
from .profile import (
    DatasetSpec,
    FieldSpec,
    IdentityComponent,
    NormalizationSpec,
    ProfileDocument,
    ProfileIdentity,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from .projects import MigrationProject, SourceFile
from .quality import (
    QualityError,
    QualityRuleSet,
    QualityRun,
    QualityRunSummary,
    default_quality_ruleset,
    eligible_prepared_bundle,
    evaluate_quality,
)
from .source import (
    PreparedBundle,
    SourceRow,
    SourceTable,
    load_selected_source_table,
    prepare_source_tables,
)
from .staging import StagingRunSummary
from .staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    CanonicalControlTotal,
    CanonicalStagingRun,
    StagingDatasetRole,
)
from .workspace import SourceDataset, SourceSelection, WorkspaceError


READINESS_CONTRACT_VERSION = 3
MANIFEST_NAME = "impodo_preflight_manifest.json"
TRANSFORMATION_IMPACT_DETAIL_LIMIT = 5_000
TRANSFORMATION_IMPACT_CONTRACT_VERSION = 1
BROWSER_EVALUATION_ROW_LIMIT = 25_000


class ReadinessError(WorkspaceError):
    """Raised when the current browser evidence cannot be checked safely."""


@dataclass(frozen=True, slots=True)
class BrowserEvaluationScale:
    """Plain-language supported-size decision for the in-memory evaluator."""

    physical_rows: int
    supported_limit: int = BROWSER_EVALUATION_ROW_LIMIT

    @property
    def supported(self) -> bool:
        return self.physical_rows <= self.supported_limit


def browser_evaluation_scale(selection: SourceSelection) -> BrowserEvaluationScale:
    """Count frozen physical rows once, before any derived datasets expand them."""

    return BrowserEvaluationScale(
        physical_rows=sum(item.row_count for item in selection.datasets)
    )


def require_supported_browser_scale(selection: SourceSelection) -> None:
    scale = browser_evaluation_scale(selection)
    if scale.supported:
        return
    raise ReadinessError(
        f"This project contains {scale.physical_rows:,} source rows. "
        f"This version of Impodo can safely check up to "
        f"{scale.supported_limit:,} rows in one project. Split the source into "
        "smaller projects before checking; no data was changed."
    )


@dataclass(frozen=True, slots=True)
class TransformationImpactRow:
    """One visible raw-to-proposed scalar value change."""

    dataset: str
    source_row: int
    source_column: str
    target_field: str
    raw_value: str
    proposed_value: str
    rules: str
    outcome: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class TransformationImpactReport:
    """Bounded browser projection with complete all-row outcome counts."""

    mapping_content_hash: str
    evaluated_count: int
    changed_count: int
    fallback_count: int
    null_count: int
    invalid_count: int
    provided_count: int
    unchanged_count: int
    rows: tuple[TransformationImpactRow, ...]
    detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT

    @property
    def impact_count(self) -> int:
        return (
            self.changed_count
            + self.fallback_count
            + self.null_count
            + self.invalid_count
            + self.provided_count
        )

    @property
    def truncated(self) -> bool:
        return self.impact_count > len(self.rows)


@dataclass(frozen=True, slots=True)
class TransformationImpactIdentity:
    """Hash-bound identity for one reusable transformation-impact snapshot."""

    physical_selection_hash: str
    source_selection_hash: str
    mapping_content_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    contract_version: int = TRANSFORMATION_IMPACT_CONTRACT_VERSION
    evaluator_version: int = BROWSER_EVALUATOR_VERSION

    @property
    def content_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(
                {
                    "physical_selection_hash": self.physical_selection_hash,
                    "source_selection_hash": self.source_selection_hash,
                    "mapping_content_hash": self.mapping_content_hash,
                    "schema_hash": self.schema_hash,
                    "derived_plan_hash": self.derived_plan_hash,
                    "contract_version": self.contract_version,
                    "evaluator_version": self.evaluator_version,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class TransformationImpactSnapshot:
    """Complete counts for one persisted, filterable impact projection."""

    identity: TransformationImpactIdentity
    created_at: datetime
    created_by: str
    affected_row_count: int
    report: TransformationImpactReport


@dataclass(frozen=True, slots=True)
class TransformationImpactFilter:
    """Server-side filters shared by the browser table and CSV export."""

    dataset: str = ""
    outcome: str = ""
    target_field: str = ""
    query: str = ""


@dataclass(frozen=True, slots=True)
class TransformationImpactPage:
    """One bounded, deterministically ordered impact-result page."""

    rows: tuple[TransformationImpactRow, ...]
    matching_count: int
    start_position: int
    end_position: int
    previous_before: int | None
    next_after: int | None


@dataclass(slots=True)
class _TransformationImpactCollector:
    mapping_content_hash: str
    detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT
    evaluated_count: int = 0
    changed_count: int = 0
    fallback_count: int = 0
    null_count: int = 0
    invalid_count: int = 0
    provided_count: int = 0
    unchanged_count: int = 0
    rows: list[TransformationImpactRow] | None = None
    sink: Callable[[TransformationImpactRow], None] | None = None

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = []

    def record(
        self,
        *,
        dataset: str,
        source_row: int,
        source_column: str,
        target_field: str,
        raw_value: object,
        proposed_value: object,
        rules: str,
        outcome: str,
        message: str = "",
    ) -> None:
        self.evaluated_count += 1
        attribute = f"{outcome}_count"
        setattr(self, attribute, getattr(self, attribute) + 1)
        if outcome == "unchanged":
            return
        impact = TransformationImpactRow(
            dataset=dataset,
            source_row=source_row,
            source_column=source_column,
            target_field=target_field,
            raw_value=_display_value(raw_value),
            proposed_value=_display_value(proposed_value),
            rules=rules,
            outcome=outcome,
            message=message,
        )
        if self.sink is not None:
            self.sink(impact)
        if len(self.rows or ()) >= self.detail_limit:
            return
        assert self.rows is not None
        self.rows.append(impact)

    def report(self) -> TransformationImpactReport:
        return TransformationImpactReport(
            mapping_content_hash=self.mapping_content_hash,
            evaluated_count=self.evaluated_count,
            changed_count=self.changed_count,
            fallback_count=self.fallback_count,
            null_count=self.null_count,
            invalid_count=self.invalid_count,
            provided_count=self.provided_count,
            unchanged_count=self.unchanged_count,
            rows=tuple(self.rows or ()),
            detail_limit=self.detail_limit,
        )


@dataclass(frozen=True, slots=True)
class ReadinessRow:
    dataset: str
    dataset_label: str
    source_row: int
    status: str
    classification: str
    identity: str
    reason: str
    field: str
    recommended_action: str
    technical_code: str
    issue_count: int = 0


@dataclass(frozen=True, slots=True)
class ReadinessDataset:
    dataset: str
    label: str
    target_model: str
    total: int
    ready: int
    needs_review: int
    blocked: int


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    run_id: str
    project_id: str
    mapping_id: str
    mapping_version: int
    mapping_content_hash: str
    staging_run_id: str
    staging_content_hash: str
    quality_run_id: str
    quality_content_hash: str
    target_hash: str
    checked_at: datetime
    checked_by: str
    datasets: tuple[ReadinessDataset, ...]
    rows: tuple[ReadinessRow, ...]
    contract_version: int = READINESS_CONTRACT_VERSION

    @property
    def ready_count(self) -> int:
        return sum(item.ready for item in self.datasets)

    @property
    def needs_review_count(self) -> int:
        return sum(item.needs_review for item in self.datasets)

    @property
    def blocked_count(self) -> int:
        return sum(item.blocked for item in self.datasets)

    @property
    def total_count(self) -> int:
        return sum(item.total for item in self.datasets)

    @property
    def status(self) -> str:
        if self.blocked_count:
            return "BLOCKED"
        if self.needs_review_count:
            return "NEEDS_REVIEW"
        return "READY"

    def to_json(self) -> str:
        payload = asdict(self)
        payload["checked_at"] = self.checked_at.isoformat()
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> "ReadinessReport":
        payload = json.loads(value)
        if int(payload.get("contract_version", 0)) != READINESS_CONTRACT_VERSION:
            raise ValueError("Readiness report contract version is unsupported")
        return cls(
            run_id=str(payload["run_id"]),
            project_id=str(payload["project_id"]),
            mapping_id=str(payload["mapping_id"]),
            mapping_version=int(payload["mapping_version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            staging_run_id=str(payload["staging_run_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            quality_run_id=str(payload["quality_run_id"]),
            quality_content_hash=str(payload["quality_content_hash"]),
            target_hash=str(payload["target_hash"]),
            checked_at=datetime.fromisoformat(str(payload["checked_at"])),
            checked_by=str(payload["checked_by"]),
            datasets=tuple(
                ReadinessDataset(**item) for item in payload.get("datasets", ())
            ),
            rows=tuple(ReadinessRow(**item) for item in payload.get("rows", ())),
        )


class ReadinessRepository(Protocol):
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

    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary: ...

    def get_current_staging_summary(
        self,
        project_id: str,
    ) -> StagingRunSummary | None: ...

    def get_current_quality_ruleset(
        self,
        project_id: str,
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
        self,
        project_id: str,
    ) -> QualityRunSummary | None: ...

    def get_quality_run(
        self,
        project_id: str,
        run_id: str,
    ) -> QualityRun | None: ...

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
        self,
        project_id: str,
    ) -> NormalizationRunSummary | None: ...

    def get_normalization_evaluation(
        self,
        project_id: str,
        run_id: str,
    ) -> NormalizationEvaluation | None: ...

    def get_normalization_dry_run(
        self,
        project_id: str,
        run_id: str,
    ) -> DryRun | None: ...

    def get_normalization_review_groups(
        self,
        project_id: str,
        run_id: str,
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

    def save_readiness_report(
        self,
        project_id: str,
        report: ReadinessReport,
        *,
        actor: Actor,
    ) -> None: ...

    def project_directory(self, project_id: str) -> Path: ...


ReadinessReader = Callable[
    [tuple[MetadataRequest, ...], tuple[RecordRequest, ...]],
    tuple[MetadataSnapshot, RecordSnapshot],
]


@dataclass(frozen=True, slots=True)
class StagedBrowserMapping:
    profile: ProfileDocument
    prepared: PreparedBundle
    canonical_run: CanonicalStagingRun
    dataset_labels: Mapping[str, str]
    source_field_labels: Mapping[tuple[str, str], str]
    physical_rows: Mapping[str, tuple[int, ...]]
    transformation_impact: TransformationImpactReport | None = None


@dataclass(frozen=True, slots=True)
class PreparedReadinessContext:
    project: MigrationProject
    revision: MappingRevision
    staged: StagedBrowserMapping
    staging: StagingRunSummary
    quality_run: QualityRun
    quality: QualityRunSummary
    normalization: NormalizationRunSummary


class BrowserReadinessService:
    """Run and persist one row-level check for the current submitted mapping."""

    def __init__(
        self,
        repository: ReadinessRepository,
        artifacts: ArtifactStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.authorization = authorization
        self.engine = PreflightEngine()

    def current_report(self, project_id: str) -> ReadinessReport | None:
        staging = self.repository.get_current_staging_summary(project_id)
        if staging is None:
            return None
        quality = self.repository.get_current_quality_summary(project_id)
        if quality is None or quality.staging_run_id != staging.run_id:
            return None
        normalization = self.repository.get_current_normalization_summary(
            project_id
        )
        if (
            normalization is None
            or not normalization.frozen
            or normalization.staging_run_id != staging.run_id
            or normalization.quality_run_id != quality.run_id
        ):
            return None
        revision = self.repository.get_mapping_revision(project_id)
        if revision is None:
            return None
        submission = self.repository.get_mapping_submission(
            project_id, revision.version
        )
        if submission is None:
            return None
        return self.repository.get_readiness_report(
            project_id,
            revision.mapping_id,
            revision.version,
            revision.definition.content_hash,
            staging.run_id,
            staging.content_hash,
            quality.run_id,
            quality.content_hash,
        )

    def current_staging(self, project_id: str) -> StagingRunSummary | None:
        return self.repository.get_current_staging_summary(project_id)

    def current_quality_summary(
        self,
        project_id: str,
    ) -> QualityRunSummary | None:
        return self.repository.get_current_quality_summary(project_id)

    def current_quality(self, project_id: str) -> QualityRun | None:
        summary = self.repository.get_current_quality_summary(project_id)
        if summary is None:
            return None
        return self.repository.get_quality_run(project_id, summary.run_id)

    def current_normalization_summary(
        self,
        project_id: str,
    ) -> NormalizationRunSummary | None:
        return self.repository.get_current_normalization_summary(project_id)

    def current_normalization_review(
        self,
        project_id: str,
    ) -> tuple[NormalizationRunSummary, NormalizationEvaluation, DryRun] | None:
        summary = self.repository.get_current_normalization_summary(project_id)
        if summary is None:
            return None
        evaluation = self.repository.get_normalization_evaluation(
            project_id,
            summary.run_id,
        )
        dry_run = self.repository.get_normalization_dry_run(
            project_id,
            summary.run_id,
        )
        if evaluation is None or dry_run is None:
            raise ReadinessError("Prepared review evidence is incomplete")
        return summary, evaluation, dry_run

    def current_normalization_group_review(
        self,
        project_id: str,
    ) -> tuple[
        NormalizationRunSummary,
        tuple[NormalizationReviewGroup, ...],
        DryRun,
        int,
    ] | None:
        """Return browser-sized group evidence without loading every effect."""

        summary = self.repository.get_current_normalization_summary(project_id)
        if summary is None:
            return None
        groups, automatic_record_count = (
            self.repository.get_normalization_review_groups(
                project_id,
                summary.run_id,
            )
        )
        dry_run = self.repository.get_normalization_dry_run(
            project_id,
            summary.run_id,
        )
        if dry_run is None:
            raise ReadinessError("Prepared review evidence is incomplete")
        return summary, groups, dry_run, automatic_record_count

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

    def approve_normalization(
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

    def prepare(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> NormalizationRunSummary:
        """Prepare and persist review evidence without contacting Odoo."""

        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        return self._prepare(project_id, actor=actor).normalization

    def run(
        self,
        project_id: str,
        *,
        reader: ReadinessReader,
        actor: Actor,
    ) -> ReadinessReport:
        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            project_id=project_id,
        )
        context = self._prepare(project_id, actor=actor)
        if not context.normalization.frozen:
            raise ReadinessError(
                "Approve the prepared data before comparing it with Odoo. "
                "Odoo was not contacted."
            )
        project = context.project
        revision = context.revision
        staged = context.staged
        publication = context.staging
        quality_run = context.quality_run
        quality = context.quality
        eligible = eligible_prepared_bundle(
            staged.canonical_run,
            staged.prepared,
            quality_run,
        )
        metadata_requests = plan_metadata_requests(staged.profile)
        record_requests = plan_record_requests(
            staged.profile,
            eligible.records,
        )
        metadata, records = reader(metadata_requests, record_requests)
        expected_target = target_identity_hash(
            connection_mode=(
                project.odoo_connection_mode.value
                if project.odoo_connection_mode is not None
                else ""
            ),
            base_url=project.odoo_base_url,
            database=project.odoo_database,
        )
        if metadata.fingerprint.target_hash != expected_target:
            raise ReadinessError("Readiness data came from a different Odoo target")
        result = self.engine.run(
            staged.profile,
            eligible,
            metadata,
            records,
        )
        run_id = str(uuid4())
        report = _readiness_report(
            run_id,
            project,
            revision,
            result,
            staged.dataset_labels,
            staged.source_field_labels,
            actor,
            publication,
            quality,
        )
        _write_manifest(self.repository, project_id, run_id, result)
        self.repository.save_readiness_report(
            project_id,
            report,
            actor=actor,
        )
        return report

    def _prepare(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> PreparedReadinessContext:
        project = self.repository.get(project_id)
        revision = self.repository.get_mapping_revision(project_id)
        if revision is None:
            raise ReadinessError("Submit the mapping before checking data")
        submission = self.repository.get_mapping_submission(
            project_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise ReadinessError("Submit the current mapping before checking data")
        physical_selection = self.repository.get_source_selection(project_id)
        effective_selection = self.repository.get_mapping_source_selection(project_id)
        if physical_selection is None or effective_selection is None:
            raise ReadinessError("Freeze the source datasets before checking data")

        impact_rows: list[TransformationImpactRow] = []
        staged = stage_browser_mapping(
            project,
            revision.definition,
            physical_selection,
            effective_selection,
            self.repository.get_derived_entity_plan(project_id),
            self.repository.get_source_catalogs(project_id),
            self.artifacts,
            collect_transformation_impact=True,
            transformation_detail_limit=0,
            transformation_impact_sink=impact_rows.append,
        )
        publication = self.repository.publish_canonical_staging(
            project_id,
            staged.canonical_run,
            mapping_version=revision.version,
            actor=actor,
        )
        ruleset = self.repository.get_current_quality_ruleset(project_id)
        if (
            ruleset is None
            or ruleset.mapping_hash != revision.definition.content_hash
            or ruleset.schema_hash != revision.definition.schema_hash
        ):
            ruleset = default_quality_ruleset(
                project_id=project_id,
                mapping_hash=revision.definition.content_hash,
                schema_hash=revision.definition.schema_hash,
                datasets=(item.name for item in effective_selection.datasets),
                version=(ruleset.version + 1 if ruleset is not None else 1),
                parent_version=(ruleset.version if ruleset is not None else None),
            )
            ruleset = self.repository.publish_quality_ruleset(
                project_id,
                ruleset,
                actor=actor,
            )
        try:
            quality_run = evaluate_quality(
                project=project,
                staging=staged.canonical_run,
                prepared=staged.prepared,
                physical_rows=staged.physical_rows,
                ruleset=ruleset,
            )
        except QualityError as error:
            raise ReadinessError(str(error)) from error
        quality = self.repository.publish_quality_run(
            project_id,
            quality_run,
            staging_run_id=publication.run_id,
            actor=actor,
        )
        if not quality.can_compare:
            raise ReadinessError(
                "Fix the data-check setup shown below, then check all rows again. "
                "Odoo was not contacted."
            )
        effective_by_id = {
            item.dataset_id: item for item in effective_selection.datasets
        }
        mappings = {
            effective_by_id[item.dataset_id].name: item
            for item in revision.definition.datasets
        }
        candidates = tuple(
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
            normalization_evaluation = evaluate_normalization(
                project=project,
                staging=staged.canonical_run,
                quality=quality_run,
                mappings=mappings,
                candidates=candidates,
            )
        except NormalizationError as error:
            raise ReadinessError(str(error)) from error
        source_hashes = {
            item.file_id: (
                item.source_sha256
                if item.source_sha256.startswith("sha256:")
                else f"sha256:{item.source_sha256}"
            )
            for item in physical_selection.datasets
        }
        normalization = self.repository.publish_normalization_run(
            project_id,
            normalization_evaluation,
            staging_run_id=publication.run_id,
            quality_run_id=quality.run_id,
            source_hashes=source_hashes,
            actor=actor,
        )
        return PreparedReadinessContext(
            project=project,
            revision=revision,
            staged=staged,
            staging=publication,
            quality_run=quality_run,
            quality=quality,
            normalization=normalization,
        )


def stage_browser_mapping(
    project: MigrationProject,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: ArtifactStore,
    *,
    collect_transformation_impact: bool = False,
    transformation_detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT,
    transformation_impact_sink: Callable[[TransformationImpactRow], None]
    | None = None,
) -> StagedBrowserMapping:
    """Load frozen artifacts, then delegate to the reusable evaluator."""

    require_supported_browser_scale(physical_selection)
    loaded = _load_browser_source_tables(
        project,
        physical_selection,
        catalogs,
        artifacts,
    )
    return evaluate_browser_mapping(
        project_id=project.project_id,
        definition=definition,
        physical_selection=physical_selection,
        effective_selection=effective_selection,
        plan=plan,
        loaded_tables=loaded,
        collect_transformation_impact=collect_transformation_impact,
        transformation_detail_limit=transformation_detail_limit,
        transformation_impact_sink=transformation_impact_sink,
    )


def evaluate_browser_mapping(
    *,
    project_id: str,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
    loaded_tables: Mapping[str, SourceTable],
    collect_transformation_impact: bool = False,
    transformation_detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT,
    transformation_impact_sink: Callable[[TransformationImpactRow], None]
    | None = None,
) -> StagedBrowserMapping:
    """Evaluate every frozen row without storage access or Odoo access.

    ``loaded_tables`` is keyed by physical dataset identifier.  The caller owns
    artifact materialization; this function owns mapping compilation,
    target-independent normalization, issue collection, lineage, and row
    reconciliation.
    """

    require_supported_browser_scale(physical_selection)
    if (
        physical_selection.project_id != project_id
        or effective_selection.project_id != project_id
        or (plan is not None and plan.project_id != project_id)
    ):
        raise ReadinessError("Canonical evaluation evidence belongs to another project")
    if (
        plan is not None
        and plan.source_selection_hash != physical_selection.content_hash
    ):
        raise ReadinessError(
            "The related-record plan no longer matches its source data"
        )
    if definition.source_selection_hash != effective_selection.content_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    effective_by_id = {item.dataset_id: item for item in effective_selection.datasets}
    mapping_by_id = {item.dataset_id: item for item in definition.datasets}
    if len(effective_by_id) != len(effective_selection.datasets):
        raise ReadinessError("The frozen source contains duplicate dataset identifiers")
    if len(mapping_by_id) != len(definition.datasets):
        raise ReadinessError("The submitted mapping contains duplicate datasets")
    if set(mapping_by_id) != set(effective_by_id):
        raise ReadinessError("The submitted mapping does not cover every dataset")
    physical_by_id = {item.dataset_id: item for item in physical_selection.datasets}
    if len(physical_by_id) != len(physical_selection.datasets):
        raise ReadinessError(
            "The physical source contains duplicate dataset identifiers"
        )
    split_by_name = {
        name: (rule, role)
        for rule in (plan.rules if plan else ())
        if isinstance(rule, RelatedDatasetRule)
        for name, role in (
            (rule.parent_dataset_name, "parent"),
            (rule.child_dataset_name, "child"),
        )
    }
    lookup_links = derived_dataset_links(plan)
    lookup_rules = tuple(
        item
        for item in (plan.rules if plan else ())
        if isinstance(item, DerivedEntityRule)
    )
    lookup_by_dataset_id = {
        link.derived_dataset_id: (rule, link)
        for link, rule in zip(lookup_links, lookup_rules, strict=True)
    }
    impact_collector = (
        _TransformationImpactCollector(
            definition.content_hash,
            detail_limit=transformation_detail_limit,
            sink=transformation_impact_sink,
        )
        if collect_transformation_impact
        else None
    )
    lookup_by_consumer: dict[
        str,
        list[tuple[DerivedEntityRule, DerivedDatasetLink]],
    ] = {}
    for link, rule in zip(lookup_links, lookup_rules, strict=True):
        lookup_by_consumer.setdefault(link.consumer_dataset_id, []).append(
            (rule, link)
        )

    if set(loaded_tables) != set(physical_by_id):
        raise ReadinessError("Loaded source tables do not match the frozen selection")
    for dataset_id, table in loaded_tables.items():
        physical = physical_by_id[dataset_id]
        if table.dataset != physical.name:
            raise ReadinessError("A loaded source table has the wrong dataset name")
        expected_hash = physical.source_sha256.removeprefix("sha256:")
        if table.content_hash != f"sha256:{expected_hash}":
            raise ReadinessError("Stored source content changed after selection")

    profile = _compile_profile(definition, effective_selection)
    staged_tables: list[SourceTable] = []
    preparation_issues: list[Issue] = []
    source_labels: dict[tuple[str, str], str] = {}
    source_lineage: dict[
        tuple[str, int], tuple[str, tuple[int, ...]]
    ] = {}
    dataset_evidence: dict[
        str, tuple[str, StagingDatasetRole, int]
    ] = {}
    for dataset_spec in profile.datasets:
        effective = next(
            item
            for item in effective_selection.datasets
            if item.name == dataset_spec.name
        )
        mapping = mapping_by_id[effective.dataset_id]
        lookup = lookup_by_dataset_id.get(effective.dataset_id)
        split = split_by_name.get(effective.name)
        if lookup is not None:
            lookup_rule, lookup_link = lookup
            physical = physical_by_id.get(lookup_rule.source_dataset_id)
            role = "lookup"
            rule = None
        elif split is None:
            physical = physical_by_id.get(effective.dataset_id)
            role = "source"
            rule = None
        else:
            rule, role = split
            physical = physical_by_id.get(rule.source_dataset_id)
        if physical is None:
            raise ReadinessError("Prepared dataset no longer has a source")
        if lookup is not None:
            staged, issues, row_lineage = _stage_derived_table(
                effective,
                physical,
                mapping,
                loaded_tables[physical.dataset_id],
                lookup_rule,
                lookup_link,
                impact_collector=impact_collector,
            )
        else:
            staged, issues, row_lineage = _stage_table(
                effective,
                physical,
                mapping,
                loaded_tables[physical.dataset_id],
                rule,
                role,
                tuple(lookup_by_consumer.get(effective.dataset_id, ())),
                impact_collector=impact_collector,
            )
        staged_tables.append(staged)
        preparation_issues.extend(issues)
        source_lineage.update(
            {
                (effective.name, source_row): (
                    physical.dataset_id,
                    physical_rows,
                )
                for source_row, physical_rows in row_lineage.items()
            }
        )
        dataset_evidence[effective.name] = (
            physical.dataset_id,
            {
                "source": StagingDatasetRole.DIRECT,
                "parent": StagingDatasetRole.PARENT,
                "child": StagingDatasetRole.CHILD,
                "lookup": StagingDatasetRole.LOOKUP,
            }[role],
            len(loaded_tables[physical.dataset_id].rows),
        )
        for column in effective.columns:
            source_labels[(effective.name, column.stable_key)] = column.source_name
        column_name_by_key = {
            column.stable_key: column.source_name for column in effective.columns
        }
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            source_labels[(effective.name, _synthetic_field(index))] = (
                column_name_by_key.get(field.source_column_key or "")
                or field.target_field
            )

    prepared = prepare_source_tables(
        profile,
        staged_tables,
        source_hashes={
            item.name: f"sha256:{item.source_sha256.removeprefix('sha256:')}"
            for item in effective_selection.datasets
        },
    )
    if preparation_issues:
        prepared = _attach_preparation_issues(
            prepared,
            preparation_issues,
        )
    canonical_run = CanonicalStagingRun.from_prepared(
        project_id=project_id,
        mapping_id=definition.mapping_id,
        physical_selection_hash=physical_selection.content_hash,
        source_selection_hash=effective_selection.content_hash,
        mapping_hash=definition.content_hash,
        schema_hash=definition.schema_hash,
        derived_plan_hash=plan.content_hash if plan is not None else None,
        profile=profile,
        prepared=prepared,
        field_sources=_canonical_field_sources(definition, effective_selection),
        source_lineage=source_lineage,
        dataset_evidence=dataset_evidence,
        control_totals=_evaluate_control_totals(
            definition,
            effective_selection,
            prepared,
        ),
    )
    return StagedBrowserMapping(
        profile=profile,
        prepared=prepared,
        canonical_run=canonical_run,
        dataset_labels={
            item.name: item.name.replace("_", " ").title()
            for item in effective_selection.datasets
        },
        source_field_labels=source_labels,
        physical_rows={
            dataset_id: tuple(row.number for row in table.rows)
            for dataset_id, table in sorted(loaded_tables.items())
        },
        transformation_impact=(
            impact_collector.report() if impact_collector is not None else None
        ),
    )


def _evaluate_control_totals(
    definition: MappingDefinition,
    selection: SourceSelection,
    prepared: PreparedBundle,
) -> tuple[CanonicalControlTotal, ...]:
    """Evaluate only explicitly declared sums over canonical numeric values."""

    dataset_name_by_id = {
        item.dataset_id: item.name for item in selection.datasets
    }
    records_by_dataset = prepared.by_dataset()
    results: list[CanonicalControlTotal] = []
    for dataset in definition.datasets:
        dataset_name = dataset_name_by_id[dataset.dataset_id]
        records = records_by_dataset.get(dataset_name, ())
        for control in dataset.control_totals:
            actual = Decimal("0")
            included_rows = 0
            empty_rows = 0
            for record in records:
                value = record.scalar_values.get(control.target_field)
                if value is None:
                    empty_rows += 1
                    continue
                if isinstance(value, bool) or not isinstance(
                    value, (int, Decimal)
                ):
                    raise ReadinessError(
                        f"The named total {control.name!r} did not produce "
                        "numeric prepared values"
                    )
                actual += Decimal(value)
                included_rows += 1
            control_id = "sha256:" + sha256(
                canonical_json_bytes(
                    {
                        "mapping_hash": definition.content_hash,
                        "dataset": dataset_name,
                        "name": control.name,
                        "target_field": control.target_field,
                        "expected_total": control.expected_total,
                        "unit": control.unit,
                        "tolerance": control.tolerance,
                    }
                )
            ).hexdigest()
            results.append(
                CanonicalControlTotal(
                    control_id=control_id,
                    name=control.name,
                    dataset=dataset_name,
                    target_field=control.target_field,
                    expected_total=control.expected_total,
                    actual_total=format(actual, "f"),
                    tolerance=control.tolerance,
                    unit=control.unit,
                    included_rows=included_rows,
                    empty_rows=empty_rows,
                )
            )
    return tuple(sorted(results, key=lambda item: item.control_id))


def _load_browser_source_tables(
    project: MigrationProject,
    physical_selection: SourceSelection,
    catalogs: Iterable[SourceFileCatalog],
    artifacts: ArtifactStore,
) -> dict[str, SourceTable]:
    """Materialize and validate physical tables before pure evaluation."""

    catalog_by_file = {item.file_id: item for item in catalogs}
    source_file_by_id = {item.file_id: item for item in project.source_files}
    loaded: dict[str, SourceTable] = {}
    with ExitStack() as stack:
        for physical in physical_selection.datasets:
            source_file = source_file_by_id.get(physical.file_id)
            catalog = catalog_by_file.get(physical.file_id)
            table_catalog = next(
                (
                    item
                    for item in (catalog.tables if catalog else ())
                    if item.table_key == physical.table_key
                ),
                None,
            )
            if source_file is None or catalog is None or table_catalog is None:
                raise ReadinessError("Frozen source evidence is incomplete")
            path = stack.enter_context(
                artifacts.materialize_source(
                    project.project_id,
                    source_file.stored_name,
                )
            )
            named_range = (
                table_catalog.named_tables[0].cell_range
                if table_catalog.kind == "NAMED_TABLE"
                and table_catalog.named_tables
                else None
            )
            loaded[physical.dataset_id] = load_selected_source_table(
                path,
                dataset=physical.name,
                table_key=physical.table_key,
                encoding=physical.encoding,
                delimiter=physical.delimiter,
                header_row=physical.header_row,
                named_table_range=named_range,
            )
    return loaded


def _canonical_field_sources(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Describe which source columns govern each proposed target value."""

    dataset_by_id = {item.dataset_id: item for item in selection.datasets}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for mapping in definition.datasets:
        dataset = dataset_by_id[mapping.dataset_id]
        fields: dict[str, tuple[str, ...]] = {
            "$source_identity": mapping.source_identity_column_keys,
        }
        for component in (*mapping.target_identity, *mapping.target_scope):
            for target_field in component.target_fields:
                fields[target_field] = component.source_column_keys
        for field in mapping.fields:
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            fields[field.target_field] = (
                (field.source_column_key,) if field.source_column_key else ()
            )
        for relationship in mapping.relationships:
            fields[relationship.target_field] = relationship.source_column_keys
        result[dataset.name] = dict(sorted(fields.items()))
    return result


def _compile_profile(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> ProfileDocument:
    datasets = {item.dataset_id: item for item in selection.datasets}
    mappings = {item.dataset_id: item for item in definition.datasets}

    def resolver(value: RelationshipResolver) -> ResolveSpec:
        if value.origin is ResolverOrigin.DATASET:
            target_mapping = mappings.get(str(value.dataset_id))
            target_dataset = datasets.get(str(value.dataset_id))
            if target_mapping is None or target_dataset is None:
                raise ReadinessError("A mapped relationship dataset is missing")
            return ResolveSpec(
                dataset=target_dataset.name,
                target_source_fields=target_mapping.source_identity_column_keys,
            )
        return ResolveSpec(
            target_model=value.model,
            target_fields=tuple(item.target_field for item in value.key_mappings),
            target_scope_fields=tuple(
                item.target_field for item in value.scope_mappings
            ),
        )

    profile_datasets: list[DatasetSpec] = []
    for mapping in definition.datasets:
        source_dataset = datasets[mapping.dataset_id]
        scalar_fields = {}
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            scalar_fields[field.target_field] = FieldSpec(
                source=_synthetic_field(index),
                type=field.value_type,
                required=field.required,
                required_on_create=field.required_on_create,
                compare=field.compare,
                validate_only=field.validate_only,
                normalize=NormalizationSpec(empty_as_null=True),
                null_policy=field.null_policy,
            )
        relations = {
            item.target_field: RelationSpec(
                kind=item.kind,
                source_fields=item.source_column_keys,
                resolve=resolver(item.resolver),
                compare=item.compare,
                validate_only=item.validate_only,
                required=item.required,
                required_on_create=item.required_on_create,
                on_missing=item.on_missing,
                on_ambiguous=item.on_ambiguous,
                operation=item.operation,
                separator=item.separator,
                null_policy=item.null_policy,
            )
            for item in mapping.relationships
        }
        identity_normalization = NormalizationSpec(
            trim=True,
            collapse_whitespace=True,
            empty_as_null=True,
        )
        profile_datasets.append(
            DatasetSpec(
                name=source_dataset.name,
                source=SourceSpec(file=f"{source_dataset.name}.csv"),
                target=TargetSpec(
                    model=mapping.target_model,
                    mode=mapping.mode.value,
                    on_existing=mapping.on_existing,
                ),
                source_identity=SourceIdentitySpec(
                    fields=mapping.source_identity_column_keys
                ),
                target_identity=TargetIdentitySpec(
                    components=tuple(
                        IdentityComponent(
                            source_fields=item.source_column_keys,
                            target_fields=item.target_fields,
                            type=item.value_type,
                            normalize=identity_normalization,
                            resolve=(
                                resolver(item.resolver)
                                if item.resolver is not None
                                else None
                            ),
                        )
                        for item in mapping.target_identity
                    ),
                    scope=tuple(
                        IdentityComponent(
                            source_fields=item.source_column_keys,
                            target_fields=item.target_fields,
                            type=item.value_type,
                            normalize=identity_normalization,
                            resolve=(
                                resolver(item.resolver)
                                if item.resolver is not None
                                else None
                            ),
                        )
                        for item in mapping.target_scope
                    ),
                ),
                fields=scalar_fields,
                relations=relations,
            )
        )
    token = definition.content_hash.removeprefix("sha256:")[:24]
    return ProfileDocument(
        profile=ProfileIdentity(
            id=f"browser_{token}",
            description="Compiled from a submitted Impodo browser mapping",
        ),
        datasets=tuple(profile_datasets),
    )


def _stage_table(
    effective: SourceDataset,
    physical: SourceDataset,
    mapping: DatasetMapping,
    table: SourceTable,
    rule: RelatedDatasetRule | None,
    role: str,
    lookup_bindings: tuple[
        tuple[DerivedEntityRule, DerivedDatasetLink], ...
    ] = (),
    *,
    impact_collector: _TransformationImpactCollector | None = None,
) -> tuple[
    SourceTable,
    tuple[Issue, ...],
    dict[int, tuple[int, ...]],
]:
    source_name_by_key = {
        column.stable_key: column.source_name for column in physical.columns
    }
    staged_rows: list[SourceRow] = []
    issues: list[Issue] = []
    parent_row_by_key: dict[tuple[str, ...], int] = {}
    source_rows_by_output: dict[int, list[int]] = {}
    for row in table.rows:
        values = {
            column.stable_key: row.values.get(source_name_by_key[column.stable_key])
            for column in effective.columns
        }
        if rule is not None:
            for key in (
                rule.parent_key_column_key,
                rule.scope_column_key,
                rule.child_key_column_key if role == "child" else None,
            ):
                if key is not None and key in values:
                    values[key] = _normalized_key(values.get(key))
        if role == "parent" and rule is not None:
            keys = tuple(
                values.get(key)
                for key in (
                    rule.parent_key_column_key,
                    rule.scope_column_key,
                )
                if key is not None
            )
            if all(value is not None for value in keys):
                canonical = tuple(str(value) for value in keys)
                existing_row = parent_row_by_key.get(canonical)
                if existing_row is not None:
                    source_rows_by_output[existing_row].append(row.number)
                    continue
                parent_row_by_key[canonical] = row.number
        issues.extend(
            _normalize_derived_references(
                values,
                mapping,
                lookup_bindings,
                dataset=effective.name,
                source_row=row.number,
            )
        )
        _record_identity_preparation(
            values,
            effective,
            mapping,
            source_row=row.number,
            impact_collector=impact_collector,
        )
        _apply_relationship_value_mappings(
            values,
            effective,
            mapping,
            source_row=row.number,
            impact_collector=impact_collector,
        )
        _apply_scalar_mappings(
            values,
            effective,
            mapping,
            source_row=row.number,
            impact_collector=impact_collector,
        )
        staged_rows.append(SourceRow(number=row.number, values=values))
        source_rows_by_output[row.number] = [row.number]
    headers = (
        *(column.stable_key for column in effective.columns),
        *(
            _synthetic_field(index)
            for index, field in enumerate(mapping.fields)
            if field.value_source is not ScalarValueSource.ODOO_DEFAULT
        ),
    )
    return (
        SourceTable(
            dataset=effective.name,
            path=table.path,
            headers=tuple(headers),
            rows=tuple(staged_rows),
            content_hash=table.content_hash,
        ),
        tuple(issues),
        {
            output_row: tuple(source_rows)
            for output_row, source_rows in source_rows_by_output.items()
        },
    )


def _stage_derived_table(
    effective: SourceDataset,
    physical: SourceDataset,
    mapping: DatasetMapping,
    table: SourceTable,
    rule: DerivedEntityRule,
    link: DerivedDatasetLink,
    *,
    impact_collector: _TransformationImpactCollector | None = None,
) -> tuple[
    SourceTable,
    tuple[Issue, ...],
    dict[int, tuple[int, ...]],
]:
    """Materialize every unique related record from the full source table."""

    source_column = next(
        item
        for item in physical.columns
        if item.stable_key == rule.source_column_key
    )
    accumulated: dict[tuple[str, ...], dict[str, object]] = {}
    for row in table.rows:
        path = _normalized_path(
            row.values.get(source_column.source_name),
            rule.parent_separator,
        )
        if path is None:
            continue
        display_parts, key_parts = path
        if not display_parts:
            continue
        for depth in range(1, len(key_parts) + 1):
            key_path = key_parts[:depth]
            display_path = display_parts[:depth]
            entry = accumulated.setdefault(
                key_path,
                {
                    "name": display_path[-1],
                    "aliases": set(),
                    "source_row": row.number,
                    "source_rows": set(),
                },
            )
            aliases = entry["aliases"]
            assert isinstance(aliases, set)
            aliases.add(_display_path(display_path, rule.parent_separator))
            source_rows = entry["source_rows"]
            assert isinstance(source_rows, set)
            source_rows.add(row.number)

    rows: list[SourceRow] = []
    issues: list[Issue] = []
    source_rows_by_output: dict[int, tuple[int, ...]] = {}
    ordered_candidates = sorted(
        accumulated.items(),
        key=lambda item: (len(item[0]), item[0]),
    )
    for generated_row, (key_path, entry) in enumerate(
        ordered_candidates,
        start=2,
    ):
        values: dict[str, object] = {
            link.canonical_key_column_key: " / ".join(key_path),
            link.name_column_key: str(entry["name"]),
        }
        if link.parent_key_column_key is not None:
            values[link.parent_key_column_key] = (
                " / ".join(key_path[:-1]) if key_path[:-1] else None
            )
        _record_identity_preparation(
            values,
            effective,
            mapping,
            source_row=generated_row,
            impact_collector=impact_collector,
        )
        _apply_relationship_value_mappings(
            values,
            effective,
            mapping,
            source_row=generated_row,
            impact_collector=impact_collector,
        )
        _apply_scalar_mappings(
            values,
            effective,
            mapping,
            source_row=generated_row,
            impact_collector=impact_collector,
        )
        evidence_row = int(entry["source_row"])
        aliases = entry["aliases"]
        assert isinstance(aliases, set)
        if len(aliases) > 1:
            issues.append(
                Issue(
                    code="DERIVED_ALIAS_REVIEW_REQUIRED",
                    message=(
                        "multiple source spellings produce the same related "
                        "record; review the preferred display value "
                        f"(first seen at source row {evidence_row})"
                    ),
                    severity=Severity.ERROR,
                    dataset=effective.name,
                    row=generated_row,
                    field=link.name_column_key,
                )
            )
        rows.append(SourceRow(number=generated_row, values=values))
        source_rows = entry["source_rows"]
        assert isinstance(source_rows, set)
        source_rows_by_output[generated_row] = tuple(sorted(source_rows))

    headers = (
        *(column.stable_key for column in effective.columns),
        *(
            _synthetic_field(index)
            for index, field in enumerate(mapping.fields)
            if field.value_source is not ScalarValueSource.ODOO_DEFAULT
        ),
    )
    return (
        SourceTable(
            dataset=effective.name,
            path=table.path,
            headers=tuple(headers),
            rows=tuple(rows),
            content_hash=table.content_hash,
        ),
        tuple(issues),
        source_rows_by_output,
    )


def _normalize_derived_references(
    values: dict[str, object],
    mapping: DatasetMapping,
    lookup_bindings: tuple[
        tuple[DerivedEntityRule, DerivedDatasetLink], ...
    ],
    *,
    dataset: str,
    source_row: int,
) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    for rule, link in lookup_bindings:
        relationship = next(
            (
                item
                for item in mapping.relationships
                if item.resolver.origin is ResolverOrigin.DATASET
                and item.resolver.dataset_id == link.derived_dataset_id
                and link.source_column_key in item.source_column_keys
            ),
            None,
        )
        if relationship is None:
            continue
        path = _normalized_path(
            values.get(link.source_column_key),
            rule.parent_separator,
        )
        if path is not None and path[0]:
            values[link.source_column_key] = " / ".join(path[1])
            continue
        values[link.source_column_key] = None
        issues.append(
            Issue(
                code=(
                    "DERIVED_REFERENCE_QUARANTINED"
                    if rule.blank_policy == "quarantine"
                    else "DERIVED_REFERENCE_MISSING"
                ),
                message=(
                    "the source value cannot identify a related record in "
                    f"{rule.output_dataset_name}"
                ),
                severity=Severity.ERROR,
                dataset=dataset,
                row=source_row,
                field=relationship.target_field,
            )
        )
    return tuple(issues)


def _apply_scalar_mappings(
    values: dict[str, object],
    effective: SourceDataset,
    mapping: DatasetMapping,
    *,
    source_row: int,
    impact_collector: _TransformationImpactCollector | None = None,
) -> None:
    source_name_by_key = {
        column.stable_key: column.source_name for column in effective.columns
    }
    for index, field in enumerate(mapping.fields):
        if field.value_source is ScalarValueSource.ODOO_DEFAULT:
            continue
        raw = (
            values.get(field.source_column_key)
            if field.source_column_key is not None
            else None
        )
        try:
            proposed = evaluate_scalar_mapping_value(
                field,
                raw,
                source_values_by_ordinal={
                    column.ordinal: values.get(column.stable_key)
                    for column in effective.columns
                },
            )
            values[_synthetic_field(index)] = proposed
            if impact_collector is not None:
                outcome = _transformation_outcome(field, raw, proposed)
                impact_collector.record(
                    dataset=effective.name,
                    source_row=source_row,
                    source_column=(
                        source_name_by_key.get(field.source_column_key or "")
                        or "Constant value"
                    ),
                    target_field=field.target_field,
                    raw_value=raw,
                    proposed_value=proposed,
                    rules=_transformation_rule_summary(field),
                    outcome=outcome,
                )
        except ScalarValueRuleError as error:
            values[_synthetic_field(index)] = InvalidPreparedValue(
                code=error.code,
                message=str(error),
            )
            if impact_collector is not None:
                impact_collector.record(
                    dataset=effective.name,
                    source_row=source_row,
                    source_column=(
                        source_name_by_key.get(field.source_column_key or "")
                        or "Constant value"
                    ),
                    target_field=field.target_field,
                    raw_value=raw,
                    proposed_value="Invalid",
                    rules=_transformation_rule_summary(field),
                    outcome="invalid",
                    message=str(error),
                )
        except ScalarValueError as error:
            values[_synthetic_field(index)] = (
                None
                if "required value" in str(error).casefold()
                else "__impodo_invalid_value__"
            )
            if impact_collector is not None:
                impact_collector.record(
                    dataset=effective.name,
                    source_row=source_row,
                    source_column=(
                        source_name_by_key.get(field.source_column_key or "")
                        or "Constant value"
                    ),
                    target_field=field.target_field,
                    raw_value=raw,
                    proposed_value="Invalid",
                    rules=_transformation_rule_summary(field),
                    outcome="invalid",
                    message=str(error),
                )


def _record_identity_preparation(
    values: Mapping[str, object],
    effective: SourceDataset,
    mapping: DatasetMapping,
    *,
    source_row: int,
    impact_collector: _TransformationImpactCollector | None,
) -> None:
    """Expose identity whitespace cleanup as an explicit reviewable change."""

    if impact_collector is None:
        return
    labels = {item.stable_key: item.source_name for item in effective.columns}
    for component in (*mapping.target_identity, *mapping.target_scope):
        raw_values = tuple(values.get(key) for key in component.source_column_keys)
        proposed_values = tuple(
            (
                " ".join(str(value).strip().split())
                if value is not None and " ".join(str(value).strip().split())
                else None
            )
            for value in raw_values
        )
        if tuple(_display_value(item) for item in raw_values) == tuple(
            _display_value(item) for item in proposed_values
        ):
            continue
        raw_display = " | ".join(_display_value(item) for item in raw_values)
        proposed_display = " | ".join(
            _display_value(item) for item in proposed_values
        )
        source_label = " + ".join(
            labels.get(key, "Identity field")
            for key in component.source_column_keys
        )
        for target_field in component.target_fields:
            impact_collector.record(
                dataset=effective.name,
                source_row=source_row,
                source_column=source_label,
                target_field=target_field,
                raw_value=raw_display,
                proposed_value=proposed_display,
                rules="Identity preparation",
                outcome="changed",
            )


def _apply_relationship_value_mappings(
    values: dict[str, object],
    effective: SourceDataset,
    mapping: DatasetMapping,
    *,
    source_row: int,
    impact_collector: _TransformationImpactCollector | None = None,
) -> None:
    """Replace authored source choices with confirmed Odoo business keys."""

    for relationship in mapping.relationships:
        matches = relationship.resolver.value_mappings
        if not matches or len(relationship.source_column_keys) != 1:
            continue
        source_column = relationship.source_column_keys[0]
        raw_value = values.get(source_column)
        if raw_value is None:
            continue
        source_value = str(raw_value).strip()
        target_value = next(
            (
                item.target_value
                for item in matches
                if item.source_value == source_value
            ),
            None,
        )
        if target_value is not None:
            values[source_column] = target_value
            if impact_collector is not None:
                source_label = next(
                    (
                        item.source_name
                        for item in effective.columns
                        if item.stable_key == source_column
                    ),
                    "Matched value",
                )
                impact_collector.record(
                    dataset=effective.name,
                    source_row=source_row,
                    source_column=source_label,
                    target_field=relationship.target_field,
                    raw_value=raw_value,
                    proposed_value=target_value,
                    rules=(
                        f"Reviewed value match ({len(matches)} confirmed choice(s))"
                    ),
                    outcome=(
                        "changed"
                        if _display_value(raw_value) != _display_value(target_value)
                        else "unchanged"
                    ),
                )


def _transformation_outcome(
    field,
    raw_value: object,
    proposed_value: object,
) -> str:
    if field.value_source is ScalarValueSource.CONSTANT:
        return "provided"
    if (
        field.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK
        and _fallback_was_used(field, raw_value)
    ):
        return "fallback"
    if proposed_value is None and raw_value is not None:
        return "null"
    if _display_value(raw_value) != _display_value(proposed_value):
        return "changed"
    return "unchanged"


def _fallback_was_used(field, raw_value: object) -> bool:
    if raw_value is None:
        return True
    value = str(raw_value)
    if field.transform.trim:
        value = value.strip()
    if field.transform.collapse_whitespace:
        value = " ".join(value.split())
    return field.transform.empty_as_null and value == ""


def _transformation_rule_summary(field) -> str:
    rules = []
    if field.value_source is ScalarValueSource.CONSTANT:
        rules.append("Constant")
    elif field.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK:
        rules.append("Source + fallback")
    else:
        rules.append("Source")
    if field.value_mappings:
        rules.append(f"Match {len(field.value_mappings)} source choice(s)")
    transform = field.transform
    if transform.formula:
        rules.append("Formula")
    if transform.trim:
        rules.append("Trim")
    if transform.collapse_whitespace:
        rules.append("Collapse spaces")
    if transform.search_value:
        rules.append("Find and replace")
    if transform.case_mode != "preserve":
        rules.append(f"Case: {transform.case_mode}")
    if transform.empty_as_null:
        rules.append("Empty to null")
    if field.value_type != "string":
        rules.append(f"Parse {field.value_type}")
    if transform.decimal_places is not None:
        rules.append(f"Round to {transform.decimal_places} places")
    if field.validation.configured:
        rules.append("Final value check")
    return " + ".join(rules)


def _attach_preparation_issues(
    prepared: PreparedBundle,
    issues: Iterable[Issue],
) -> PreparedBundle:
    by_row: dict[tuple[str, int], list[Issue]] = {}
    for issue in issues:
        if issue.dataset is None or issue.row is None:
            continue
        by_row.setdefault((issue.dataset, issue.row), []).append(issue)
    return PreparedBundle(
        records=tuple(
            replace(
                record,
                issues=(
                    *record.issues,
                    *by_row.get((record.dataset, record.source_row), ()),
                ),
            )
            for record in prepared.records
        ),
        issues=prepared.issues,
        source_hashes=prepared.source_hashes,
    )


def _synthetic_field(index: int) -> str:
    return f"__impodo_scalar_{index}"


def _normalized_key(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    return normalized or None


def _readiness_report(
    run_id: str,
    project: MigrationProject,
    revision: MappingRevision,
    result: PreflightResult,
    dataset_labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
    actor: Actor,
    staging: StagingRunSummary,
    quality: QualityRunSummary,
) -> ReadinessReport:
    rows = tuple(
        _readiness_row(decision, dataset_labels, source_labels)
        for decision in result.decisions
    )
    target_by_dataset: dict[str, str] = {}
    for item in result.metadata_coverage:
        if item.get("dataset") and item.get("model"):
            target_by_dataset.setdefault(
                str(item["dataset"]),
                str(item["model"]),
            )
    datasets = []
    for dataset in dict.fromkeys(
        [*dataset_labels, *(item.dataset for item in rows)]
    ):
        dataset_rows = [item for item in rows if item.dataset == dataset]
        datasets.append(
            ReadinessDataset(
                dataset=dataset,
                label=dataset_labels.get(dataset, dataset),
                target_model=target_by_dataset.get(dataset, ""),
                total=len(dataset_rows),
                ready=sum(item.status == "ready" for item in dataset_rows),
                needs_review=sum(
                    item.status == "needs_review" for item in dataset_rows
                ),
                blocked=sum(item.status == "blocked" for item in dataset_rows),
            )
        )
    return ReadinessReport(
        run_id=run_id,
        project_id=project.project_id,
        mapping_id=revision.mapping_id,
        mapping_version=revision.version,
        mapping_content_hash=revision.definition.content_hash,
        staging_run_id=staging.run_id,
        staging_content_hash=staging.content_hash,
        quality_run_id=quality.run_id,
        quality_content_hash=quality.content_hash,
        target_hash=result.fingerprint.target_hash,
        checked_at=datetime.now(timezone.utc),
        checked_by=actor.identity.display_name,
        datasets=tuple(datasets),
        rows=rows,
    )


def _readiness_row(
    decision: Decision,
    labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
) -> ReadinessRow:
    status = (
        "needs_review"
        if decision.classification is Classification.AMBIGUOUS
        else (
            "blocked"
            if decision.classification is Classification.BLOCKED
            else "ready"
        )
    )
    issue = next((item for item in decision.issues if item.blocking), None)
    if issue is None and decision.issues:
        issue = decision.issues[0]
    code = (
        issue.code
        if issue is not None
        else (
            "TARGET_IDENTITY_AMBIGUOUS"
            if decision.classification is Classification.AMBIGUOUS
            else ""
        )
    )
    reason, action = _plain_guidance(code, decision.classification)
    field = issue.field if issue is not None and issue.field else ""
    field = source_labels.get((decision.dataset, field), field)
    identity = " · ".join(
        _display_value(item) for item in decision.business_identity
    ) or "—"
    return ReadinessRow(
        dataset=decision.dataset,
        dataset_label=labels.get(decision.dataset, decision.dataset),
        source_row=decision.source_row,
        status=status,
        classification=decision.classification.value,
        identity=identity,
        reason=reason,
        field=field,
        recommended_action=action,
        technical_code=code,
        issue_count=len(decision.issues),
    )


def _plain_guidance(
    code: str,
    classification: Classification,
) -> tuple[str, str]:
    guidance = {
        "SOURCE_FIELD_MISSING": (
            "A mapped source column is unavailable.",
            "Return to mapping and choose an available column.",
        ),
        "SOURCE_IDENTITY_INVALID": (
            "A required key is empty or invalid.",
            "Complete the key in the source data.",
        ),
        "SOURCE_IDENTITY_DUPLICATE": (
            "This row uses the same key as another row.",
            "Keep one unique row or correct the key.",
        ),
        "SOURCE_REQUIRED_VALUE_MISSING": (
            "A required value is missing.",
            "Complete the value and check again.",
        ),
        "SOURCE_TYPE_INVALID": (
            "A value has the wrong format.",
            "Correct the value format and check again.",
        ),
        "SOURCE_TEXT_LENGTH_INVALID": (
            "A value has the wrong number of characters.",
            "Correct the value or review its exact-length rule.",
        ),
        "SOURCE_TEXT_SEGMENT_INVALID": (
            "Part of a value contains unexpected characters.",
            "Correct the value or review its character rule.",
        ),
        "SOURCE_PATTERN_MISMATCH": (
            "A value does not follow its custom format.",
            "Correct the value or review the advanced custom pattern.",
        ),
        "SOURCE_FORMULA_INVALID": (
            "A formula could not calculate this value.",
            "Review the row inputs and the field formula.",
        ),
        "SOURCE_REPLACEMENT_INVALID": (
            "Find and replace could not process this value safely.",
            "Review the find-and-replace rule.",
        ),
        "SOURCE_DECIMAL_ROUNDING_INVALID": (
            "A decimal value could not be rounded safely.",
            "Review the decimal value and rounding rule.",
        ),
        "SOURCE_REFERENCE_DUPLICATE": (
            "This row repeats the same related key.",
            "Remove the duplicate related value.",
        ),
        "REFERENCE_NOT_FOUND": (
            "A related record cannot be found.",
            "Add or correct the related key.",
        ),
        "REFERENCE_AMBIGUOUS": (
            "A related key matches more than one record.",
            "Use a more specific business key.",
        ),
        "REFERENCE_BLOCKED_BY_DEPENDENCY": (
            "A related parent row is blocked.",
            "Resolve the parent row first.",
        ),
        "TARGET_REFERENCE_UNRESOLVED": (
            "An Odoo relationship has no usable business key.",
            "Check the related Odoo record and its business key.",
        ),
        "TARGET_IDENTITY_AMBIGUOUS": (
            "More than one Odoo record matches this key.",
            "Review the matching Odoo records.",
        ),
        "REQUIRED_ON_CREATE_MISSING": (
            "Odoo needs another value to create this record.",
            "Map or provide the required value.",
        ),
        "CREATE_IDENTITY_EXISTS": (
            "This create-only key already exists in Odoo.",
            "Review the create-only policy.",
        ),
        "COMPARISON_UNSUPPORTED": (
            "This value cannot be compared safely.",
            "Review the mapped field type and comparison rule.",
        ),
    }
    if code in guidance:
        return guidance[code]
    if classification is Classification.CREATE:
        return "Ready to create.", "No action needed."
    if classification is Classification.UPDATE:
        return "Ready to update.", "Review changes in the package."
    if classification is Classification.UNCHANGED:
        return "Already matches Odoo.", "No action needed."
    if classification is Classification.AMBIGUOUS:
        return "More than one Odoo record matches.", "Review the matching records."
    return "This row cannot be processed safely.", "Review the row details."


def _display_value(value: object) -> str:
    portable = portable_value(value)
    if isinstance(portable, Mapping) and "value" in portable:
        return str(portable["value"])
    if isinstance(portable, list):
        return " / ".join(_display_value(item) for item in portable)
    if isinstance(portable, Mapping):
        return json.dumps(portable, ensure_ascii=False, separators=(",", ":"))
    return str(portable) if portable is not None else "—"


def _write_manifest(
    repository: ReadinessRepository,
    project_id: str,
    run_id: str,
    result: PreflightResult,
) -> Path:
    canonical_run_id = str(UUID(run_id))
    reports = repository.project_directory(project_id) / "reports" / canonical_run_id
    reports.mkdir(parents=False, exist_ok=False)
    target = reports / MANIFEST_NAME
    partial = target.with_suffix(".json.partial")
    partial.write_bytes(canonical_json_bytes(result.to_portable_dict()) + b"\n")
    partial.replace(target)
    return target
