"""Stage-H orchestration over approved, frozen source-side evidence.

``PreflightService.compare`` verifies and adapts current Stages D–G evidence,
builds bounded read requirements, invokes a caller-supplied read-only target
reader, runs the shared comparison engine, and publishes a portable report plus
protected snapshots. It never reloads source files and exposes no Odoo write.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Iterable, Mapping
from uuid import uuid4

from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.application.shared.artifacts import (
    ArtifactStoreError,
    WorkspaceArtifactStore,
)
from impodo.domain.execution.planner import (
    PreflightRequirementPlan,
    plan_preflight_requirements,
)
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.staging import StagingRunSummary
from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.shared.models import canonical_json_bytes, target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode
from impodo.domain.workspace.reference_keys import (
    REFERENCE_POLICY_HASH,
    reference_policy_hash,
)

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
from ..domain.preflight.frozen_input import (
    FrozenPreflightInput,
    build_frozen_preflight_input,
)
from ..domain.preflight.missing_parents import (
    MissingParentGroup,
    missing_parent_groups,
)
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
from ..domain.preflight.reports import (
    ReadinessReport,
    ReadinessRowPage,
    ReviewWorkbookCellEffect,
    ReviewWorkbookEvidence,
    _readiness_report,
)
from .odoo_comparison_service import (
    ODOO_COMPARISON_ARTIFACT_NAME,
    build_odoo_comparison_publication,
)
from .odoo_provenance_service import OdooProvenanceService
from .odoo_read_failures import (
    OdooReadFailureCode,
    OdooReadWorkflowError,
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
from .workspace.execution.navigation import build_execution_preview_summary

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


@dataclass(frozen=True, slots=True)
class DeferredScopeReview:
    """Current isolatable issues and an optional locally calculated preview."""

    comparison_id: str
    comparison_hash: str
    issues: tuple[DeferredIssue, ...]
    candidates: tuple["DeferredIssueCandidate", ...]
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
        artifacts: WorkspaceArtifactStore,
        authorization: AuthorizationPolicy,
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

    def current_staging(self, workspace_id: str) -> StagingRunSummary | None:
        """Return the current staging summary used by package eligibility UI."""

        return self.staging.get_current_staging_summary(workspace_id)

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
        manifest_evidence = dict(manifest["preflight_evidence"])
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
                    frozen.quality.quarantined_count
                    + frozen.quality.excluded_count
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
            accepted_at=datetime.now(timezone.utc),
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
                decision_hash=decision.semantic_hash,
                actor=actor,
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
                    pass
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
            decision = DeferredScopeDecision.from_json(
                decision_path.read_text("utf-8")
            )
            preview = DeferredScopePreview.from_json(
                preview_path.read_text("utf-8")
            )
            reduced = ExecutionSnapshot.from_json(
                snapshot_path.read_text("utf-8")
            )
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
        page: int = 1,
        page_size: int = 100,
    ) -> ReadinessRowPage:
        """Load one filtered, bounded page from a published readiness run."""

        return self.preflight.get_readiness_rows(
            workspace_id,
            run_id,
            status=status,
            dataset=dataset,
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
    ) -> ReadinessReport:
        """Compare approved rows without invoking preparation or source loading.

        The fixed sequence is: authorize; verify frozen input; create narrowed
        requests; read and hash one target snapshot; verify its projection and
        target identity; run deterministic comparison; write the protected
        manifest; and atomically publish report rows plus snapshots. A failed
        database publication removes the otherwise orphaned manifest.
        """

        _report_progress(progress, "VERIFYING")
        self.authorization.require(
            actor,
            Capability.PREFLIGHT_RUN,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if getattr(workspace_state, "source_mode", SourceMode.FILE) is SourceMode.ODOO:
            return self._compare_odoo_source(
                workspace_state,
                reader=reader,
                actor=actor,
                progress=progress,
            )
        frozen = self._load_frozen_input(workspace_id)
        version_decision = assess_odoo_operation(
            frozen.captured_schema.odoo_version,
            OdooOperation.COMPARE,
        )
        current_reference_policy_hash = (
            reference_policy_hash(version_decision.version.major)
            if version_decision.allowed
            else None
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
        _report_progress(progress, "READING")
        metadata, records = reader(requirements)
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
        )
        execution_summary = build_execution_preview_summary(
            report,
            execution_snapshot,
            execution_shape_ready=(
                assess_odoo_operation(
                    execution_snapshot.target_odoo_version, OdooOperation.WRITE,
                ).allowed
                and not execution_snapshot.relationship_plan.blockers
                and not precision_issues
            ),
        )
        decision_count = len(report.rows)
        decision_rows = iter(report.rows)
        report = replace(report, rows=())
        del frozen, requirements, result
        _report_progress(progress, "PUBLISHING")
        try:
            self.artifacts.write_report(
                workspace_id,
                run_id,
                MANIFEST_NAME,
                manifest_content,
            )
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
                actor=actor,
            )
        except Exception:
            for filename in (MANIFEST_NAME, EXECUTION_SNAPSHOT_NAME):
                try:
                    self.artifacts.delete_report(workspace_id, run_id, filename)
                except Exception:
                    pass
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

    def _compare_odoo_source(
        self,
        workspace_state,
        *,
        reader: ReadinessReader,
        actor: Actor,
        progress: PreflightProgress | None = None,
    ) -> ReadinessReport:
        """Publish a read-only pinned comparison without portable Odoo IDs."""

        if self.odoo_provenance is None:
            raise ReadinessError(
                "Protected Odoo comparison support is unavailable. Odoo was not contacted."
            )
        _report_progress(progress, "VERIFYING")
        frozen = self._load_frozen_input(workspace_state.workspace_id)
        selection = self.sources.get_mapping_source_selection(
            workspace_state.workspace_id
        )
        if selection is None:
            raise ReadinessError(
                "Refresh the captured Odoo records before comparing. Odoo was not contacted."
            )
        run_id = str(uuid4())
        _report_progress(progress, "READING")
        publication = build_odoo_comparison_publication(
            workspace_state=workspace_state,
            frozen=frozen,
            selection=selection,
            source_snapshots=self.sources.get_current_source_snapshots(
                workspace_state.workspace_id
            ),
            artifacts=self.artifacts,
            provenance=self.odoo_provenance,
            reader=reader,
            actor=actor,
            run_id=run_id,
        )
        _report_progress(progress, "PUBLISHING")
        try:
            self.artifacts.write_report(
                workspace_state.workspace_id,
                run_id,
                MANIFEST_NAME,
                publication.portable_manifest,
            )
            self.artifacts.write_report(
                workspace_state.workspace_id,
                run_id,
                ODOO_COMPARISON_ARTIFACT_NAME,
                publication.protected.encrypted_bytes,
            )
            self.preflight.save_readiness_report(
                workspace_state.workspace_id,
                replace(publication.report, rows=()),
                decision_rows=iter(publication.rows),
                decision_count=len(publication.rows),
                metadata_snapshot=publication.metadata_snapshot,
                record_snapshot=publication.redacted_record_snapshot,
                actor=actor,
            )
        except Exception:
            for filename in (MANIFEST_NAME, ODOO_COMPARISON_ARTIFACT_NAME):
                try:
                    self.artifacts.delete_report(
                        workspace_state.workspace_id, run_id, filename
                    )
                except Exception:
                    pass
            raise
        return publication.report

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
                        captured_schema.models
                        if captured_schema is not None
                        else ()
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
