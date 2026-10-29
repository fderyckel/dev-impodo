"""Portable final-review report contracts and browser projections.

``PreflightEngine`` produces technical :class:`Decision` objects. The private
``_readiness_report`` adapter turns them into dataset summaries and plain-
language row guidance, then ``PreflightRepository`` stores the report header
and rows separately for bounded review. These objects contain business
identities and source trace IDs, never environment-local numeric Odoo IDs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import (
    Classification,
    Decision,
    PreflightResult,
    PreparedRecord,
)
from impodo.domain.workspace.workbench import WorkspaceState

from ..contracts import READINESS_CONTRACT_VERSION
from ..staging.transformation_impact import _display_value
from .frozen_input import (
    FrozenMappingRevisionEvidence,
    FrozenNormalizationEvidence,
    FrozenQualityEvidence,
    FrozenStagingEvidence,
)


@dataclass(frozen=True, slots=True)
class ReadinessRow:
    """Plain-language projection of one engine decision for human review.

    ``status`` is the review state (ready/review/blocked), while
    ``classification`` preserves the engine outcome such as ``CREATE`` or
    ``UPDATE``. ``source_trace_id`` links back to the eligible canonical row.
    """

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
    technical_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        codes = tuple(dict.fromkeys(self.technical_codes))
        if not codes and self.technical_code:
            codes = (self.technical_code,)
        object.__setattr__(self, "technical_codes", codes)


@dataclass(frozen=True, slots=True)
class ReadinessDataset:
    """Reconciled final-review counts for one logical source dataset."""

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
class ReadinessIssueCount:
    """Complete-run count for one actionable final-review finding.

    The projection contains only portable labels and counts. Protected target
    values, source values, target identifiers, and per-record identities stay
    in their existing bounded evidence owners.
    """

    dataset: str
    dataset_label: str
    status: str
    technical_code: str
    reason: str
    field: str
    recommended_action: str
    affected_record_count: int


@dataclass(frozen=True, slots=True)
class ReadinessRowPage:
    """Bounded repository page of readiness rows for browser navigation."""

    items: tuple[ReadinessRow, ...]
    matching_count: int
    page: int
    page_count: int


class ReviewWorkbookCellStatus(StrEnum):
    """Business status for one final prepared workbook value."""

    AS_PROVIDED = "As provided"
    CHANGED = "Changed by Impodo"
    ADDED = "Added by Impodo"
    REVIEW_RECOMMENDED = "Review recommended"
    NEEDS_ATTENTION = "Needs attention"
    EMPTY_ALLOWED = "Empty but allowed"


class ReviewWorkbookActionPriority(StrEnum):
    """Business priority for one actionable workbook issue."""

    MUST_FIX = "Must fix"
    REVIEW = "Review"


@dataclass(frozen=True, slots=True)
class ReviewWorkbookCellEffect:
    """One frozen preparation effect that may explain a workbook cell."""

    source_trace_id: str
    dataset: str
    source_row: int
    target_field: str
    before: str
    after: str
    rule_name: str
    explanation: str

    def __post_init__(self) -> None:
        if (
            not self.source_trace_id.startswith("sha256:")
            or not self.dataset
            or self.source_row < 1
            or not self.target_field
            or not self.rule_name
        ):
            raise ValueError("Review workbook cell effect is incomplete")


@dataclass(frozen=True, slots=True)
class ReviewWorkbookCellFeedback:
    """Status and hidden supporting note for one final prepared value."""

    status: ReviewWorkbookCellStatus
    note: str
    original_value: str = ""


@dataclass(frozen=True, slots=True)
class ReviewWorkbookEvidence:
    """Exact prepared values and labels allowed in one review workbook.

    The manifest remains the source for classifications and issues. This
    evidence adds the file-source values that the data manager will review and
    binds them to the same frozen input used for the Odoo comparison. Odoo-
    source comparisons never create this portable value projection.
    """

    frozen_input_hash: str
    records: tuple[PreparedRecord, ...]
    dataset_labels: Mapping[str, str]
    target_model_labels: Mapping[str, str]
    target_field_labels: Mapping[tuple[str, str], str]
    normalization_content_hash: str
    cell_effects: tuple[ReviewWorkbookCellEffect, ...]
    target_field_required: Mapping[tuple[str, str], bool]


def review_workbook_action_priority(
    issue_severity: str,
) -> ReviewWorkbookActionPriority:
    """Convert one manifest issue severity into its operator priority.

    Errors must be resolved before loading. Warnings require a deliberate
    review but do not become blockers merely because they appear in the
    workbook. The manifest supplies the severity; the workbook cannot infer a
    priority from cell content or presentation.
    """

    severity = issue_severity.casefold()
    if severity == "error":
        return ReviewWorkbookActionPriority.MUST_FIX
    if severity == "warning":
        return ReviewWorkbookActionPriority.REVIEW
    raise ValueError("Review workbook action severity must be error or warning")


def review_workbook_cell_feedback(
    value: object,
    effects: tuple[ReviewWorkbookCellEffect, ...] = (),
    *,
    issue_severity: str = "",
    issue_message: str = "",
    required: bool = False,
) -> ReviewWorkbookCellFeedback:
    """Classify one final value from governed comparison and preparation facts.

    A manifest issue takes precedence over preparation history. Frozen
    normalization effects then distinguish values added or changed by Impodo.
    A blank without an issue is informational; the function never infers a
    blocker from appearance alone.
    """

    severity = issue_severity.casefold()
    if severity == "error":
        return ReviewWorkbookCellFeedback(
            status=ReviewWorkbookCellStatus.NEEDS_ATTENTION,
            note=issue_message or "Impodo cannot safely prepare this value.",
        )
    if severity == "warning":
        return ReviewWorkbookCellFeedback(
            status=ReviewWorkbookCellStatus.REVIEW_RECOMMENDED,
            note=issue_message or "Review this prepared value in Impodo.",
        )

    if effects:
        ordered = tuple(
            sorted(
                effects,
                key=lambda item: (
                    item.rule_name.casefold(),
                    item.explanation.casefold(),
                    item.before,
                    item.after,
                ),
            )
        )
        added = all(_normalization_display_is_empty(item.before) for item in ordered)
        status = (
            ReviewWorkbookCellStatus.ADDED
            if added
            else ReviewWorkbookCellStatus.CHANGED
        )
        rules = " · ".join(dict.fromkeys(item.rule_name for item in ordered))
        explanations = " ".join(
            dict.fromkeys(
                item.explanation.strip() for item in ordered if item.explanation.strip()
            )
        )
        note = f"{status.value}. Applied preparation: {rules}."
        if explanations:
            note = f"{note} {explanations}"
        originals = " · ".join(
            dict.fromkeys(item.before for item in ordered if item.before)
        )
        return ReviewWorkbookCellFeedback(
            status=status,
            note=note,
            original_value=originals,
        )

    if _prepared_value_is_empty(value):
        note = (
            "No prepared value will be sent for this required Odoo field, and "
            "the current comparison found no missing-value blocker."
            if required
            else "This optional value is blank and needs no action."
        )
        return ReviewWorkbookCellFeedback(
            status=ReviewWorkbookCellStatus.EMPTY_ALLOWED,
            note=note,
        )

    return ReviewWorkbookCellFeedback(
        status=ReviewWorkbookCellStatus.AS_PROVIDED,
        note="Impodo will use this prepared value without a recorded change.",
    )


def _normalization_display_is_empty(value: str) -> bool:
    return not value.strip() or value.strip() == "—"


def _prepared_value_is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping | tuple | list | set | frozenset):
        return not value
    return False


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Portable durable summary of one read-only Odoo comparison.

    The report binds the submitted mapping, staging, quality, frozen
    normalization input, bounded requirement plan, protected target snapshots,
    engine result, manifest, and target identity. ``rows`` is present while
    building the report and removed from the stored header after rows are
    streamed separately into the repository.
    """

    run_id: str
    workspace_id: str
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
    issue_counts: tuple[ReadinessIssueCount, ...] = ()
    contract_version: int = READINESS_CONTRACT_VERSION
    operation_id: str = ""

    def __post_init__(self) -> None:
        if self.contract_version != READINESS_CONTRACT_VERSION:
            raise ValueError("Readiness report contract version is unsupported")
        if self.operation_id:
            try:
                canonical_operation_id = str(UUID(self.operation_id))
            except (ValueError, AttributeError) as error:
                raise ValueError("Readiness operation identifier is invalid") from error
            if canonical_operation_id != self.operation_id:
                raise ValueError("Readiness operation identifier is invalid")

    @property
    def ready_count(self) -> int:
        """Count rows ready for a future import-plan decision."""

        return sum(item.ready for item in self.datasets)

    @property
    def needs_review_count(self) -> int:
        """Count rows whose comparison outcome needs human review."""

        return sum(item.needs_review for item in self.datasets)

    @property
    def blocked_count(self) -> int:
        """Count rows that cannot safely progress."""

        return sum(item.blocked for item in self.datasets)

    @property
    def create_count(self) -> int:
        """Count eligible rows with no matching target business identity."""

        return sum(item.create_count for item in self.datasets)

    @property
    def update_count(self) -> int:
        """Count uniquely matched rows with material field differences."""

        return sum(item.update_count for item in self.datasets)

    @property
    def unchanged_count(self) -> int:
        """Count uniquely matched rows with no material differences."""

        return sum(item.unchanged_count for item in self.datasets)

    @property
    def ambiguous_count(self) -> int:
        """Count rows whose identity matched more than one target record."""

        return sum(item.ambiguous_count for item in self.datasets)

    @property
    def attention_count(self) -> int:
        """Count ambiguous plus blocked outcomes requiring intervention."""

        return self.ambiguous_count + self.blocked_count

    @property
    def total_count(self) -> int:
        """Count every compared eligible canonical row."""

        return sum(item.total for item in self.datasets)

    @property
    def status(self) -> str:
        """Derive the run status using blocked-before-review precedence."""

        if self.blocked_count:
            return "BLOCKED"
        if self.needs_review_count:
            return "NEEDS_REVIEW"
        return "READY"

    def to_json(self) -> str:
        """Serialize the portable report header/projection deterministically."""

        payload = asdict(self)
        payload["checked_at"] = self.checked_at.isoformat()
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> ReadinessReport:
        """Load the exact current report contract."""

        payload = json.loads(value)
        payload.setdefault("operation_id", "")
        payload.setdefault("issue_counts", [])
        _require_dataclass_fields(payload, ReadinessReport, "readiness report")
        for item in payload["datasets"]:
            _require_dataclass_fields(item, ReadinessDataset, "readiness dataset")
        for item in payload["rows"]:
            _require_dataclass_fields(item, ReadinessRow, "readiness row")
        for item in payload["issue_counts"]:
            _require_dataclass_fields(
                item,
                ReadinessIssueCount,
                "readiness issue count",
            )
        return cls(
            run_id=str(payload["run_id"]),
            workspace_id=str(payload["workspace_id"]),
            mapping_id=str(payload["mapping_id"]),
            mapping_version=int(payload["mapping_version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            staging_run_id=str(payload["staging_run_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            quality_run_id=str(payload["quality_run_id"]),
            quality_content_hash=str(payload["quality_content_hash"]),
            normalization_run_id=str(payload["normalization_run_id"]),
            normalization_content_hash=str(payload["normalization_content_hash"]),
            normalization_lifecycle_version=int(
                payload["normalization_lifecycle_version"]
            ),
            eligible_dataset_hash=str(payload["eligible_dataset_hash"]),
            frozen_input_hash=str(payload["frozen_input_hash"]),
            requirement_plan_hash=str(payload["requirement_plan_hash"]),
            metadata_snapshot_hash=str(payload["metadata_snapshot_hash"]),
            record_snapshot_hash=str(payload["record_snapshot_hash"]),
            result_hash=str(payload["result_hash"]),
            manifest_hash=str(payload["manifest_hash"]),
            target_hash=str(payload["target_hash"]),
            target_database=str(payload["target_database"]),
            target_odoo_version=str(payload["target_odoo_version"]),
            target_snapshot_at=str(payload["target_snapshot_at"]),
            target_module_versions={
                str(key): str(item)
                for key, item in dict(payload["target_module_versions"]).items()
            },
            checked_at=datetime.fromisoformat(str(payload["checked_at"])),
            checked_by=str(payload["checked_by"]),
            datasets=tuple(ReadinessDataset(**item) for item in payload["datasets"]),
            rows=tuple(ReadinessRow(**item) for item in payload["rows"]),
            issue_counts=tuple(
                ReadinessIssueCount(**item) for item in payload["issue_counts"]
            ),
            contract_version=int(payload["contract_version"]),
            operation_id=str(payload["operation_id"]),
        )


def _require_dataclass_fields(
    payload: object,
    contract: type[object],
    label: str,
) -> None:
    if not is_dataclass(contract):
        raise TypeError("Readiness report contract must be a dataclass")
    expected = {item.name for item in dataclass_fields(contract)}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(f"Stored {label} does not match the current contract")


def _readiness_report(
    run_id: str,
    workspace_state: WorkspaceState,
    revision: FrozenMappingRevisionEvidence,
    result: PreflightResult,
    dataset_labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
    actor: Actor,
    staging: FrozenStagingEvidence,
    quality: FrozenQualityEvidence,
    normalization: FrozenNormalizationEvidence,
    *,
    frozen_input_hash: str,
    requirement_plan_hash: str,
    metadata_snapshot_hash: str,
    record_snapshot_hash: str,
    manifest_hash: str = "",
) -> ReadinessReport:
    """Bind engine decisions and every evidence hash into a portable report."""

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
    for dataset in dict.fromkeys([*dataset_labels, *(item.dataset for item in rows)]):
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
        workspace_id=workspace_state.workspace_id,
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
        checked_at=datetime.now(UTC),
        checked_by=actor.identity.display_name,
        datasets=tuple(datasets),
        rows=rows,
        issue_counts=readiness_issue_counts(
            result.decisions,
            dataset_labels,
            source_labels,
        ),
    )


def readiness_issue_counts(
    decisions: Iterable[Decision],
    dataset_labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
) -> tuple[ReadinessIssueCount, ...]:
    """Aggregate every blocking or ambiguous decision without a page scan."""

    grouped: dict[tuple[str, ...], set[tuple[str, int, str]]] = {}
    for decision in decisions:
        record_key = (
            decision.dataset,
            decision.source_row,
            decision.source_trace_id,
        )
        dataset_label = dataset_labels.get(decision.dataset, decision.dataset)
        blocking_issues = tuple(item for item in decision.issues if item.blocking)
        for issue in blocking_issues:
            code = issue.code or "FINAL_REVIEW_BLOCKED"
            reason, action = plain_readiness_guidance(
                code,
                Classification.BLOCKED,
            )
            field = issue.field or ""
            _add_readiness_issue_count(
                grouped,
                dataset=decision.dataset,
                dataset_label=dataset_label,
                status="blocked",
                code=code,
                reason=reason,
                field=source_labels.get((decision.dataset, field), field),
                action=action,
                record_key=record_key,
            )
        if decision.classification is Classification.BLOCKED and not blocking_issues:
            reason, action = plain_readiness_guidance(
                "FINAL_REVIEW_BLOCKED",
                Classification.BLOCKED,
            )
            _add_readiness_issue_count(
                grouped,
                dataset=decision.dataset,
                dataset_label=dataset_label,
                status="blocked",
                code="FINAL_REVIEW_BLOCKED",
                reason=reason,
                field="",
                action=action,
                record_key=record_key,
            )
        if decision.classification is Classification.AMBIGUOUS:
            code = "TARGET_IDENTITY_AMBIGUOUS"
            reason, action = plain_readiness_guidance(
                code,
                Classification.AMBIGUOUS,
            )
            _add_readiness_issue_count(
                grouped,
                dataset=decision.dataset,
                dataset_label=dataset_label,
                status="needs_review",
                code=code,
                reason=reason,
                field="",
                action=action,
                record_key=record_key,
            )
    return _project_readiness_issue_counts(grouped)


def readiness_row_issue_counts(
    rows: Iterable[ReadinessRow],
) -> tuple[ReadinessIssueCount, ...]:
    """Aggregate already projected final-review rows by stable cause."""

    grouped: dict[tuple[str, ...], set[tuple[str, int, str]]] = {}
    for row in rows:
        if row.status not in {"blocked", "needs_review"}:
            continue
        code = row.technical_code or "FINAL_REVIEW_BLOCKED"
        _add_readiness_issue_count(
            grouped,
            dataset=row.dataset,
            dataset_label=row.dataset_label,
            status=row.status,
            code=code,
            reason=row.reason,
            field=row.field,
            action=row.recommended_action,
            record_key=(row.dataset, row.source_row, row.source_trace_id),
        )
    return _project_readiness_issue_counts(grouped)


def _add_readiness_issue_count(
    grouped: dict[tuple[str, ...], set[tuple[str, int, str]]],
    *,
    dataset: str,
    dataset_label: str,
    status: str,
    code: str,
    reason: str,
    field: str,
    action: str,
    record_key: tuple[str, int, str],
) -> None:
    key = (
        dataset,
        dataset_label,
        status,
        code,
        reason,
        field,
        action,
    )
    grouped.setdefault(key, set()).add(record_key)


def _project_readiness_issue_counts(
    grouped: Mapping[
        tuple[str, ...],
        set[tuple[str, int, str]],
    ],
) -> tuple[ReadinessIssueCount, ...]:
    """Project deterministic portable counts from publication-time groups."""

    return tuple(
        ReadinessIssueCount(
            dataset=key[0],
            dataset_label=key[1],
            status=key[2],
            technical_code=key[3],
            reason=key[4],
            field=key[5],
            recommended_action=key[6],
            affected_record_count=len(records),
        )
        for key, records in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][2] != "blocked",
                item[0][1].casefold(),
                item[0][4].casefold(),
                item[0][5].casefold(),
                item[0][3],
            ),
        )
    )


def _readiness_row(
    decision: Decision,
    labels: Mapping[str, str],
    source_labels: Mapping[tuple[str, str], str],
) -> ReadinessRow:
    """Translate one technical decision into status, reason, and next action."""

    status = (
        "needs_review"
        if decision.classification is Classification.AMBIGUOUS
        else (
            "blocked" if decision.classification is Classification.BLOCKED else "ready"
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
    technical_codes = tuple(
        dict.fromkeys(item.code for item in decision.issues if item.blocking)
    )
    if decision.classification is Classification.AMBIGUOUS:
        technical_codes = (
            *technical_codes,
            "TARGET_IDENTITY_AMBIGUOUS",
        )
    reason, action = plain_readiness_guidance(code, decision.classification)
    field = issue.field if issue is not None and issue.field else ""
    field = source_labels.get((decision.dataset, field), field)
    identity = (
        " · ".join(_display_value(item) for item in decision.business_identity) or "—"
    )
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
        technical_codes=technical_codes,
    )


def plain_readiness_guidance(
    code: str,
    classification: Classification,
) -> tuple[str, str]:
    """Translate one stable issue code into a reason and an owning action."""

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
        "REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED": (
            "An Odoo record differs only by letter case.",
            "Explicitly reuse the Odoo record or keep a separate incoming record.",
        ),
        "REFERENCE_TARGET_PRECEDENCE_CONFLICT": (
            "The incoming record has conflicting Odoo matches.",
            "Choose one explicit Odoo match before continuing.",
        ),
        "REFERENCE_TARGET_PRECEDENCE_REUSE": (
            "The existing Odoo record is reused.",
            "No Odoo values will be changed by this incoming row.",
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
        "TARGET_SELECTION_CHOICES_CHANGED": (
            "The available Odoo choices changed after mapping.",
            "Review this field match in Impodo and compare again if it changes.",
        ),
        "TARGET_SELECTION_FIELD_DRIFT": (
            "The Odoo field type changed after mapping.",
            "Refresh the Odoo fields and review this field match.",
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
