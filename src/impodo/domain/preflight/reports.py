"""Extracted reports domain behavior."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Mapping

from ...access import Actor
from ..mapping.artifacts import MappingRevision
from ...models import (
    Classification,
    Decision,
    PreflightResult,
)
from ...projects import MigrationProject
from ...quality import QualityRunSummary
from ...normalization import NormalizationRunSummary
from ...staging import StagingRunSummary
from ..contracts import READINESS_CONTRACT_VERSION
from ..staging.transformation_impact import _display_value


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
    source_trace_id: str = ""


@dataclass(frozen=True, slots=True)
class ReadinessDataset:
    dataset: str
    label: str
    target_model: str
    total: int
    ready: int
    needs_review: int
    blocked: int
    create_count: int = 0
    update_count: int = 0
    unchanged_count: int = 0
    ambiguous_count: int = 0


@dataclass(frozen=True, slots=True)
class ReadinessRowPage:
    items: tuple[ReadinessRow, ...]
    matching_count: int
    page: int
    page_count: int


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
    normalization_run_id: str
    normalization_content_hash: str
    normalization_lifecycle_version: int
    eligible_dataset_hash: str
    frozen_input_hash: str
    requirement_plan_hash: str
    metadata_snapshot_hash: str
    record_snapshot_hash: str
    result_hash: str
    manifest_hash: str
    target_hash: str
    target_database: str
    target_odoo_version: str
    target_snapshot_at: str
    target_module_versions: Mapping[str, str]
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
    def create_count(self) -> int:
        return sum(item.create_count for item in self.datasets)

    @property
    def update_count(self) -> int:
        return sum(item.update_count for item in self.datasets)

    @property
    def unchanged_count(self) -> int:
        return sum(item.unchanged_count for item in self.datasets)

    @property
    def ambiguous_count(self) -> int:
        return sum(item.ambiguous_count for item in self.datasets)

    @property
    def attention_count(self) -> int:
        return self.ambiguous_count + self.blocked_count

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
        contract_version = int(payload.get("contract_version", 0))
        if contract_version not in {3, 4, READINESS_CONTRACT_VERSION}:
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
            normalization_run_id=str(payload.get("normalization_run_id", "")),
            normalization_content_hash=str(
                payload.get("normalization_content_hash", "")
            ),
            normalization_lifecycle_version=int(
                payload.get("normalization_lifecycle_version", 0)
            ),
            eligible_dataset_hash=str(payload.get("eligible_dataset_hash", "")),
            frozen_input_hash=str(payload.get("frozen_input_hash", "")),
            requirement_plan_hash=str(payload.get("requirement_plan_hash", "")),
            metadata_snapshot_hash=str(payload.get("metadata_snapshot_hash", "")),
            record_snapshot_hash=str(payload.get("record_snapshot_hash", "")),
            result_hash=str(payload.get("result_hash", "")),
            manifest_hash=str(payload.get("manifest_hash", "")),
            target_hash=str(payload["target_hash"]),
            target_database=str(payload.get("target_database", "")),
            target_odoo_version=str(payload.get("target_odoo_version", "")),
            target_snapshot_at=str(payload.get("target_snapshot_at", "")),
            target_module_versions={
                str(key): str(item)
                for key, item in dict(
                    payload.get("target_module_versions", {})
                ).items()
            },
            checked_at=datetime.fromisoformat(str(payload["checked_at"])),
            checked_by=str(payload["checked_by"]),
            datasets=tuple(
                ReadinessDataset(**item) for item in payload.get("datasets", ())
            ),
            rows=tuple(ReadinessRow(**item) for item in payload.get("rows", ())),
            contract_version=contract_version,
        )


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
    normalization: NormalizationRunSummary,
    *,
    frozen_input_hash: str,
    requirement_plan_hash: str,
    metadata_snapshot_hash: str,
    record_snapshot_hash: str,
    manifest_hash: str = "",
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
                create_count=sum(
                    item.classification == Classification.CREATE.value
                    for item in dataset_rows
                ),
                update_count=sum(
                    item.classification == Classification.UPDATE.value
                    for item in dataset_rows
                ),
                unchanged_count=sum(
                    item.classification == Classification.UNCHANGED.value
                    for item in dataset_rows
                ),
                ambiguous_count=sum(
                    item.classification == Classification.AMBIGUOUS.value
                    for item in dataset_rows
                ),
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
        normalization_run_id=normalization.run_id,
        normalization_content_hash=normalization.content_hash,
        normalization_lifecycle_version=normalization.lifecycle_version,
        eligible_dataset_hash=normalization.eligible_dataset_hash,
        frozen_input_hash=frozen_input_hash,
        requirement_plan_hash=requirement_plan_hash,
        metadata_snapshot_hash=metadata_snapshot_hash,
        record_snapshot_hash=record_snapshot_hash,
        result_hash=result.semantic_hash,
        manifest_hash=manifest_hash,
        target_hash=result.fingerprint.target_hash,
        target_database=result.fingerprint.database,
        target_odoo_version=result.fingerprint.odoo_version,
        target_snapshot_at=result.fingerprint.snapshot_timestamp,
        target_module_versions=dict(sorted(result.fingerprint.module_versions.items())),
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
        source_trace_id=decision.source_trace_id,
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
