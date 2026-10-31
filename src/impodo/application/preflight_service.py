"""Orchestrate final review over approved, frozen source-side evidence.

``PreflightService.compare`` verifies and adapts current mapping and
preparation evidence, builds bounded read requirements, invokes a
caller-supplied read-only target reader, runs the shared comparison engine,
and publishes a portable report plus
protected snapshots. It never reloads source files and exposes no Odoo write.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from impodo.application.shared.artifacts import (
    ArtifactStoreError,
    GovernedArtifactStores,
)
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.execution.planner import (
    PreflightRequirementPlan,
    plan_preflight_requirements,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.staging import StagingRunSummary
from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.shared.models import canonical_json_bytes, target_identity_hash
from impodo.domain.workspace.business_intent_assessment import (
    BusinessIntentAssessment,
    BusinessIntentAssessmentSummary,
    observe_business_writes,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import (
    REFERENCE_POLICY_HASH,
    reference_policy_hash,
)
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState

from ..domain.compiler.browser_mapping_compiler import (
    browser_mapping_labels,
    compile_browser_mapping,
)
from ..domain.errors import ReadinessError
from ..domain.execution_snapshot import (
    ExecutionSnapshot,
    build_execution_snapshot,
)
from ..domain.odoo_comparison import OdooComparisonArtifact
from ..domain.preflight.deferred_execution import reduce_execution_snapshot
from ..domain.preflight.deferred_scope import (
    DeferredIssue,
    DeferredScopeDecision,
    DeferredScopeEvidenceError,
    DeferredScopePreview,
    exact_numeric_precision_issues,
    portable_preflight_deferred_issues,
    portable_reference_resolutions,
    prepared_dependency_facts,
    preview_deferred_scope,
)
from ..domain.preflight.frozen_input import (
    FrozenPreflightInput,
    build_frozen_preflight_input,
)
from ..domain.preflight.missing_parents import (
    MissingParentGroup,
    missing_parent_groups,
)
from ..domain.preflight.reports import (
    ReadinessReport,
    ReadinessRowPage,
    ReviewWorkbookCellEffect,
    ReviewWorkbookEvidence,
    _readiness_report,
)
from .odoo_comparison_service import (
    ODOO_COMPARISON_ARTIFACT_NAME,
    OdooComparisonPreparation,
    build_odoo_comparison_publication,
    prepare_odoo_comparison,
)
from .odoo_provenance_service import OdooProvenanceService
from .odoo_read_failures import (
    OdooReadFailureCode,
    OdooReadWorkflowError,
)
from .preflight_attempts import (
    PreflightComparisonReceipt,
    PreflightComparisonRequest,
    PreflightPublicationCommittedError,
)
from .preflight_timing import timed_preflight_publication
from .workspace.execution.business_review import (
    BusinessIntentEvidencePage,
    BusinessIntentReview,
)
from .workspace.execution.navigation import (
    ExecutionReviewProjection,
    build_execution_preview_summary,
    build_execution_review_projection,
)
from .workspace.preparation.readiness_ports import (
    PreflightEffectiveRepository,
    PreflightMappingRepository,
    PreflightNormalizationRepository,
    PreflightQualityRepository,
    PreflightRepository,
    PreflightSchemaRepository,
    PreflightSourceRepository,
    PreflightStagingRepository,
    PreflightWorkspaceRepository,
)

MANIFEST_NAME = "impodo_preflight_manifest.json"
EXECUTION_SNAPSHOT_NAME = "impodo_execution_snapshot.json"
DEFERRED_SCOPE_PREVIEW_NAME = "impodo_deferred_scope_preview.json"
DEFERRED_SCOPE_DECISION_NAME = "impodo_deferred_scope_decision.json"
REDUCED_EXECUTION_SNAPSHOT_NAME = "impodo_reduced_execution_snapshot.json"

ReadinessReader = Callable[
    [PreflightRequirementPlan],
    tuple[MetadataSnapshot, RecordSnapshot],
]
PreflightProgress = Callable[[str], None]


class LocalPreflightOperation(Protocol):
    """Run one synchronous local comparison phase in a bounded store scope."""

    def __call__[T](self, function: Callable[[], T], /) -> T: ...


@dataclass(frozen=True, slots=True)
class _PreparedFileComparison:
    workspace_id: str
    workspace_state: WorkspaceState
    frozen: FrozenPreflightInput
    requirements: PreflightRequirementPlan


PreparedPreflightComparison = _PreparedFileComparison | OdooComparisonPreparation


def _direct_local_operation[T](function: Callable[[], T]) -> T:
    return function()


@dataclass(frozen=True, slots=True)
class DeferredScopeReview:
    """Current isolatable issues and an optional locally calculated preview."""

    comparison_id: str
    comparison_hash: str
    issues: tuple[DeferredIssue, ...]
    candidates: tuple[DeferredIssueCandidate, ...]
    preview: DeferredScopePreview | None
    decision: DeferredScopeDecision | None = None


@dataclass(frozen=True, slots=True)
class DeferredScopeAcceptance:
    """Verified stored decision, preview, and exact reduced write snapshot."""

    decision: DeferredScopeDecision
    preview: DeferredScopePreview
    snapshot: ExecutionSnapshot


@dataclass(frozen=True, slots=True)
class DeferredIssueCandidate:
    """Business-row context for one issue shown in the review page."""

    issue: DeferredIssue
    dataset: str = ""
    source_row: int = 0
    source_identity: tuple[object, ...] = ()
    target_model: str = ""

    @property
    def selectable(self) -> bool:
        return bool(self.issue.row_id)


def _report_progress(progress: PreflightProgress | None, phase: str) -> None:
    """Publish one coarse phase without making progress reporting mandatory."""

    if progress is not None:
        progress(phase)


def _portable_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DeferredScopeEvidenceError(f"{label} must be an object")
    result = {key: item for key, item in value.items() if isinstance(key, str)}
    if len(result) != len(value):
        raise DeferredScopeEvidenceError(f"{label} contains a non-string key")
    return result


class PreflightService:
    """Plan batched Odoo reads and publish target-dependent classifications.

    The service is the browser workflow's only point at which Odoo may be
    contacted. Every source-side prerequisite and every record domain is
    checked first; failures explicitly occur before calling ``reader``.
    """

    def __init__(
        self,
        staging: PreflightStagingRepository,
        quality: PreflightQualityRepository,
        normalization: PreflightNormalizationRepository,
        mappings: PreflightMappingRepository,
        workspaces: PreflightWorkspaceRepository,
        sources: PreflightSourceRepository,
        preflight: PreflightRepository,
        artifacts: GovernedArtifactStores,
        authorization: WorkspaceAuthorizationPolicy,
        effective: PreflightEffectiveRepository | None = None,
        schemas: PreflightSchemaRepository | None = None,
        odoo_provenance: OdooProvenanceService | None = None,
    ) -> None:
        self.staging = staging
        self.quality = quality
        self.normalization = normalization
        self.mappings = mappings
        self.workspaces = workspaces
        self.sources = sources
        self.preflight = preflight
        self.artifacts = artifacts
        self.authorization = authorization
        self.effective = effective
        self.schemas = schemas
        self.odoo_provenance = odoo_provenance
        self.engine = PreflightEngine()

    def current_report(self, workspace_id: str) -> ReadinessReport | None:
        """Return the report only if every current upstream/target binding matches.

        A report is treated as absent when staging, quality, normalization,
        mapping submission, lifecycle version, eligible hash, or configured
        target identity moved since publication.
        """

        staging = self.staging.get_current_staging_summary(workspace_id)
        if staging is None:
            return None
        quality = self.quality.get_current_quality_summary(workspace_id)
        if quality is None or quality.staging_run_id != staging.run_id:
            return None
        normalization = self.normalization.get_current_normalization_summary(
            workspace_id
        )
        if (
            normalization is None
            or not normalization.frozen
            or normalization.staging_run_id != staging.run_id
            or normalization.quality_run_id != quality.run_id
        ):
            return None
        revision = self.mappings.get_mapping_revision(workspace_id)
        if revision is None:
            return None
        submission = self.mappings.get_mapping_submission(
            workspace_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            return None
        report = self.preflight.get_readiness_report(
            workspace_id,
            revision.mapping_id,
            revision.version,
            revision.definition.content_hash,
            staging.run_id,
            staging.content_hash,
            quality.run_id,
            quality.content_hash,
            normalization.run_id,
            normalization.content_hash,
            normalization.lifecycle_version,
            normalization.eligible_dataset_hash,
        )
        if report is None:
            return None
        workspace_state = self.workspaces.get(workspace_id)
        expected_target = target_identity_hash(
            connection_mode=(
                workspace_state.odoo_connection_mode.value
                if workspace_state.odoo_connection_mode is not None
                else ""
            ),
            base_url=workspace_state.odoo_base_url,
            database=workspace_state.odoo_database,
        )
        return report if report.target_hash == expected_target else None

    def current_business_intent_assessment(self, workspace_id: str, snapshot_hash: str) -> BusinessIntentAssessmentSummary | None:
        """Read bounded current assessment evidence without traversing rows."""
        return self.preflight.get_business_intent_assessment(workspace_id, snapshot_hash)

    def business_intent_review(self, workspace_id: str, snapshot_hash: str, *, actor: Actor, run_id: str = "") -> BusinessIntentReview | None:
        """Read the committed compact review without reconstructing row effects."""
        self.authorization.require(actor, Capability.PROJECT_VIEW, workspace_id=workspace_id)
        return self.preflight.get_business_intent_review(workspace_id, snapshot_hash, run_id=run_id)

    def business_intent_evidence(self, workspace_id: str, snapshot_hash: str, *, actor: Actor,
        expected_assessment_hash: str, model: str = "", page: int = 1, run_id: str = "",
    ) -> BusinessIntentEvidencePage:
        """Authorize first, then read one bounded page of exact expectation detail."""
        self.authorization.require(actor, Capability.PROJECT_VIEW, workspace_id=workspace_id)
        if page < 1 or page > 100_000:
            raise WorkspaceError("Choose a current evidence page")
        if run_id:
            run_id = str(UUID(run_id))
        elif self.current_report(workspace_id) is None:
            raise WorkspaceError("The comparison changed. Open Final review again.")
        return self.preflight.get_business_intent_evidence(workspace_id, snapshot_hash,
            expected_assessment_hash=expected_assessment_hash, model=model, page=page, run_id=run_id)

    def require_business_intent_approval(self, workspace_id: str, run: ExecutionRun) -> None:
        """Retain original journal approval during read-back-gated recovery."""
        self.preflight.require_business_intent_approval(workspace_id, run)

    def reassess_business_intent(self, workspace_id: str, *, actor: Actor) -> BusinessIntentAssessment:
        """Reassess a verified saved comparison locally after an explicit edit."""
        self.authorization.require(actor, Capability.PREFLIGHT_RUN, workspace_id=workspace_id)
        projection = self.current_execution_review_projection(workspace_id)
        if projection is None:
            raise WorkspaceError("Compare with Odoo before assessing business expectations")
        return self.preflight.reassess_business_intent(workspace_id, projection.summary.snapshot_hash, actor=actor)

    def current_execution_review_projection(
        self,
        workspace_id: str,
    ) -> ExecutionReviewProjection | None:
        """Return bounded Check changes facts for the exact current report."""

        report = self.current_report(workspace_id)
        if report is None:
            return None
        return self.preflight.get_execution_review_projection(
            workspace_id,
            report.run_id,
        )

    def current_staging(self, workspace_id: str) -> StagingRunSummary | None:
        """Return the current staging summary used by package eligibility UI."""

        return self.staging.get_current_staging_summary(workspace_id)

    def comparison_input_hash(
        self,
        workspace_id: str,
        *,
        read_credential_binding_hash: str,
    ) -> str:
        """Hash only bounded current bindings needed to identify one request."""

        workspace_state = self.workspaces.get(workspace_id)
        revision = self.mappings.get_mapping_revision(workspace_id)
        staging = self.staging.get_current_staging_summary(workspace_id)
        quality = self.quality.get_current_quality_summary(workspace_id)
        normalization = self.normalization.get_current_normalization_summary(
            workspace_id
        )
        if revision is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Submit the mapping before comparing with Odoo",
            )
        submission = self.mappings.get_mapping_submission(
            workspace_id,
            revision.version,
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Submit the current mapping before comparing with Odoo",
            )
        if staging is None or quality is None or normalization is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "Prepare and approve the data before comparing with Odoo",
            )
        if (
            quality.staging_run_id != staging.run_id
            or not normalization.frozen
            or normalization.staging_run_id != staging.run_id
            or normalization.quality_run_id != quality.run_id
        ):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "The approved prepared data is no longer current",
            )
        payload = {
            "eligible_dataset_hash": normalization.eligible_dataset_hash,
            "mapping_content_hash": revision.definition.content_hash,
            "mapping_id": revision.mapping_id,
            "mapping_version": revision.version,
            "normalization_content_hash": normalization.content_hash,
            "normalization_lifecycle_version": normalization.lifecycle_version,
            "normalization_run_id": normalization.run_id,
            "quality_content_hash": quality.content_hash,
            "quality_run_id": quality.run_id,
            "read_credential_binding_hash": read_credential_binding_hash,
            "staging_content_hash": staging.content_hash,
            "staging_run_id": staging.run_id,
            "target_hash": target_identity_hash(
                connection_mode=(
                    workspace_state.odoo_connection_mode.value
                    if workspace_state.odoo_connection_mode is not None
                    else ""
                ),
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
            ),
            "workspace_id": workspace_id,
            "workspace_revision": workspace_state.revision,
        }
        return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()

    def comparison_receipt(
        self,
        report: ReadinessReport,
    ) -> PreflightComparisonReceipt:
        """Verify immutable artifacts and derive one value-free commit receipt."""

        if not report.operation_id:
            raise ReadinessError(
                "The comparison does not contain a durable operation identity."
            )
        try:
            workspace = self.workspaces.get(report.workspace_id)
            execution_snapshot_hash = ""
            if workspace.source_mode is SourceMode.ODOO:
                self._verify_odoo_receipt_artifacts(report)
            else:
                manifest, _content = self._verified_manifest(report)
                evidence = _portable_mapping(
                    manifest["preflight_evidence"], "Preflight evidence"
                )
                execution_snapshot_hash = str(evidence["execution_snapshot_hash"])
                snapshot = self._full_execution_snapshot(
                    report.workspace_id,
                    report.run_id,
                )
                if (
                    snapshot.semantic_hash != execution_snapshot_hash
                    or not _snapshot_matches_report(snapshot, report)
                ):
                    raise ValueError(
                        "Execution snapshot differs from the committed comparison"
                    )
        except (
            ArtifactStoreError,
            DeferredScopeEvidenceError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.COMPARISON_STORAGE_FAILED,
                "The committed comparison artifacts could not be verified",
            ) from error
        return PreflightComparisonReceipt(
            operation_id=report.operation_id,
            workspace_id=report.workspace_id,
            preflight_run_id=report.run_id,
            frozen_input_hash=report.frozen_input_hash,
            requirement_plan_hash=report.requirement_plan_hash,
            result_hash=report.result_hash,
            manifest_hash=report.manifest_hash,
            execution_snapshot_hash=execution_snapshot_hash,
        )

    def _verify_odoo_receipt_artifacts(self, report: ReadinessReport) -> None:
        """Verify the pinned manifest and encrypted evidence without decryption."""

        with self.artifacts.materialize_report(
            report.workspace_id, report.run_id, MANIFEST_NAME
        ) as path:
            content = path.read_bytes()
        manifest = _portable_mapping(
            json.loads(content), "Odoo comparison manifest"
        )
        evidence = _portable_mapping(
            manifest["preflight_evidence"], "Odoo evidence"
        )
        if (
            "sha256:" + sha256(content).hexdigest() != report.manifest_hash
            or manifest.get("contract") != "odoo-pinned-comparison-v1"
            or manifest.get("workspace_id") != report.workspace_id
            or manifest.get("run_id") != report.run_id
            or manifest.get("status") != report.status
            or evidence.get("frozen_input_hash") != report.frozen_input_hash
            or evidence.get("metadata_snapshot_hash") != report.metadata_snapshot_hash
            or evidence.get("record_snapshot_hash") != report.record_snapshot_hash
        ):
            raise ValueError("Pinned comparison manifest differs from its report")
        with self.artifacts.materialize_report(
            report.workspace_id, report.run_id, ODOO_COMPARISON_ARTIFACT_NAME
        ) as path:
            encrypted = path.read_bytes()
        if "sha256:" + sha256(encrypted).hexdigest() != evidence.get(
            "protected_artifact_hash"
        ):
            raise ValueError("Protected comparison artifact differs from its manifest")

    def recover_comparison_receipt(
        self,
        workspace_id: str,
        operation_id: str,
    ) -> PreflightComparisonReceipt | None:
        """Recover only a matching current commit whose artifacts still verify."""

        raw_report = self.preflight.get_current_readiness_report(workspace_id)
        if raw_report is None or raw_report.operation_id != operation_id:
            return None
        current_report = self.current_report(workspace_id)
        if current_report is None or current_report.run_id != raw_report.run_id:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "The comparison input changed while Impodo was restarting",
            )
        return self.comparison_receipt(current_report)

    def review_workbook_evidence(
        self,
        workspace_id: str,
        run_id: str,
    ) -> ReviewWorkbookEvidence | None:
        """Load exact file-source values for the current review workbook.

        The method reuses the frozen-input verifier, so one bounded source-side
        read loads the prepared rows and proves their mapping, quality, and
        normalization bindings. It makes no Odoo call. Odoo-source values stay
        inside their protected comparison artifact and are never returned for
        a portable workbook.
        """

        report = self.current_report(workspace_id)
        if report is None or report.run_id != run_id:
            raise ReadinessError(
                "The review workbook no longer matches the current comparison. "
                "Compare with Odoo again."
            )
        workspace_state = self.workspaces.get(workspace_id)
        if getattr(workspace_state, "source_mode", SourceMode.FILE) is SourceMode.ODOO:
            return None
        frozen = self._load_frozen_input(workspace_id)
        if frozen.content_hash != report.frozen_input_hash:
            raise ReadinessError(
                "The prepared values no longer match the current comparison. "
                "Compare with Odoo again."
            )
        normalization = self.normalization.get_normalization_evaluation(
            workspace_id,
            frozen.normalization.run_id,
        )
        if (
            normalization is None
            or normalization.content_hash != frozen.normalization.content_hash
        ):
            raise ReadinessError(
                "The prepared-value feedback no longer matches the current "
                "comparison. Prepare the data and compare with Odoo again."
            )
        groups = {item.group_id: item for item in normalization.groups}
        if len(groups) != len(normalization.groups):
            raise ReadinessError(
                "The prepared-value feedback is invalid. Prepare the data again."
            )
        cell_effects = []
        for effect in normalization.effects:
            if not effect.eligible:
                continue
            group = groups.get(effect.group_id)
            if group is None:
                raise ReadinessError(
                    "The prepared-value feedback is incomplete. Prepare the data again."
                )
            cell_effects.append(
                ReviewWorkbookCellEffect(
                    source_trace_id=effect.row_id,
                    dataset=effect.dataset,
                    source_row=effect.source_row,
                    target_field=effect.target_field,
                    before=effect.before,
                    after=effect.after,
                    rule_name=group.name,
                    explanation=group.explanation,
                )
            )
        models = frozen.captured_schema.models if frozen.captured_schema else ()
        return ReviewWorkbookEvidence(
            frozen_input_hash=frozen.content_hash,
            records=tuple(frozen.prepared.records),
            dataset_labels=dict(sorted(frozen.dataset_labels.items())),
            target_model_labels={
                model.name: model.label
                for model in sorted(models, key=lambda item: item.name)
            },
            target_field_labels={
                (model.name, field.name): field.label
                for model in sorted(models, key=lambda item: item.name)
                for field in sorted(model.fields, key=lambda item: item.name)
            },
            normalization_content_hash=normalization.content_hash,
            cell_effects=tuple(
                sorted(
                    cell_effects,
                    key=lambda item: (
                        item.source_trace_id,
                        item.target_field,
                        item.rule_name,
                    ),
                )
            ),
            target_field_required={
                (model.name, field.name): bool(getattr(field, "required", False))
                for model in sorted(models, key=lambda item: item.name)
                for field in sorted(model.fields, key=lambda item: item.name)
            },
        )

    def current_execution_snapshot(self, workspace_id: str) -> ExecutionSnapshot | None:
        """Load the full or explicitly reviewed reduced current snapshot.

        The artifact is usable only while the current report still matches all
        source, normalization, mapping, and target bindings.  Corrupt or
        substituted content fails closed instead of silently rebuilding a
        different execution input.
        """

        if (
            getattr(self.workspaces.get(workspace_id), "source_mode", SourceMode.FILE)
            is SourceMode.ODOO
        ):
            return None
        report = self.current_report(workspace_id)
        if report is None:
            return None
        try:
            full_snapshot = self._full_execution_snapshot(
                workspace_id,
                report.run_id,
            )
            manifest, manifest_content = self._verified_manifest(report)
            acceptance = self._deferred_scope_acceptance(
                workspace_id,
                report.run_id,
                full_snapshot,
                comparison_hash=report.result_hash,
            )
        except (
            ArtifactStoreError,
            DeferredScopeEvidenceError,
            OSError,
            ValueError,
        ) as error:
            raise ReadinessError(
                "The execution snapshot is missing or invalid. Run the Odoo "
                "comparison again."
            ) from error
        manifest_evidence = _portable_mapping(
            manifest["preflight_evidence"],
            "Preflight evidence",
        )
        if not _snapshot_matches_report(full_snapshot, report) or not (
            "sha256:" + sha256(manifest_content).hexdigest() == report.manifest_hash
            and manifest_evidence.get("execution_snapshot_hash")
            == full_snapshot.semantic_hash
        ):
            raise ReadinessError(
                "The execution snapshot no longer matches the current Odoo "
                "comparison. Run the comparison again."
            )
        return acceptance.snapshot if acceptance is not None else full_snapshot

    def execution_snapshot(
        self,
        workspace_id: str,
        preflight_run_id: str,
    ) -> ExecutionSnapshot:
        """Load one immutable execution snapshot by its preflight run.

        Reconciliation uses the exact historical artifact named by the load
        journal. It must not silently switch to a newer comparison while
        checking an older write outcome.
        """

        try:
            full_snapshot = self._full_execution_snapshot(
                workspace_id,
                preflight_run_id,
            )
            acceptance = self._deferred_scope_acceptance(
                workspace_id,
                preflight_run_id,
                full_snapshot,
                comparison_hash=full_snapshot.preflight_result_hash,
            )
        except (
            ArtifactStoreError,
            DeferredScopeEvidenceError,
            OSError,
            ValueError,
        ) as error:
            raise ReadinessError(
                "The saved load preview is missing or invalid. Compare with "
                "Odoo again before another load."
            ) from error
        return acceptance.snapshot if acceptance is not None else full_snapshot

    def deferred_scope_review(
        self,
        workspace_id: str,
        *,
        selected_issue_ids: Iterable[str] = (),
    ) -> DeferredScopeReview | None:
        """Build or load a bounded group preview without another Odoo read."""

        report = self.current_report(workspace_id)
        if report is None or (
            getattr(self.workspaces.get(workspace_id), "source_mode", SourceMode.FILE)
            is SourceMode.ODOO
        ):
            return None
        full_snapshot = self._full_execution_snapshot(workspace_id, report.run_id)
        manifest, _content = self._verified_manifest(report)
        acceptance = self._deferred_scope_acceptance(
            workspace_id,
            report.run_id,
            full_snapshot,
            comparison_hash=report.result_hash,
        )
        issues = self._deferred_issues(
            report.run_id,
            full_snapshot,
            manifest,
        )
        candidates = self._deferred_issue_candidates(issues, full_snapshot)
        if acceptance is not None:
            return DeferredScopeReview(
                comparison_id=report.run_id,
                comparison_hash=report.result_hash,
                issues=issues,
                candidates=candidates,
                preview=acceptance.preview,
                decision=acceptance.decision,
            )
        selected = tuple(sorted(set(selected_issue_ids)))
        preview = None
        if selected:
            frozen = self._load_frozen_input(workspace_id)
            dependencies = prepared_dependency_facts(
                full_snapshot.rows,
                tuple(frozen.prepared.records),
                portable_reference_resolutions(manifest),
            )
            preview = preview_deferred_scope(
                comparison_id=report.run_id,
                comparison_hash=report.result_hash,
                execution_snapshot_hash=full_snapshot.semantic_hash,
                rows=full_snapshot.rows,
                issues=issues,
                dependencies=dependencies,
                selected_issue_ids=selected,
                already_set_aside_count=(
                    frozen.quality.quarantined_count + frozen.quality.excluded_count
                ),
            )
        return DeferredScopeReview(
            comparison_id=report.run_id,
            comparison_hash=report.result_hash,
            issues=issues,
            candidates=candidates,
            preview=preview,
        )

    def accept_deferred_scope(
        self,
        workspace_id: str,
        *,
        selected_issue_ids: Iterable[str],
        actor: Actor,
    ) -> DeferredScopeAcceptance:
        """Persist one reviewed safe remainder; this performs no target read."""

        self.authorization.require(
            actor,
            Capability.PREFLIGHT_RUN,
            workspace_id=workspace_id,
        )
        review = self.deferred_scope_review(
            workspace_id,
            selected_issue_ids=selected_issue_ids,
        )
        if review is None or review.preview is None:
            raise DeferredScopeEvidenceError(
                "A current group preview is required before accepting set-aside rows"
            )
        if review.decision is not None:
            raise DeferredScopeEvidenceError(
                "The current comparison already has a reviewed set-aside decision"
            )
        full_snapshot = self._full_execution_snapshot(
            workspace_id,
            review.comparison_id,
        )
        reduced = reduce_execution_snapshot(full_snapshot, review.preview)
        decision = DeferredScopeDecision.accept(
            workspace_id=workspace_id,
            preview=review.preview,
            reduced_execution_snapshot_hash=reduced.semantic_hash,
            accepted_by=actor.identity,
            accepted_at=datetime.now(UTC),
        )
        original_observation = self.preflight.get_business_write_observation(workspace_id, full_snapshot.semantic_hash)
        business_observation = observe_business_writes(
            reduced, authored_operations=original_observation.authored_operations,
            reduced_scope_hash=decision.semantic_hash,
        )
        report = self.current_report(workspace_id)
        if report is None or report.run_id != review.comparison_id:
            raise DeferredScopeEvidenceError(
                "The comparison changed before the set-aside decision was saved"
            )
        execution_summary = replace(
            build_execution_preview_summary(
                report,
                reduced,
                execution_shape_ready=(
                    assess_odoo_operation(
                        reduced.target_odoo_version,
                        OdooOperation.WRITE,
                    ).allowed
                    and not reduced.relationship_plan.blockers
                ),
            ),
            comparison_status="READY",
        )
        execution_review = replace(
            build_execution_review_projection(
                report,
                reduced,
                execution_shape_ready=execution_summary.execution_shape_ready,
            ),
            summary=execution_summary,
        )
        payloads = (
            (
                DEFERRED_SCOPE_PREVIEW_NAME,
                review.preview.to_json().encode("utf-8") + b"\n",
            ),
            (
                REDUCED_EXECUTION_SNAPSHOT_NAME,
                reduced.to_json().encode("utf-8") + b"\n",
            ),
            (
                DEFERRED_SCOPE_DECISION_NAME,
                decision.to_json().encode("utf-8") + b"\n",
            ),
        )
        try:
            for filename, content in payloads:
                self.artifacts.write_report(
                    workspace_id,
                    review.comparison_id,
                    filename,
                    content,
                )
            self.preflight.save_deferred_scope_projection(
                workspace_id,
                review.comparison_id,
                execution_summary=execution_summary,
                execution_review=execution_review,
                decision_hash=decision.semantic_hash,
                actor=actor,
                business_observation=business_observation,
            )
        except Exception:
            for filename, _content in payloads:
                try:
                    self.artifacts.delete_report(
                        workspace_id,
                        review.comparison_id,
                        filename,
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not remove an incomplete deferred-scope report"
                    )
            raise
        return DeferredScopeAcceptance(
            decision=decision,
            preview=review.preview,
            snapshot=reduced,
        )

    def _full_execution_snapshot(
        self,
        workspace_id: str,
        preflight_run_id: str,
    ) -> ExecutionSnapshot:
        with self.artifacts.materialize_report(
            workspace_id,
            preflight_run_id,
            EXECUTION_SNAPSHOT_NAME,
        ) as path:
            snapshot = ExecutionSnapshot.from_json(path.read_text("utf-8"))
        if (
            snapshot.workspace_id != workspace_id
            or snapshot.preflight_run_id != preflight_run_id
        ):
            raise DeferredScopeEvidenceError(
                "The full execution snapshot does not match this workspace"
            )
        return snapshot

    def _verified_manifest(
        self,
        report: ReadinessReport,
    ) -> tuple[dict[str, object], bytes]:
        with self.artifacts.materialize_report(
            report.workspace_id,
            report.run_id,
            MANIFEST_NAME,
        ) as path:
            content = path.read_bytes()
        manifest = json.loads(content)
        if (
            not isinstance(manifest, dict)
            or manifest.get("semantic_hash") != report.result_hash
            or not isinstance(manifest.get("preflight_evidence"), dict)
            or "sha256:" + sha256(content).hexdigest() != report.manifest_hash
        ):
            raise DeferredScopeEvidenceError(
                "The saved comparison manifest is not current"
            )
        return manifest, content

    def _deferred_scope_acceptance(
        self,
        workspace_id: str,
        preflight_run_id: str,
        full_snapshot: ExecutionSnapshot,
        *,
        comparison_hash: str,
    ) -> DeferredScopeAcceptance | None:
        if not self.artifacts.report_exists(
            workspace_id,
            preflight_run_id,
            DEFERRED_SCOPE_DECISION_NAME,
        ):
            return None
        with (
            self.artifacts.materialize_report(
                workspace_id,
                preflight_run_id,
                DEFERRED_SCOPE_DECISION_NAME,
            ) as decision_path,
            self.artifacts.materialize_report(
                workspace_id,
                preflight_run_id,
                DEFERRED_SCOPE_PREVIEW_NAME,
            ) as preview_path,
            self.artifacts.materialize_report(
                workspace_id,
                preflight_run_id,
                REDUCED_EXECUTION_SNAPSHOT_NAME,
            ) as snapshot_path,
        ):
            decision = DeferredScopeDecision.from_json(decision_path.read_text("utf-8"))
            preview = DeferredScopePreview.from_json(preview_path.read_text("utf-8"))
            reduced = ExecutionSnapshot.from_json(snapshot_path.read_text("utf-8"))
        omitted_ids = set(decision.omitted_row_ids)
        full_ids = {row.row_id for row in full_snapshot.rows}
        reduced_ids = {row.row_id for row in reduced.rows}
        if (
            decision.workspace_id != workspace_id
            or decision.comparison_id != preflight_run_id
            or decision.comparison_hash != comparison_hash
            or decision.full_execution_snapshot_hash != full_snapshot.semantic_hash
            or decision.preview_hash != preview.semantic_hash
            or decision.reduced_execution_snapshot_hash != reduced.semantic_hash
            or preview.comparison_id != preflight_run_id
            or preview.comparison_hash != comparison_hash
            or preview.execution_snapshot_hash != full_snapshot.semantic_hash
            or preview.selected_issue_ids != decision.selected_issue_ids
            or {row.row_id for row in preview.omitted_rows} != omitted_ids
            or reduced.workspace_id != workspace_id
            or reduced.preflight_run_id != preflight_run_id
            or full_ids - omitted_ids != reduced_ids
            or reduced_ids & omitted_ids
            or reduced.write_count != preview.remaining_write_count
        ):
            raise DeferredScopeEvidenceError(
                "The reviewed deferred scope no longer matches its comparison"
            )
        return DeferredScopeAcceptance(
            decision=decision,
            preview=preview,
            snapshot=reduced,
        )

    @staticmethod
    def _deferred_issues(
        comparison_id: str,
        full_snapshot: ExecutionSnapshot,
        manifest: Mapping[str, object],
    ) -> tuple[DeferredIssue, ...]:
        issues = {
            item.issue_id: item
            for item in portable_preflight_deferred_issues(
                comparison_id,
                full_snapshot.rows,
                manifest,
                relationship_blockers=full_snapshot.relationship_plan.blockers,
            )
        }
        for item in exact_numeric_precision_issues(
            comparison_id,
            full_snapshot.rows,
            full_snapshot.datasets,
        ):
            issues[item.issue_id] = item
        return tuple(sorted(issues.values(), key=lambda item: item.issue_id))

    @staticmethod
    def _deferred_issue_candidates(
        issues: tuple[DeferredIssue, ...],
        snapshot: ExecutionSnapshot,
    ) -> tuple[DeferredIssueCandidate, ...]:
        rows = {row.row_id: row for row in snapshot.rows}
        candidates = []
        for issue in issues:
            row = rows.get(issue.row_id)
            candidates.append(
                DeferredIssueCandidate(
                    issue=issue,
                    dataset=row.dataset if row is not None else "",
                    source_row=row.source_row if row is not None else 0,
                    source_identity=(
                        tuple(row.source_identity) if row is not None else ()
                    ),
                    target_model=row.target_model if row is not None else "",
                )
            )
        return tuple(candidates)

    def readiness_rows(
        self,
        workspace_id: str,
        run_id: str,
        *,
        status: str = "",
        dataset: str = "",
        issue_code: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> ReadinessRowPage:
        """Load one filtered, bounded page from a published readiness run."""

        return self.preflight.get_readiness_rows(
            workspace_id,
            run_id,
            status=status,
            dataset=dataset,
            issue_code=issue_code,
            page=page,
            page_size=page_size,
        )

    def current_missing_parent_groups(
        self, workspace_id: str
    ) -> tuple[MissingParentGroup, ...]:
        """Read confirmed missing references from the current verified manifest."""

        report = self.current_report(workspace_id)
        if report is None:
            return ()
        try:
            with self.artifacts.materialize_report(
                workspace_id, report.run_id, MANIFEST_NAME
            ) as path:
                content = path.read_bytes()
            if "sha256:" + sha256(content).hexdigest() != report.manifest_hash:
                raise ValueError("Manifest hash differs from the current comparison")
            manifest = json.loads(content)
            if (
                not isinstance(manifest, dict)
                or manifest.get("semantic_hash") != report.result_hash
                or not isinstance(manifest.get("reference_resolutions"), list)
            ):
                raise ValueError("Manifest lacks current structured references")
            return missing_parent_groups(manifest["reference_resolutions"])
        except (ArtifactStoreError, OSError, ValueError) as error:
            raise ReadinessError(
                "The saved Odoo comparison evidence is unavailable. Compare with "
                "Odoo again before reviewing missing relationships."
            ) from error

    def compare(
        self,
        workspace_id: str,
        *,
        reader: ReadinessReader,
        actor: Actor,
        progress: PreflightProgress | None = None,
        local_operation: LocalPreflightOperation | None = None,
        operation_id: str = "",
        comparison_request: PreflightComparisonRequest | None = None,
        current_read_credential_binding_hash: Callable[[], str] | None = None,
    ) -> ReadinessReport:
        """Compare approved rows without invoking preparation or source loading.

        The fixed sequence is one bounded local verification phase, one remote
        read with no retained local owner, and one bounded local classification
        and publication phase. A failed database publication removes otherwise
        orphaned artifacts and leaves the previous current report unchanged.
        The optional credential-generation reader runs only in the local phases.
        """

        run_local = local_operation or _direct_local_operation
        _report_progress(progress, "VERIFYING")

        def prepare() -> PreparedPreflightComparison:
            self.authorization.require(
                actor, Capability.PREFLIGHT_RUN, workspace_id=workspace_id
            )
            self._require_reserved_comparison(
                workspace_id, operation_id, comparison_request,
                current_read_credential_binding_hash,
            )
            prepared = self._prepare_comparison(workspace_id, actor)
            self._require_reserved_comparison(
                workspace_id, operation_id, comparison_request,
                current_read_credential_binding_hash,
            )
            return prepared

        prepared = run_local(prepare)
        _report_progress(progress, "READING")
        metadata, records = reader(prepared.requirements)

        def publish() -> ReadinessReport:
            self._require_reserved_comparison(
                workspace_id, operation_id, comparison_request,
                current_read_credential_binding_hash,
            )
            return self._publish_comparison(
                prepared,
                metadata,
                records,
                actor=actor,
                progress=progress,
                operation_id=operation_id,
                expected_workspace_revision=(
                    prepared.workspace_state.revision
                    if comparison_request is not None else None
                ),
            )

        return run_local(publish)

    def _require_reserved_comparison(
        self,
        workspace_id: str,
        operation_id: str,
        request: PreflightComparisonRequest | None,
        current_read_credential_binding_hash: Callable[[], str] | None,
    ) -> None:
        """Reject changed queued inputs before reads or comparison publication."""

        if request is None:
            return
        if (
            request.workspace_id != workspace_id
            or request.operation_id != operation_id
            or self.comparison_input_hash(
                workspace_id,
                read_credential_binding_hash=(
                    current_read_credential_binding_hash()
                    if current_read_credential_binding_hash is not None
                    else request.read_credential_binding_hash
                ),
            ) != request.semantic_input_hash
        ):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "The comparison inputs changed after this attempt was reserved. Compare again.",
            )

    def _prepare_comparison(
        self,
        workspace_id: str,
        actor: Actor,
    ) -> PreparedPreflightComparison:
        """Authorize and materialize complete immutable local comparison input."""

        self.authorization.require(
            actor,
            Capability.PREFLIGHT_RUN,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if getattr(workspace_state, "source_mode", SourceMode.FILE) is SourceMode.ODOO:
            if self.odoo_provenance is None:
                raise ReadinessError(
                    "Protected Odoo comparison support is unavailable. "
                    "Odoo was not contacted."
                )
            frozen = self._load_frozen_input(workspace_id)
            selection = self.sources.get_mapping_source_selection(workspace_id)
            if selection is None:
                raise ReadinessError(
                    "Refresh the captured Odoo records before comparing. "
                    "Odoo was not contacted."
                )
            return prepare_odoo_comparison(
                workspace_state=workspace_state,
                frozen=frozen,
                selection=selection,
                source_snapshots=self.sources.get_current_source_snapshots(
                    workspace_id
                ),
                artifacts=self.artifacts,
                provenance=self.odoo_provenance,
                actor=actor,
                run_id=str(uuid4()),
            )
        frozen = self._load_frozen_input(workspace_id)
        current_reference_policy_hash = None
        if frozen.captured_schema is not None:
            version_decision = assess_odoo_operation(
                frozen.captured_schema.odoo_version,
                OdooOperation.COMPARE,
            )
            odoo_major_version = version_decision.version.major
            if version_decision.allowed and odoo_major_version is not None:
                current_reference_policy_hash = reference_policy_hash(
                    odoo_major_version
                )
        requirements = plan_preflight_requirements(
            frozen.plan,
            frozen.prepared.records,
            reference_policy_hash=(
                current_reference_policy_hash or REFERENCE_POLICY_HASH
            ),
        )
        if any(not request.domain for request in requirements.record_requests):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "An Odoo record read could not be narrowed safely. "
                "Odoo was not contacted.",
            )
        return _PreparedFileComparison(
            workspace_id=workspace_id,
            workspace_state=workspace_state,
            frozen=frozen,
            requirements=requirements,
        )

    def _publish_comparison(
        self,
        prepared: PreparedPreflightComparison,
        metadata: MetadataSnapshot,
        records: RecordSnapshot,
        *,
        actor: Actor,
        progress: PreflightProgress | None,
        operation_id: str,
        expected_workspace_revision: int | None = None,
    ) -> ReadinessReport:
        """Classify and publish a completed remote read in one local phase."""

        if isinstance(prepared, OdooComparisonPreparation):
            return self._publish_odoo_source_comparison(
                prepared,
                metadata,
                records,
                actor=actor,
                progress=progress,
                operation_id=operation_id,
                expected_workspace_revision=expected_workspace_revision,
            )
        workspace_state = prepared.workspace_state
        workspace_id = prepared.workspace_id
        frozen = prepared.frozen
        requirements = prepared.requirements
        metadata, records = bind_snapshot_hashes(metadata, records)
        _validate_snapshot_projection(
            requirements.metadata_requests,
            requirements.record_requests,
            metadata,
            records,
        )
        expected_target = target_identity_hash(
            connection_mode=(
                workspace_state.odoo_connection_mode.value
                if workspace_state.odoo_connection_mode is not None
                else ""
            ),
            base_url=workspace_state.odoo_base_url,
            database=workspace_state.odoo_database,
        )
        if metadata.fingerprint.target_hash != expected_target:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
                "Readiness data came from a different Odoo target",
            )
        _report_progress(progress, "COMPARING")
        result = self.engine.run(
            frozen.plan,
            frozen.prepared,
            metadata,
            records,
            captured_schema=getattr(frozen, "captured_schema", None),
        )
        if not result.metadata_snapshot_hash or not result.record_snapshot_hash:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                "Odoo snapshot evidence is incomplete",
            )
        _report_progress(progress, "BUILDING")
        run_id = str(uuid4())
        execution_snapshot = build_execution_snapshot(
            preflight_run_id=run_id,
            frozen=frozen,
            result=result,
        )
        precision_issues = exact_numeric_precision_issues(
            run_id,
            execution_snapshot.rows,
            execution_snapshot.datasets,
        )
        with timed_preflight_publication("execution_snapshot_serialization"):
            execution_snapshot_content = (
                execution_snapshot.to_json().encode("utf-8") + b"\n"
            )
        frozen_input_hash = frozen.content_hash
        requirement_plan_hash = requirements.semantic_hash
        manifest = result.to_portable_dict()
        manifest["preflight_evidence"] = {
            "frozen_input_hash": frozen_input_hash,
            "normalization_run_id": frozen.normalization.run_id,
            "normalization_content_hash": frozen.normalization.content_hash,
            "normalization_lifecycle_version": (frozen.normalization.lifecycle_version),
            "eligible_dataset_hash": frozen.normalization.eligible_dataset_hash,
            "compiled_migration_plan_hash": frozen.plan.semantic_hash,
            "requirement_plan_hash": requirement_plan_hash,
            "requirement_model_count": requirements.model_count,
            "requirement_chunk_count": requirements.chunk_count,
            "source_record_count": requirements.source_record_count,
            "execution_snapshot_hash": execution_snapshot.semantic_hash,
            "execution_snapshot_root_hash": execution_snapshot.root_hash,
        }
        with timed_preflight_publication("manifest_serialization"):
            manifest_content = canonical_json_bytes(manifest) + b"\n"
        del manifest
        report = _readiness_report(
            run_id,
            workspace_state,
            frozen.revision,
            result,
            frozen.dataset_labels,
            frozen.source_field_labels,
            actor,
            frozen.staging,
            frozen.quality,
            frozen.normalization,
            frozen_input_hash=frozen_input_hash,
            requirement_plan_hash=requirement_plan_hash,
            metadata_snapshot_hash=result.metadata_snapshot_hash,
            record_snapshot_hash=result.record_snapshot_hash,
        )
        report = replace(
            report,
            manifest_hash="sha256:" + sha256(manifest_content).hexdigest(),
            operation_id=operation_id,
        )
        execution_summary = build_execution_preview_summary(
            report,
            execution_snapshot,
            execution_shape_ready=(
                assess_odoo_operation(
                    execution_snapshot.target_odoo_version,
                    OdooOperation.WRITE,
                ).allowed
                and not execution_snapshot.relationship_plan.blockers
                and not precision_issues
            ),
        )
        execution_review = build_execution_review_projection(
            report,
            execution_snapshot,
            execution_shape_ready=execution_summary.execution_shape_ready,
        )
        business_observation = observe_business_writes(
            execution_snapshot,
            authored_operations=tuple(
                (dataset.name, name, rule.operation)
                for dataset in frozen.plan.datasets
                for name, rule in dataset.relations.items()
            ),
        )
        decision_count = len(report.rows)
        decision_rows = iter(report.rows)
        report = replace(report, rows=())
        del frozen, requirements, result
        _report_progress(progress, "PUBLISHING")
        try:
            with timed_preflight_publication("manifest_artifact_write"):
                self.artifacts.write_report(
                    workspace_id,
                    run_id,
                    MANIFEST_NAME,
                    manifest_content,
                )
            with timed_preflight_publication(
                "execution_snapshot_artifact_write"
            ):
                self.artifacts.write_report(
                    workspace_id,
                    run_id,
                    EXECUTION_SNAPSHOT_NAME,
                    execution_snapshot_content,
                )
            self.preflight.save_readiness_report(
                workspace_id,
                report,
                decision_rows=decision_rows,
                decision_count=decision_count,
                metadata_snapshot=metadata,
                record_snapshot=records,
                execution_summary=execution_summary,
                execution_review=execution_review,
                expected_workspace_revision=expected_workspace_revision,
                actor=actor,
                business_observation=business_observation,
            )
        except PreflightPublicationCommittedError:
            logging.getLogger(__name__).warning(
                "Comparison committed; registration-file refresh needs retry"
            )
        except Exception:
            for filename in (MANIFEST_NAME, EXECUTION_SNAPSHOT_NAME):
                try:
                    self.artifacts.delete_report(workspace_id, run_id, filename)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not remove incomplete preflight evidence"
                    )
            raise
        return report

    def current_odoo_comparison(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooComparisonArtifact | None:
        """Decrypt the current exact-ID comparison for an authorized backend."""

        report = self.current_report(workspace_id)
        if report is None or (
            getattr(self.workspaces.get(workspace_id), "source_mode", SourceMode.FILE)
            is not SourceMode.ODOO
        ):
            return None
        if self.odoo_provenance is None:
            raise ReadinessError("Protected Odoo comparison support is unavailable")
        try:
            with self.artifacts.materialize_report(
                workspace_id,
                report.run_id,
                MANIFEST_NAME,
            ) as path:
                manifest = json.loads(path.read_text("utf-8"))
            evidence = manifest["preflight_evidence"]
            with self.artifacts.materialize_report(
                workspace_id,
                report.run_id,
                ODOO_COMPARISON_ARTIFACT_NAME,
            ) as path:
                encrypted = path.read_bytes()
            plaintext = self.odoo_provenance.open_comparison(
                workspace_id,
                report.run_id,
                str(evidence["capture_manifest_hash"]),
                encrypted,
                expected_logical_hash=str(evidence["protected_logical_hash"]),
                expected_artifact_hash=str(evidence["protected_artifact_hash"]),
                actor=actor,
            )
            artifact = OdooComparisonArtifact.from_json(plaintext.decode("utf-8"))
        except (
            ArtifactStoreError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            WorkspaceError,
        ) as error:
            raise ReadinessError(
                "The protected Odoo comparison is missing or invalid. Compare again."
            ) from error
        if (
            artifact.workspace_id != workspace_id
            or artifact.run_id != report.run_id
            or artifact.frozen_input_hash != report.frozen_input_hash
            or artifact.content_hash != evidence.get("protected_comparison_hash")
        ):
            raise ReadinessError(
                "The protected Odoo comparison no longer matches this review."
            )
        return artifact

    def _publish_odoo_source_comparison(
        self,
        prepared: OdooComparisonPreparation,
        metadata: MetadataSnapshot,
        records: RecordSnapshot,
        *,
        actor: Actor,
        progress: PreflightProgress | None = None,
        operation_id: str = "",
        expected_workspace_revision: int | None = None,
    ) -> ReadinessReport:
        """Publish a read-only pinned comparison without portable Odoo IDs."""

        if self.odoo_provenance is None:
            raise ReadinessError(
                "Protected Odoo comparison support is unavailable. Odoo was not contacted."
            )
        _report_progress(progress, "COMPARING")
        publication = build_odoo_comparison_publication(
            preparation=prepared,
            provenance=self.odoo_provenance,
            metadata=metadata,
            records=records,
            actor=actor,
        )
        workspace_id = prepared.workspace_state.workspace_id
        run_id = prepared.run_id
        report = replace(publication.report, operation_id=operation_id)
        _report_progress(progress, "PUBLISHING")
        try:
            self.artifacts.write_report(
                workspace_id,
                run_id,
                MANIFEST_NAME,
                publication.portable_manifest,
            )
            self.artifacts.write_report(
                workspace_id,
                run_id,
                ODOO_COMPARISON_ARTIFACT_NAME,
                publication.protected.encrypted_bytes,
            )
            self.preflight.save_readiness_report(
                workspace_id,
                replace(report, rows=()),
                decision_rows=iter(publication.rows),
                decision_count=len(publication.rows),
                metadata_snapshot=publication.metadata_snapshot,
                record_snapshot=publication.redacted_record_snapshot,
                expected_workspace_revision=expected_workspace_revision,
                actor=actor,
            )
        except PreflightPublicationCommittedError:
            logging.getLogger(__name__).warning(
                "Comparison committed; registration-file refresh needs retry"
            )
        except Exception:
            for filename in (MANIFEST_NAME, ODOO_COMPARISON_ARTIFACT_NAME):
                try:
                    self.artifacts.delete_report(
                        workspace_id, run_id, filename
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Could not remove incomplete Odoo comparison evidence"
                    )
            raise
        return report

    def _load_frozen_input(self, workspace_id: str) -> FrozenPreflightInput:
        """Load version-checked durable evidence without source artifacts."""

        workspace_state = self.workspaces.get(workspace_id)
        revision = self.mappings.get_mapping_revision(workspace_id)
        if revision is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Submit the mapping before comparing with Odoo",
            )
        submission = self.mappings.get_mapping_submission(
            workspace_id, revision.version
        )
        if (
            submission is None
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Submit the current mapping before comparing with Odoo",
            )
        captured_schema = None
        if self.schemas is not None:
            captured_schema = self.schemas.get_odoo_schema_catalog(workspace_id)
            governance = self.schemas.get_schema_governance(workspace_id)
            expected_schema_hash = (
                governance.content_hash
                if governance is not None
                else (
                    captured_schema.content_hash
                    if captured_schema is not None
                    else None
                )
            )
            if (
                captured_schema is None
                or expected_schema_hash != revision.definition.schema_hash
                or (
                    governance is not None
                    and governance.catalog_hash != captured_schema.content_hash
                )
            ):
                raise OdooReadWorkflowError(
                    (
                        OdooReadFailureCode.SCHEMA_EVIDENCE_MISSING
                        if captured_schema is None
                        else OdooReadFailureCode.SCHEMA_EVIDENCE_STALE
                    ),
                    "The captured Odoo fields no longer match the submitted "
                    "mapping. Odoo was not contacted.",
                )
        selection = self.sources.get_mapping_source_selection(workspace_id)
        staging_summary = self.staging.get_current_staging_summary(workspace_id)
        quality_summary = self.quality.get_current_quality_summary(workspace_id)
        normalization = self.normalization.get_current_normalization_summary(
            workspace_id
        )
        if selection is None or staging_summary is None or quality_summary is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "Prepare the data before comparing it with Odoo. "
                "Odoo was not contacted.",
            )
        if normalization is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "Approve the prepared data before comparing it with Odoo. "
                "Odoo was not contacted.",
            )
        staging = self.staging.get_canonical_staging_run(
            workspace_id, staging_summary.run_id
        )
        quality = self.quality.get_quality_run(workspace_id, quality_summary.run_id)
        effective = None
        if quality_summary.effective_dataset_run_id is not None:
            if self.effective is None:
                raise OdooReadWorkflowError(
                    OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                    "The approved resolved rows could not be loaded. "
                    "Odoo was not contacted.",
                )
            effective = self.effective.get_current_effective_dataset(workspace_id)
            if (
                effective is None
                or effective.content_hash != quality_summary.effective_dataset_hash
            ):
                raise OdooReadWorkflowError(
                    OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                    "The approved resolved rows could not be verified. "
                    "Odoo was not contacted.",
                )
        dry_run = self.normalization.get_normalization_dry_run(
            workspace_id, normalization.run_id
        )
        if staging is None or quality is None or dry_run is None:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                "The approved prepared evidence is incomplete. Odoo was not contacted.",
            )
        try:
            plan = compile_browser_mapping(
                revision.definition,
                selection,
                derived_plan_hash=staging.derived_plan_hash,
                required_relationship_fields={
                    model.name: frozenset(
                        field.name for field in model.fields if field.required
                    )
                    for model in (
                        captured_schema.models if captured_schema is not None else ()
                    )
                },
            )
            dataset_labels, source_field_labels = browser_mapping_labels(
                revision.definition,
                selection,
            )
        except ReadinessError as error:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                str(error),
            ) from error
        try:
            return build_frozen_preflight_input(
                workspace_id=workspace_state.workspace_id,
                revision=revision,
                selection=selection,
                staging_summary=staging_summary,
                staging=staging,
                quality_summary=quality_summary,
                quality=quality,
                normalization=normalization,
                dry_run=dry_run,
                plan=plan,
                dataset_labels=dataset_labels,
                source_field_labels=source_field_labels,
                effective=effective,
                captured_schema=captured_schema,
            )
        except ReadinessError as error:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                str(error),
            ) from error


def _validate_snapshot_projection(
    metadata_requests: tuple[MetadataRequest, ...],
    record_requests: tuple[RecordRequest, ...],
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
) -> None:
    """Require exact planned models/fields before comparison.

    Extra fields are rejected as well as omissions, proving that the protected
    snapshot came from the bounded requirement plan rather than a broad target
    export.
    """

    expected_metadata = {item.model: item.fields for item in metadata_requests}
    if set(metadata.models) != set(expected_metadata):
        raise OdooReadWorkflowError(
            OdooReadFailureCode.RESPONSE_INCOMPLETE,
            "Odoo metadata snapshot is incomplete",
        )
    for model, fields in expected_metadata.items():
        actual_fields = set(metadata.models[model].fields)
        expected_fields = set(fields)
        if actual_fields != expected_fields:
            if actual_fields - expected_fields:
                raise OdooReadWorkflowError(
                    OdooReadFailureCode.RESPONSE_INCOMPLETE,
                    "Odoo metadata snapshot contains unplanned fields",
                )
            raise OdooReadWorkflowError(
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                "Odoo metadata snapshot is incomplete",
            )

    expected_records: dict[str, tuple[str, ...]] = {}
    for request in record_requests:
        previous = expected_records.setdefault(request.model, request.fields)
        if previous != request.fields:
            raise OdooReadWorkflowError(
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                "Odoo record plan has inconsistent field projections",
            )
    if set(records.records) != set(expected_records) or set(
        records.requested_fields
    ) != set(expected_records):
        raise OdooReadWorkflowError(
            OdooReadFailureCode.RESPONSE_INCOMPLETE,
            "Odoo record snapshot is incomplete",
        )
    for model, fields in expected_records.items():
        if tuple(records.requested_fields[model]) != tuple(fields):
            raise OdooReadWorkflowError(
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                "Odoo record snapshot omitted requested fields",
            )


def _snapshot_matches_report(
    snapshot: ExecutionSnapshot,
    report: ReadinessReport,
) -> bool:
    """Bind a stored execution payload to the exact current readiness report."""

    return (
        snapshot.workspace_id == report.workspace_id
        and snapshot.preflight_run_id == report.run_id
        and snapshot.mapping_id == report.mapping_id
        and snapshot.mapping_version == report.mapping_version
        and snapshot.mapping_content_hash == report.mapping_content_hash
        and snapshot.staging_run_id == report.staging_run_id
        and snapshot.staging_content_hash == report.staging_content_hash
        and snapshot.quality_run_id == report.quality_run_id
        and snapshot.quality_content_hash == report.quality_content_hash
        and snapshot.normalization_run_id == report.normalization_run_id
        and snapshot.normalization_content_hash == report.normalization_content_hash
        and snapshot.normalization_lifecycle_version
        == report.normalization_lifecycle_version
        and snapshot.eligible_dataset_hash == report.eligible_dataset_hash
        and snapshot.frozen_input_hash == report.frozen_input_hash
        and snapshot.preflight_result_hash == report.result_hash
        and snapshot.metadata_snapshot_hash == report.metadata_snapshot_hash
        and snapshot.record_snapshot_hash == report.record_snapshot_hash
        and snapshot.target_hash == report.target_hash
        and snapshot.target_database == report.target_database
        and snapshot.target_odoo_version == report.target_odoo_version
        and snapshot.target_snapshot_at == report.target_snapshot_at
        and dict(snapshot.target_module_versions) == dict(report.target_module_versions)
        and dict(snapshot.counts)
        == {
            "AMBIGUOUS": report.ambiguous_count,
            "BLOCKED": report.blocked_count,
            "CREATE": report.create_count,
            "UNCHANGED": report.unchanged_count,
            "UPDATE": report.update_count,
        }
    )
