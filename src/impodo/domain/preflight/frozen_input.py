"""Verify and adapt approved source-side evidence for Odoo preflight.

Migration stage: boundary from E–G evidence into H preflight. Layer: domain.

``build_frozen_preflight_input`` binds the current submitted mapping,
canonical staging, quality run, frozen normalization approval, and compiled
plan. It rejects stale or inconsistent evidence before target I/O and adapts
eligible canonical rows to the shared preflight ``PreparedBundle`` without
executing transformation rules again.

See ``docs/architecture/python-code-map.md``,
``docs/developer/contracts/preflight.md``, and
``tests/application/workspace/preparation/test_readiness.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping

from impodo.domain.cutover.governance import DryRun, DryRunStatus
from impodo.domain.shared.models import (
    Issue,
    PreparedRecord,
    Severity,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
)
from impodo.domain.preparation.normalization import (
    NormalizationRunSummary,
    canonical_eligible_dataset_hash,
)
from impodo.domain.preparation.quality import QualityRun, QualityRunSummary
from ..resolution import EffectiveDataset
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.preparation.staging import StagingRunSummary
from impodo.domain.preparation.staging_contracts import CanonicalRow, CanonicalStagingRun
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from ..compiler.contracts import CompiledMigrationPlan
from ..errors import ReadinessError
from ..mapping.artifacts import MappingRevision


FROZEN_PREFLIGHT_INPUT_VERSION = 5


@dataclass(frozen=True, slots=True)
class FrozenPreflightInput:
    """Hold exact approved rows plus every source-side evidence binding.

    The immutable envelope is storage-independent and safe to pass into
    request planning and comparison. ``prepared`` contains only quality-
    eligible rows; the other fields prove which mapping, staging, quality, and
    normalization evidence authorized them.
    """

    workspace_id: str
    revision: MappingRevision
    staging: StagingRunSummary
    quality: QualityRunSummary
    normalization: NormalizationRunSummary
    plan: CompiledMigrationPlan
    prepared: PreparedBundle
    dataset_labels: Mapping[str, str]
    source_field_labels: Mapping[tuple[str, str], str]
    eligible_row_ids: tuple[str, ...]
    captured_schema: OdooSchemaCatalog | None = None
    contract_version: int = FROZEN_PREFLIGHT_INPUT_VERSION

    @property
    def content_hash(self) -> str:
        """Bind the portable preflight input to all consequential evidence."""

        payload = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "mapping_id": self.revision.mapping_id,
            "mapping_version": self.revision.version,
            "mapping_content_hash": self.revision.definition.content_hash,
            "staging_run_id": self.staging.run_id,
            "staging_content_hash": self.staging.content_hash,
            "quality_run_id": self.quality.run_id,
            "quality_content_hash": self.quality.content_hash,
            "effective_dataset_run_id": self.quality.effective_dataset_run_id,
            "effective_dataset_hash": self.quality.effective_dataset_hash,
            "normalization_run_id": self.normalization.run_id,
            "normalization_content_hash": self.normalization.content_hash,
            "normalization_lifecycle_version": self.normalization.lifecycle_version,
            "eligible_dataset_hash": self.normalization.eligible_dataset_hash,
            "eligible_row_ids": list(self.eligible_row_ids),
            "source_hashes": dict(sorted(self.prepared.source_hashes.items())),
            "compiled_plan_hash": self.plan.semantic_hash,
            "captured_schema_hash": (
                self.captured_schema.content_hash
                if self.captured_schema is not None
                else None
            ),
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
    workspace_id: str,
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
    effective: EffectiveDataset | None = None,
    captured_schema: OdooSchemaCatalog | None = None,
) -> FrozenPreflightInput:
    """Validate current durable evidence and build the preflight envelope.

    Validation covers upstream content hashes, lifecycle/run identities,
    compiled semantics, eligible-row accounting, source lineage, and the
    frozen normalization dataset hash. Every failure occurs before an Odoo
    reader is available to this function.

    Raises:
        ReadinessError: If any evidence is stale, incomplete, unapproved, or
            inconsistent with another bound input.
    """

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
        or quality.effective_dataset_hash != quality_summary.effective_dataset_hash
        or normalization.effective_dataset_hash != quality.effective_dataset_hash
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

    eligible_ids = quality.eligible_row_ids
    eligible_hash = canonical_eligible_dataset_hash(
        staging,
        quality,
        staging_content_hash=staging_summary.content_hash,
        quality_content_hash=quality_summary.content_hash,
        effective=effective,
    )
    if (
        eligible_hash != normalization.eligible_dataset_hash
        or dry_run.canonical_dataset_hash != eligible_hash
        or len(quality.eligible_row_ids) != normalization.eligible_record_count
    ):
        raise ReadinessError(
            "The approved prepared rows could not be verified. Odoo was not contacted."
        )

    source_hashes = {
        dataset.name: _canonical_source_hash(dataset.source_evidence_hash)
        for dataset in selection.datasets
    }
    rows = tuple(
        item.canonical_row for item in effective.rows
    ) if effective is not None else staging.rows
    if effective is not None and (
        effective.workspace_id != workspace_id
        or effective.staging_content_hash != staging_summary.content_hash
        or effective.content_hash != quality.effective_dataset_hash
        or quality_summary.effective_dataset_run_id is None
    ):
        raise ReadinessError(
            "The approved resolved rows could not be verified. Odoo was not contacted."
        )
    for row in rows:
        expected = source_hashes.get(row.dataset)
        if expected is None or row.lineage.source_hash != expected:
            raise ReadinessError(
                "The approved prepared row lineage is incomplete. Odoo was not contacted."
            )

    prepared = canonical_rows_to_prepared_bundle(
        staging,
        quality,
        source_hashes=source_hashes,
        staging_content_hash=staging_summary.content_hash,
        eligible_row_ids=eligible_ids,
        effective=effective,
    )
    eligible_row_ids = tuple(
        row.row_id for row in rows if row.row_id in eligible_ids
    )
    result = FrozenPreflightInput(
        workspace_id=workspace_id,
        revision=revision,
        staging=staging_summary,
        quality=quality_summary,
        normalization=normalization,
        plan=plan,
        prepared=prepared,
        dataset_labels=dict(dataset_labels),
        source_field_labels=dict(source_field_labels),
        eligible_row_ids=eligible_row_ids,
        captured_schema=captured_schema,
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
    staging_content_hash: str | None = None,
    eligible_row_ids: frozenset[str] | None = None,
    effective: EffectiveDataset | None = None,
) -> PreparedBundle:
    """Adapt quality-eligible canonical rows without preparing values again.

    The adapter preserves typed proposed values, symbolic references, issues,
    and canonical row IDs as source trace IDs. It applies no parsing,
    normalization, lookup, fallback, or validation rule.
    """

    canonical_staging_hash = staging_content_hash or staging.content_hash
    if quality.staging_content_hash != canonical_staging_hash:
        raise ReadinessError("Prepared rows no longer match their data checks")
    rows = tuple(
        item.canonical_row for item in effective.rows
    ) if effective is not None else staging.rows
    if quality.effective_dataset_hash != (
        effective.content_hash if effective is not None else canonical_staging_hash
    ):
        raise ReadinessError("Prepared rows no longer match resolved data")
    by_id = {row.row_id: row for row in rows}
    if set(by_id) != {item.row_id for item in quality.row_results}:
        raise ReadinessError("Prepared row evidence is incomplete")
    eligible_ids = eligible_row_ids or quality.eligible_row_ids
    records = tuple(
        _prepared_record(row)
        for row in rows
        if row.row_id in eligible_ids
    )
    return PreparedBundle(
        records=records,
        issues=(),
        source_hashes=dict(sorted(source_hashes.items())),
    )


def _prepared_record(row: CanonicalRow) -> PreparedRecord:
    """Adapt one canonical row while preserving values, references, and trace ID."""

    return PreparedRecord(
        dataset=row.dataset,
        source_row=row.source_row,
        target_model=row.target_model,
        source_identity=row.source_identity,
        target_identity=row.target_identity,
        target_scope=row.target_scope,
        scalar_values=row.proposed_values,
        references=row.references,
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
    """Normalize a frozen source hash and reject malformed lineage evidence."""

    digest = value.removeprefix("sha256:").casefold()
    if len(digest) != 64:
        raise ReadinessError("Approved source evidence has an invalid hash")
    try:
        int(digest, 16)
    except ValueError as error:
        raise ReadinessError("Approved source evidence has an invalid hash") from error
    return f"sha256:{digest}"
