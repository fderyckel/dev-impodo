"""Capture current correction evidence without requiring successful validation.

This use case reads each complete immutable evidence owner once. It preserves
the ordinary comparison and load gates and never supplies an Odoo reader.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from typing import Any, Protocol

from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.workspace.preparation.readiness_ports import (
    PreflightEffectiveRepository,
    PreflightMappingRepository,
    PreflightQualityRepository,
    PreflightSchemaRepository,
    PreflightSourceRepository,
    PreflightStagingRepository,
    PreflightWorkspaceRepository,
    PreparationDerivedRepository,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.preflight.attention import AttentionExport, AttentionFinding
from impodo.domain.preflight.attention_lineage import attention_column_origins
from impodo.domain.preflight.attention_projection import project_attention_export
from impodo.domain.preflight.reports import ReadinessReport
from impodo.domain.preparation.quality import retention_context_hash
from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.business_intent_assessment import (
    BusinessIntentAssessmentSummary,
)
from impodo.domain.workspace.workbench import SourceMode

_STALE = "The saved rows or checks changed. Prepare the current data and reopen Final review before exporting."


class AttentionEvidenceReader(Protocol):
    """Read current evidence through the existing final-review service ports."""

    authorization: WorkspaceAuthorizationPolicy
    workspaces: PreflightWorkspaceRepository
    mappings: PreflightMappingRepository
    sources: PreflightSourceRepository
    staging: PreflightStagingRepository
    quality: PreflightQualityRepository
    schemas: PreflightSchemaRepository | None
    effective: PreflightEffectiveRepository | None
    artifacts: GovernedArtifactStores
    derived: PreparationDerivedRepository | None

    def current_report(self, workspace_id: str) -> ReadinessReport | None: ...

    def current_business_intent_assessment(
        self, workspace_id: str, snapshot_hash: str
    ) -> BusinessIntentAssessmentSummary | None: ...


def capture_attention_export(
    service: AttentionEvidenceReader,
    workspace_id: str,
    *,
    actor: Actor,
    expected_quality_run_id: str = "",
    expected_comparison_id: str = "",
) -> AttentionExport:
    """Verify source, mapping, schema, quality, and optional comparison bindings."""

    service.authorization.require(
        actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=workspace_id
    )
    workspace = service.workspaces.get(workspace_id)
    revision = service.mappings.get_mapping_revision(workspace_id)
    selection = service.sources.get_mapping_source_selection(workspace_id)
    staging_summary = service.staging.get_current_staging_summary(workspace_id)
    quality_summary = service.quality.get_current_quality_summary(workspace_id)
    if (
        revision is None
        or selection is None
        or staging_summary is None
        or quality_summary is None
    ):
        raise ReadinessError(
            "Prepare the data before downloading rows needing attention."
        )
    if expected_quality_run_id and quality_summary.run_id != expected_quality_run_id:
        raise ReadinessError(_STALE)
    staging = service.staging.get_canonical_staging_run(
        workspace_id,
        staging_summary.run_id,
        expected_content_hash=staging_summary.content_hash,
    )
    quality = service.quality.get_quality_run(workspace_id, quality_summary.run_id)
    if staging is None or quality is None:
        raise ReadinessError(_STALE)
    if (
        staging.mapping_hash != revision.definition.content_hash
        or staging.mapping_id != revision.mapping_id
        or staging_summary.mapping_version != revision.version
        or staging.source_selection_hash != selection.content_hash
        or quality_summary.staging_run_id != staging_summary.run_id
        or quality_summary.staging_content_hash != staging_summary.content_hash
        or quality.staging_content_hash != staging_summary.content_hash
        or quality.content_hash != quality_summary.content_hash
        or quality.mapping_hash != staging.mapping_hash
        or quality.schema_hash != staging.schema_hash
        or quality.retention_context_hash != retention_context_hash(workspace)
        or quality.effective_dataset_hash != quality_summary.effective_dataset_hash
    ):
        raise ReadinessError(_STALE)
    schema = (
        service.schemas.get_odoo_schema_catalog(workspace_id)
        if service.schemas
        else None
    )
    governance = (
        service.schemas.get_schema_governance(workspace_id) if service.schemas else None
    )
    if service.schemas is not None and schema is None:
        raise ReadinessError("Refresh Odoo data and prepare the data before exporting.")
    if schema is not None and (
        (governance.content_hash if governance else schema.content_hash)
        != staging.schema_hash
        or (governance is not None and governance.catalog_hash != schema.content_hash)
    ):
        raise ReadinessError(
            "The Odoo fields changed. Refresh Odoo data, prepare the data, and reopen Final review."
        )
    snapshots = service.sources.get_current_source_snapshots(workspace_id)
    if any(
        item.physical_selection_hash != staging.physical_selection_hash
        or item.data_version_id != selection.data_version_id
        for item in snapshots
    ):
        raise ReadinessError(_STALE)
    derived = (
        service.derived.get_derived_entity_plan(workspace_id)
        if staging.derived_plan_hash is not None and service.derived is not None
        else None
    )
    if staging.derived_plan_hash is not None and (
        derived is None or derived.content_hash != staging.derived_plan_hash
    ):
        raise ReadinessError(_STALE)
    report = service.current_report(workspace_id)
    if expected_comparison_id and (
        report is None or report.run_id != expected_comparison_id
    ):
        raise ReadinessError(
            "The saved comparison changed. Reopen Final review before exporting."
        )
    manifest: Mapping[str, Any] = {}
    if report is not None:
        with service.artifacts.materialize_report(
            workspace_id, report.run_id, "impodo_preflight_manifest.json"
        ) as path:
            content = path.read_bytes()
        if "sha256:" + sha256(content).hexdigest() != report.manifest_hash:
            raise ReadinessError(
                "The comparison report could not be verified. Compare with Odoo again."
            )
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ReadinessError(
                "The comparison report is incomplete. Compare with Odoo again."
            )
        manifest = payload
    files = {item.file_id: item.display_name for item in workspace.source_files}
    source_names = {
        snapshot.dataset_id: (
            f"{files.get(snapshot.source.file_id, snapshot.dataset_name)} · {snapshot.dataset_name}"
            if isinstance(snapshot.source, FileSourceBinding)
            else snapshot.dataset_name
        )
        for snapshot in snapshots
    }
    source_columns = {
        column.stable_key: column.source_name
        for snapshot in snapshots
        for column in snapshot.schema.columns
    }
    source_columns.update(
        {
            column.stable_key: column.source_name
            for dataset in selection.datasets
            for column in dataset.columns
        }
    )
    header = AttentionExport(
        workspace_id=workspace_id,
        project=workspace.name,
        data_version_id=selection.data_version_id,
        company=workspace.odoo_company.name
        if workspace.odoo_company
        else "Company not selected",
        prepared_at=quality_summary.published_at,
        comparison_at=report.checked_at if report else None,
        quality_run_id=quality_summary.run_id,
        comparison_id=report.run_id if report else "",
        records=(),
        findings=(),
        snapshots=snapshots,
        source_names=source_names,
        source_columns=source_columns,
        filtered_count=0,
        derived_plan_hash=derived.content_hash if derived else "",
        source_origins=attention_column_origins(selection, snapshots, derived),
        restricted=workspace.source_mode is SourceMode.ODOO,
        input_bindings=(
            staging_summary.content_hash,
            quality_summary.content_hash,
            revision.definition.content_hash,
            selection.content_hash,
            schema.content_hash if schema else "",
            governance.content_hash if governance else "",
            str(workspace.revision),
            retention_context_hash(workspace),
        ),
    )
    prepared_rows = None
    if quality_summary.effective_dataset_run_id is not None:
        effective = (
            service.effective.get_current_effective_dataset(workspace_id)
            if service.effective
            else None
        )
        if (
            effective is None
            or effective.content_hash != quality.effective_dataset_hash
        ):
            raise ReadinessError(_STALE)
        prepared_rows = tuple(item.canonical_row for item in effective.rows)
    result = project_attention_export(
        header,
        staging,
        quality,
        revision.definition,
        selection,
        schema,
        manifest,
        prepared_rows=prepared_rows,
    )
    snapshot_hash = str(
        manifest.get("preflight_evidence", {}).get("execution_snapshot_hash") or ""
    )
    if snapshot_hash and not header.restricted:
        assessment = service.current_business_intent_assessment(
            workspace_id, snapshot_hash
        )
        result = replace(
            result,
            business_snapshot_hash=snapshot_hash,
            business_assessment_hash=assessment.content_hash if assessment else "",
        )
        if assessment is not None and not assessment.ready:
            finding = AttentionFinding(
                "",
                "",
                (),
                "Must fix",
                "Load expectations need review",
                " ".join(assessment.deviations)
                or "The proposed load has no current approved business expectations.",
                "Review Expected versus found. Correct the source or field matches if the proposed changes are wrong; update the expectations only when they reflect the intended load.",
                "Final review → Edit business expectations",
                "Data manager",
                "Applies to the whole load",
                "BUSINESS_EXPECTATIONS_NEED_REVIEW",
            )
            result = replace(result, findings=(finding, *result.findings))
    return result


def verify_attention_export_current(
    service: AttentionEvidenceReader, export: AttentionExport, *, actor: Actor
) -> None:
    """Recheck authorization and current pointers before returning completed bytes."""

    service.authorization.require(
        actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=export.workspace_id
    )
    quality = service.quality.get_current_quality_summary(export.workspace_id)
    staging = service.staging.get_current_staging_summary(export.workspace_id)
    revision = service.mappings.get_mapping_revision(export.workspace_id)
    selection = service.sources.get_mapping_source_selection(export.workspace_id)
    workspace = service.workspaces.get(export.workspace_id)
    schema = (
        service.schemas.get_odoo_schema_catalog(export.workspace_id)
        if service.schemas
        else None
    )
    governance = (
        service.schemas.get_schema_governance(export.workspace_id)
        if service.schemas
        else None
    )
    if (
        quality is None
        or staging is None
        or revision is None
        or selection is None
        or (
            quality.run_id != export.quality_run_id
            or quality.staging_run_id != staging.run_id
            or staging.mapping_version != revision.version
            or selection.data_version_id != export.data_version_id
            or (
                staging.content_hash,
                quality.content_hash,
                revision.definition.content_hash,
                selection.content_hash,
                schema.content_hash if schema else "",
                governance.content_hash if governance else "",
                str(workspace.revision),
                retention_context_hash(workspace),
            )
            != export.input_bindings
        )
    ):
        raise ReadinessError(_STALE)
    report = service.current_report(export.workspace_id)
    if export.derived_plan_hash:
        derived = (
            service.derived.get_derived_entity_plan(export.workspace_id)
            if service.derived is not None
            else None
        )
        if derived is None or derived.content_hash != export.derived_plan_hash:
            raise ReadinessError(_STALE)
    if (report.run_id if report else "") != export.comparison_id:
        raise ReadinessError(
            "The comparison changed during export. Reopen Final review and download again."
        )
    if export.business_snapshot_hash:
        assessment = service.current_business_intent_assessment(
            export.workspace_id, export.business_snapshot_hash
        )
        if (
            assessment.content_hash if assessment else ""
        ) != export.business_assessment_hash:
            raise ReadinessError(
                "The business expectations changed during export. Reopen Final review and download again."
            )
