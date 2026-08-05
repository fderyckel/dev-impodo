"""Durable source-side input for read-only Odoo preflight."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping

from ...governance import DryRun, DryRunStatus
from ...models import (
    Issue,
    PreparedRecord,
    Severity,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
)
from ...normalization import (
    NormalizationRunSummary,
    canonical_eligible_dataset_hash,
)
from ...quality import QualityRun, QualityRunSummary
from ...source import PreparedBundle
from ...staging import StagingRunSummary
from ...staging_contracts import CanonicalRow, CanonicalStagingRun
from ...workspace_contracts import SourceSelection
from ..compiler.contracts import CompiledMigrationPlan
from ..errors import ReadinessError
from ..mapping.artifacts import MappingRevision


FROZEN_PREFLIGHT_INPUT_VERSION = 2


@dataclass(frozen=True, slots=True)
class FrozenPreflightInput:
    """Exact approved rows plus every source-side evidence binding."""

    project_id: str
    revision: MappingRevision
    staging: StagingRunSummary
    quality: QualityRunSummary
    normalization: NormalizationRunSummary
    plan: CompiledMigrationPlan
    prepared: PreparedBundle
    dataset_labels: Mapping[str, str]
    source_field_labels: Mapping[tuple[str, str], str]
    eligible_row_ids: tuple[str, ...]
    contract_version: int = FROZEN_PREFLIGHT_INPUT_VERSION

    @property
    def content_hash(self) -> str:
        payload = {
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "mapping_id": self.revision.mapping_id,
            "mapping_version": self.revision.version,
            "mapping_content_hash": self.revision.definition.content_hash,
            "staging_run_id": self.staging.run_id,
            "staging_content_hash": self.staging.content_hash,
            "quality_run_id": self.quality.run_id,
            "quality_content_hash": self.quality.content_hash,
            "normalization_run_id": self.normalization.run_id,
            "normalization_content_hash": self.normalization.content_hash,
            "normalization_lifecycle_version": self.normalization.lifecycle_version,
            "eligible_dataset_hash": self.normalization.eligible_dataset_hash,
            "eligible_row_ids": list(self.eligible_row_ids),
            "source_hashes": dict(sorted(self.prepared.source_hashes.items())),
            "compiled_plan_hash": self.plan.semantic_hash,
            "dataset_labels": dict(sorted(self.dataset_labels.items())),
            "source_field_labels": [
                [dataset, field, label]
                for (dataset, field), label in sorted(
                    self.source_field_labels.items()
                )
            ],
        }
        return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()


def build_frozen_preflight_input(
    *,
    project_id: str,
    revision: MappingRevision,
    selection: SourceSelection,
    staging_summary: StagingRunSummary,
    staging: CanonicalStagingRun,
    quality_summary: QualityRunSummary,
    quality: QualityRun,
    normalization: NormalizationRunSummary,
    dry_run: DryRun,
    plan: CompiledMigrationPlan,
    dataset_labels: Mapping[str, str],
    source_field_labels: Mapping[tuple[str, str], str],
) -> FrozenPreflightInput:
    """Validate and adapt the exact frozen repository evidence."""

    if (
        revision.definition.content_hash != staging.mapping_hash
        or revision.mapping_id != staging.mapping_id
        or revision.version != staging_summary.mapping_version
        or staging_summary.run_id == ""
        or staging_summary.content_hash != staging.content_hash
        or staging_summary.run_id != quality_summary.staging_run_id
        or quality_summary.run_id != normalization.quality_run_id
        or quality_summary.content_hash != quality.content_hash
        or quality_summary.content_hash != normalization.quality_content_hash
        or staging_summary.run_id != normalization.staging_run_id
        or staging_summary.content_hash != normalization.staging_content_hash
        or staging.compiled_plan_hash != plan.semantic_hash
    ):
        raise ReadinessError(
            "The approved prepared data is no longer current. Odoo was not contacted."
        )
    if not normalization.frozen or dry_run.status is not DryRunStatus.FROZEN:
        raise ReadinessError(
            "Approve the prepared data before comparing it with Odoo. "
            "Odoo was not contacted."
        )
    if dry_run.run_id != normalization.run_id:
        raise ReadinessError("The prepared-data approval evidence is incomplete")

    eligible_hash = canonical_eligible_dataset_hash(staging, quality)
    if (
        eligible_hash != normalization.eligible_dataset_hash
        or dry_run.canonical_dataset_hash != eligible_hash
        or len(quality.eligible_row_ids) != normalization.eligible_record_count
    ):
        raise ReadinessError(
            "The approved prepared rows could not be verified. Odoo was not contacted."
        )

    source_hashes = {
        dataset.name: _canonical_source_hash(dataset.source_sha256)
        for dataset in selection.datasets
    }
    for row in staging.rows:
        expected = source_hashes.get(row.dataset)
        if expected is None or row.lineage.source_hash != expected:
            raise ReadinessError(
                "The approved prepared row lineage is incomplete. Odoo was not contacted."
            )

    prepared = canonical_rows_to_prepared_bundle(
        staging,
        quality,
        source_hashes=source_hashes,
    )
    eligible_row_ids = tuple(
        row.row_id for row in staging.rows if row.row_id in quality.eligible_row_ids
    )
    result = FrozenPreflightInput(
        project_id=project_id,
        revision=revision,
        staging=staging_summary,
        quality=quality_summary,
        normalization=normalization,
        plan=plan,
        prepared=prepared,
        dataset_labels=dict(dataset_labels),
        source_field_labels=dict(source_field_labels),
        eligible_row_ids=eligible_row_ids,
    )
    assert_no_numeric_odoo_ids(
        {
            "input_content_hash": result.content_hash,
            "eligible_row_ids": eligible_row_ids,
            "source_hashes": source_hashes,
        }
    )
    return result


def canonical_rows_to_prepared_bundle(
    staging: CanonicalStagingRun,
    quality: QualityRun,
    *,
    source_hashes: Mapping[str, str],
) -> PreparedBundle:
    """Adapt stored canonical rows without applying any preparation rule."""

    if quality.staging_content_hash != staging.content_hash:
        raise ReadinessError("Prepared rows no longer match their data checks")
    by_id = {row.row_id: row for row in staging.rows}
    if set(by_id) != {item.row_id for item in quality.row_results}:
        raise ReadinessError("Prepared row evidence is incomplete")
    records = tuple(
        _prepared_record(row)
        for row in staging.rows
        if row.row_id in quality.eligible_row_ids
    )
    return PreparedBundle(
        records=records,
        issues=(),
        source_hashes=dict(sorted(source_hashes.items())),
    )


def _prepared_record(row: CanonicalRow) -> PreparedRecord:
    return PreparedRecord(
        dataset=row.dataset,
        source_row=row.source_row,
        target_model=row.target_model,
        source_identity=row.source_identity,
        target_identity=row.target_identity,
        target_scope=row.target_scope,
        scalar_values=dict(row.proposed_values),
        references=dict(row.references),
        source_trace_id=row.row_id,
        issues=tuple(
            Issue(
                code=item.code,
                message=item.message,
                severity=Severity(item.severity),
                dataset=item.dataset,
                row=item.source_row,
                field=item.field,
                affected_count=item.affected_count,
            )
            for item in row.issues
        ),
    )


def _canonical_source_hash(value: str) -> str:
    digest = value.removeprefix("sha256:").casefold()
    if len(digest) != 64:
        raise ReadinessError("Approved source evidence has an invalid hash")
    try:
        int(digest, 16)
    except ValueError as error:
        raise ReadinessError("Approved source evidence has an invalid hash") from error
    return f"sha256:{digest}"
